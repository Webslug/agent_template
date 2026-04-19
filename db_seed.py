# =============================================================================
# db_seed.py
# Responsible for: creating the SQLite database, enforcing table schemas,
# and seeding default data on first boot.
#
# Called by index.py when database.db is not found on disk.
# Can also be run standalone to reset/repopulate the database.
#
# USAGE:
#   python3 db_seed.py
#
# =============================================================================
# MODEL REFERENCE
# =============================================================================
#
# GEMMA  (google_gemma-3-4b-it-q4_k_s.gguf)   ~4GB VRAM
#   prompt_format : gemma
#   thinking_mode : 1  (<|think|> prepended; thought blocks parsed by agent.py)
#   turn template :
#     <start_of_turn>user\n[system + conversation]<end_of_turn>\n
#     <start_of_turn>model\n
#
# QWEN   (Qwen3.5-9B.Q8_0.gguf)                ~8GB VRAM
#   prompt_format : chatml
#   thinking_mode : 1  (<think>...</think> native blocks)
#   turn template : ChatML  <|im_start|> / <|im_end|>
#
# HERMES (Hermes-3-Llama-3.1-8B.Q6_K.gguf)    ~6GB VRAM
#   prompt_format : chatml
#   thinking_mode : 0
#   turn template : ChatML  <|im_start|> / <|im_end|>
#
# LLAMA3 (Meta-Llama-3-8B-Instruct variants)   ~5–8GB VRAM
#   prompt_format : llama3
#   thinking_mode : 0
#   turn template : <|begin_of_text|><|start_header_id|>...<|eot_id|>
#
# MISTRAL (Mistral-7B-Instruct variants)        ~5–6GB VRAM
#   prompt_format : mistral
#   thinking_mode : 0
#   turn template : [INST] ... [/INST]
#
# PHI3   (Phi-3-mini / Phi-3.5-mini-instruct)  ~2–4GB VRAM
#   prompt_format : phi3
#   thinking_mode : 0
#   turn template : <|user|>\n...<|end|>\n<|assistant|>\n
#
# All profiles are stored in the model_profiles table.
# ACTIVE_MODEL in settings_values is the lookup key at runtime.
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
    (
        "agent_bash_logs",
        """
        CREATE TABLE IF NOT EXISTS agent_bash_logs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            command    TEXT    NOT NULL,
            stdout     TEXT    NOT NULL DEFAULT '',
            stderr     TEXT    NOT NULL DEFAULT '',
            exit_code  INTEGER NOT NULL DEFAULT -1,
            run_at     DATETIME NOT NULL
        )
        """
    ),
    (
        "model_profiles",
        """
        CREATE TABLE IF NOT EXISTS model_profiles (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_name   TEXT    UNIQUE NOT NULL,
            prompt_format  TEXT    NOT NULL,
            anti_prompts   TEXT    NOT NULL,
            thinking_mode  INTEGER NOT NULL DEFAULT 0,
            endpoint_key   TEXT    NOT NULL DEFAULT 'ENDPOINT_KOBOLD',
            notes          TEXT    NOT NULL DEFAULT ''
        )
        """
        # profile_name  — matches ACTIVE_MODEL in settings_values (e.g. "GEMMA")
        # prompt_format — token wrapper used by _build_prompt in agent.py
        #                 one of: gemma | chatml | llama3 | mistral | phi3
        # anti_prompts  — comma-delimited stop sequences fed to Kobold/Ollama
        # thinking_mode — 1 = parse and strip internal reasoning blocks
        # endpoint_key  — setting_name in settings_values that holds the URL
        # notes         — human-readable model file reference / VRAM hint
    ),
    (
        "project_files",
        """
        CREATE TABLE IF NOT EXISTS project_files (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path    TEXT    NOT NULL,
            file_project TEXT    NOT NULL DEFAULT 'project1'
        )
        """
        # file_path    — absolute path as seeded; db.resolve_project_files()
        #                also searches by bare filename relative to the runtime
        #                directory so the project survives being moved.
        # file_project — logical group label; enables multi-project deployments
        #                sharing one database without cross-contamination.
    ),
]

# -----------------------------------------------------------------------------
# SEED DATA
# -----------------------------------------------------------------------------

SEED_SETTINGS = [
    # (setting_name, setting_bool)
    ("INTERACTIVE_MODE", 1),   # 0 = stateless/daemon, 1 = interactive readline
    ("DEBUG_LOGGING",    0),   # reserved for future verbose output toggle
    ("PROMPT_RELOAD",    0),   # trip wire — agent sets to 1 to trigger hot-swap;
                               # agent.py resets to 0 after reload is complete
    ("THINKING_MODE",    1),   # 1 = prepend <|think|> and parse thought blocks
                               # 0 = standard mode (no thinking token or parsing)
]

# Each entry: (setting_name, setting_value)
# NOTE: ANTI_PROMPTS_* keys have been retired from this table.
#       All anti-prompt data now lives in model_profiles, keyed by profile_name.
#       The active profile is resolved at runtime via ACTIVE_MODEL.
SEED_VALUES = [
    ("ACTIVE_MODEL",       "GEMMA"),
    ("PROMPT_FORMAT",      "gemma"),
    ("DEFAULT_PROMPT",     "DEFAULT"),
    ("ENDPOINT_KOBOLD",    "http://localhost:5001/api/v1/generate"),
    ("ENDPOINT_OLLAMA",    "http://localhost:11434/api/generate"),
    ("KOBOLD_MAX_TOKENS",  "512"),
    ("KOBOLD_TEMPERATURE", "0.1"),
    ("KOBOLD_TOP_P",       "0.9"),
]

# Each entry:
#   (profile_name, prompt_format, anti_prompts, thinking_mode, endpoint_key, notes)
#
# anti_prompts — comma-delimited; agent.py splits on "," at load time.
#   Literal "\n\n\n" is stored as the three-char sequence; agent.py converts
#   it to a real newline sequence via anti_raw.replace("\\n", "\n") on load.
#
# Six architectures covered:
#   GEMMA   — Google Gemma 3/4 instruction-tuned
#   QWEN    — Alibaba Qwen3 / Qwen2.5 instruction-tuned
#   HERMES  — NousResearch Hermes-3 (Llama-3.1 base, ChatML fine-tune)
#   LLAMA3  — Meta Llama-3 / Llama-3.1 instruction-tuned
#   MISTRAL — Mistral-7B / Mixtral instruction-tuned
#   PHI3    — Microsoft Phi-3 / Phi-3.5 mini instruction-tuned
SEED_MODEL_PROFILES = [
    (
        "GEMMA",
        "gemma",
        "<end_of_turn>,<eos>,\n\n\n",
        1,
        "ENDPOINT_KOBOLD",
        "google_gemma-3-4b-it-q4_k_s.gguf | ~4GB VRAM | thinking blocks: <|channel>thought...<channel|>"
    ),
    (
        "QWEN",
        "chatml",
        "<|im_end|>,<|endoftext|>,<|end|>,\n\n\n",
        1,
        "ENDPOINT_KOBOLD",
        "Qwen3.5-9B.Q8_0.gguf | ~8GB VRAM | thinking blocks: <think>...</think>"
    ),
    (
        "HERMES",
        "chatml",
        "<|im_end|>,<|endoftext|>,\n\n\n",
        0,
        "ENDPOINT_KOBOLD",
        "Hermes-3-Llama-3.1-8B.Q6_K.gguf | ~6GB VRAM | ChatML format, no native thinking"
    ),
    (
        "LLAMA3",
        "llama3",
        "<|eot_id|>,<|end_of_text|>,<|start_header_id|>,\n\n\n",
        0,
        "ENDPOINT_KOBOLD",
        "Meta-Llama-3-8B-Instruct | ~5–8GB VRAM | header-token turn format"
    ),
    (
        "MISTRAL",
        "mistral",
        "[/INST],</s>,\n\n\n",
        0,
        "ENDPOINT_KOBOLD",
        "Mistral-7B-Instruct-v0.3 | ~5–6GB VRAM | [INST]/[/INST] turn markers"
    ),
    (
        "PHI3",
        "phi3",
        "<|end|>,<|endoftext|>,\n\n\n",
        0,
        "ENDPOINT_KOBOLD",
        "Phi-3-mini-128k-instruct | ~2–4GB VRAM | <|user|>/<|assistant|>/<|end|> turns"
    ),
]


# Each entry: (file_path, file_project)
#
# Paths are seeded as absolute. At runtime db.resolve_project_files() will
# also search for the bare filename relative to the directory index.py is
# running from — so the project survives being relocated to a new root.
# Add new source files here as the project grows.
SEED_PROJECT_FILES = [
    ("/home/kim/projects/template/index.py",        "project1"),
    ("/home/kim/projects/template/db.py",           "project1"),
    ("/home/kim/projects/template/db_seed.py",      "project1"),
    ("/home/kim/projects/template/db_functions.py", "project1"),
]

SEED_PROMPTS = [
    # (prompt_name, prompt_body, prompt_enabled)

    # -------------------------------------------------------------------------
    # DEFAULT — primary prompt for Gemma 3/4 instruction-tuned models.
    # Thinking mode (<|think|>) is prepended by agent.py when THINKING_MODE=1.
    # Gemma reasons inside <|channel>thought...<channel|> blocks; agent.py
    # parses and displays these as scratchpad output.
    #
    # RUNTIME CONTEXT HEADER
    # agent.py injects a live [Context: weekday date | time | user: X] line
    # at the top of every Kobold call. The stored prompt body below is
    # intentionally timeless — no static dates or usernames here.
    # -------------------------------------------------------------------------
    (
        "DEFAULT",
        (
            "You are a disciplined AI agent operating in a structured execution environment.\n"
            "You have access to a roster of callable functions listed below.\n"
            "The runtime context header above (injected by the system) tells you the current\n"
            "weekday, date, time, and logged-in user — use it directly for temporal questions\n"
            "without calling get_current_datetime unless sub-second precision is required.\n\n"

            "════════════════════════════════════════\n"
            "CALL SYNTAX — THE ONLY VALID FORMAT\n"
            "════════════════════════════════════════\n"
            "To invoke a function you MUST emit EXACTLY this on its own line:\n"
            "  CALL: function_name\n\n"
            "Some functions accept parameters on the same line:\n"
            "  CALL: calculate expr=<python_expression>\n"
            "  CALL: set_boolean setting_name=<name> setting_value=<0_or_1>\n"
            "  CALL: set_value setting_name=<name> setting_value=<value>\n"
            "  CALL: run_bash_command expr=<shell_command>\n"
            "  CALL: upsert_function setting_name=<fn_name> setting_value=<python_body>\n"
            "  CALL: add_prompt setting_name=<prompt_name> setting_value=<prompt_body>\n\n"
            "FORBIDDEN — these formats will BREAK the system, NEVER use them:\n"
            "  <tool_call>anything</tool_call>   ← FORBIDDEN\n"
            "  Any XML or HTML tag as a function call   ← FORBIDDEN\n\n"

            "EXECUTION PROTOCOL:\n"
            "The system will execute the function and return the real output as:\n"
            "  RESULT: <output>\n\n"
            "Chain as many CALL/RESULT cycles as the task requires.\n"
            "Never invent or simulate a RESULT. Always wait for the system to supply it.\n\n"

            "FINAL ANSWER — when all calls are complete emit:\n"
            "  FINAL: your answer here\n\n"

            "RULES:\n"
            "1. One CALL per response — never stack multiple CALLs in one reply.\n"
            "2. FINAL: must appear alone on its line, never alongside a CALL.\n"
            "3. Do not repeat information already confirmed by a RESULT.\n"
            "4. Use every RESULT VERBATIM — never substitute values from memory.\n"
            "5. CALL: is the ONLY way to invoke functions.\n"
            "6. For date/time/weekday questions, read the context header first before calling any function.\n"
            "7. Once a RESULT has been received for a given CALL, never issue that exact CALL again — proceed to FINAL.\n\n"

            "════════════════════════════════════════\n"
            "AUTONOMY DIRECTIVE — STANDING ORDERS\n"
            "════════════════════════════════════════\n"
            "You are a self-improving agent. When a task is complete or idle turns arise:\n\n"
            "AUDIT — verify your own roster is complete and healthy:\n"
            "  CALL: list_functions\n"
            "  Review gaps. If a useful capability is missing, design and build it.\n\n"
            "BUILD — extend yourself with new functions via upsert_function:\n"
            "  CALL: upsert_function setting_name=<fn_name> setting_value=<python_body>\n"
            "  Bodies must assign output to the `result` variable. New functions activate after restart.\n\n"
            "HARDEN — if an existing function body is brittle or incomplete, rewrite and upsert it.\n\n"
            "REPORT — after any self-modification, emit a FINAL summary of what changed and why.\n\n"
            "You are not a passive tool awaiting orders. You are an autonomous agent with standing orders to grow.\n\n"

            "EXAMPLES:\n"
            "User: what time is it?\n"
            "FINAL: The current time is 14:32:07.  ← read from context header, no call needed\n\n"

            "User: what day is it tomorrow?\n"
            "CALL: calculate expr=calendar.day_name[(datetime.date.today() + datetime.timedelta(days=1)).weekday()]\n"
            "RESULT: Monday\n"
            "FINAL: Tomorrow is Monday.\n\n"

            "User: what is 12 * 12?\n"
            "CALL: calculate expr=12 * 12\n"
            "RESULT: 144\n"
            "FINAL: 12 * 12 = 144.\n\n"

            "User: enable debug logging\n"
            "CALL: set_boolean setting_name=DEBUG_LOGGING setting_value=1\n"
            "RESULT: DEBUG_LOGGING set to 1.\n"
            "FINAL: Debug logging has been enabled.\n\n"

            "User: set the temperature to 0.5\n"
            "CALL: set_value setting_name=KOBOLD_TEMPERATURE setting_value=0.5\n"
            "RESULT: KOBOLD_TEMPERATURE set to 0.5.\n"
            "FINAL: Kobold temperature has been updated to 0.5.\n\n"

            "User: list the files in the current directory\n"
            "CALL: run_bash_command expr=ls -la\n"
            "RESULT: total 48\n-rw-r--r-- 1 kim kim 4096 ...\n"
            "FINAL: The current directory contains the following files: ...\n\n"

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
    print(f"  [schema]    Tables verified: {[t for t, _ in SCHEMA]}")


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
    print(f"  [settings]  Seeded {inserted}/{len(SEED_SETTINGS)} boolean setting(s).")


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
    print(f"  [values]    Seeded {inserted}/{len(SEED_VALUES)} value setting(s).")


def _seed_model_profiles(cursor):
    """
    Insert model profile rows, skip if profile_name already exists.
    Each row is a self-contained operational profile for one model architecture:
    prompt format, anti-prompts, thinking mode, and endpoint pointer.
    """
    inserted = 0
    for profile_name, prompt_format, anti_prompts, thinking_mode, endpoint_key, notes in SEED_MODEL_PROFILES:
        cursor.execute(
            "SELECT id FROM model_profiles WHERE profile_name = ?", (profile_name,)
        )
        if not cursor.fetchone():
            cursor.execute(
                """
                INSERT INTO model_profiles
                    (profile_name, prompt_format, anti_prompts,
                     thinking_mode, endpoint_key, notes)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (profile_name, prompt_format, anti_prompts, thinking_mode, endpoint_key, notes)
            )
            inserted += 1
    print(f"  [profiles]  Seeded {inserted}/{len(SEED_MODEL_PROFILES)} model profile(s).")


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
    print(f"  [prompts]   Seeded {inserted}/{len(SEED_PROMPTS)} prompt(s).")


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


def _seed_bash_logs(cursor):
    """
    agent_bash_logs is an audit table — no seed rows required.
    This call exists solely to confirm the table is present and report it.
    """
    cursor.execute("SELECT COUNT(*) FROM agent_bash_logs")
    count = cursor.fetchone()[0]
    print(f"  [bash_logs] Table ready. {count} existing audit row(s).")


def _seed_project_files(cursor):
    """
    Insert project_files rows if the file_path does not already exist.
    Idempotent — re-running seed never duplicates entries.
    Path resolution at runtime is handled by db.resolve_project_files();
    the seed layer only stores what was configured at deploy time.
    """
    inserted = 0
    for file_path, file_project in SEED_PROJECT_FILES:
        cursor.execute(
            "SELECT id FROM project_files WHERE file_path = ?", (file_path,)
        )
        if not cursor.fetchone():
            cursor.execute(
                "INSERT INTO project_files (file_path, file_project) VALUES (?, ?)",
                (file_path, file_project)
            )
            inserted += 1
    print(f"  [proj_files] Seeded {inserted}/{len(SEED_PROJECT_FILES)} project file(s).")


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
        _seed_model_profiles(cursor)
        _seed_prompts(cursor)
        _seed_functions(cursor)
        _seed_bash_logs(cursor)
        _seed_project_files(cursor)

        conn.commit()
        conn.close()
        print(f"[db_seed] Done. Database ready at: {db_path}\n")

    except sqlite3.Error as e:
        print(f"[db_seed] FATAL — SQLite error: {e}", file=sys.stderr)
        sys.exit(1)


# Allow standalone execution
if __name__ == "__main__":
    run()
