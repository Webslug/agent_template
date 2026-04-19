# =============================================================================
# db.py
# SQLite data access layer. Pure database reads — no business logic.
# Each public function handles exactly one responsibility.
#
# Also owns the in-memory resolution helpers that were previously scattered
# across index.py. Anything that touches rows or interprets their values
# belongs here. index.py never touches sqlite3 directly.
#
# TABLE INVENTORY
# ───────────────────────────────────────────────────────────────────────────
# settings_boolean — binary switches (0 or 1)
# settings_values  — string values, endpoints, paths, ranges
# agent_prompts    — system prompt bodies, swappable at runtime
# functions        — callable agent function roster
# model_profiles   — per-architecture anti-prompts, format, thinking mode
# project_files    — source files registered for context injection
# agent_bash_logs  — audit trail (no fetcher needed — write-only from agent)
# =============================================================================

import os
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


def fetch_all_model_profiles(db_path):
    """
    Return all rows from model_profiles as a list of dicts.
    Each row is the full operational profile for one model architecture.
    Keys: id, profile_name, prompt_format, anti_prompts,
          thinking_mode, endpoint_key, notes
    """
    conn = _connect(db_path)
    rows = conn.execute("SELECT * FROM model_profiles").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def fetch_all_project_files(db_path):
    """
    Return all rows from project_files as a list of dicts.
    Paths are resolved at load time — seeded paths are absolute but the
    resolver will search the runtime directory if a file is missing there.
    Keys: id, file_path, file_project
    """
    conn = _connect(db_path)
    rows = conn.execute("SELECT * FROM project_files").fetchall()
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
# Returns all six operational arrays in a single call.
# -----------------------------------------------------------------------------

def load_all_tables(db_path):
    """
    Load all six operational tables into memory in one pass.
    Returns: (settings, values, prompts, functions, profiles, project_files)
             as lists of dicts.

    settings      — settings_boolean — binary switches (0 or 1)
    values        — settings_values  — string values, endpoints, paths, ranges
    prompts       — agent_prompts    — system prompt bodies
    functions     — functions        — callable agent function roster
    profiles      — model_profiles   — per-architecture anti-prompts and format
    project_files — project_files    — registered source files for context use
    """
    settings      = fetch_all_settings(db_path)
    values        = fetch_all_values(db_path)
    prompts       = fetch_all_prompts(db_path)
    functions     = fetch_all_functions(db_path)
    profiles      = fetch_all_model_profiles(db_path)
    project_files = fetch_all_project_files(db_path)
    return settings, values, prompts, functions, profiles, project_files

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


def resolve_active_profile(profiles, active_model):
    """
    Locate and return the model_profiles row whose profile_name matches
    active_model (case-insensitive). Returns the dict on success.

    Exits fatally if the profile is missing — an unrecognised model label
    is a misconfiguration, not a recoverable runtime condition.
    The profile dict is the single source of truth for:
      - anti_prompts  (comma-delimited stop sequences)
      - prompt_format (gemma | chatml | llama3 | mistral | phi3)
      - thinking_mode (0 or 1)
      - endpoint_key  (pointer into settings_values)
    """
    for row in profiles:
        if row["profile_name"].upper() == active_model.upper():
            return row

    known = [r["profile_name"] for r in profiles]
    print(
        f"[db] FATAL — no model profile found for '{active_model}'. "
        f"Known profiles: {known}",
        file=sys.stderr
    )
    sys.exit(1)


def resolve_anti_prompts(profile):
    """
    Parse the anti_prompts field of a resolved profile dict into a clean list.

    The stored value is comma-delimited. The literal token '\\n\\n\\n' is
    normalised to a real triple-newline so Kobold receives the correct bytes.
    Empty tokens produced by trailing commas are filtered out.

    Returns a list of strings ready to pass directly to the Kobold payload.
    """
    raw = profile.get("anti_prompts", "")
    tokens = [t.replace("\\n", "\n") for t in raw.split(",") if t.strip()]
    return tokens


def resolve_project_files(project_files, base_dir=None):
    """
    Resolve the registered project_files rows into a list of dicts, each
    augmented with a 'resolved_path' key containing the best available
    filesystem path for that file.

    Resolution order for each entry:
      1. The seeded file_path as-is — used if the file exists there.
      2. The bare filename joined to base_dir — handles deployments where
         the project has been moved to a different root directory.
      3. The bare filename joined to the current working directory.
      4. Unresolvable — resolved_path is set to None and a warning is printed.

    base_dir — typically os.path.dirname(os.path.abspath(__file__)) from
               index.py, passed in so this module stays import-free of index.
    """
    resolved = []
    for row in project_files:
        entry        = dict(row)
        seeded_path  = row["file_path"]
        filename     = os.path.basename(seeded_path)
        found        = None

        # Pass 1 — seeded absolute path
        if os.path.isfile(seeded_path):
            found = seeded_path

        # Pass 2 — filename relative to the caller's directory
        if found is None and base_dir:
            candidate = os.path.join(base_dir, filename)
            if os.path.isfile(candidate):
                found = candidate

        # Pass 3 — filename relative to cwd
        if found is None:
            candidate = os.path.join(os.getcwd(), filename)
            if os.path.isfile(candidate):
                found = candidate

        if found is None:
            print(
                f"[db] WARNING — project file not found: '{seeded_path}' "
                f"(also tried '{filename}' relative to runtime dir)",
                file=sys.stderr
            )

        entry["resolved_path"] = found
        resolved.append(entry)

    return resolved

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
