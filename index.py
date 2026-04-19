# =============================================================================
# index.py
# Root command file. Responsible for:
#   - Defining static deployment constants (DB_PATH, MCP_PORT only)
#   - Bootstrapping the database via db_seed.py if absent
#   - Loading all runtime tables and assembling the system prompt
#   - Resolving the active model profile and anti-prompts
#   - Delegating execution to agent.py
#
# USAGE:
#   python3 index.py
#
# RUNTIME TUPLE SHAPE (7 elements):
#   (settings, values, prompts, functions, profiles, project_files, system_prompt)
#
#   settings      — settings_boolean rows  (binary switches)
#   values        — settings_values rows   (endpoints, paths, ranges)
#   prompts       — agent_prompts rows     (system prompt bodies)
#   functions     — functions rows         (callable agent roster)
#   profiles      — model_profiles rows    (per-architecture anti-prompts + format)
#   project_files — project_files rows     (source files for context injection)
#   system_prompt — assembled string       (base prompt + function digest)
#
# The active model profile and its anti-prompts are resolved from the profiles
# array at boot and on every reload. agent.py reads them via:
#   db.resolve_active_profile(profiles, active_model)
#   db.resolve_anti_prompts(profile)
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

# Resolve base directory so db.resolve_project_files() can locate files
# relative to this file's location even when cwd differs.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

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
    Load all tables and assemble the full runtime state.

    Returns a 7-tuple:
      (settings, values, prompts, functions, profiles, project_files, system_prompt)

    Called once at boot. Passed as a callable into agent.py so the prompt
    reload trip-wire and !reload command can rebuild state without creating
    a circular import.

    Profile resolution is performed here so agent.py can read the active
    anti-prompts and prompt_format directly from the profiles array without
    touching the database again.
    """
    settings, values, prompts, functions, profiles, project_files = db.load_all_tables(DB_PATH)

    # Resolve active model profile — fatal if ACTIVE_MODEL has no matching row
    active_model   = db.resolve_value(values, "ACTIVE_MODEL", fallback="GEMMA")
    active_profile = db.resolve_active_profile(profiles, active_model)
    anti_prompts   = db.resolve_anti_prompts(active_profile)

    # Resolve project files — three-pass path search (seeded path → base_dir → cwd)
    resolved_files = db.resolve_project_files(project_files, base_dir=BASE_DIR)

    # Assemble system prompt from active prompt body + enabled function digest
    prompt_name   = db.resolve_value(values, "DEFAULT_PROMPT", fallback="DEFAULT")
    base_prompt   = db.resolve_prompt(prompts, prompt_name)
    system_prompt = db.assemble_system_prompt(base_prompt, functions)

    print(
        f"[boot] Model: {active_model} | "
        f"Format: {active_profile['prompt_format']} | "
        f"Thinking: {active_profile['thinking_mode']} | "
        f"Anti-prompts: {anti_prompts}"
    )

    return settings, values, prompts, functions, profiles, resolved_files, system_prompt


# -----------------------------------------------------------------------------
# MAIN
# -----------------------------------------------------------------------------

def main():
    # 1. Ensure database exists and is seeded
    _boot_database()

    # 2. Load all tables, resolve active profile, assemble system prompt
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
