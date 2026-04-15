# =============================================================================
# db.py
# SQLite data access layer. Pure database reads — no business logic.
# Each public function handles exactly one responsibility.
#
# Also owns the in-memory resolution helpers that were previously scattered
# across index.py. Anything that touches rows or interprets their values
# belongs here. index.py never touches sqlite3 directly.
# =============================================================================

import sqlite3
import sys

# -----------------------------------------------------------------------------
# INTERNAL HELPER
# -----------------------------------------------------------------------------

def _connect(db_path):
    """Open a WAL-mode connection and return rows as dicts."""
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as e:
        print(f"[db] FATAL — cannot connect to '{db_path}': {e}", file=sys.stderr)
        sys.exit(1)

# -----------------------------------------------------------------------------
# RAW TABLE FETCHERS
# One function per table. Returns a list of dicts.
# -----------------------------------------------------------------------------

def fetch_all_settings(db_path):
    """
    Return all rows from settings_boolean as a list of dicts.
    Stores binary switches only — 0 or 1 values.
    Keys: id, setting_name, setting_bool
    """
    conn = _connect(db_path)
    rows = conn.execute("SELECT * FROM settings_boolean").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def fetch_all_values(db_path):
    """
    Return all rows from settings_values as a list of dicts.
    Stores string values, intervals, ranges, and paths.
    Keys: id, setting_name, setting_value
    """
    conn = _connect(db_path)
    rows = conn.execute("SELECT * FROM settings_values").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def fetch_all_prompts(db_path):
    """
    Return all rows from agent_prompts as a list of dicts.
    Keys: id, prompt_name, prompt_body, prompt_enabled
    """
    conn = _connect(db_path)
    rows = conn.execute("SELECT * FROM agent_prompts").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def fetch_all_functions(db_path):
    """
    Return all rows from functions as a list of dicts.
    Includes function_body for runtime execution.
    Keys: id, function_name, function_description, function_body,
          function_language, function_created, function_modified, function_enabled
    """
    conn = _connect(db_path)
    rows = conn.execute("SELECT * FROM functions").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def fetch_function_by_name(db_path, function_name):
    """
    Retrieve a single function row by exact function_name.
    Returns a dict or None if not found.
    Used at execution time when the agent calls a specific function.
    """
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT * FROM functions WHERE function_name = ? AND function_enabled = 1",
        (function_name,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None

# -----------------------------------------------------------------------------
# BULK LOADER
# Called once at boot and again on any prompt-reload cycle.
# Returns all four operational arrays in a single call.
# -----------------------------------------------------------------------------

def load_all_tables(db_path):
    """
    Load all four operational tables into memory in one pass.
    Returns: (settings, values, prompts, functions) as lists of dicts.

    settings  — settings_boolean  — binary switches (0 or 1)
    values    — settings_values   — string values, endpoints, paths, ranges
    prompts   — agent_prompts     — system prompt bodies
    functions — functions         — callable agent function roster
    """
    settings  = fetch_all_settings(db_path)
    values    = fetch_all_values(db_path)
    prompts   = fetch_all_prompts(db_path)
    functions = fetch_all_functions(db_path)
    return settings, values, prompts, functions

# -----------------------------------------------------------------------------
# IN-MEMORY RESOLVERS
# Interpret loaded arrays — no further DB access required.
# -----------------------------------------------------------------------------

def resolve_setting(settings, name, fallback=0):
    """
    Pull a boolean setting value from the loaded settings array by name.
    Returns fallback (default 0) if the entry is not found.
    """
    for row in settings:
        if row["setting_name"] == name:
            return row["setting_bool"]
    return fallback


def resolve_value(values, name, fallback=""):
    """
    Pull a string value from the loaded values array by name.
    Returns fallback if the entry is not found.
    Callers are responsible for casting to int/float where needed.
    """
    for row in values:
        if row["setting_name"] == name:
            return row["setting_value"]
    return fallback


def resolve_prompt(prompts, prompt_name):
    """
    Retrieve the prompt_body for the given prompt_name from the prompts array.
    Exits fatally if the named prompt is not found or is disabled.
    A missing prompt is a broken chain of command — no silent fallback.
    """
    for row in prompts:
        if row["prompt_name"].upper() == prompt_name.upper() and row["prompt_enabled"]:
            return row["prompt_body"]

    print(f"[db] FATAL — prompt '{prompt_name}' not found or disabled.", file=sys.stderr)
    sys.exit(1)

# -----------------------------------------------------------------------------
# PROMPT ASSEMBLER
# Combines the base prompt body with the enabled function digest.
# Owned here because it interprets DB-sourced data into a deployable string.
# -----------------------------------------------------------------------------

def assemble_system_prompt(base_prompt, functions):
    """
    Assemble the full system prompt:
      Part 1 — base prompt body (standing orders from agent_prompts)
      Part 2 — sequential digest of enabled function names + descriptions

    Called at boot and again after any prompt-reload cycle.
    """
    lines = [base_prompt, ""]
    enabled = [f for f in functions if f["function_enabled"]]
    for fn in enabled:
        lines.append(f"- {fn['function_name']}: {fn['function_description']}")
    return "\n".join(lines)
