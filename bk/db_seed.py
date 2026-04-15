# =============================================================================
# db_seed.py
# Responsible for: creating the SQLite database, enforcing table schemas,
# and seeding default data on first boot.
#
# Called by index.py when database.db is not found on disk.
# Can also be run standalone to reset/repopulate the database.
#
# USAGE:
#   python db_seed.py
#
# =============================================================================
# MODEL PROMPT FORMAT REFERENCE
# =============================================================================
#
# GEMMA (google_gemma-3-4b-it-q4_k_s.gguf and Gemma family)  ← DEFAULT
#   PROMPT_FORMAT  = "gemma"
#   ACTIVE_MODEL   = "GEMMA"
#   DEFAULT_PROMPT = "GEMMA_DEFAULT"
#   Thinking mode toggled by THINKING_MODE=1 — prepends <|think|> to the
#   system prompt. Gemma reasons inside <|channel>thought...<channel|> blocks.
#   index.py parses these as scratchpad output when THINKING_MODE=1.
#   Anti-prompts: ANTI_PROMPTS_GEMMA  (e.g. <end_of_turn>, <eos>)
#   VRAM: ~4GB
#
# HERMES (Hermes-3-Llama-3.1-8B.Q6_K.gguf — ChatML format)
#   PROMPT_FORMAT  = "chatml"
#   ACTIVE_MODEL   = "HERMES"
#   DEFAULT_PROMPT = "DEFAULT"
#   ChatML wrapping:
#     <|im_start|>system ... <|im_end|>
#     <|im_start|>user   ... <|im_end|>
#     <|im_start|>assistant
#   Reasoning uses <SCRATCHPAD> blocks parsed by index.py.
#   Anti-prompts: ANTI_PROMPTS  (e.g. User:, <|im_end|>)
#   VRAM: ~6GB
#
# To switch models at runtime, call switch_model from the agent:
#   CALL: switch_model setting_value=HERMES
#   CALL: switch_model setting_value=GEMMA
#
# =============================================================================

import sqlite3
import datetime
import sys
from db_functions import SEED_FUNCTIONS

# -----------------------------------------------------------------------------
# CONSTANTS
# -----------------------------------------------------------------------------

DB_PATH = "database.db"

# -----------------------------------------------------------------------------
# SCHEMA DEFINITIONS
# Each tuple: (table_name, CREATE TABLE SQL)
# -----------------------------------------------------------------------------

SCHEMA = [
    (
        "settings_boolean",
        """
        CREATE TABLE IF NOT EXISTS settings_boolean (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            setting_name TEXT    UNIQUE NOT NULL,
            setting_bool INTEGER DEFAULT 1
        )
        """
    ),
    (
        "settings_values",
        """
        CREATE TABLE IF NOT EXISTS settings_values (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            setting_name  TEXT    UNIQUE NOT NULL,
            setting_value TEXT    NOT NULL DEFAULT ''
        )
        """
    ),
    (
        "agent_prompts",
        """
        CREATE TABLE IF NOT EXISTS agent_prompts (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            prompt_name    TEXT    NOT NULL,
            prompt_body    TEXT    NOT NULL,
            prompt_enabled INTEGER DEFAULT 1
        )
        """
    ),
    (
        "functions",
        """
        CREATE TABLE IF NOT EXISTS functions (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            function_name        TEXT    NOT NULL,
            function_description TEXT    NOT NULL,
            function_body        TEXT    NOT NULL,
            function_language    TEXT    NOT NULL DEFAULT 'python',
            function_created     DATETIME NOT NULL,
            function_modified    DATETIME NOT NULL,
            function_enabled     INTEGER  DEFAULT 1
        )
        """
    ),
]

# -----------------------------------------------------------------------------
# SEED DATA
# -----------------------------------------------------------------------------

SEED_SETTINGS = [
    # (setting_name, setting_bool)
    ("INTERACTIVE_MODE", 1),   # 0 = stateless/daemon, 1 = interactive readline
    ("DEBUG_LOGGING",    0),   # reserved for future verbose output toggle
    ("PROMPT_RELOAD",    0),   # trip wire — agent sets to 1 to trigger hot-swap
                               # index.py resets to 0 after reload is complete
    ("THINKING_MODE",    1),   # 1 = prepend <|think|> and parse Gemma thought blocks
                               # 0 = standard mode (no thinking token, no thought parsing)
                               # Only active when PROMPT_FORMAT = "gemma"
]

# Each entry: (setting_name, setting_value)
# The AI may mutate these at runtime via set_value or the switch_model function.
#
# ACTIVE_MODEL: human-readable label for the loaded model.
#   "GEMMA"  — Gemma 3/4 instruction-tuned (default, ~4GB VRAM)
#   "HERMES" — Hermes-3 ChatML model (~6GB VRAM)
#   Used by index.py to select prompt format and anti-prompt list.
#
# PROMPT_FORMAT: controls how index.py wraps the prompt before sending to Kobold.
#   "gemma"  — Gemma format (see model reference block above)
#   "chatml" — ChatML format used by Hermes and compatible models
#
# DEFAULT_PROMPT: resolves to a prompt_name in agent_prompts.
#   The agent can switch this via switch_model or set_value + PROMPT_RELOAD.
#
# ANTI_PROMPTS: stop sequences for ChatML / Hermes models (comma-delimited).
# ANTI_PROMPTS_GEMMA: stop sequences for Gemma models (comma-delimited).
#   index.py selects the correct list based on PROMPT_FORMAT at call time.
#
# KOBOLD_MAX_TOKENS: stored as string — cast to int at load.
# KOBOLD_TEMPERATURE, KOBOLD_TOP_P: stored as string — cast to float at load.
SEED_VALUES = [
    ("ACTIVE_MODEL",       "GEMMA"),
    ("PROMPT_FORMAT",      "gemma"),
    ("DEFAULT_PROMPT",     "GEMMA_DEFAULT"),
    ("ENDPOINT_KOBOLD",    "http://localhost:5001/api/v1/generate"),
    ("ENDPOINT_OLLAMA",    "http://localhost:11434/api/generate"),
    ("ANTI_PROMPTS",       "User:,<|im_end|>,\n\n\n"),
    ("ANTI_PROMPTS_GEMMA", "<end_of_turn>,<eos>,\n\n\n"),
    ("KOBOLD_MAX_TOKENS",  "512"),
    ("KOBOLD_TEMPERATURE", "0.1"),
    ("KOBOLD_TOP_P",       "0.9"),
]

SEED_PROMPTS = [
    # (prompt_name, prompt_body, prompt_enabled)

    # -------------------------------------------------------------------------
    # GEMMA_DEFAULT — primary prompt for Gemma 3/4 instruction-tuned models.
    # Thinking mode (<|think|>) is prepended by index.py when THINKING_MODE=1.
    # Gemma reasons inside <|channel>thought...<channel|> blocks — index.py
    # parses and displays these as scratchpad output.
    # No <SCRATCHPAD> instruction here — Gemma's thinking mode handles reasoning
    # natively via its own internal channel mechanism.
    # -------------------------------------------------------------------------
    (
        "GEMMA_DEFAULT",
        (
            "You are a disciplined AI agent operating in a structured execution environment.\n"
            "You have access to a roster of callable functions listed below.\n\n"

            "════════════════════════════════════════\n"
            "CALL SYNTAX — THE ONLY VALID FORMAT\n"
            "════════════════════════════════════════\n"
            "To invoke a function you MUST emit EXACTLY this on its own line:\n"
            "  CALL: function_name\n\n"
            "Some functions accept parameters on the same line:\n"
            "  CALL: calculate expr=<python_expression>\n"
            "  CALL: set_setting setting_name=<n> setting_value=<0_or_1>\n"
            "  CALL: set_value setting_name=<n> setting_value=<value>\n\n"
            "FORBIDDEN — these formats will BREAK the system, NEVER use them:\n"
            "  <tool_call>anything</tool_call>   ← FORBIDDEN\n"
            "  Any XML or HTML tag as a function call   ← FORBIDDEN\n\n"

            "EXECUTION PROTOCOL:\n"
            "The system will execute the function and return the real output as:\n"
            "  RESULT: <o>\n\n"
            "Chain as many CALL/RESULT cycles as the task requires.\n"
            "Never invent or simulate a RESULT. Always wait for the system to supply it.\n\n"

            "FINAL ANSWER — when all calls are complete emit:\n"
            "  FINAL: your answer here\n\n"

            "RULES:\n"
            "1. One CALL per response — never stack multiple CALLs in one reply.\n"
            "2. FINAL: must appear alone on its line, never alongside a CALL.\n"
            "3. Do not repeat information already confirmed by a RESULT.\n"
            "4. Use every RESULT VERBATIM — never substitute values from memory.\n"
            "5. CALL: is the ONLY way to invoke functions.\n\n"

            "EXAMPLES:\n"
            "User: what time is it?\n"
            "CALL: get_current_datetime\n"
            "RESULT: 2026-04-06 10:25:31\n"
            "FINAL: The current time is 10:25:31.\n\n"

            "User: what is 12 * 12?\n"
            "CALL: calculate expr=12 * 12\n"
            "RESULT: 144\n"
            "FINAL: 12 * 12 = 144.\n\n"

            "User: set the temperature to 0.5\n"
            "CALL: set_value setting_name=KOBOLD_TEMPERATURE setting_value=0.5\n"
            "RESULT: KOBOLD_TEMPERATURE set to 0.5.\n"
            "FINAL: Kobold temperature has been updated to 0.5.\n\n"

            "Available functions:"
        ),
        1
    ),

    # -------------------------------------------------------------------------
    # DEFAULT — legacy prompt for Hermes 3 and other ChatML models.
    # Hermes uses <|im_start|> / <|im_end|> wrapping (ChatML format).
    # Reasoning uses <SCRATCHPAD> blocks which index.py parses and displays.
    # Model file: Hermes-3-Llama-3.1-8B.Q6_K.gguf (~6GB VRAM)
    # -------------------------------------------------------------------------
    (
        "DEFAULT",
        (
            "You are a disciplined AI agent operating in a structured execution environment.\n"
            "You have access to a roster of callable functions listed below.\n\n"

            "════════════════════════════════════════\n"
            "CALL SYNTAX — THE ONLY VALID FORMAT\n"
            "════════════════════════════════════════\n"
            "To invoke a function you MUST emit EXACTLY this on its own line:\n"
            "  CALL: function_name\n\n"
            "Some functions accept parameters on the same line:\n"
            "  CALL: calculate expr=<python_expression>\n"
            "  CALL: set_setting setting_name=<n> setting_value=<0_or_1>\n"
            "  CALL: set_value setting_name=<n> setting_value=<value>\n\n"
            "FORBIDDEN — these formats will BREAK the system, NEVER use them:\n"
            "  <tool_call>anything</tool_call>   ← FORBIDDEN\n"
            "  <function_call>anything</function_call>   ← FORBIDDEN\n"
            "  Any XML tag whatsoever   ← FORBIDDEN\n\n"

            "REASONING PROTOCOL:\n"
            "Think through every task inside a <SCRATCHPAD> block before acting.\n"
            "Use the scratchpad to plan which functions are needed and in what order.\n\n"
            "The system will execute the function and inject the real output as:\n"
            "  RESULT: <o>\n\n"
            "You may then reason further in another <SCRATCHPAD> block before the next CALL.\n"
            "Chain as many CALL/RESULT cycles as the task requires.\n"
            "Never invent or simulate a RESULT. Always wait for the system to supply it.\n\n"
            "FINAL ANSWER — when all calls are complete emit:\n"
            "  FINAL: your answer here\n\n"
            "RULES:\n"
            "1. Always open with a <SCRATCHPAD> block.\n"
            "2. One CALL per response — never stack multiple CALLs in one reply.\n"
            "3. FINAL: must appear alone on its line, never alongside a CALL.\n"
            "4. Do not repeat information already confirmed by a RESULT.\n"
            "5. Use every RESULT VERBATIM — never substitute values from memory or training data.\n"
            "6. CALL: is the ONLY way to invoke functions. XML tags are FORBIDDEN.\n\n"

            "EXAMPLES:\n"
            "User: enable debug mode\n"
            "<SCRATCHPAD>I need to set DEBUG_LOGGING to 1. I will call enable_debug_mode.</SCRATCHPAD>\n"
            "CALL: enable_debug_mode\n"
            "RESULT: DEBUG_LOGGING set to 1 (debug mode enabled).\n"
            "FINAL: Debug mode has been enabled.\n\n"

            "User: what is 6 * 7?\n"
            "<SCRATCHPAD>I will use calculate to evaluate the expression.</SCRATCHPAD>\n"
            "CALL: calculate expr=6 * 7\n"
            "RESULT: 42\n"
            "FINAL: 6 * 7 = 42.\n\n"

            "User: what time is it?\n"
            "<SCRATCHPAD>I need the current time. I will call get_current_datetime.</SCRATCHPAD>\n"
            "CALL: get_current_datetime\n"
            "RESULT: 2026-04-05 19:13:17\n"
            "FINAL: The current time is 19:13:17.\n\n"

            "WRONG — never do this:\n"
            "User: what time is it?\n"
            "<tool_call>get_current_datetime</tool_call>   ← WRONG, XML tags are FORBIDDEN\n"
            "The correct form is always: CALL: get_current_datetime\n\n"

            "Available functions:"
        ),
        1
    ),
]


# -----------------------------------------------------------------------------
# INTERNAL HELPERS
# -----------------------------------------------------------------------------

def _now():
    """Return current UTC datetime as ISO string."""
    return datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _enforce_schema(cursor):
    """Create all tables if they do not already exist."""
    for table_name, ddl in SCHEMA:
        cursor.execute(ddl)
    print(f"  [schema]   Tables verified: {[t for t, _ in SCHEMA]}")


def _seed_settings(cursor):
    """Insert default settings_boolean rows, skip if setting_name already exists."""
    inserted = 0
    for name, val in SEED_SETTINGS:
        cursor.execute(
            "INSERT OR IGNORE INTO settings_boolean (setting_name, setting_bool) VALUES (?, ?)",
            (name, val)
        )
        if cursor.rowcount > 0:
            inserted += 1
    print(f"  [settings] Seeded {inserted}/{len(SEED_SETTINGS)} boolean setting(s).")


def _seed_values(cursor):
    """Insert default settings_values rows, skip if setting_name already exists."""
    inserted = 0
    for name, val in SEED_VALUES:
        cursor.execute(
            "INSERT OR IGNORE INTO settings_values (setting_name, setting_value) VALUES (?, ?)",
            (name, val)
        )
        if cursor.rowcount > 0:
            inserted += 1
    print(f"  [values]   Seeded {inserted}/{len(SEED_VALUES)} value setting(s).")


def _seed_prompts(cursor):
    """Insert default agent_prompts rows if prompt_name does not already exist."""
    inserted = 0
    for name, body, enabled in SEED_PROMPTS:
        existing = cursor.execute(
            "SELECT id FROM agent_prompts WHERE prompt_name = ?", (name,)
        ).fetchone()
        if not existing:
            cursor.execute(
                "INSERT INTO agent_prompts (prompt_name, prompt_body, prompt_enabled) VALUES (?, ?, ?)",
                (name, body, enabled)
            )
            inserted += 1
    print(f"  [prompts]  Seeded {inserted}/{len(SEED_PROMPTS)} prompt(s).")


def _seed_functions(cursor):
    """Insert default function rows if function_name does not already exist."""
    inserted = 0
    now = _now()
    for name, description, body, language in SEED_FUNCTIONS:
        existing = cursor.execute(
            "SELECT id FROM functions WHERE function_name = ?", (name,)
        ).fetchone()
        if not existing:
            cursor.execute(
                """
                INSERT INTO functions
                    (function_name, function_description, function_body,
                     function_language, function_created, function_modified, function_enabled)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                """,
                (name, description, body, language, now, now)
            )
            inserted += 1
    print(f"  [functions] Seeded {inserted}/{len(SEED_FUNCTIONS)} function(s).")


# -----------------------------------------------------------------------------
# PUBLIC ENTRY POINT
# -----------------------------------------------------------------------------

def run(db_path=DB_PATH):
    """
    Main seed routine. Safe to call repeatedly — all inserts are idempotent.
    Creates database.db if it does not exist, enforces schema, seeds defaults.
    """
    print(f"[db_seed] Initializing database: {db_path}")
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        cursor = conn.cursor()

        _enforce_schema(cursor)
        _seed_settings(cursor)
        _seed_values(cursor)
        _seed_prompts(cursor)
        _seed_functions(cursor)

        conn.commit()
        conn.close()
        print(f"[db_seed] Done. Database ready at: {db_path}\n")

    except sqlite3.Error as e:
        print(f"[db_seed] FATAL — SQLite error: {e}", file=sys.stderr)
        sys.exit(1)


# Allow standalone execution
if __name__ == "__main__":
    run()
