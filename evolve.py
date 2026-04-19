# =============================================================================
# evolve.py
# /evolve command module. Introspects the live database, assembles a
# self-improvement brief, dispatches it to either the local Kobold model
# or the Claude API, and writes the response to a text file in the project root.
#
# Called exclusively by agent.py — never by index.py directly.
#
# USAGE (from loop_interactive):
#   /evolve         → local (Kobold)
#   /evolve local   → local (Kobold)
#   /evolve claude  → Claude Haiku (requires api/claude.key)
#
# OUTPUT FILES:
#   output_local.txt   — Kobold's self-improvement suggestions
#   output_claude.txt  — Claude's self-improvement suggestions
#
# SNAPSHOT TABLES CONSULTED:
#   functions        — name + description of all enabled functions
#   settings_boolean — full roster of boolean switches
#   settings_values  — full roster of string/endpoint/path values
#   harnesses        — name + rule for all harness constraints
#   agent_bash_logs  — last EVOLVE_BASH_LOG_LIMIT audit entries
#   project_files    — registered source file paths
#
# CLAUDE KEY PATH:
#   <project_root>/api/claude.key
#   Must be present for /evolve claude to function.
#   The key file contains only the raw API key string (no quotes, no newline).
#
# DESIGN NOTES:
#   - evolve.py owns its own DB connection — it does not use the in-memory
#     arrays from the runtime tuple. This is intentional: the snapshot must
#     reflect the current on-disk state, not a potentially stale in-memory
#     view from the last reseed cycle.
#   - Kobold dispatch reuses the same HTTP pattern as agent._call_kobold
#     but is intentionally not imported from agent.py to keep evolve.py
#     self-contained and independently testable.
#   - Claude dispatch uses urllib only — no third-party SDK required.
#   - Both paths write to their respective output files regardless of whether
#     the response contains useful content (empty responses are flagged clearly).
# =============================================================================

import datetime
import json
import os
import sqlite3
import urllib.request
import urllib.error

# -----------------------------------------------------------------------------
# CONSTANTS
# -----------------------------------------------------------------------------

EVOLVE_CLAUDE_MODEL     = "claude-haiku-4-5-20251001"
EVOLVE_CLAUDE_API_URL   = "https://api.anthropic.com/v1/messages"
EVOLVE_CLAUDE_MAX_TOKENS = 1500

EVOLVE_KOBOLD_MAX_TOKENS  = 900
EVOLVE_KOBOLD_TEMPERATURE = 0.3    # slightly warmer than default for ideation
EVOLVE_KOBOLD_TOP_P       = 0.9

EVOLVE_BASH_LOG_LIMIT   = 10       # recent audit entries included in snapshot
EVOLVE_OUTPUT_LOCAL     = "output_local.txt"
EVOLVE_OUTPUT_CLAUDE    = "output_claude.txt"

# Key file path relative to the project root (base_dir injected at call time)
EVOLVE_KEY_SUBPATH      = os.path.join("api", "claude.key")

# -----------------------------------------------------------------------------
# SNAPSHOT BUILDER
# Assembles a structured plain-text brief from the live database.
# This is the context payload given to whichever LLM is being consulted.
# -----------------------------------------------------------------------------

def _build_snapshot(db_path):
    """
    Read the live database and return a structured plain-text snapshot string.
    Opens its own connection — does not depend on the runtime in-memory arrays.
    """
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        lines = []

        # ── Functions ─────────────────────────────────────────────────────────
        rows = conn.execute(
            "SELECT function_name, function_description "
            "FROM functions WHERE function_enabled = 1 ORDER BY function_name"
        ).fetchall()
        lines.append("=== FUNCTIONS (enabled) ===")
        if rows:
            for r in rows:
                lines.append(f"  {r['function_name']}: {r['function_description']}")
        else:
            lines.append("  (none)")
        lines.append("")

        # ── Boolean Settings ──────────────────────────────────────────────────
        rows = conn.execute(
            "SELECT setting_name, setting_bool FROM settings_boolean ORDER BY setting_name"
        ).fetchall()
        lines.append("=== SETTINGS (boolean) ===")
        for r in rows:
            lines.append(f"  {r['setting_name']} = {r['setting_bool']}")
        lines.append("")

        # ── Value Settings ────────────────────────────────────────────────────
        rows = conn.execute(
            "SELECT setting_name, setting_value FROM settings_values ORDER BY setting_name"
        ).fetchall()
        lines.append("=== SETTINGS (values) ===")
        for r in rows:
            lines.append(f"  {r['setting_name']} = {r['setting_value']}")
        lines.append("")

        # ── Harnesses ─────────────────────────────────────────────────────────
        rows = conn.execute(
            "SELECT harness_name, harness_rule, harness_enabled "
            "FROM harnesses ORDER BY harness_name"
        ).fetchall()
        lines.append("=== HARNESSES ===")
        for r in rows:
            status = "ON" if r["harness_enabled"] else "OFF"
            lines.append(f"  [{status}] {r['harness_name']}: {r['harness_rule'][:120]}")
        lines.append("")

        # ── Project Files ─────────────────────────────────────────────────────
        rows = conn.execute(
            "SELECT file_path, file_project FROM project_files ORDER BY file_project, file_path"
        ).fetchall()
        lines.append("=== PROJECT FILES ===")
        for r in rows:
            lines.append(f"  [{r['file_project']}] {r['file_path']}")
        lines.append("")

        # ── Recent Bash Audit Log ─────────────────────────────────────────────
        rows = conn.execute(
            "SELECT run_at, exit_code, command, stderr "
            "FROM agent_bash_logs ORDER BY id DESC LIMIT ?",
            (EVOLVE_BASH_LOG_LIMIT,)
        ).fetchall()
        lines.append(f"=== RECENT BASH LOG (last {EVOLVE_BASH_LOG_LIMIT}) ===")
        if rows:
            for r in rows:
                entry = f"  [{r['run_at']}] exit={r['exit_code']} | {r['command'][:80]}"
                if r["stderr"]:
                    entry += f" | stderr: {r['stderr'][:60]}"
                lines.append(entry)
        else:
            lines.append("  (no entries)")
        lines.append("")

        conn.close()
        return "\n".join(lines)

    except sqlite3.Error as e:
        return f"[SNAPSHOT ERROR] Could not read database: {e}"


# -----------------------------------------------------------------------------
# EVOLVE PROMPT ASSEMBLER
# Wraps the snapshot in a structured self-improvement brief.
# The same brief is sent to both Kobold and Claude — the only difference
# is which HTTP endpoint receives it.
# -----------------------------------------------------------------------------

def _build_evolve_prompt(snapshot):
    """
    Wrap the database snapshot in a self-improvement analysis prompt.
    Returns the full prompt string ready to send to an LLM.
    """
    return (
        "You are a senior AI systems architect reviewing an autonomous AI agent framework.\n"
        "Below is a complete snapshot of the agent's current state: its function roster, "
        "configuration settings, operator harnesses, project files, and recent bash audit log.\n\n"
        "Your task is to analyse this snapshot and produce a prioritised list of concrete "
        "improvement recommendations. Focus on:\n"
        "  1. MISSING FUNCTIONS — capabilities the agent demonstrably lacks given its stated "
        "     purpose (versatile autonomous agent for cron, daemon, and interactive deployment).\n"
        "  2. MISSING SETTINGS — boolean switches or value settings that would improve "
        "     observability, resilience, or configurability.\n"
        "  3. HARNESS GAPS — operator constraints that are missing or under-specified.\n"
        "  4. FUNCTION QUALITY — existing function bodies or descriptions that are brittle, "
        "     incomplete, or could be meaningfully improved.\n"
        "  5. ARCHITECTURAL OBSERVATIONS — anything structural in the project files or "
        "     settings that suggests a systemic weakness.\n\n"
        "Be specific. Name the exact function, setting key, or harness rule you would add "
        "or change. Provide a brief rationale for each recommendation.\n"
        "Do not pad the response. Prioritise by impact.\n\n"
        "────────────────────────────────────────────────────────────────\n"
        "AGENT SNAPSHOT\n"
        "────────────────────────────────────────────────────────────────\n"
        f"{snapshot}\n"
        "────────────────────────────────────────────────────────────────\n"
        "BEGIN RECOMMENDATIONS:\n"
    )


# -----------------------------------------------------------------------------
# KOBOLD DISPATCH
# Fires the evolve prompt at the local Kobold instance.
# Uses raw urllib — no dependency on agent._call_kobold.
# -----------------------------------------------------------------------------

def _call_kobold_evolve(prompt, kobold_endpoint):
    """
    Send the evolve prompt to Kobold and return the response string.
    Returns an error string prefixed with [ERROR] on failure.
    """
    payload = json.dumps({
        "prompt":      prompt,
        "max_length":  EVOLVE_KOBOLD_MAX_TOKENS,
        "temperature": EVOLVE_KOBOLD_TEMPERATURE,
        "top_p":       EVOLVE_KOBOLD_TOP_P,
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            kobold_endpoint,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return body["results"][0]["text"].strip()
    except urllib.error.URLError as e:
        return f"[ERROR] Kobold unreachable: {e.reason}"
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        return f"[ERROR] Unexpected Kobold response format: {e}"
    except Exception as e:
        return f"[ERROR] Kobold connection lost — {type(e).__name__}: {e}"


# -----------------------------------------------------------------------------
# CLAUDE DISPATCH
# Fires the evolve prompt at the Claude API using the Haiku model.
# Key loaded from api/claude.key relative to the project base directory.
# -----------------------------------------------------------------------------

def _load_claude_key(base_dir):
    """
    Load the Claude API key from <base_dir>/api/claude.key.
    Returns the key string on success, or None with a printed error on failure.
    """
    key_path = os.path.join(base_dir, EVOLVE_KEY_SUBPATH)
    if not os.path.isfile(key_path):
        print(f"  [evolve] Claude key not found at: {key_path}")
        print(f"  [evolve] Create the api/ subdirectory and place your key in claude.key")
        return None
    try:
        key = open(key_path).read().strip()
        if not key:
            print(f"  [evolve] Claude key file is empty: {key_path}")
            return None
        return key
    except OSError as e:
        print(f"  [evolve] Could not read Claude key: {e}")
        return None


def _call_claude_evolve(prompt, base_dir):
    """
    Send the evolve prompt to Claude Haiku and return the response string.
    Returns an error string prefixed with [ERROR] on failure.
    """
    api_key = _load_claude_key(base_dir)
    if not api_key:
        return "[ERROR] Claude API key unavailable — see above."

    payload = json.dumps({
        "model":      EVOLVE_CLAUDE_MODEL,
        "max_tokens": EVOLVE_CLAUDE_MAX_TOKENS,
        "messages":   [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            EVOLVE_CLAUDE_API_URL,
            data=payload,
            headers={
                "Content-Type":    "application/json",
                "x-api-key":       api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return body["content"][0]["text"].strip()

    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            err_body = json.loads(raw)
            err_msg  = err_body.get("error", {}).get("message", raw[:200])
        except json.JSONDecodeError:
            err_msg = raw[:200]
        return f"[ERROR] Claude API HTTP {e.code}: {err_msg}"
    except urllib.error.URLError as e:
        return f"[ERROR] Claude API unreachable: {e.reason}"
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        return f"[ERROR] Unexpected Claude response format: {e}"
    except Exception as e:
        return f"[ERROR] Claude call failed — {type(e).__name__}: {e}"


# -----------------------------------------------------------------------------
# FILE WRITER
# Writes the LLM response to the designated output file in the project root.
# Always overwrites — each /evolve run is a fresh verdict, not an appendix.
# -----------------------------------------------------------------------------

def _write_output(response, output_filename, base_dir, mode_label):
    """
    Write the LLM response to <base_dir>/<output_filename>.
    Prepends a timestamped header for traceability.
    Prints the output path on success.
    """
    output_path = os.path.join(base_dir, output_filename)
    timestamp   = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header      = (
        f"# /evolve {mode_label} — {timestamp}\n"
        f"# Model: {'Claude Haiku' if mode_label == 'claude' else 'Kobold (local)'}\n"
        f"{'=' * 64}\n\n"
    )
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(header + response + "\n")
        print(f"  [evolve] Output written → {output_path}")
    except OSError as e:
        print(f"  [evolve] WARNING — could not write output file: {e}")


# -----------------------------------------------------------------------------
# PUBLIC ENTRY POINT
# Called by agent._dispatch_command when it sees a /evolve directive.
# -----------------------------------------------------------------------------

def dispatch_evolve(mode, db_path, base_dir, values):
    """
    Main /evolve handler. Orchestrates snapshot → prompt → LLM → file.

    mode      — "local" or "claude"
    db_path   — path to database.db (from agent.DB_PATH)
    base_dir  — project root directory (from index.BASE_DIR, injected via agent)
    values    — settings_values array from the runtime tuple (for Kobold endpoint)

    Prints status lines to the terminal throughout so the operator knows
    the evolve run is progressing — LLM calls can take 30–180 seconds.
    """
    mode = (mode or "local").strip().lower()
    if mode not in ("local", "claude"):
        print(f"  [evolve] Unknown mode '{mode}'. Use: /evolve local | /evolve claude\n")
        return

    print(f"\n  [evolve] Mode: {mode}")
    print(f"  [evolve] Building database snapshot...")

    snapshot = _build_snapshot(db_path)
    prompt   = _build_evolve_prompt(snapshot)

    if mode == "claude":
        print(f"  [evolve] Dispatching to Claude Haiku ({EVOLVE_CLAUDE_MODEL})...")
        response      = _call_claude_evolve(prompt, base_dir)
        output_file   = EVOLVE_OUTPUT_CLAUDE
    else:
        # Resolve Kobold endpoint from the live settings_values array
        kobold_endpoint = "http://localhost:5001/api/v1/generate"
        for row in values:
            if row.get("setting_name") == "ENDPOINT_KOBOLD":
                kobold_endpoint = row["setting_value"]
                break
        print(f"  [evolve] Dispatching to Kobold ({kobold_endpoint})...")
        response    = _call_kobold_evolve(prompt, kobold_endpoint)
        output_file = EVOLVE_OUTPUT_LOCAL

    if response.startswith("[ERROR]"):
        print(f"  [evolve] {response}\n")
    else:
        print(f"  [evolve] Response received ({len(response)} chars).")
        _write_output(response, output_file, base_dir, mode)
        # Print a brief preview so the operator gets immediate value
        preview = response[:400].replace("\n", "\n  ")
        print(f"\n  Preview:\n  {preview}{'...' if len(response) > 400 else ''}\n")
