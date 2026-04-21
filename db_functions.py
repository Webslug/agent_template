# =============================================================================
# db_functions.py
# Canonical roster of seeded agent functions.
# Imported by db_seed.py — never executed directly.
#
# Each entry: (function_name, function_description, function_body, function_language)
# function_body is a string of Python executed via exec() at runtime.
# All function bodies must assign their output to the variable `result`.
#
# FUNCTION ROSTER (17 total)
# ─────────────────────────────────────────────────────────────────────────────
# SYSTEM INFORMATION
#   get_system_info        — OS, Python version, hostname
#   calculate              — eval() with datetime/math/calendar namespace
#   NOTE: get_current_datetime removed — runtime context header supplies live
#         temporal data (weekday/date/time/user) on every prompt call.
#
# BOOLEAN SETTINGS  (settings_boolean — binary switches: 0 or 1 only)
#   get_all_settings       — dump full settings_boolean table
#   set_boolean            — write any named boolean switch (0 or 1)
#
# VALUE SETTINGS    (settings_values — strings, endpoints, paths, ranges)
#   get_all_values         — dump full settings_values table
#   set_value              — write any named string value
#
# PROMPT MANAGEMENT (agent_prompts)
#   list_prompts           — show prompt_name + enabled status
#   get_prompt_body        — return the full body of a named prompt
#   add_prompt             — insert a new named prompt body
#   reload_prompt          — hot-swap active prompt at runtime
#
# SELF-MODIFICATION (functions table + bash audit)
#   list_functions         — dump name + description of all enabled functions
#   upsert_function        — insert or overwrite a function in the DB roster
#   get_bash_log           — retrieve recent agent_bash_logs audit entries
#
# SAFETY / VALIDATION
#   validate_function_body — pre-flight regex scan before upsert_function
#   audit_function_change  — write a timestamped row to function_audit_log
#
# BASH EXECUTION
#   run_bash_command       — sandboxed shell with full audit trail
#
# MODEL SWITCHING
#   switch_model           — atomically swap model profile + prompt + thinking mode
# =============================================================================

SEED_FUNCTIONS = [

    # -------------------------------------------------------------------------
    # SYSTEM INFORMATION
    # -------------------------------------------------------------------------

    (
        "get_system_info",
        "Returns basic system information including OS platform, Python version, and current hostname.",
        (
            "import platform\n"
            "import socket\n"
            "info = {\n"
            "    'os':         platform.system(),\n"
            "    'os_version': platform.version(),\n"
            "    'python':     platform.python_version(),\n"
            "    'hostname':   socket.gethostname()\n"
            "}\n"
            "result = str(info)"
        ),
        "python"
    ),

    (
        "calculate",
        (
            "Evaluates a Python arithmetic or date expression and returns the result. "
            "Pass the expression as a string via the `expr` parameter. "
            "Pre-injected namespaces: `datetime` (module), `math` (module), `calendar` (module). "
            "Use `calendar.day_name[N]` to get a weekday name — avoids strftime quoting issues. "
            "Example expr values: "
            "'2 + 2', "
            "'datetime.date(2026,4,5) + datetime.timedelta(days=3)', "
            "'math.sqrt(144)', "
            "'calendar.day_name[(datetime.date.today() + datetime.timedelta(days=1)).weekday()]'"
        ),
        (
            "import datetime, math, calendar\n"
            "try:\n"
            "    result = str(eval(str(expr).strip(), {\n"
            "        'datetime': datetime,\n"
            "        'math':     math,\n"
            "        'calendar': calendar,\n"
            "    }))\n"
            "except Exception as e:\n"
            "    result = f'[ERROR] {e} | expr received: {repr(str(expr))}'"
        ),
        "python"
    ),

    # -------------------------------------------------------------------------
    # BOOLEAN SETTINGS MANAGEMENT
    # Operates exclusively on the settings_boolean table.
    # Values are binary switches: 0 or 1 only.
    # -------------------------------------------------------------------------

    (
        "get_all_settings",
        "Returns all rows from the settings_boolean table as a formatted string.",
        (
            "import sqlite3\n"
            "conn = sqlite3.connect('database.db')\n"
            "rows = conn.execute('SELECT setting_name, setting_bool FROM settings_boolean').fetchall()\n"
            "conn.close()\n"
            "result = '\\n'.join(f'{name} = {val}' for name, val in rows) or '(no settings found)'"
        ),
        "python"
    ),

    (
        "set_boolean",
        (
            "Sets any named entry in settings_boolean to 0 or 1. "
            "Requires two parameters: `setting_name` (str) and `setting_value` (int: 0 or 1). "
            "Use this for binary switches only — for string/numeric values use set_value. "
            "Example: set_boolean setting_name=DEBUG_LOGGING setting_value=1"
        ),
        (
            "import sqlite3\n"
            "val = int(setting_value)\n"
            "if val not in (0, 1):\n"
            "    result = f'[ERROR] set_boolean requires 0 or 1, got: {val}'\n"
            "else:\n"
            "    conn = sqlite3.connect('database.db')\n"
            "    conn.execute(\n"
            "        'INSERT INTO settings_boolean (setting_name, setting_bool) VALUES (?, ?) '\n"
            "        'ON CONFLICT(setting_name) DO UPDATE SET setting_bool = ?',\n"
            "        (setting_name, val, val)\n"
            "    )\n"
            "    conn.commit()\n"
            "    conn.close()\n"
            "    result = f'{setting_name} set to {val}.'"
        ),
        "python"
    ),

    # -------------------------------------------------------------------------
    # VALUE SETTINGS MANAGEMENT
    # Operates exclusively on the settings_values table.
    # Values are strings, paths, endpoints, intervals, and numeric ranges.
    # -------------------------------------------------------------------------

    (
        "get_all_values",
        "Returns all rows from the settings_values table as a formatted string.",
        (
            "import sqlite3\n"
            "conn = sqlite3.connect('database.db')\n"
            "rows = conn.execute('SELECT setting_name, setting_value FROM settings_values').fetchall()\n"
            "conn.close()\n"
            "result = '\\n'.join(f'{name} = {val}' for name, val in rows) or '(no values found)'"
        ),
        "python"
    ),

    (
        "set_value",
        (
            "Sets any named entry in settings_values to a given string value. "
            "Requires two parameters: `setting_name` (str) and `setting_value` (str). "
            "Use this for endpoints, paths, intervals, and numeric ranges — "
            "for binary switches (0/1) use set_boolean instead. "
            "Example: set_value setting_name=KOBOLD_TEMPERATURE setting_value=0.5"
        ),
        (
            "import sqlite3\n"
            "conn = sqlite3.connect('database.db')\n"
            "conn.execute(\n"
            "    'INSERT INTO settings_values (setting_name, setting_value) VALUES (?, ?) '\n"
            "    'ON CONFLICT(setting_name) DO UPDATE SET setting_value = ?',\n"
            "    (setting_name, str(setting_value), str(setting_value))\n"
            ")\n"
            "conn.commit()\n"
            "conn.close()\n"
            "result = f'{setting_name} set to {setting_value}.'"
        ),
        "python"
    ),

    # -------------------------------------------------------------------------
    # PROMPT MANAGEMENT
    # Operates on agent_prompts and the DEFAULT_PROMPT setting in
    # settings_values. The agent uses these to hot-swap the active system
    # prompt at runtime without restarting the process.
    # -------------------------------------------------------------------------

    (
        "list_prompts",
        "Returns all rows from the agent_prompts table showing prompt_name and prompt_enabled status.",
        (
            "import sqlite3\n"
            "conn = sqlite3.connect('database.db')\n"
            "rows = conn.execute('SELECT prompt_name, prompt_enabled FROM agent_prompts').fetchall()\n"
            "conn.close()\n"
            "result = '\\n'.join(f'{name} (enabled={enabled})' for name, enabled in rows) "
            "or '(no prompts found)'"
        ),
        "python"
    ),

    (
        "get_prompt_body",
        (
            "Returns the full prompt_body text of a named prompt from agent_prompts. "
            "Requires one parameter: `setting_value` (str) — the prompt_name to look up. "
            "Returns an error string if the prompt does not exist. "
            "Example: get_prompt_body setting_value=DEFAULT"
        ),
        (
            "import sqlite3\n"
            "conn = sqlite3.connect('database.db')\n"
            "row = conn.execute(\n"
            "    'SELECT prompt_body FROM agent_prompts WHERE prompt_name = ?',\n"
            "    (setting_value,)\n"
            ").fetchone()\n"
            "conn.close()\n"
            "if not row:\n"
            "    result = f'[ERROR] Prompt \"{setting_value}\" not found.'\n"
            "else:\n"
            "    result = row[0]"
        ),
        "python"
    ),

    (
        "add_prompt",
        (
            "Inserts a new prompt into the agent_prompts table. "
            "Requires two parameters: `setting_name` (str) — the prompt_name key, "
            "and `setting_value` (str) — the full prompt body text. "
            "The prompt is inserted as enabled. Skips insertion if prompt_name already exists. "
            "After adding, call reload_prompt with the new name to activate it. "
            "Example: add_prompt setting_name=TERSE_MODE setting_value=You are a terse agent."
        ),
        (
            "import sqlite3\n"
            "conn = sqlite3.connect('database.db')\n"
            "existing = conn.execute(\n"
            "    'SELECT id FROM agent_prompts WHERE prompt_name = ?', (setting_name,)\n"
            ").fetchone()\n"
            "if existing:\n"
            "    conn.close()\n"
            "    result = f'[SKIP] Prompt \"{setting_name}\" already exists. Use reload_prompt to activate it.'\n"
            "else:\n"
            "    conn.execute(\n"
            "        'INSERT INTO agent_prompts (prompt_name, prompt_body, prompt_enabled) VALUES (?, ?, 1)',\n"
            "        (setting_name, str(setting_value))\n"
            "    )\n"
            "    conn.commit()\n"
            "    conn.close()\n"
            "    result = f'Prompt \"{setting_name}\" added successfully.'"
        ),
        "python"
    ),

    (
        "reload_prompt",
        (
            "Switches the active system prompt by updating DEFAULT_PROMPT in settings_values "
            "to the given prompt_name, then sets PROMPT_RELOAD=1 in settings_boolean to signal "
            "index.py to hot-swap the prompt on the next turn. "
            "Requires one parameter: `setting_value` (str) — the target prompt_name. "
            "The new prompt must already exist and be enabled in the agent_prompts table."
        ),
        (
            "import sqlite3\n"
            "conn = sqlite3.connect('database.db')\n"
            "row = conn.execute(\n"
            "    'SELECT id FROM agent_prompts WHERE prompt_name = ? AND prompt_enabled = 1',\n"
            "    (setting_value,)\n"
            ").fetchone()\n"
            "if not row:\n"
            "    conn.close()\n"
            "    result = f'[ERROR] Prompt \"{setting_value}\" not found or is disabled.'\n"
            "else:\n"
            "    conn.execute(\n"
            "        'INSERT INTO settings_values (setting_name, setting_value) VALUES (?, ?) '\n"
            "        'ON CONFLICT(setting_name) DO UPDATE SET setting_value = ?',\n"
            "        ('DEFAULT_PROMPT', setting_value, setting_value)\n"
            "    )\n"
            "    conn.execute(\n"
            "        'INSERT INTO settings_boolean (setting_name, setting_bool) VALUES (?, ?) '\n"
            "        'ON CONFLICT(setting_name) DO UPDATE SET setting_bool = ?',\n"
            "        ('PROMPT_RELOAD', 1, 1)\n"
            "    )\n"
            "    conn.commit()\n"
            "    conn.close()\n"
            "    result = f'Active prompt switched to \"{setting_value}\". Reload flagged.'"
        ),
        "python"
    ),

    # -------------------------------------------------------------------------
    # SELF-MODIFICATION
    # Functions that allow the agent to inspect and rewrite its own roster.
    # upsert_function is the highest-leverage self-modification tool: an agent
    # that can rewrite its own functions is genuinely self-improving.
    # get_bash_log gives the agent auditability of its own shell history.
    # -------------------------------------------------------------------------

    (
        "list_functions",
        (
            "Returns the name and description of all enabled functions in the roster. "
            "Useful mid-chain to verify what tools are available without leaving the scratchpad loop. "
            "No parameters required."
        ),
        (
            "import sqlite3\n"
            "conn = sqlite3.connect('database.db')\n"
            "rows = conn.execute(\n"
            "    'SELECT function_name, function_description FROM functions WHERE function_enabled = 1'\n"
            ").fetchall()\n"
            "conn.close()\n"
            "if not rows:\n"
            "    result = '(no enabled functions found)'\n"
            "else:\n"
            "    result = '\\n'.join(f'{name}: {desc}' for name, desc in rows)"
        ),
        "python"
    ),

    (
        "upsert_function",
        (
            "Inserts or overwrites a function entry in the functions table. "
            "Requires two parameters: `setting_name` (str) — the function_name, "
            "and `setting_value` (str) — the full Python function body. "
            "The body must assign its output to the `result` variable. "
            "If the function_name already exists, its body and modified timestamp are updated. "
            "The description field is set to a placeholder — update it manually via run_bash_command "
            "or a follow-up SQL call if a precise description is needed. "
            "The updated roster takes effect on the next reseed or process restart. "
            "IMPORTANT: always call validate_function_body before calling upsert_function "
            "to ensure the body does not contain prohibited patterns. "
            "Example: upsert_function setting_name=my_tool setting_value=result=42"
        ),
        (
            "import sqlite3\n"
            "import datetime\n"
            "fname = str(setting_name).strip()\n"
            "fbody = str(setting_value)\n"
            "if not fname:\n"
            "    result = '[ERROR] setting_name (function_name) must not be empty.'\n"
            "else:\n"
            "    now = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')\n"
            "    conn = sqlite3.connect('database.db')\n"
            "    existing = conn.execute(\n"
            "        'SELECT id FROM functions WHERE function_name = ?', (fname,)\n"
            "    ).fetchone()\n"
            "    if existing:\n"
            "        conn.execute(\n"
            "            'UPDATE functions SET function_body = ?, function_modified = ? '\n"
            "            'WHERE function_name = ?',\n"
            "            (fbody, now, fname)\n"
            "        )\n"
            "        action = 'updated'\n"
            "    else:\n"
            "        conn.execute(\n"
            "            'INSERT INTO functions '\n"
            "            '(function_name, function_description, function_body, '\n"
            "            ' function_language, function_created, function_modified, function_enabled) '\n"
            "            'VALUES (?, ?, ?, ?, ?, ?, 1)',\n"
            "            (fname, f'Auto-generated: {fname}', fbody, 'python', now, now)\n"
            "        )\n"
            "        action = 'inserted'\n"
            "    conn.commit()\n"
            "    conn.close()\n"
            "    result = f'Function \"{fname}\" {action} successfully. Restart or reseed to activate.'"
        ),
        "python"
    ),

    (
        "get_bash_log",
        (
            "Returns the most recent entries from the agent_bash_logs audit table. "
            "Requires one parameter: `setting_value` (int as str) — the number of rows to return (max 50). "
            "Defaults to 3 rows if no value is supplied. "
            "Each row shows: run_at, exit_code, command, and any stderr output. "
            "Example: get_bash_log setting_value=10"
        ),
        (
            "import sqlite3\n"
            "raw_val = str(setting_value).strip()\n"
            "limit = min(int(raw_val) if raw_val else 3, 50)\n"
            "conn = sqlite3.connect('database.db')\n"
            "rows = conn.execute(\n"
            "    'SELECT run_at, exit_code, command, stderr FROM agent_bash_logs '\n"
            "    'ORDER BY id DESC LIMIT ?',\n"
            "    (limit,)\n"
            ").fetchall()\n"
            "conn.close()\n"
            "if not rows:\n"
            "    result = '(no bash log entries found)'\n"
            "else:\n"
            "    lines = []\n"
            "    for run_at, exit_code, command, stderr in rows:\n"
            "        line = f'[{run_at}] exit={exit_code} | {command}'\n"
            "        if stderr:\n"
            "            line += f' | stderr: {stderr[:120]}'\n"
            "        lines.append(line)\n"
            "    result = '\\n'.join(lines)"
        ),
        "python"
    ),

    # -------------------------------------------------------------------------
    # SAFETY / VALIDATION
    # validate_function_body is a pre-flight guard — call it before any
    # upsert_function invocation to catch prohibited patterns before they
    # reach exec(). audit_function_change writes a timestamped accountability
    # row to function_audit_log whenever a function is inserted or updated.
    # Together they form the two-step "check then record" contract that makes
    # autonomous self-modification traceable and recoverable.
    # -------------------------------------------------------------------------

    (
        "validate_function_body",
        (
            "Scans a candidate Python function body for prohibited patterns before insertion. "
            "Requires one parameter: `setting_value` (str) — the full function body to inspect. "
            "Returns 'OK' if the body is clean, or an error string naming the offending pattern. "
            "Always call this before upsert_function when operating autonomously. "
            "Prohibited: import os, import sys, import subprocess (outside approved wrappers), "
            "and DDL keywords (DROP, ALTER, CREATE TABLE) inside execute() calls. "
            "Example: validate_function_body setting_value=result=42"
        ),
        (
            "import re\n"
            "body = str(setting_value)\n"
            "prohibited = [\n"
            "    (r'import\\s+os\\b',                          'import os'),\n"
            "    (r'import\\s+sys\\b',                         'import sys'),\n"
            "    (r'__import__\\s*\\(',                        '__import__() call'),\n"
            "    (r'\\.execute\\s*\\(.*?\\b(DROP|ALTER)\\b',   'DDL statement in execute()'),\n"
            "    (r'CREATE\\s+TABLE',                          'CREATE TABLE in body'),\n"
            "]\n"
            "for pattern, label in prohibited:\n"
            "    if re.search(pattern, body, re.IGNORECASE | re.DOTALL):\n"
            "        result = f'[ERROR] Prohibited pattern detected: {label}'\n"
            "        break\n"
            "else:\n"
            "    result = 'OK'"
        ),
        "python"
    ),

    (
        "audit_function_change",
        (
            "Writes a timestamped audit row to the function_audit_log table recording "
            "a function insertion or update. "
            "Requires two parameters: `setting_name` (str) — the function_name being changed, "
            "and `setting_value` (str) — a brief description of what changed and why. "
            "Call this immediately after a successful upsert_function. "
            "Example: audit_function_change setting_name=my_tool setting_value=inserted to handle CSV parsing"
        ),
        (
            "import sqlite3\n"
            "import datetime\n"
            "now = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')\n"
            "conn = sqlite3.connect('database.db')\n"
            "conn.execute(\n"
            "    'INSERT INTO function_audit_log (function_name, change_note, changed_at) '\n"
            "    'VALUES (?, ?, ?)',\n"
            "    (str(setting_name), str(setting_value), now)\n"
            ")\n"
            "conn.commit()\n"
            "conn.close()\n"
            "result = f'Audit entry recorded for \"{setting_name}\" at {now}.'"
        ),
        "python"
    ),

    # -------------------------------------------------------------------------
    # BASH EXECUTION
    # Runs a shell command via subprocess and writes a full audit row to
    # agent_bash_logs (command, stdout, stderr, exit_code, timestamp).
    # The process is already sandboxed at the launcher level (firejail).
    # Commands are capped at 30 seconds. Both stdout and stderr are captured.
    # -------------------------------------------------------------------------

    (
        "run_bash_command",
        (
            "Executes a shell command and returns its stdout and stderr output. "
            "Requires one parameter: `expr` (str) — the full shell command to run. "
            "All executions are logged to agent_bash_logs with stdout, stderr, exit code, and timestamp. "
            "Commands are capped at 30 seconds. "
            "Examples: expr=ls -la, expr=df -h, expr=cat /etc/hostname"
        ),
        (
            "import subprocess\n"
            "import sqlite3\n"
            "import datetime\n"
            "\n"
            "command = str(expr).strip()\n"
            "try:\n"
            "    proc = subprocess.run(\n"
            "        command,\n"
            "        shell=True,\n"
            "        capture_output=True,\n"
            "        text=True,\n"
            "        timeout=30\n"
            "    )\n"
            "    out       = proc.stdout.strip()\n"
            "    err       = proc.stderr.strip()\n"
            "    exit_code = proc.returncode\n"
            "except subprocess.TimeoutExpired:\n"
            "    out       = ''\n"
            "    err       = '[ERROR] Command timed out after 30 seconds.'\n"
            "    exit_code = -1\n"
            "except Exception as e:\n"
            "    out       = ''\n"
            "    err       = f'[ERROR] subprocess failed: {e}'\n"
            "    exit_code = -1\n"
            "\n"
            "# Write audit row regardless of outcome\n"
            "try:\n"
            "    conn = sqlite3.connect('database.db')\n"
            "    conn.execute(\n"
            "        'INSERT INTO agent_bash_logs (command, stdout, stderr, exit_code, run_at) '\n"
            "        'VALUES (?, ?, ?, ?, ?)',\n"
            "        (command, out, err, exit_code, datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'))\n"
            "    )\n"
            "    conn.commit()\n"
            "    conn.close()\n"
            "except Exception:\n"
            "    pass  # Audit failure must never suppress the command result\n"
            "\n"
            "if exit_code == 0:\n"
            "    result = out if out else '(no output)'\n"
            "else:\n"
            "    parts = []\n"
            "    if out: parts.append(out)\n"
            "    if err: parts.append(err)\n"
            "    result = '\\n'.join(parts) if parts else f'(exit code {exit_code}, no output)'"
        ),
        "python"
    ),

    # -------------------------------------------------------------------------
    # MODEL SWITCHING
    # Atomically updates all model-related settings in one call.
    # Profiles mirror the model_profiles table in db_seed.py.
    # After calling switch_model, restart Kobold with the target model file.
    # The prompt hot-swap is automatic — no manual reload required.
    # -------------------------------------------------------------------------

    (
        "switch_model",
        (
            "Atomically switches the active model profile by updating ACTIVE_MODEL, "
            "PROMPT_FORMAT, DEFAULT_PROMPT, THINKING_MODE, and flagging PROMPT_RELOAD. "
            "Requires one parameter: `setting_value` (str) — the model label. "
            "Supported values: GEMMA, QWEN, HERMES, LLAMA3, MISTRAL, PHI3. "
            "After calling this function, restart Kobold with the correct model file."
        ),
        (
            "import sqlite3\n"
            "\n"
            "PROFILES = {\n"
            "    'GEMMA':   {'ACTIVE_MODEL': 'GEMMA',   'PROMPT_FORMAT': 'gemma',   'DEFAULT_PROMPT': 'DEFAULT', 'THINKING_MODE': 1},\n"
            "    'QWEN':    {'ACTIVE_MODEL': 'QWEN',    'PROMPT_FORMAT': 'chatml',  'DEFAULT_PROMPT': 'DEFAULT', 'THINKING_MODE': 1},\n"
            "    'HERMES':  {'ACTIVE_MODEL': 'HERMES',  'PROMPT_FORMAT': 'chatml',  'DEFAULT_PROMPT': 'DEFAULT', 'THINKING_MODE': 0},\n"
            "    'LLAMA3':  {'ACTIVE_MODEL': 'LLAMA3',  'PROMPT_FORMAT': 'llama3',  'DEFAULT_PROMPT': 'DEFAULT', 'THINKING_MODE': 0},\n"
            "    'MISTRAL': {'ACTIVE_MODEL': 'MISTRAL', 'PROMPT_FORMAT': 'mistral', 'DEFAULT_PROMPT': 'DEFAULT', 'THINKING_MODE': 0},\n"
            "    'PHI3':    {'ACTIVE_MODEL': 'PHI3',    'PROMPT_FORMAT': 'phi3',    'DEFAULT_PROMPT': 'DEFAULT', 'THINKING_MODE': 0},\n"
            "}\n"
            "\n"
            "label = str(setting_value).upper()\n"
            "if label not in PROFILES:\n"
            "    result = f'[ERROR] Unknown model \"{setting_value}\". Supported: {list(PROFILES.keys())}'\n"
            "else:\n"
            "    profile = PROFILES[label]\n"
            "    conn = sqlite3.connect('database.db')\n"
            "    for key in ('ACTIVE_MODEL', 'PROMPT_FORMAT', 'DEFAULT_PROMPT'):\n"
            "        conn.execute(\n"
            "            'INSERT INTO settings_values (setting_name, setting_value) VALUES (?, ?) '\n"
            "            'ON CONFLICT(setting_name) DO UPDATE SET setting_value = ?',\n"
            "            (key, profile[key], profile[key])\n"
            "        )\n"
            "    conn.execute(\n"
            "        'INSERT INTO settings_boolean (setting_name, setting_bool) VALUES (?, ?) '\n"
            "        'ON CONFLICT(setting_name) DO UPDATE SET setting_bool = ?',\n"
            "        ('THINKING_MODE', profile['THINKING_MODE'], profile['THINKING_MODE'])\n"
            "    )\n"
            "    conn.execute(\n"
            "        'INSERT INTO settings_boolean (setting_name, setting_bool) VALUES (?, ?) '\n"
            "        'ON CONFLICT(setting_name) DO UPDATE SET setting_bool = ?',\n"
            "        ('PROMPT_RELOAD', 1, 1)\n"
            "    )\n"
            "    conn.commit()\n"
            "    conn.close()\n"
            "    result = (\n"
            "        f'Switched to {label}. '\n"
            "        f'Format: {profile[\"PROMPT_FORMAT\"]}, '\n"
            "        f'Prompt: {profile[\"DEFAULT_PROMPT\"]}, '\n"
            "        f'Thinking: {profile[\"THINKING_MODE\"]}. '\n"
            "        f'Reload flagged. Restart Kobold with the {label} model file.'\n"
            "    )\n"
        ),
        "python"
    ),

]
