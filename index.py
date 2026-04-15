# =============================================================================
# index.py
# Root command file. Responsible for:
#   - Defining static deployment constants (DB_PATH, MCP_PORT only)
#   - Bootstrapping the database via db_seed.py if absent
#   - Loading all runtime tables and assembling the system prompt
#   - Delegating execution to agent.py
#
# USAGE:
#   python3 index.py
# =============================================================================

import os
import sys
import readline  # noqa: F401 — activates readline editing in interactive mode

import db
import db_seed
import agent

# -----------------------------------------------------------------------------
# CONSTANTS
# Static deployment anchors only. DB_PATH and MCP_PORT cannot live in the
# database — they are needed before the DB is open. Everything else lives
# in settings_values and is loaded at runtime.
# -----------------------------------------------------------------------------

DB_PATH  = "database.db"
MCP_PORT = "8206"

# Inject DB_PATH into agent so trip-wire reset can reach the database.
agent.DB_PATH = DB_PATH

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

    Called once at boot. Passed as a callable into agent.py so the
    prompt reload trip-wire and !reload command can rebuild state without
    creating a circular import.
    """
    settings, values, prompts, functions = db.load_all_tables(DB_PATH)
    prompt_name   = db.resolve_value(values, "DEFAULT_PROMPT", fallback="DEFAULT")
    base_prompt   = db.resolve_prompt(prompts, prompt_name)
    system_prompt = db.assemble_system_prompt(base_prompt, functions)
    return settings, values, prompts, functions, system_prompt

# -----------------------------------------------------------------------------
# MAIN
# -----------------------------------------------------------------------------

def main():
    # 1. Ensure database exists and is seeded
    _boot_database()

    # 2. Load all tables, resolve active prompt, assemble system prompt
    runtime  = _build_runtime_state()
    settings = runtime[0]

    # 3. Evaluate deployment mode from settings
    interactive_mode = db.resolve_setting(settings, "INTERACTIVE_MODE", fallback=0)

    # 4. Enter appropriate loop — pass _build_runtime_state so agent.py can
    #    reload state on demand without importing index.py (circular import).
    if interactive_mode:
        agent.loop_interactive(runtime, _build_runtime_state)
    else:
        agent.loop_stateless(runtime, _build_runtime_state)


if __name__ == "__main__":
    main()
