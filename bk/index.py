# =============================================================================
# index.py
# Root command file. Responsible for:
#   - Defining static deployment constants (DB_PATH, MCP_PORT only)
#   - Bootstrapping the database via db_seed.py if absent
#   - Delegating all table loading and resolution to db.py
#   - Assembling the final system prompt
#   - Entering interactive or stateless execution loop
#
# USAGE:
#   python index.py
# =============================================================================

import os
import sys
import json
import urllib.request
import urllib.error
import readline  # noqa: F401 — activates readline editing in interactive mode

import db
import db_seed

# -----------------------------------------------------------------------------
# CONSTANTS
# Static deployment anchors only. DB_PATH and MCP_PORT are the only values
# that cannot live in the database — they are needed before the DB is open.
# All other runtime-tunable values live in settings_values.
# -----------------------------------------------------------------------------

DB_PATH  = "database.db"
MCP_PORT = "8206"

# -----------------------------------------------------------------------------
# BOOT SEQUENCE
# -----------------------------------------------------------------------------

def _boot_database():
    """Ensure database.db exists and is seeded. Deploy the barracks first."""
    if not os.path.exists(DB_PATH):
        db_seed.run(DB_PATH)


def _build_runtime_state():
    """
    Load all tables and assemble the initial system prompt.
    Returns: (settings, values, prompts, functions, system_prompt)

    Called once at boot. Called again by _check_prompt_reload() whenever the
    agent hot-swaps the active prompt via the PROMPT_RELOAD trip wire.
    """
    settings, values, prompts, functions = db.load_all_tables(DB_PATH)
    prompt_name   = db.resolve_value(values, "DEFAULT_PROMPT", fallback="GEMMA_DEFAULT")
    base_prompt   = db.resolve_prompt(prompts, prompt_name)
    system_prompt = db.assemble_system_prompt(base_prompt, functions)
    return settings, values, prompts, functions, system_prompt

# -----------------------------------------------------------------------------
# KOBOLD INTERFACE
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# PROMPT FORMAT REFERENCE
# -----------------------------------------------------------------------------
# GEMMA  (PROMPT_FORMAT = "gemma")
#   Model: google_gemma-3-4b-it-q4_k_s.gguf (~4GB VRAM)
#   When THINKING_MODE=1, <|think|> is prepended to the system prompt.
#   Gemma reasons inside <|channel>thought...<channel|> blocks which
#   _agent_turn extracts and prints as scratchpad output.
#   Anti-prompts: ANTI_PROMPTS_GEMMA  (e.g. <end_of_turn>, <eos>)
#
# HERMES (PROMPT_FORMAT = "chatml")
#   Model: Hermes-3-Llama-3.1-8B.Q6_K.gguf (~6GB VRAM)
#   ChatML wrapping:
#     <|im_start|>system ... <|im_end|>
#     <|im_start|>user   ... <|im_end|>
#     <|im_start|>assistant
#   Reasoning uses <SCRATCHPAD> blocks parsed by _agent_turn.
#   Anti-prompts: ANTI_PROMPTS  (e.g. User:, <|im_end|>)
# -----------------------------------------------------------------------------

def _build_prompt(fmt, system_prompt, conversation, thinking_mode):
    """
    Wrap system_prompt + conversation into the correct wire format for Kobold.

    fmt            — "gemma" or "chatml" (from PROMPT_FORMAT in settings_values)
    system_prompt  — fully assembled system prompt string
    conversation   — accumulated user + RESULT context for this turn
    thinking_mode  — bool, True = prepend <|think|> (Gemma only)

    Returns the raw prompt string ready to POST to Kobold.
    """
    if fmt == "gemma":
        sys_block = f"<|think|>\n{system_prompt}" if thinking_mode else system_prompt
        return (
            f"<start_of_turn>user\n"
            f"{sys_block}\n\n"
            f"{conversation}<end_of_turn>\n"
            f"<start_of_turn>model\n"
        )
    else:
        # chatml — Hermes and compatible models
        return (
            f"<|im_start|>system\n{system_prompt}<|im_end|>\n"
            f"<|im_start|>user\n{conversation}<|im_end|>\n"
            f"<|im_start|>assistant\n"
        )


def _call_kobold(system_prompt, user_input, values, settings):
    """
    Fire a single generate request to Kobold. Returns the stripped response
    string, or an error string prefixed with [ERROR] on failure.

    Generation parameters and prompt format are resolved live from the values
    and settings arrays so the agent can mutate them at runtime and have the
    changes take effect on the very next call.

    PROMPT_FORMAT selects the wire format:
      "gemma"  — Gemma 3/4 instruction format; THINKING_MODE prepends <|think|>
      "chatml" — ChatML format used by Hermes and compatible models
    """
    endpoint      = db.resolve_value(values, "ENDPOINT_KOBOLD",    "http://localhost:5001/api/v1/generate")
    max_tokens    = int(db.resolve_value(values,   "KOBOLD_MAX_TOKENS",  "512"))
    temperature   = float(db.resolve_value(values, "KOBOLD_TEMPERATURE", "0.1"))
    top_p         = float(db.resolve_value(values, "KOBOLD_TOP_P",       "0.9"))
    fmt           = db.resolve_value(values, "PROMPT_FORMAT", "gemma")
    thinking_mode = db.resolve_setting(settings, "THINKING_MODE", fallback=1) == 1

    # Select the correct anti-prompt list for the active model format
    if fmt == "gemma":
        anti_raw = db.resolve_value(values, "ANTI_PROMPTS_GEMMA", "<end_of_turn>,<eos>,\n\n\n")
    else:
        anti_raw = db.resolve_value(values, "ANTI_PROMPTS", "User:,<|im_end|>,\n\n\n")
    anti_prompts = [t for t in anti_raw.split(",") if t]

    prompt = _build_prompt(fmt, system_prompt, user_input, thinking_mode)

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
        # Returns a recoverable error string instead of killing the process.
        return f"[ERROR] Kobold connection lost — {type(e).__name__}: {e}"

    # Strip anti-prompts from the tail of the response
    for token in anti_prompts:
        text = text.split(token)[0]

    return text.strip()

# -----------------------------------------------------------------------------
# FUNCTION EXECUTOR
# -----------------------------------------------------------------------------

def _execute_function(functions, function_name, **kwargs):
    """
    Locate function_name in the loaded functions roster and exec() it.
    Any keyword arguments are injected into the local scope before execution,
    allowing functions like calculate (expr=) and set_setting (setting_name=,
    setting_value=) to receive their parameters.
    Returns the string value of `result` after execution, or an error string.
    """
    fn = next(
        (f for f in functions if f["function_name"] == function_name and f["function_enabled"]),
        None
    )
    if not fn:
        return f"[ERROR] Unknown or disabled function: '{function_name}'"

    local_scope = dict(kwargs)   # pre-seed scope with any supplied parameters
    try:
        exec(fn["function_body"], {}, local_scope)
        return str(local_scope.get("result", "[ERROR] function body set no `result` variable"))
    except Exception as e:
        return f"[ERROR] Exception in '{function_name}': {e}"

# -----------------------------------------------------------------------------
# SCRATCHPAD AGENT LOOP
# -----------------------------------------------------------------------------

def _extract_tool_call(raw, known_functions=None):
    """
    Fallback parser for when the model emits its native <tool_call> format
    instead of our CALL: directive.

    Strategy (in priority order):
      1. Closed <tool_call>...</tool_call> tag — extract inner content.
      2. Open <tool_call>... tag with no closing — grab to end of line.
      3. No tag found — return None immediately.

    Inner content resolution (in priority order):
      A. Valid JSON with a "name" or "function" key — use that value.
      B. First word that exactly matches a known function name — use it.
      C. Scan ALL words in the prose for any known function name — use
         the first match.
      D. Nothing matched — return None.
    """
    import re, json as _json

    m = re.search(r'<tool_call>(.*?)</tool_call>', raw, re.DOTALL)
    if not m:
        m = re.search(r'<tool_call>(.*)', raw, re.DOTALL)
    if not m:
        return None

    inner = m.group(1).strip()
    inner = inner.split('\n')[0].strip()

    try:
        parsed = _json.loads(inner)
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
    Extract Gemma's internal reasoning from its native thought channel block.

    Gemma 3/4 wraps thinking output in:
      <|channel>thought
      [reasoning here]
      <channel|>

    Returns the stripped inner content, or None if no thought block is present.
    Called by _agent_turn when THINKING_MODE=1 and PROMPT_FORMAT="gemma".
    """
    import re
    m = re.search(r'<\|channel>thought\s*(.*?)\s*<channel\|>', raw, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # Gemma may also emit an unclosed block — grab to end of raw if so
    m = re.search(r'<\|channel>thought\s*(.*)', raw, re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else None


def _parse_call_params(call_line):
    """
    Parse optional key=value parameters from a CALL line.
    The first token is the function name; subsequent tokens are params.

    Two-pass strategy:
      Pass 1 — known single-value keys (setting_name, setting_value, days):
               split on the FIRST recognised top-level key= only, so that
               a second key= inside the value (e.g. timedelta(days=3)) is
               NOT mistaken for a new parameter.
      Pass 2 — expr= is always greedy: everything after expr= to end-of-line
               is the expression, no further splitting. This prevents
               nested = signs inside date/math expressions from being
               parsed as extra parameters.

    Examples:
      "get_current_datetime"                                        -> {}
      "calculate expr=6 * 7"                                       -> {"expr": "6 * 7"}
      "calculate expr=datetime.date(2026,4,6)+timedelta(days=3)"   -> {"expr": "datetime.date(2026,4,6)+timedelta(days=3)"}
      "set_setting setting_name=X setting_value=1"                 -> {"setting_name": "X", "setting_value": 1}
      "set_value setting_name=FOO setting_value=bar"               -> {"setting_name": "FOO", "setting_value": "bar"}
    """
    import re
    tokens = call_line.strip().split(None, 1)
    if len(tokens) < 2:
        return {}

    fn_name   = tokens[0]
    param_str = tokens[1].strip()
    kwargs    = {}

    # ── Special case: expr= captures everything to end-of-line, greedy ───────
    # Handles nested parens and = signs (e.g. timedelta(days=3)) without
    # mangling the expression.
    if fn_name == "calculate" or param_str.startswith("expr="):
        m = re.match(r'expr=(.+)', param_str, re.DOTALL)
        if m:
            raw_val = m.group(1).strip()
            # Strip surrounding quotes the model may have added
            if len(raw_val) >= 2 and raw_val[0] in ('"', "'") and raw_val[-1] == raw_val[0]:
                raw_val = raw_val[1:-1]
            kwargs["expr"] = raw_val
        return kwargs

    # ── General case: split on top-level key= boundaries only ────────────────
    # Only treat a word= as a new key if it appears at a word boundary and
    # the key is one of the known flat-value parameters. This avoids treating
    # days= or hours= inside a nested expression as a new key.
    KNOWN_KEYS = {"setting_name", "setting_value", "days", "prompt_name"}
    pattern = re.compile(r'\b(\w+)=')
    matches = [m for m in pattern.finditer(param_str) if m.group(1) in KNOWN_KEYS]

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


MAX_SCRATCHPAD_TURNS = 12  # Hard cap — no infinite hallucination chains

def _agent_turn(system_prompt, user_input, functions, values, settings):
    """
    Full scratchpad dispatch cycle for one user message.

    Each turn:
      1. Fire Kobold (format and params resolved live from values/settings arrays).
      2. Extract and print reasoning output:
           - THINKING_MODE=1 + PROMPT_FORMAT="gemma": parse <|channel>thought...<channel|>
           - Otherwise: parse <SCRATCHPAD>...</SCRATCHPAD>
      3. If CALL: found — execute the real function, print the result, loop.
      4. If FINAL: found (and no CALL:) — print and return.
      5. Otherwise — return raw output verbatim.

    CALL: always beats FINAL: in the same response.
    """
    fmt           = db.resolve_value(values, "PROMPT_FORMAT", "gemma")
    thinking_mode = db.resolve_setting(settings, "THINKING_MODE", fallback=1) == 1
    use_gemma_thinking = (fmt == "gemma" and thinking_mode)

    conversation = user_input

    for _turn in range(MAX_SCRATCHPAD_TURNS):
        raw = _call_kobold(system_prompt, conversation, values, settings)

        # ── 1. Extract, print, and excise reasoning block from raw ───────────
        if use_gemma_thinking:
            thought = _extract_gemma_thought(raw)
            if thought:
                print(f"\n  [thinking] {thought}")
            # Strip thought blocks from raw so the directive scanner sees clean
            # lines only. Two passes are required:
            #   Pass 1 — closed blocks: <|channel>thought ... <channel|>
            #   Pass 2 — unclosed blocks: <|channel>thought ... (no closing tag)
            # Gemma occasionally emits an unclosed block, especially on error
            # recovery turns. If left in raw it poisons the conversation context
            # fed back to Kobold on the next iteration.
            import re as _re
            raw = _re.sub(
                r'<\|channel>thought.*?<channel\|>',
                '',
                raw,
                flags=_re.DOTALL | _re.IGNORECASE
            ).strip()
            raw = _re.sub(
                r'<\|channel>thought.*',
                '',
                raw,
                flags=_re.DOTALL | _re.IGNORECASE
            ).strip()
        else:
            scratch = _extract_tag(raw, "SCRATCHPAD")
            if scratch:
                print(f"\n  [scratchpad] {scratch}")

        # ── 2. Single-pass scan for CALL: and FINAL: directives ───────────────
        call_target = None
        final_lines = []
        for line in raw.splitlines():
            stripped = line.strip()
            if call_target is None and stripped.upper().startswith("CALL:"):
                call_target = stripped[5:].strip()
            if stripped.upper().startswith("FINAL:"):
                final_lines.append(stripped[6:].strip())

        # Fallback: model used its native <tool_call> format instead of CALL:
        if call_target is None:
            _roster = {f["function_name"] for f in functions}
            native_fn = _extract_tool_call(raw, known_functions=_roster)
            if native_fn:
                call_target = native_fn

        # ── 3. CALL: wins — parse optional params, execute, and loop ──────────
        if call_target is not None:
            call_kwargs = _parse_call_params(call_target)
            fn_name     = call_target.split()[0]
            print(f"  → {call_target}")
            fn_result = _execute_function(functions, fn_name, **call_kwargs)
            print(f"  ← {fn_result}")
            conversation = (
                f"{conversation}\n"
                f"{raw}\n"
                f"RESULT: {fn_result}"
            )
            continue

        # ── 4. FINAL: with no outstanding CALL — return first answer only ───────
        # Model sometimes hallucinates the same FINAL: line a dozen times.
        # Taking only the first one is the correct answer; the rest are noise.
        if final_lines:
            return final_lines[0]

        # ── 5. No directive at all — return whatever the model said ───────────
        return raw

    return "[AGENT] Turn cap reached without final answer."


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
# COMMAND DISPATCHER  (!commands — never forwarded to the LLM)
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

def _dispatch_command(raw_input, runtime):
    """
    Handle any input that begins with '!'. Returns the (possibly mutated)
    runtime tuple — callers must unpack the return value to pick up any
    state changes triggered by !reload.

    Commands are local only — Kobold never sees them.
    """
    settings, values, prompts, functions, system_prompt = runtime
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
        active  = db.resolve_value(values, "DEFAULT_PROMPT", fallback="GEMMA_DEFAULT")
        fmt     = db.resolve_value(values, "PROMPT_FORMAT",  fallback="gemma")
        model   = db.resolve_value(values, "ACTIVE_MODEL",   fallback="GEMMA")
        print(f"\n--- Active Prompt: {active}  |  Model: {model}  |  Format: {fmt} ---")
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
        runtime = _build_runtime_state()
        active  = db.resolve_value(runtime[1], "DEFAULT_PROMPT", fallback="GEMMA_DEFAULT")
        model   = db.resolve_value(runtime[1], "ACTIVE_MODEL",   fallback="GEMMA")
        print(f"  [reload] Prompt hot-swapped to '{active}' (model: {model}).\n")

    elif token == "!clear":
        os.system("clear")

    else:
        print(f"  [cmd] Unknown command '{raw_input}'. Type !help for a list.\n")

    return runtime

# -----------------------------------------------------------------------------
# PROMPT RELOAD CHECK
# Called at the top of each interactive turn. If the agent has set the
# PROMPT_RELOAD trip wire (=1), reload all tables, reassemble the prompt,
# and reset the flag to 0 in the database.
# -----------------------------------------------------------------------------

def _check_prompt_reload(runtime):
    """
    Inspect the PROMPT_RELOAD trip wire in settings_boolean.
    If set, reload all tables from the DB, reassemble the system prompt,
    reset the flag, and return the fresh runtime tuple.
    Otherwise return the existing runtime unchanged.
    """
    settings, values, prompts, functions, system_prompt = runtime

    if db.resolve_setting(settings, "PROMPT_RELOAD", fallback=0) != 1:
        return runtime

    # Reload everything fresh from the DB
    runtime = _build_runtime_state()
    settings, values, prompts, functions, system_prompt = runtime
    active = db.resolve_value(values, "DEFAULT_PROMPT", fallback="GEMMA_DEFAULT")
    model  = db.resolve_value(values, "ACTIVE_MODEL",   fallback="GEMMA")
    fmt    = db.resolve_value(values, "PROMPT_FORMAT",  fallback="gemma")
    print(f"\n  [index] Prompt reloaded — model: {model}, format: {fmt}, prompt: '{active}'.\n")

    # Reset the trip wire so we don't reload every turn
    import sqlite3
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "UPDATE settings_boolean SET setting_bool = 0 WHERE setting_name = 'PROMPT_RELOAD'"
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        print(f"  [index] WARNING — could not reset PROMPT_RELOAD flag: {e}")

    return runtime

# -----------------------------------------------------------------------------
# EXECUTION LOOPS
# -----------------------------------------------------------------------------

def _loop_interactive(runtime):
    """
    Interactive readline loop. Kobold is called for every non-command input.
    Commands beginning with '!' are intercepted and handled locally.

    Checks the PROMPT_RELOAD trip wire at the top of every turn so the agent
    can hot-swap the active system prompt and model profile without restarting.

    `runtime` is a 5-tuple: (settings, values, prompts, functions, system_prompt)
    Reassigned in-place whenever a reload occurs.
    """
    print("  Type '!help' for commands. Type 'exit' or 'quit' to terminate.\n")

    while True:
        # ── Trip-wire check — reload prompt if agent flagged it ───────────────
        runtime = _check_prompt_reload(runtime)
        settings, values, prompts, functions, system_prompt = runtime

        try:
            user_input = input("You > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[index] Session terminated.")
            break

        if not user_input:
            continue

        if user_input.lower() in ("exit", "quit"):
            print("[index] Goodbye.")
            break

        if user_input.startswith("!"):
            runtime = _dispatch_command(user_input, runtime)
            continue

        print("Agent > ", end="", flush=True)
        response = _agent_turn(system_prompt, user_input, functions, values, settings)
        print(f"{response}\n")


def _loop_stateless(runtime):
    """
    Stateless/daemon loop. Reads from stdin pipe. No terminal output.
    Suitable for cron, daemon, or service deployment.
    Each line of stdin is forwarded to the scratchpad agent; response to stdout.

    Checks PROMPT_RELOAD on every iteration so even the daemon path can
    hot-swap its active model profile without a process restart.
    """
    while True:
        runtime = _check_prompt_reload(runtime)
        _, values, _, functions, system_prompt = runtime
        settings = runtime[0]

        line = sys.stdin.readline()
        if not line:
            break  # EOF — pipe closed or cron job complete
        user_input = line.strip()
        if not user_input or user_input.startswith("!"):
            continue
        response = _agent_turn(system_prompt, user_input, functions, values, settings)
        sys.stdout.write(response + "\n")
        sys.stdout.flush()

# -----------------------------------------------------------------------------
# MAIN
# -----------------------------------------------------------------------------

def main():
    # 1. Ensure database exists and is seeded
    _boot_database()

    # 2. Load all tables, resolve active prompt, assemble system prompt
    runtime = _build_runtime_state()
    settings = runtime[0]

    # 3. Evaluate deployment mode from settings
    interactive_mode = db.resolve_setting(settings, "INTERACTIVE_MODE", fallback=0)

    # 4. Enter appropriate loop
    if interactive_mode:
        _loop_interactive(runtime)
    else:
        _loop_stateless(runtime)


if __name__ == "__main__":
    main()
