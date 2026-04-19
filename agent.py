# =============================================================================
# agent.py
# All runtime agent machinery. Imported by index.py.
#
# Responsibilities:
#   - Kobold HTTP interface (_build_prompt, _call_kobold)
#   - Function executor (_execute_function)
#   - Scratchpad dispatch loop (_agent_turn and helpers)
#   - Terminal command dispatcher (_dispatch_command, COMMANDS)
#   - Prompt reload trip-wire (_check_prompt_reload)
#   - Execution loops (_loop_interactive, _loop_stateless)
#
# Model: Gemma 3/4 instruction-tuned (google_gemma-3-4b-it-q4_k_s.gguf)
# Format: Gemma turn template — <start_of_turn> / <end_of_turn>
# Thinking: THINKING_MODE=1 prepends <|think|> to the system block.
#           Gemma reasons inside <|channel>thought...<channel|> blocks;
#           _agent_turn extracts, prints, and strips them each cycle.
# Anti-prompts: ANTI_PROMPTS_GEMMA  (e.g. <end_of_turn>, <eos>)
# =============================================================================

import os
import re
import sys
import json
import sqlite3
import urllib.request
import urllib.error

import db

# Injected by index.py after import so agent functions can reach the DB.
DB_PATH = None

# -----------------------------------------------------------------------------
# KOBOLD INTERFACE
# -----------------------------------------------------------------------------

def _build_prompt(system_prompt, conversation, thinking_mode):
    """
    Wrap system_prompt + conversation into Gemma's wire format for Kobold.

    system_prompt  — fully assembled system prompt string
    conversation   — accumulated user + RESULT context for this turn
    thinking_mode  — bool; True = prepend <|think|> to the system block

    Returns the raw prompt string ready to POST to Kobold.
    """
    sys_block = f"<|think|>\n{system_prompt}" if thinking_mode else system_prompt
    return (
        f"<start_of_turn>user\n"
        f"{sys_block}\n\n"
        f"{conversation}<end_of_turn>\n"
        f"<start_of_turn>model\n"
    )


def _call_kobold(system_prompt, user_input, values, settings):
    """
    Fire a single generate request to Kobold. Returns the stripped response
    string, or an error string prefixed with [ERROR] on failure.

    All generation parameters are resolved live from the values/settings arrays
    so the agent can mutate them at runtime and have changes take effect on the
    very next call without a process restart.
    """
    endpoint      = db.resolve_value(values, "ENDPOINT_KOBOLD",    "http://localhost:5001/api/v1/generate")
    max_tokens    = int(db.resolve_value(values,   "KOBOLD_MAX_TOKENS",  "512"))
    temperature   = float(db.resolve_value(values, "KOBOLD_TEMPERATURE", "0.1"))
    top_p         = float(db.resolve_value(values, "KOBOLD_TOP_P",       "0.9"))
    thinking_mode = db.resolve_setting(settings, "THINKING_MODE", fallback=1) == 1

    anti_raw     = db.resolve_value(values, "ANTI_PROMPTS_GEMMA", "<end_of_turn>,<eos>,\n\n\n")
    anti_prompts = [t for t in anti_raw.split(",") if t]

    prompt  = _build_prompt(system_prompt, user_input, thinking_mode)
    payload = json.dumps({
        "prompt":      prompt,
        "max_length":  max_tokens,
        "temperature": temperature,
        "top_p":       top_p,
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            endpoint,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            text = body["results"][0]["text"]
    except urllib.error.URLError as e:
        return f"[ERROR] Kobold unreachable — is it running? ({e.reason})"
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        return f"[ERROR] Unexpected Kobold response format: {e}"
    except Exception as e:
        # Catches RemoteDisconnected, ConnectionResetError, and any other
        # mid-response drop from Kobold (OOM crash, VRAM exhaustion, etc.).
        return f"[ERROR] Kobold connection lost — {type(e).__name__}: {e}"

    for token in anti_prompts:
        text = text.split(token)[0]

    return text.strip()

# -----------------------------------------------------------------------------
# FUNCTION EXECUTOR
# -----------------------------------------------------------------------------

def _execute_function(functions, function_name, **kwargs):
    """
    Locate function_name in the loaded functions roster and exec() it.
    Keyword arguments are injected into the local scope before execution,
    allowing parameterised functions (calculate, set_setting, set_value, etc.)
    to receive their arguments cleanly.
    Returns the string value of `result` after execution, or an error string.
    """
    fn = next(
        (f for f in functions if f["function_name"] == function_name and f["function_enabled"]),
        None
    )
    if not fn:
        return f"[ERROR] Unknown or disabled function: '{function_name}'"

    local_scope = dict(kwargs)
    try:
        exec(fn["function_body"], {}, local_scope)
        return str(local_scope.get("result", "[ERROR] function body set no `result` variable"))
    except Exception as e:
        return f"[ERROR] Exception in '{function_name}': {e}"

# -----------------------------------------------------------------------------
# SCRATCHPAD AGENT HELPERS
# -----------------------------------------------------------------------------

def _extract_tool_call(raw, known_functions=None):
    """
    Fallback parser for when the model emits its native <tool_call> format
    instead of our CALL: directive.

    Priority order:
      1. Closed <tool_call>...</tool_call> — extract inner content.
      2. Open  <tool_call>...             — grab to end of line.
      3. No tag found                     — return None.

    Inner content resolution:
      A. Valid JSON with a "name" or "function" key.
      B. First word that exactly matches a known function name.
      C. Any word in the prose that matches a known function name.
      D. Nothing matched — return None.
    """
    import json as _json

    m = re.search(r'<tool_call>(.*?)</tool_call>', raw, re.DOTALL)
    if not m:
        m = re.search(r'<tool_call>(.*)', raw, re.DOTALL)
    if not m:
        return None

    inner = m.group(1).strip().split('\n')[0].strip()

    try:
        parsed    = _json.loads(inner)
        candidate = parsed.get("name") or parsed.get("function")
        if candidate and (known_functions is None or candidate in known_functions):
            return candidate
    except (_json.JSONDecodeError, AttributeError):
        pass

    if known_functions is None:
        first_word = inner.split()[0] if inner else None
        return first_word if (first_word and first_word.isidentifier()) else None

    words = inner.split()
    if words and words[0] in known_functions:
        return words[0]
    for word in words:
        clean = word.rstrip('.,;:()')
        if clean in known_functions:
            return clean

    return None


def _extract_gemma_thought(raw):
    """
    Extract Gemma's internal reasoning from its thought channel block.

    Gemma 3/4 wraps thinking output in:
      <|channel>thought
      [reasoning here]
      <channel|>

    Returns the stripped inner content, or None if no thought block is present.
    Handles both closed and unclosed blocks (Gemma occasionally emits the
    latter on error-recovery turns).
    """
    m = re.search(r'<\|channel>thought\s*(.*?)\s*<channel\|>', raw, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r'<\|channel>thought\s*(.*)', raw, re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else None


def _parse_call_params(call_line):
    """
    Parse optional key=value parameters from a CALL line.
    The first token is the function name; subsequent tokens are params.

    Two-pass strategy:
      expr=   — greedy, captures everything to end-of-line. Prevents nested
                = signs inside date/math expressions being split as new keys.
      others  — split only on known top-level keys to avoid mangling nested
                expressions such as timedelta(days=3).

    Known keys: setting_name, setting_value, days, prompt_name

    Examples:
      "get_current_datetime"                                      -> {}
      "calculate expr=6 * 7"                                     -> {"expr": "6 * 7"}
      "calculate expr=datetime.date(2026,4,6)+timedelta(days=3)" -> {"expr": "..."}
      "set_setting setting_name=X setting_value=1"               -> {"setting_name": "X", "setting_value": 1}
      "set_value setting_name=FOO setting_value=bar"             -> {"setting_name": "FOO", "setting_value": "bar"}
    """
    tokens = call_line.strip().split(None, 1)
    if len(tokens) < 2:
        return {}

    fn_name   = tokens[0]
    param_str = tokens[1].strip()
    kwargs    = {}

    if fn_name == "calculate" or param_str.startswith("expr="):
        m = re.match(r'expr=(.+)', param_str, re.DOTALL)
        if m:
            raw_val = m.group(1).strip()
            if len(raw_val) >= 2 and raw_val[0] in ('"', "'") and raw_val[-1] == raw_val[0]:
                raw_val = raw_val[1:-1]
            kwargs["expr"] = raw_val
        return kwargs

    KNOWN_KEYS = {"setting_name", "setting_value", "days", "prompt_name"}
    pattern    = re.compile(r'\b(\w+)=')
    matches    = [m for m in pattern.finditer(param_str) if m.group(1) in KNOWN_KEYS]

    for idx, m in enumerate(matches):
        key       = m.group(1)
        val_start = m.end()
        val_end   = matches[idx + 1].start() if idx + 1 < len(matches) else len(param_str)
        raw_val   = param_str[val_start:val_end].strip()
        if len(raw_val) >= 2 and raw_val[0] in ('"', "'") and raw_val[-1] == raw_val[0]:
            raw_val = raw_val[1:-1]
        try:
            kwargs[key] = int(raw_val)
        except ValueError:
            kwargs[key] = raw_val

    return kwargs


def _extract_tag(text, tag):
    """Pull the inner content of <TAG>...</TAG> from a string. Returns None if absent."""
    open_tag  = f"<{tag}>"
    close_tag = f"</{tag}>"
    start = text.find(open_tag)
    end   = text.find(close_tag)
    if start == -1 or end == -1 or end <= start:
        return None
    return text[start + len(open_tag):end].strip()

# -----------------------------------------------------------------------------
# SCRATCHPAD DISPATCH LOOP
# -----------------------------------------------------------------------------

MAX_SCRATCHPAD_TURNS = 12  # Hard cap — no infinite hallucination chains

def _agent_turn(system_prompt, user_input, functions, values, settings):
    """
    Full scratchpad dispatch cycle for one user message.

    Each iteration:
      1. Fire Kobold (params resolved live so runtime mutations take effect).
      2. Extract and print Gemma thought block; strip it from raw.
      3. Scan for CALL: — if found, execute, append RESULT, loop.
         Fallback: model emitted <tool_call> tag instead of CALL: directive.
      4. Scan for FINAL: — if found (and no CALL:), return first answer.
      5. No directive — return raw verbatim.

    CALL: always beats FINAL: when both appear in the same response.
    """
    thinking_mode = db.resolve_setting(settings, "THINKING_MODE", fallback=1) == 1
    conversation  = user_input

    for _turn in range(MAX_SCRATCHPAD_TURNS):
        raw = _call_kobold(system_prompt, conversation, values, settings)

        # ── 1. Extract, print, and excise Gemma thought block ────────────────
        if thinking_mode:
            thought = _extract_gemma_thought(raw)
            if thought:
                print(f"\n  [thinking] {thought}")
            # Two-pass strip: closed blocks first, then any unclosed tail.
            raw = re.sub(
                r'<\|channel>thought.*?<channel\|>', '', raw,
                flags=re.DOTALL | re.IGNORECASE
            ).strip()
            raw = re.sub(
                r'<\|channel>thought.*', '', raw,
                flags=re.DOTALL | re.IGNORECASE
            ).strip()

        # ── 2. Single-pass scan for CALL: and FINAL: directives ──────────────
        call_target = None
        final_lines = []
        for line in raw.splitlines():
            stripped = line.strip()
            if call_target is None and stripped.upper().startswith("CALL:"):
                call_target = stripped[5:].strip()
            if stripped.upper().startswith("FINAL:"):
                final_lines.append(stripped[6:].strip())

        # Fallback: model used native <tool_call> tag instead of CALL:
        if call_target is None:
            _roster   = {f["function_name"] for f in functions}
            native_fn = _extract_tool_call(raw, known_functions=_roster)
            if native_fn:
                call_target = native_fn

        # ── 3. CALL: wins — execute and loop ─────────────────────────────────
        if call_target is not None:
            call_kwargs = _parse_call_params(call_target)
            fn_name     = call_target.split()[0]
            print(f"  → {call_target}")
            fn_result = _execute_function(functions, fn_name, **call_kwargs)
            print(f"  ← {fn_result}")
            conversation = f"{conversation}\n{raw}\nRESULT: {fn_result}"
            continue

        # ── 4. FINAL: with no pending CALL — first answer only ───────────────
        if final_lines:
            return final_lines[0]

        # ── 5. No directive — return verbatim ────────────────────────────────
        return raw

    return "[AGENT] Turn cap reached without final answer."

# -----------------------------------------------------------------------------
# COMMAND DISPATCHER  (!commands — never forwarded to Kobold)
# -----------------------------------------------------------------------------

COMMANDS = {
    "!help":      "List all available terminal commands.",
    "!functions": "Display the loaded function roster from the database.",
    "!prompt":    "Print the assembled system prompt currently in use.",
    "!settings":  "Display all settings_boolean values loaded from the database.",
    "!values":    "Display all settings_values entries loaded from the database.",
    "!reload":    "Force a prompt reload from the database immediately.",
    "!clear":     "Clear the terminal screen.",
}


def _dispatch_command(raw_input, runtime, build_runtime_fn):
    """
    Handle any input that begins with '!'. Returns the (possibly mutated)
    runtime tuple — callers must unpack the return value to capture any
    state changes triggered by !reload.

    build_runtime_fn is passed in from index.py to avoid a circular import.
    Commands are local only — Kobold never sees them.
    """
    settings, values, prompts, functions, profiles, project_files, system_prompt = runtime
    token = raw_input.strip().lower()

    if token == "!help":
        print("\n  Available commands:")
        for cmd, desc in COMMANDS.items():
            print(f"    {cmd:<14} — {desc}")
        print()

    elif token == "!functions":
        enabled = [f for f in functions if f["function_enabled"]]
        if not enabled:
            print("  [functions] No enabled functions loaded.\n")
        else:
            print(f"\n  Loaded functions ({len(enabled)}):")
            for fn in enabled:
                print(f"    • {fn['function_name']}: {fn['function_description']}")
            print()

    elif token == "!prompt":
        active = db.resolve_value(values, "DEFAULT_PROMPT", fallback="DEFAULT")
        model  = db.resolve_value(values, "ACTIVE_MODEL",   fallback="GEMMA")
        print(f"\n--- Active Prompt: {active}  |  Model: {model} ---")
        print(f"{system_prompt}\n--- End ---\n")

    elif token == "!settings":
        print("\n  Boolean Settings (settings_boolean):")
        for s in settings:
            print(f"    {s['setting_name']:<24} = {s['setting_bool']}")
        print()

    elif token == "!values":
        print("\n  Value Settings (settings_values):")
        for v in values:
            print(f"    {v['setting_name']:<24} = {v['setting_value']}")
        print()

    elif token == "!reload":
        runtime = build_runtime_fn()
        active  = db.resolve_value(runtime[1], "DEFAULT_PROMPT", fallback="DEFAULT")
        model   = db.resolve_value(runtime[1], "ACTIVE_MODEL",   fallback="GEMMA")
        print(f"  [reload] Prompt hot-swapped to '{active}' (model: {model}).\n")

    elif token == "!clear":
        os.system("clear")

    else:
        print(f"  [cmd] Unknown command '{raw_input}'. Type !help for a list.\n")

    return runtime

# -----------------------------------------------------------------------------
# PROMPT RELOAD TRIP-WIRE
# The agent sets PROMPT_RELOAD=1 in the DB to request a hot-swap.
# index.py calls _check_prompt_reload at the top of every turn.
# -----------------------------------------------------------------------------

def _check_prompt_reload(runtime, build_runtime_fn):
    """
    Inspect the PROMPT_RELOAD trip wire in settings_boolean.
    If set, reload all tables, reassemble the system prompt, reset the flag,
    and return the fresh runtime tuple. Otherwise return runtime unchanged.

    build_runtime_fn is passed in from index.py to avoid a circular import.
    """
    settings = runtime[0]
    values   = runtime[1]

    if db.resolve_setting(settings, "PROMPT_RELOAD", fallback=0) != 1:
        return runtime

    runtime  = build_runtime_fn()
    settings, values = runtime[0], runtime[1]
    active = db.resolve_value(values, "DEFAULT_PROMPT", fallback="DEFAULT")
    model  = db.resolve_value(values, "ACTIVE_MODEL",   fallback="GEMMA")
    print(f"\n  [agent] Prompt reloaded — model: {model}, prompt: '{active}'.\n")

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "UPDATE settings_boolean SET setting_bool = 0 WHERE setting_name = 'PROMPT_RELOAD'"
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        print(f"  [agent] WARNING — could not reset PROMPT_RELOAD flag: {e}")

    return runtime

# -----------------------------------------------------------------------------
# EXECUTION LOOPS
# -----------------------------------------------------------------------------

def loop_interactive(runtime, build_runtime_fn):
    """
    Interactive readline loop. Kobold is called for every non-command input.
    Commands beginning with '!' are intercepted and handled locally.
    Checks PROMPT_RELOAD at the top of every turn for seamless hot-swaps.

    runtime          — 7-tuple: (settings, values, prompts, functions, profiles, project_files, system_prompt)
    build_runtime_fn — callable from index.py that reloads all tables and
                       reassembles the system prompt; passed to avoid circular import.
    """
    print("  Type '!help' for commands. Type 'exit' or 'quit' to terminate.\n")

    while True:
        runtime  = _check_prompt_reload(runtime, build_runtime_fn)
        settings, values, prompts, functions, profiles, project_files, system_prompt = runtime

        try:
            user_input = input("You > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[agent] Session terminated.")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            print("[agent] Goodbye.")
            break
        if user_input.startswith("!"):
            runtime = _dispatch_command(user_input, runtime, build_runtime_fn)
            continue

        print("Agent > ", end="", flush=True)
        response = _agent_turn(system_prompt, user_input, functions, values, settings)
        print(f"{response}\n")


def loop_stateless(runtime, build_runtime_fn):
    """
    Stateless/daemon loop. Reads from stdin pipe. No terminal output.
    Suitable for cron, daemon, or service deployment.
    Each stdin line is forwarded to the scratchpad agent; response to stdout.
    Checks PROMPT_RELOAD on every iteration so the daemon can hot-swap too.
    """
    while True:
        runtime  = _check_prompt_reload(runtime, build_runtime_fn)
        settings, values, _, functions, profiles, project_files, system_prompt = runtime

        line = sys.stdin.readline()
        if not line:
            break
        user_input = line.strip()
        if not user_input or user_input.startswith("!"):
            continue
        response = _agent_turn(system_prompt, user_input, functions, values, settings)
        sys.stdout.write(response + "\n")
        sys.stdout.flush()
