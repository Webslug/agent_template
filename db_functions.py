# =============================================================================
# db_functions.py
# Canonical roster of seeded agent functions.
# Imported by db_seed.py — never executed directly.
#
# Each entry: (function_name, function_description, function_body, function_language)
# function_body is a string of Python executed via exec() at runtime.
# All function bodies must assign their output to the variable `result`.
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
        "get_current_datetime",
        "Returns the current date and time as a formatted string (YYYY-MM-DD HH:MM:SS).",
        (
            "import datetime\n"
            "result = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')"
        ),
        "python"
    ),

    (
        "calculate",
        (
            "Evaluates a Python arithmetic or date expression and returns the result. "
            "Pass the expression as a string assigned to the variable `expr` before calling. "
            "Supports datetime and math operations via pre-injected `datetime` and `math` modules. "
            "Example expr values: "
            "'2 + 2', "
            "'datetime.date(2026,4,5) + datetime.timedelta(days=3)', "
            "'math.sqrt(144)'"
        ),
        (
            "import datetime, math\n"
            "try:\n"
            "    result = str(eval(str(expr).strip(), {'datetime': datetime, 'math': math}))\n"
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
        "enable_debug_mode",
        "Sets DEBUG_LOGGING to 1 in the settings_boolean table (enables debug mode).",
        (
            "import sqlite3\n"
            "conn = sqlite3.connect('database.db')\n"
            "conn.execute(\"INSERT INTO settings_boolean (setting_name, setting_bool) VALUES ('DEBUG_LOGGING', 1) \"\n"
            "             \"ON CONFLICT(setting_name) DO UPDATE SET setting_bool = 1\")\n"
            "conn.commit()\n"
            "conn.close()\n"
            "result = 'DEBUG_LOGGING set to 1 (debug mode enabled).'"
        ),
        "python"
    ),

    (
        "disable_debug_mode",
        "Sets DEBUG_LOGGING to 0 in the settings_boolean table (disables debug mode).",
        (
            "import sqlite3\n"
            "conn = sqlite3.connect('database.db')\n"
            "conn.execute(\"INSERT INTO settings_boolean (setting_name, setting_bool) VALUES ('DEBUG_LOGGING', 0) \"\n"
            "             \"ON CONFLICT(setting_name) DO UPDATE SET setting_bool = 0\")\n"
            "conn.commit()\n"
            "conn.close()\n"
            "result = 'DEBUG_LOGGING set to 0 (debug mode disabled).'"
        ),
        "python"
    ),

    (
        "set_setting",
        (
            "Sets any named entry in settings_boolean to a given integer value (0 or 1). "
            "Requires two parameters: `setting_name` (str) and `setting_value` (int). "
            "Use this for binary switches only — for string/numeric values use set_value."
        ),
        (
            "import sqlite3\n"
            "conn = sqlite3.connect('database.db')\n"
            "conn.execute(\n"
            "    'INSERT INTO settings_boolean (setting_name, setting_bool) VALUES (?, ?) '\n"
            "    'ON CONFLICT(setting_name) DO UPDATE SET setting_bool = ?',\n"
            "    (setting_name, setting_value, setting_value)\n"
            ")\n"
            "conn.commit()\n"
            "conn.close()\n"
            "result = f'{setting_name} set to {setting_value}.'"
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
            "for binary switches (0/1) use set_setting instead."
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
    # Supported profiles: GEMMA, HERMES
    #
    # GEMMA  — google_gemma-3-4b-it-q4_k_s.gguf (~4GB VRAM)
    #          prompt: GEMMA_DEFAULT, format: gemma, thinking: 1
    # HERMES — Hermes-3-Llama-3.1-8B.Q6_K.gguf  (~6GB VRAM, ChatML format)
    #          prompt: DEFAULT,       format: chatml, thinking: 0
    #
    # After calling switch_model, restart Kobold with the target model file.
    # The prompt hot-swap is automatic — no manual reload required.
    # -------------------------------------------------------------------------

    (
        "switch_model",
        (
            "Atomically switches the active model profile by updating ACTIVE_MODEL, "
            "PROMPT_FORMAT, DEFAULT_PROMPT, THINKING_MODE, and flagging PROMPT_RELOAD. "
            "Requires one parameter: `setting_value` (str) — the model label. "
            "Supported values: 'GEMMA' (Gemma 3/4 instruction-tuned, ~4GB VRAM) or "
            "'HERMES' (Hermes-3 ChatML, ~6GB VRAM). "
            "After calling this function, restart Kobold with the correct model file."
        ),
        (
            "import sqlite3\n"
            "\n"
            "# Model profile definitions\n"
            "# GEMMA  — google_gemma-3-4b-it-q4_k_s.gguf\n"
            "# HERMES — Hermes-3-Llama-3.1-8B.Q6_K.gguf (ChatML format, 6GB VRAM)\n"
            "PROFILES = {\n"
            "    'GEMMA': {\n"
            "        'ACTIVE_MODEL':  'GEMMA',\n"
            "        'PROMPT_FORMAT': 'gemma',\n"
            "        'DEFAULT_PROMPT': 'GEMMA_DEFAULT',\n"
            "        'THINKING_MODE': 1,\n"
            "    },\n"
            "    'HERMES': {\n"
            "        'ACTIVE_MODEL':  'HERMES',\n"
            "        'PROMPT_FORMAT': 'chatml',\n"
            "        'DEFAULT_PROMPT': 'DEFAULT',\n"
            "        'THINKING_MODE': 0,\n"
            "    },\n"
            "}\n"
            "\n"
            "label = str(setting_value).upper()\n"
            "if label not in PROFILES:\n"
            "    result = f'[ERROR] Unknown model \"{setting_value}\". Supported: GEMMA, HERMES.'\n"
            "else:\n"
            "    profile = PROFILES[label]\n"
            "    conn = sqlite3.connect('database.db')\n"
            "    # Write all value settings for this profile\n"
            "    for key in ('ACTIVE_MODEL', 'PROMPT_FORMAT', 'DEFAULT_PROMPT'):\n"
            "        conn.execute(\n"
            "            'INSERT INTO settings_values (setting_name, setting_value) VALUES (?, ?) '\n"
            "            'ON CONFLICT(setting_name) DO UPDATE SET setting_value = ?',\n"
            "            (key, profile[key], profile[key])\n"
            "        )\n"
            "    # Write THINKING_MODE boolean\n"
            "    conn.execute(\n"
            "        'INSERT INTO settings_boolean (setting_name, setting_bool) VALUES (?, ?) '\n"
            "        'ON CONFLICT(setting_name) DO UPDATE SET setting_bool = ?',\n"
            "        ('THINKING_MODE', profile['THINKING_MODE'], profile['THINKING_MODE'])\n"
            "    )\n"
            "    # Flag prompt reload\n"
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
