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
#   - Stagger scheduler (_schedule_stagger, _dispatch_stagger)
#   - Execution loops (loop_interactive, loop_stateless)
#
# RUNTIME CONTEXT HEADER
#   _build_prompt() injects a live one-liner at call time containing the
#   current weekday, date, time, and logged-in user. This keeps the stored
#   DEFAULT prompt body timeless while the model always sees fresh temporal
#   context — no static timestamp rot, no stale "today is Monday" lies.
#
# RUNTIME TUPLE SHAPE (8 elements):
#   (settings, values, prompts, functions, profiles, project_files, harnesses, system_prompt)
#
# STAGGER SCHEDULER
#   /stagger <minutes> <command> defers any command or question by N minutes.
#   Implemented via threading.Timer — ephemeral, lost on process exit.
#   The agent itself may emit /stagger directives; loop_interactive detects
#   and routes them identically to operator-issued stagger commands.
#   Active timers are tracked in _STAGGER_REGISTRY for !stagger inspection.
#
# Model: Gemma 3/4 instruction-tuned (google_gemma-3-4b-it-q4_k_s.gguf)
# Format: Gemma turn template — <start_of_turn> / <end_of_turn>
# Thinking: THINKING_MODE=1 prepends <|think|> to the system block.
#           Gemma reasons inside <|channel>thought...<channel|> blocks;
#           _agent_turn extracts, prints, and strips them each cycle.
# =============================================================================

import datetime
import os
import re
import sys
import json
import sqlite3
import threading
import urllib.request
import urllib.error

import db
import evolve
import tts

# Injected by index.py after import so agent functions can reach the DB.
DB_PATH   = None
BASE_DIR  = None   # Injected by index.py — project root for evolve key resolution

# -----------------------------------------------------------------------------
# STAGGER REGISTRY
# In-memory log of all scheduled timers. Thread-safe append only.
# Each entry: {"id": int, "delay": int, "command": str, "fire_at": datetime,
#              "fired": bool, "timer": threading.Timer}
# -----------------------------------------------------------------------------

_STAGGER_REGISTRY  = []
_STAGGER_ID_LOCK   = threading.Lock()
_STAGGER_NEXT_ID   = 0


def _next_stagger_id():
    global _STAGGER_NEXT_ID
    with _STAGGER_ID_LOCK:
        _STAGGER_NEXT_ID += 1
        return _STAGGER_NEXT_ID

# -----------------------------------------------------------------------------
# RUNTIME CONTEXT
# Called fresh on every Kobold round-trip so temporal data never goes stale.
# -----------------------------------------------------------------------------

def _runtime_context_header():
    """
    Build a one-line context block injected at the top of every prompt.
    Values are computed at call time — never cached, never stored in the DB.

    Returns a formatted string such as:
      [Context: Sunday 2026-04-19 | 14:32:07 | user: kim]

    Logged-in user is pulled from the USER or LOGNAME environment variable.
    Falls back to 'unknown' if neither is set (daemon/cron environments).
    """
    now      = datetime.datetime.now()
    weekday  = now.strftime("%A")
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M:%S")
    user     = os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown"
    return f"[Context: {weekday} {date_str} | {time_str} | user: {user}]"

# -----------------------------------------------------------------------------
# KOBOLD INTERFACE
# -----------------------------------------------------------------------------

def _build_prompt(system_prompt, conversation, thinking_mode):
    """
    Wrap system_prompt + conversation into Gemma's wire format for Kobold.

    system_prompt  — fully assembled system prompt string (stored, timeless)
    conversation   — accumulated user + RESULT context for this turn
    thinking_mode  — bool; True = prepend <|think|> to the system block

    The runtime context header is injected TWICE:
      1. At the top of the system block — establishes authoritative context.
      2. Immediately above the conversation — adjacent to the user's question
         so the model cannot confabulate a stale time/date when answering
         temporal queries directly from the header.

    Both injections are computed from the same _runtime_context_header() call
    so they are identical and consistent within a single round-trip.
    """
    context   = _runtime_context_header()
    sys_text  = f"{context}\n{system_prompt}"
    sys_block = f"<|think|>\n{sys_text}" if thinking_mode else sys_text
    return (
        f"<start_of_turn>user\n"
        f"{sys_block}\n\n"
        f"{context}\n"
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

    Anti-prompts are sourced from the active model_profiles row (resolved in
    index.py and stored in the profiles array). The legacy ANTI_PROMPTS_GEMMA
    key in settings_values is no longer consulted.
    """
    endpoint      = db.resolve_value(values, "ENDPOINT_KOBOLD",    "http://localhost:5001/api/v1/generate")
    max_tokens    = int(db.resolve_value(values,   "KOBOLD_MAX_TOKENS",  "512"))
    temperature   = float(db.resolve_value(values, "KOBOLD_TEMPERATURE", "0.1"))
    top_p         = float(db.resolve_value(values, "KOBOLD_TOP_P",       "0.9"))
    thinking_mode = db.resolve_setting(settings, "THINKING_MODE", fallback=1) == 1

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

    return text.strip()

# -----------------------------------------------------------------------------
# FUNCTION EXECUTOR
# -----------------------------------------------------------------------------

def _execute_function(functions, function_name, **kwargs):
    """
    Locate function_name in the loaded functions roster and exec() it.
    Keyword arguments are injected into the local scope before execution,
    allowing parameterised functions (calculate, set_boolean, set_value, etc.)
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
      "set_boolean setting_name=X setting_value=1"               -> {"setting_name": "X", "setting_value": 1}
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
# STAGGER SCHEDULER
# /stagger <minutes> <command> — defer any command or question by N minutes.
# Ephemeral: timers live in process memory only. Lost on exit — by design.
# Both operators and the agent itself may issue /stagger directives.
# -----------------------------------------------------------------------------

_STAGGER_MAX_MINUTES = 60  # mirrors SCHED_MAX_STAGGER_DELAY harness rule


def _schedule_stagger(delay_minutes, command, runtime, build_runtime_fn):
    """
    Register and arm a threading.Timer to fire `command` after delay_minutes.

    The timer callback re-reads the current runtime snapshot at fire time so
    it picks up any prompt reloads or setting mutations that occurred during
    the wait. The callback is daemonised — it dies cleanly with the process.

    Returns the registry entry dict (for display / inspection).
    """
    if delay_minutes < 1 or delay_minutes > _STAGGER_MAX_MINUTES:
        print(
            f"  [stagger] Delay must be 1–{_STAGGER_MAX_MINUTES} minutes. "
            f"Got: {delay_minutes}"
        )
        return None

    entry_id = _next_stagger_id()
    fire_at  = datetime.datetime.now() + datetime.timedelta(minutes=delay_minutes)

    def _fire():
        # Re-read runtime at fire time — settings may have changed during wait
        live_runtime = build_runtime_fn()
        settings, values, _, functions, _, _, _, system_prompt = live_runtime

        # Mark as fired in registry before execution
        for e in _STAGGER_REGISTRY:
            if e["id"] == entry_id:
                e["fired"] = True
                break

        print(f"\n[stagger #{entry_id}] Firing: {command}")
        print("Agent > ", end="", flush=True)

        # Route: if it looks like a /stagger itself, recurse; else agent turn
        stripped = command.strip()
        if stripped.lower().startswith("/stagger "):
            _dispatch_stagger(stripped, live_runtime, build_runtime_fn)
        else:
            response = _agent_turn(system_prompt, command, functions, values, settings)
            print(f"{response}\n")

    t = threading.Timer(delay_minutes * 60, _fire)
    t.daemon = True
    t.start()

    entry = {
        "id":      entry_id,
        "delay":   delay_minutes,
        "command": command,
        "fire_at": fire_at,
        "fired":   False,
        "timer":   t,
    }
    _STAGGER_REGISTRY.append(entry)
    return entry


def _dispatch_stagger(raw_input, runtime, build_runtime_fn):
    """
    Parse and dispatch a /stagger directive from either the operator or the agent.

    Format:  /stagger <minutes> <command or question>
    Example: /stagger 5 show me the bash log

    Prints a confirmation line and returns. Does not block.
    """
    # Strip the prefix and split into delay + payload
    payload = raw_input.strip()
    # Remove /stagger prefix (case-insensitive)
    payload = re.sub(r'^/stagger\s+', '', payload, flags=re.IGNORECASE)

    parts = payload.split(None, 1)
    if len(parts) < 2:
        print("  [stagger] Usage: /stagger <minutes> <command>\n")
        return

    try:
        delay_minutes = int(parts[0])
    except ValueError:
        print(f"  [stagger] Delay must be an integer number of minutes. Got: '{parts[0]}'\n")
        return

    command = parts[1].strip()
    if not command:
        print("  [stagger] No command provided after delay.\n")
        return

    entry = _schedule_stagger(delay_minutes, command, runtime, build_runtime_fn)
    if entry:
        fire_at_str = entry["fire_at"].strftime("%H:%M:%S")
        print(
            f"  [stagger #{entry['id']}] '{command}' queued — "
            f"fires in {delay_minutes} minute(s) at {fire_at_str}.\n"
        )

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

    DUPLICATE CALL GUARD
    If the model fires the exact same CALL with the exact same RESULT two
    turns in a row it has stalled — the scratchpad is spinning in place.
    Treat the last result as the final answer and break immediately.
    This prevents runaway loops when the model lacks a FINAL: discipline.
    """
    thinking_mode = db.resolve_setting(settings, "THINKING_MODE", fallback=1) == 1
    conversation  = user_input

    _last_call   = None   # (fn_name, fn_result) from the previous turn
    _stall_count = 0      # consecutive identical CALL/RESULT pairs seen

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

            # Duplicate CALL guard — if identical (call, result) fires again,
            # the model is stuck. Surface the result and break the chain.
            this_call = (call_target, fn_result)
            if this_call == _last_call:
                _stall_count += 1
                if _stall_count >= 2:
                    print(f"  [agent] Loop stall detected on '{fn_name}' — surfacing result.")
                    return fn_result
            else:
                _stall_count = 0
            _last_call = this_call

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
    "!harnesses": "Display all harness constraint rules loaded from the database.",
    "!stagger":   "List all scheduled stagger timers and their status.",
    "!reload":    "Force a prompt reload from the database immediately.",
    "!clear":     "Clear the terminal screen.",
    "/evolve":    "Introspect the agent and request self-improvement suggestions. Args: local | claude",
}


def _dispatch_command(raw_input, runtime, build_runtime_fn):
    """
    Handle any input that begins with '!'. Returns the (possibly mutated)
    runtime tuple — callers must unpack the return value to capture any
    state changes triggered by !reload.

    build_runtime_fn is passed in from index.py to avoid a circular import.
    Commands are local only — Kobold never sees them.
    """
    settings, values, prompts, functions, profiles, project_files, harnesses, system_prompt = runtime
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
        active  = db.resolve_value(values, "DEFAULT_PROMPT", fallback="DEFAULT")
        model   = db.resolve_value(values, "ACTIVE_MODEL",   fallback="GEMMA")
        context = _runtime_context_header()
        print(f"\n--- Active Prompt: {active}  |  Model: {model} ---")
        print(f"{context}")
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

    elif token == "!harnesses":
        if not harnesses:
            print("  [harnesses] No harness rules loaded.\n")
        else:
            print(f"\n  Harness Rules ({len(harnesses)} total):")
            for h in harnesses:
                status = "ON " if h["harness_enabled"] else "OFF"
                print(f"    [{status}] {h['harness_name']}: {h['harness_rule'][:80]}...")
            print()

    elif token == "!stagger":
        if not _STAGGER_REGISTRY:
            print("  [stagger] No stagger timers registered this session.\n")
        else:
            print(f"\n  Stagger Timers ({len(_STAGGER_REGISTRY)} registered):")
            for e in _STAGGER_REGISTRY:
                status   = "FIRED" if e["fired"] else "PENDING"
                fire_str = e["fire_at"].strftime("%H:%M:%S")
                print(
                    f"    #{e['id']:02d} [{status}] "
                    f"delay={e['delay']}m fire_at={fire_str} | {e['command'][:60]}"
                )
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

    Input routing priority (top to bottom):
      1. Empty input         — skip
      2. exit / quit         — terminate
      3. !command            — _dispatch_command (local, never reaches Kobold)
      4. /stagger <n> <cmd>  — _dispatch_stagger (arms timer, never reaches Kobold)
      5. Everything else     — _agent_turn (forwarded to Kobold)

    STAGGER AND THE AGENT
    The agent may emit /stagger directives in its FINAL: answer when it decides
    a task should be deferred. The loop does NOT scan agent responses for
    /stagger — that would create an invisible execution path. Instead, the
    agent is trained via the prompt to emit /stagger as its FINAL: text, and
    the operator can then decide to re-issue it. If fully autonomous stagger
    is desired, enable it by checking FINAL responses here.

    Checks PROMPT_RELOAD at the top of every turn for seamless hot-swaps.

    runtime          — 8-tuple from index._build_runtime_state()
    build_runtime_fn — callable from index.py to avoid circular import.
    """
    print("  Type '!help' for commands. Type 'exit' or 'quit' to terminate.\n")

    while True:
        runtime  = _check_prompt_reload(runtime, build_runtime_fn)
        settings, values, prompts, functions, profiles, project_files, harnesses, system_prompt = runtime

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
        if user_input.lower().startswith("/stagger "):
            _dispatch_stagger(user_input, runtime, build_runtime_fn)
            continue
        if user_input.lower().startswith("/evolve"):
            parts = user_input.strip().split(None, 1)
            mode  = parts[1].strip() if len(parts) > 1 else "local"
            evolve.dispatch_evolve(mode, DB_PATH, BASE_DIR or ".", values)
            continue

        print("Agent > ", end="", flush=True)
        response = _agent_turn(system_prompt, user_input, functions, values, settings)

        # Intercept agent-emitted /stagger directives — the agent reasons about
        # scheduling and emits the directive as its FINAL: answer. We catch it
        # here and arm the timer, then confirm to the operator.
        if response.strip().lower().startswith("/stagger "):
            _dispatch_stagger(response.strip(), runtime, build_runtime_fn)
        else:
            print(f"{response}\n")
            # TTS gate — speak the response if INTERACTIVE_MODE=1 and TTS=1.
            # The cooldown and all other guards live inside tts.speak() itself.
            tts.speak(response, settings, values)


def loop_stateless(runtime, build_runtime_fn):
    """
    Stateless/daemon loop. Reads from stdin pipe. No terminal output.
    Suitable for cron, daemon, or service deployment.
    Each stdin line is forwarded to the scratchpad agent; response to stdout.
    Checks PROMPT_RELOAD on every iteration so the daemon can hot-swap too.

    /stagger is silently ignored in stateless mode — timers require a
    persistent process and are not appropriate for cron/pipe deployments.
    """
    while True:
        runtime  = _check_prompt_reload(runtime, build_runtime_fn)
        settings, values, _, functions, profiles, project_files, harnesses, system_prompt = runtime

        line = sys.stdin.readline()
        if not line:
            break
        user_input = line.strip()
        if not user_input or user_input.startswith("!"):
            continue
        if user_input.lower().startswith("/stagger "):
            # Silently drop — stagger has no place in a stateless pipe
            sys.stdout.write("[stagger] Stagger is not supported in stateless mode.\n")
            sys.stdout.flush()
            continue
        response = _agent_turn(system_prompt, user_input, functions, values, settings)
        sys.stdout.write(response + "\n")
        sys.stdout.flush()
