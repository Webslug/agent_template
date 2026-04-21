Below is a **quick‑scan of the current `SEED_FUNCTIONS`** and a set of **high‑value gaps** that show up when you think about the whole lifecycle of a deployed agent:

| # | Existing function (category) | What’s missing? |
|---|------------------------------|-----------------|
| 1️⃣ | `get_system_info` | Nothing critical – fine. |
| 2️⃣ | `calculate` | No safe‑guard for `eval` – a “sandbox” wrapper would be useful. |
| 3️⃣ | `get_all_settings` / `set_boolean` | No **list / enable / disable** helpers for individual boolean rows. |
| 4️⃣ | `get_all_values` / `set_value` | Same – no **delete / rename** helpers, nor a **validation** function. |
| 5️⃣ | Prompt helpers (`list_prompts`, `add_prompt`, `reload_prompt`) | No **enable/disable**, **delete**, or **inspect** helpers for prompts. |
| 6️⃣ | Function roster helpers (`list_functions`, `upsert_function`, `get_bash_log`) | No **disable/enable**, **delete**, **count**, or **search‑by‑description** utilities. |
| 7️⃣ | Bash executor (`run_bash_command`) | No **clear‑log**, **list‑log**, **tail‑log** utilities, nor a **whitelist/blacklist** checker. |
| 8️⃣ | Model switcher (`switch_model`) | No **list_models**, **current_model**, or **model_info** helpers. |
| – | **Harnesses** | No CRUD helpers for harness rows (list, enable/disable, add, delete). |
| – | **Project files** | No **list/add/remove** utilities. |
| – | **Scheduling** | No way to **list** or **cancel** an active stagger timer from the model side. |
| – | **Audit / diagnostic** | No generic **get_agent_log**, **clear_agent_log**, **dump_runtime_state** helpers. |
| – | **Safety / validation** | No “sanity‑check” function that runs before a user‑generated function body is up‑serted (e.g. ensure it does not contain `import os`, `subprocess`, or DDL). |
| – | **Self‑improvement** | A tiny “`suggest_improvement`” stub that calls the `evolve` module could make the autonomy loop smoother. |

Below you’ll find a **grouped checklist** of the most useful missing functions, a short **description** of why each is needed, and a **minimal skeleton** (as a string ready to be added to `SEED_FUNCTIONS`).  You can cherry‑pick the ones you want to seed now and later extend them with richer logic.

---

<details><summary>🔧 1️⃣ Boolean‑setting utilities</summary>

| Function | Why it matters |
|----------|----------------|
| `enable_boolean` | Turn a `setting_bool` to 1 without having to remember the exact column name. |
| `disable_boolean` | Turn a `setting_bool` to 0. |
| `delete_boolean` | Remove a flag (rare, but sometimes you want to clean up old feature‑toggles). |
| `rename_boolean` | Rename a setting while preserving its value (useful during schema migrations). |

```python
(
    "enable_boolean",
    "Enable a boolean switch (set to 1). Requires `setting_name`.",
    (
        "import sqlite3\n"
        "conn = sqlite3.connect('database.db')\n"
        "conn.execute(\n"
        "    'UPDATE settings_boolean SET setting_bool = 1 WHERE setting_name = ?',\n"
        "    (setting_name,)\n"
        ")\n"
        "conn.commit()\n"
        "conn.close()\n"
        "result = f\"{setting_name} enabled.\"\n"
    ),
    "python"
),

(
    "disable_boolean",
    "Disable a boolean switch (set to 0). Requires `setting_name`.",
    (
        "import sqlite3\n"
        "conn = sqlite3.connect('database.db')\n"
        "conn.execute(\n"
        "    'UPDATE settings_boolean SET setting_bool = 0 WHERE setting_name = ?',\n"
        "    (setting_name,)\n"
        ")\n"
        "conn.commit()\n"
        "conn.close()\n"
        "result = f\"{setting_name} disabled.\"\n"
    ),
    "python"
),

(
    "delete_boolean",
    "Delete a boolean flag entirely. Requires `setting_name`.",
    (
        "import sqlite3\n"
        "conn = sqlite3.connect('database.db')\n"
        "conn.execute('DELETE FROM settings_boolean WHERE setting_name = ?', (setting_name,))\n"
        "conn.commit()\n"
        "conn.close()\n"
        "result = f\"{setting_name} removed from BOOLEAN table.\"\n"
    ),
    "python"
),

(
    "rename_boolean",
    "Rename a boolean setting while preserving its value. Requires `old_name` and `new_name`.",
    (
        "import sqlite3\n"
        "conn = sqlite3.connect('database.db')\n"
        "val = conn.execute('SELECT setting_bool FROM settings_boolean WHERE setting_name = ?', (old_name,)).fetchone()\n"
        "if not val:\n"
        "    result = f'[ERROR] {old_name} not found.'\n"
        "else:\n"
        "    conn.execute('INSERT OR REPLACE INTO settings_boolean (setting_name, setting_bool) VALUES (?, ?)', (new_name, val[0]))\n"
        "    conn.execute('DELETE FROM settings_boolean WHERE setting_name = ?', (old_name,))\n"
        "    conn.commit()\n"
        "    result = f\"Renamed {old_name} → {new_name}.\"\n"
        "conn.close()\n"
    ),
    "python"
),
```

</details>

---

<details><summary>🔧 2️⃣ Value‑setting utilities</summary>

| Function | Why it matters |
|----------|----------------|
| `delete_value` | Clean up stale configuration keys. |
| `append_value` | Append to a delimited list (e.g., add a new anti‑prompt token). |
| `increment_value` | Handy for numeric counters (e.g., usage‑meter). |
| `reset_value` | Reset a setting to its default (useful for testing). |

```python
(
    "delete_value",
    "Delete a row from settings_values. Requires `setting_name`.",
    (
        "import sqlite3\n"
        "conn = sqlite3.connect('database.db')\n"
        "conn.execute('DELETE FROM settings_values WHERE setting_name = ?', (setting_name,))\n"
        "conn.commit()\n"
        "conn.close()\n"
        "result = f\"{setting_name} removed from VALUES table.\"\n"
    ),
    "python"
),

(
    "append_value",
    "Append a string to a CSV‑style value column (e.g., add a new anti‑prompt). Requires `setting_name` and `append`.",
    (
        "import sqlite3\n"
        "conn = sqlite3.connect('database.db')\n"
        "cur = conn.execute('SELECT setting_value FROM settings_values WHERE setting_name = ?', (setting_name,))\n"
        "row = cur.fetchone()\n"
        "if not row:\n"
        "    result = f'[ERROR] {setting_name} not found.'\n"
        "else:\n"
        "    current = row[0]\n"
        "    new_val = (current + ',' + append).strip(',') if current else append\n"
        "    conn.execute('UPDATE settings_values SET setting_value = ? WHERE setting_name = ?', (new_val, setting_name))\n"
        "    conn.commit()\n"
        "    result = f\"Appended to {setting_name}: {append}.\"\n"
        "conn.close()\n"
    ),
    "python"
),

(
    "increment_value",
    "Treat a settings_values entry as an integer and increment it by `delta` (default 1).",
    (
        "import sqlite3\n"
        "delta = int(delta) if 'delta' in globals() else 1\n"
        "conn = sqlite3.connect('database.db')\n"
        "cur = conn.execute('SELECT setting_value FROM settings_values WHERE setting_name = ?', (setting_name,))\n"
        "row = cur.fetchone()\n"
        "if not row:\n"
        "    result = f'[ERROR] {setting_name} not found.'\n"
        "else:\n"
        "    try:\n"
        "        new_val = str(int(row[0]) + delta)\n"
        "    except ValueError:\n"
        "        result = f'[ERROR] {setting_name} is not an integer.'\n"
        "    else:\n"
        "        conn.execute('UPDATE settings_values SET setting_value = ? WHERE setting_name = ?', (new_val, setting_name))\n"
        "        conn.commit()\n"
        "        result = f\"{setting_name} now {new_val}.\"\n"
        "conn.close()\n"
    ),
    "python"
),

(
    "reset_value",
    "Reset a value setting to a supplied default (or empty string). Requires `setting_name` and optional `default`.",
    (
        "import sqlite3\n"
        "default = '' if 'default' not in globals() else str(default)\n"
        "conn = sqlite3.connect('database.db')\n"
        "conn.execute('UPDATE settings_values SET setting_value = ? WHERE setting_name = ?', (default, setting_name))\n"
        "conn.commit()\n"
        "conn.close()\n"
        "result = f\"{setting_name} reset to '{default}'.\"\n"
    ),
    "python"
),
```

</details>

---

<details><summary>🔧 3️⃣ Prompt‑management utilities</summary>

| Function | Why it matters |
|----------|----------------|
| `enable_prompt` / `disable_prompt` | Temporarily turn a prompt on/off without deleting it. |
| `delete_prompt` | Clean up old prompt versions. |
| `rename_prompt` | Helpful when you evolve a prompt name. |
| `get_prompt_body` | Return the raw body of a named prompt (useful for introspection). |
| `list_enabled_prompts` | Show only the prompts that are currently active. |

```python
(
    "enable_prompt",
    "Mark a prompt as enabled (prompt_enabled=1). Requires `setting_name` (the prompt_name).",
    (
        "import sqlite3\n"
        "conn = sqlite3.connect('database.db')\n"
        "conn.execute('UPDATE agent_prompts SET prompt_enabled = 1 WHERE prompt_name = ?', (setting_name,))\n"
        "conn.commit()\n"
        "conn.close()\n"
        "result = f'Prompt \"{setting_name}\" enabled.'\n"
    ),
    "python"
),

(
    "disable_prompt",
    "Mark a prompt as disabled (prompt_enabled=0). Requires `setting_name`.",
    (
        "import sqlite3\n"
        "conn = sqlite3.connect('database.db')\n"
        "conn.execute('UPDATE agent_prompts SET prompt_enabled = 0 WHERE prompt_name = ?', (setting_name,))\n"
        "conn.commit()\n"
        "conn.close()\n"
        "result = f'Prompt \"{setting_name}\" disabled.'\n"
    ),
    "python"
),

(
    "delete_prompt",
    "Delete a prompt from the table. Requires `setting_name`. **Will not delete the currently active DEFAULT_PROMPT** (safety check).",
    (
        "import sqlite3\n"
        "if setting_name.upper() == 'DEFAULT':\n"
        "    result = '[ERROR] Cannot delete the DEFAULT prompt.'\n"
        "else:\n"
        "    conn = sqlite3.connect('database.db')\n"
        "    conn.execute('DELETE FROM agent_prompts WHERE prompt_name = ?', (setting_name,))\n"
        "    conn.commit()\n"
        "    conn.close()\n"
        "    result = f'Prompt \"{setting_name}\" removed.'\n"
    ),
    "python"
),

(
    "rename_prompt",
    "Rename a prompt. Requires `old_name` and `new_name`.",
    (
        "import sqlite3\n"
        "conn = sqlite3.connect('database.db')\n"
        "conn.execute('UPDATE agent_prompts SET prompt_name = ? WHERE prompt_name = ?', (new_name, old_name))\n"
        "conn.commit()\n"
        "conn.close()\n"
        "result = f'Prompt renamed {old_name} → {new_name}.'\n"
    ),
    "python"
),

(
    "get_prompt_body",
    "Return the raw body of a named prompt (no function‑digest appended). Requires `setting_name`.",
    (
        "import sqlite3\n"
        "conn = sqlite3.connect('database.db')\n"
        "row = conn.execute('SELECT prompt_body FROM agent_prompts WHERE prompt_name = ?', (setting_name,)).fetchone()\n"
        "conn.close()\n"
        "result = row[0] if row else f'[ERROR] Prompt \"{setting_name}\" not found.'\n"
    ),
    "python"
),

(
    "list_enabled_prompts",
    "List only prompts where `prompt_enabled=1`. No parameters.",
    (
        "import sqlite3\n"
        "conn = sqlite3.connect('database.db')\n"
        "rows = conn.execute('SELECT prompt_name FROM agent_prompts WHERE prompt_enabled = 1').fetchall()\n"
        "conn.close()\n"
        "result = '\\n'.join(name for (name,) in rows) or '(no enabled prompts)'\n"
    ),
    "python"
),
```

</details>

---

<details><summary>🔧 4️⃣ Function‑roster utilities (beyond `list_functions` & `upsert_function`)</summary>

| Function | Why it matters |
|----------|----------------|
| `disable_function` / `enable_function` | Quick toggle for testing without deleting the body. |
| `delete_function` | Remove an obsolete tool (subject to harness “NO SILENT DELETE”). |
| `count_functions` | Handy for enforcing the `ROSTER_MAX_FUNCTIONS` harness. |
| `search_functions` | Find functions by keyword in description (helps the agent discover missing capabilities). |

```python
(
    "disable_function",
    "Set `function_enabled = 0` for a given function. Requires `setting_name` (function_name).",
    (
        "import sqlite3\n"
        "conn = sqlite3.connect('database.db')\n"
        "conn.execute('UPDATE functions SET function_enabled = 0 WHERE function_name = ?', (setting_name,))\n"
        "conn.commit()\n"
        "conn.close()\n"
        "result = f'Function \"{setting_name}\" disabled.'\n"
    ),
    "python"
),

(
    "enable_function",
    "Set `function_enabled = 1` for a given function. Requires `setting_name`.",
    (
        "import sqlite3\n"
        "conn = sqlite3.connect('database.db')\n"
        "conn.execute('UPDATE functions SET function_enabled = 1 WHERE function_name = ?', (setting_name,))\n"
        "conn.commit()\n"
        "conn.close()\n"
        "result = f'Function \"{setting_name}\" enabled.'\n"
    ),
    "python"
),

(
    "delete_function",
    "Delete a function entry completely. Requires `setting_name`. **Respect the ROSTER_NO_SILENT_DELETE** harness – the agent should call `run_bash_command` first to log a reason.",
    (
        "import sqlite3\n"
        "conn = sqlite3.connect('database.db')\n"
        "conn.execute('DELETE FROM functions WHERE function_name = ?', (setting_name,))\n"
        "conn.commit()\n"
        "conn.close()\n"
        "result = f'Function \"{setting_name}\" removed from roster.'\n"
    ),
    "python"
),

(
    "count_functions",
    "Return the number of enabled functions. No parameters.",
    (
        "import sqlite3\n"
        "conn = sqlite3.connect('database.db')\n"
        "cnt = conn.execute('SELECT COUNT(*) FROM functions WHERE function_enabled = 1').fetchone()[0]\n"
        "conn.close()\n"
        "result = str(cnt)\n"
    ),
    "python"
),

(
    "search_functions",
    "Search enabled functions by a free‑text keyword in the description. Requires `setting_value` (the keyword).",
    (
        "import sqlite3\n"
        "kw = f\"%{setting_value}%\"\n"
        "conn = sqlite3.connect('database.db')\n"
        "rows = conn.execute(\n"
        "    'SELECT function_name, function_description FROM functions WHERE function_enabled = 1 AND function_description LIKE ?',\n"
        "    (kw,)\n"
        ").fetchall()\n"
        "conn.close()\n"
        "if not rows:\n"
        "    result = '(no matches)'\n"
        "else:\n"
        "    result = '\\n'.join(f'{n}: {d}' for n, d in rows)\n"
    ),
    "python"
),
```

</details>

---

<details><summary>🔧 5️⃣ Harness (constraint) utilities</summary>

| Function | Why it matters |
|----------|----------------|
| `list_harnesses` | Show every harness (enabled + disabled). |
| `enable_harness` / `disable_harness` | Toggle a rule without editing the DB manually. |
| `add_harness` | Insert a new operator rule at runtime. |
| `delete_harness` | Remove a custom rule (subject to future policy). |
| `count_harnesses` | Quick check for “too many constraints”. |

```python
(
    "list_harnesses",
    "List all harness rules, showing enabled state. No parameters.",
    (
        "import sqlite3\n"
        "conn = sqlite3.connect('database.db')\n"
        "rows = conn.execute('SELECT harness_name, harness_rule, harness_enabled FROM harnesses').fetchall()\n"
        "conn.close()\n"
        "if not rows:\n"
        "    result = '(no harnesses defined)'\n"
        "else:\n"
        "    lines = []\n"
        "    for name, rule, en in rows:\n"
        "        status = 'ON' if en else 'OFF'\n"
        "        lines.append(f'[{status}] {name}: {rule}')\n"
        "    result = '\\n'.join(lines)\n"
    ),
    "python"
),

(
    "enable_harness",
    "Enable a harness rule. Requires `setting_name` (the harness_name).",
    (
        "import sqlite3\n"
        "conn = sqlite3.connect('database.db')\n"
        "conn.execute('UPDATE harnesses SET harness_enabled = 1 WHERE harness_name = ?', (setting_name,))\n"
        "conn.commit()\n"
        "conn.close()\n"
        "result = f'Harness \"{setting_name}\" enabled.'\n"
    ),
    "python"
),

(
    "disable_harness",
    "Disable a harness rule. Requires `setting_name`.",
    (
        "import sqlite3\n"
        "conn = sqlite3.connect('database.db')\n"
        "conn.execute('UPDATE harnesses SET harness_enabled = 0 WHERE harness_name = ?', (setting_name,))\n"
        "conn.commit()\n"
        "conn.close()\n"
        "result = f'Harness \"{setting_name}\" disabled.'\n"
    ),
    "python"
),

(
    "add_harness",
    "Insert a new harness rule. Requires `setting_name` (name) and `setting_value` (rule text).",
    (
        "import sqlite3, datetime\n"
        "now = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')\n"
        "conn = sqlite3.connect('database.db')\n"
        "conn.execute('INSERT INTO harnesses (harness_name, harness_rule, harness_enabled) VALUES (?, ?, 1)', (setting_name, setting_value))\n"
        "conn.commit()\n"
        "conn.close()\n"
        "result = f'Added harness \"{setting_name}\".'\n"
    ),
    "python"
),

(
    "delete_harness",
    "Delete a harness rule permanently. Requires `setting_name`.",
    (
        "import sqlite3\n"
        "conn = sqlite3.connect('database.db')\n"
        "conn.execute('DELETE FROM harnesses WHERE harness_name = ?', (setting_name,))\n"
        "conn.commit()\n"
        "conn.close()\n"
        "result = f'Harness \"{setting_name}\" removed.'\n"
    ),
    "python"
),

(
    "count_harnesses",
    "Return the total number of harness rows (both enabled and disabled).",
    (
        "import sqlite3\n"
        "conn = sqlite3.connect('database.db')\n"
        "cnt = conn.execute('SELECT COUNT(*) FROM harnesses').fetchone()[0]\n"
        "conn.close()\n"
        "result = str(cnt)\n"
    ),
    "python"
),
```

</details>

---

<details><summary>🔧 6️⃣ Model‑profile utilities</summary>

| Function | Why it matters |
|----------|----------------|
| `list_models` | Show every profile in `model_profiles`. |
| `current_model` | Echo the active `ACTIVE_MODEL` value plus its meta info. |
| `model_info` | Return the full row (format, endpoint, etc.) for a requested label. |
| `set_endpoint` | Change the URL of a given endpoint key (`ENDPOINT_KOBOLD`, `ENDPOINT_OLLAMA`). |

```python
(
    "list_models",
    "Return all model profile names with a short description.",
    (
        "import sqlite3\n"
        "conn = sqlite3.connect('database.db')\n"
        "rows = conn.execute('SELECT profile_name, notes FROM model_profiles').fetchall()\n"
        "conn.close()\n"
        "result = '\\n'.join(f'{n}: {note}' for n, note in rows) if rows else '(no models defined)'\n"
    ),
    "python"
),

(
    "current_model",
    "Report the currently active model and its profile fields.",
    (
        "import sqlite3\n"
        "conn = sqlite3.connect('database.db')\n"
        "active = conn.execute('SELECT setting_value FROM settings_values WHERE setting_name = \"ACTIVE_MODEL\"').fetchone()[0]\n"
        "profile = conn.execute('SELECT * FROM model_profiles WHERE profile_name = ?', (active,)).fetchone()\n"
        "conn.close()\n"
        "if not profile:\n"
        "    result = f'[ERROR] Active model {active} not found in profiles.'\n"
        "else:\n"
        "    # turn the Row into a dict for readability\n"
        "    d = {k: profile[k] for k in profile.keys()}\n"
        "    result = f'ACTIVE_MODEL = {active}\\n' + '\\n'.join(f'{k}: {v}' for k, v in d.items())\n"
    ),
    "python"
),

(
    "model_info",
    "Return the full profile row for a supplied `setting_name`. Useful for dynamic introspection.",
    (
        "import sqlite3\n"
        "conn = sqlite3.connect('database.db')\n"
        "row = conn.execute('SELECT * FROM model_profiles WHERE profile_name = ?', (setting_name,)).fetchone()\n"
        "conn.close()\n"
        "if not row:\n"
        "    result = f'[ERROR] No profile named {setting_name}.'\n"
        "else:\n"
        "    result = ', '.join(f'{k}={row[k]}' for k in row.keys())\n"
    ),
    "python"
),

(
    "set_endpoint",
    "Update the URL for an endpoint key (e.g. ENDPOINT_KOBOLD). Requires `setting_name` (the key) and `setting_value` (the URL).",
    (
        "import sqlite3\n"
        "conn = sqlite3.connect('database.db')\n"
        "conn.execute('UPDATE settings_values SET setting_value = ? WHERE setting_name = ?', (setting_value, setting_name))\n"
        "conn.commit()\n"
        "conn.close()\n"
        "result = f'Endpoint {setting_name} set to {setting_value}.'\n"
    ),
    "python"
),
```

</details>

---

<details><summary>🔧 7️⃣ Stagger‑scheduler utilities (agent‑side only)</summary>

| Function | Why it matters |
|----------|----------------|
| `list_staggers` | Expose the in‑memory `_STAGGER_REGISTRY` to the model (so the model can ask “what timers are pending?”). |
| `cancel_stagger` | Allow the model to withdraw a timer if a task becomes unnecessary. |

> **Note** – these functions need *access* to the in‑memory registry, which lives in `agent.py`. The simplest way is to expose a thin wrapper in `agent.py` and add a “proxy” entry in `SEED_FUNCTIONS` that calls it via `import agent; result = agent._list_staggers()` etc.

```python
# In agent.py (add near the top, after the registry definition)

def _list_staggers():
    lines = []
    for e in _STAGGER_REGISTRY:
        status = "FIRED" if e["fired"] else "PENDING"
        lines.append(f'#{e["id"]:02d} [{status}] in {e["delay"]}m → {e["command"]}')
    return "\n".join(lines) if lines else "(no stagger timers)"

def _cancel_stagger(stagger_id):
    for e in _STAGGER_REGISTRY:
        if e["id"] == stagger_id and not e["fired"]:
            e["timer"].cancel()
            e["fired"] = True
            return f'Stagger #{stagger_id} cancelled.'
    return f'[ERROR] No pending stagger with id #{stagger_id}.'

# Then seed the wrappers:

(
    "list_staggers",
    "Return a human‑readable list of all active / pending / fired stagger timers.",
    (
        "import agent\n"
        "result = agent._list_staggers()\n"
    ),
    "python"
),

(
    "cancel_stagger",
    "Cancel a pending stagger timer by its integer `setting_value` (the timer id).",
    (
        "import agent\n"
        "tid = int(setting_value)\n"
        "result = agent._cancel_stagger(tid)\n"
    ),
    "python"
),
```

</details>

---

<details><summary>🔧 8️⃣ Audit / logging utilities</summary>

| Function | Why it matters |
|----------|----------------|
| `get_agent_log` | Return the most recent rows from a generic `logs` table (you already have `agent_bash_logs`, but a generic log table can hold other diagnostics). |
| `clear_agent_log` | Truncate the log table – handy for a “reset” command. |
| `dump_runtime_state` | Serialize the 8‑tuple (or just the key dicts) to JSON for debugging. |

```python
(
    "get_agent_log",
    "Return the latest `n` rows (default 5) from the generic `logs` table. Requires optional `setting_value` (int).",
    (
        "import sqlite3\n"
        "limit = int(setting_value) if 'setting_value' in globals() else 5\n"
        "conn = sqlite3.connect('database.db')\n"
        "rows = conn.execute('SELECT log_date, log_code, log_text FROM logs ORDER BY id DESC LIMIT ?', (limit,)).fetchall()\n"
        "conn.close()\n"
        "if not rows:\n"
        "    result = '(no log entries)'\n"
        "else:\n"
        "    result = '\\n'.join(f'[{d}] ({c}) {t}' for d, c, t in rows)\n"
    ),
    "python"
),

(
    "clear_agent_log",
    "Delete all rows from the generic `logs` table. No parameters.",
    (
        "import sqlite3\n"
        "conn = sqlite3.connect('database.db')\n"
        "conn.execute('DELETE FROM logs')\n"
        "conn.commit()\n"
        "conn.close()\n"
        "result = 'All log entries cleared.'\n"
    ),
    "python"
),

(
    "dump_runtime_state",
    "Serialize the in‑memory runtime tuple (settings, values, prompts, functions, profiles, project_files, harnesses) to JSON for debugging. No parameters.",
    (
        "import json, agent\n"
        "# The ``runtime`` tuple is passed in via the normal exec environment – we pull it from the global scope.\n"
        "if 'runtime' not in globals():\n"
        "    result = '[ERROR] runtime not available in exec scope.'\n"
        "else:\n"
        "    # Convert each list of Row‑dicts to JSON‑serialisable structures\n"
        "    safe = [list(map(dict, part)) for part in runtime]\n"
        "    result = json.dumps(safe, indent=2)\n"
    ),
    "python"
),
```

</details>

---

<details><summary>🔧 9️⃣ Safety / sandbox helpers (pre‑flight checks before upserting code)</summary>

| Function | Why it matters |
|----------|----------------|
| `validate_function_body` | Scan a candidate body for disallowed imports (`os`, `sys`, `subprocess`, `sqlite3` DDL) before the agent is allowed to `upsert_function`. |
| `audit_function_change` | Record a change to the `functions` table in the generic `logs` table (adds accountability). |

```python
(
    "validate_function_body",
    "Inspect a candidate Python body for prohibited patterns. Returns 'OK' or an error string. Requires `setting_value` (the body).",
    (
        "import re\n"
        "prohibited = [r'import\\s+os', r'import\\s+sys', r'import\\s+subprocess', r'cursor\\.execute\\s*\\(.*(DROP|ALTER|CREATE)']\n"
        "body = setting_value\n"
        "for pat in prohibited:\n"
        "    if re.search(pat, body, re.IGNORECASE):\n"
        "        result = f'[ERROR] Prohibited pattern detected: {pat}'\n"
        "        break\n"
        "else:\n"
        "    result = 'OK'\n"
    ),
    "python"
),

(
    "audit_function_change",
    "Write a one‑line audit entry to the generic `logs` table whenever a function is inserted/updated. Requires `setting_name` (func name) and `setting_value` (action description).",
    (
        "import sqlite3, datetime\n"
        "now = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')\n"
        "conn = sqlite3.connect('database.db')\n"
        "conn.execute('INSERT INTO logs (log_date, log_code, log_text) VALUES (?, ?, ?)', (now, 0, f'Function {setting_name}: {setting_value}'))\n"
        "conn.commit()\n"
        "conn.close()\n"
        "result = 'Audit entry recorded.'\n"
    ),
    "python"
),
```

</details>

---

## How to integrate the suggestions

1. **Append the new tuples** to the `SEED_FUNCTIONS` list in `db_functions.py`.  
2. Re‑run `python3 db_seed.py` (or simply restart the agent – the seeding routine is idempotent).  
3. The new functions will automatically appear in `!functions` output and become callable via `CALL:` syntax.  

### Quick sanity‑check checklist

- **Safety first** – add `validate_function_body` to the `upsert_function` flow (e.g., call it before executing the INSERT/UPDATE and abort on error).  
- **Harness awareness** – you may want to extend `_execute_function` later to *hard‑block* any of the “dangerous” helpers (`delete_function`, `disable_prompt`, etc.) unless a corresponding harness explicitly allows it.  
- **Testing** – spin up a fresh SQLite file, run `!functions` and try a few calls (`enable_boolean setting_name=DEBUG_LOGGING`, `list_models`, `list_staggers`). Ensure they return clean, human‑readable strings without raising exceptions.  

---

### TL;DR – Core missing categories

| Category | Representative missing functions |
|----------|-----------------------------------|
| Boolean‑setting CRUD | `enable_boolean`, `disable_boolean`, `delete_boolean`, `rename_boolean` |
| Value‑setting CRUD & utils | `delete_value`, `append_value`, `increment_value`, `reset_value` |
| Prompt‑management | `enable_prompt`, `disable_prompt`, `delete_prompt`, `rename_prompt`, `get_prompt_body` |
| Function‑roster management | `disable_function`, `enable_function`, `delete_function`, `count_functions`, `search_functions` |
| Harness (constraints) CRUD | `list_harnesses`, `enable_harness`, `disable_harness`, `add_harness`, `delete_harness` |
| Model profile utilities | `list_models`, `current_model`, `model_info`, `set_endpoint` |
| Stagger scheduler introspection | `list_staggers`, `cancel_stagger` |
| General audit / diagnostics | `get_agent_log`, `clear_agent_log`, `dump_runtime_state` |
| Safety / validation | `validate_function_body`, `audit_function_change` |

Adding these functions will give the agent **full lifecycle control** over its own configuration, prompt roster, function roster, model selection, scheduling, and auditability—all without having to touch code again. This is exactly the “rapid‑deployment, self‑modifying template” you described. 🚀
