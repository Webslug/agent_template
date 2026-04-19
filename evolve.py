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
EVOLVE_KOBOLD_TEMPERATURE = 0.15   # lower temperature helps Gemma stay on task
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


def _build_local_evolve_prompt(snapshot):
    """
    A stricter, shorter prompt for the local Gemma path.

    Claude can handle looser instructions. Gemma benefits from a tighter
    output contract with fewer degrees of freedom and explicit anti-patterns.
    """
    return (
        "You are reviewing a mostly complete local AI agent project.\n"
        "Your job is to identify the next most useful engineering work.\n\n"
        "Write plain text only. Do not use code fences, tables, roleplay, or ellipses.\n"
        "Do not invent missing items that are already present in the snapshot.\n"
        "Do not repeat the snapshot back to me.\n"
        "Be concrete and concise.\n\n"
        "Use exactly this structure:\n"
        "1. <highest priority gap> - why it matters - what to do next\n"
        "2. <next gap> - why it matters - what to do next\n"
        "3. <next gap> - why it matters - what to do next\n"
        "4. <next gap> - why it matters - what to do next\n"
        "5. <next gap> - why it matters - what to do next\n\n"
        "Prioritize real implementation gaps in this order:\n"
        "- missing functions\n"
        "- missing settings\n"
        "- missing harness rules\n"
        "- brittle function behavior\n"
        "- architectural cleanup\n\n"
        "If evidence is weak, say so briefly instead of hallucinating.\n\n"
        "AGENT SNAPSHOT\n"
        "────────────────────────────────────────────────────────────────\n"
        f"{snapshot}\n"
        "────────────────────────────────────────────────────────────────\n"
        "BEGIN REPORT:\n"
    )


def _is_low_quality_response(response):
    """
    Detect obviously unusable local model output.

    We only use this for the local /evolve path. Claude output is left alone.
    """
    if not response:
        return True

    text = response.strip()
    if len(text) < 120:
        return True

    lowered = text.lower()
    if "```" in text:
        return True
    if "..." in text:
        return True
    if "[priority" in lowered:
        return True

    numbered_lines = 0
    for line in text.splitlines():
        stripped = line.strip()
        if stripped[:1].isdigit() and "." in stripped[:3]:
            numbered_lines += 1
    return numbered_lines < 3


def _build_local_fallback_report(db_path):
    """
    Deterministic fallback used when the local model response is unusable.

    The goal is to still return a salient /evolve report instead of saving
    obvious gibberish when Gemma drifts.
    """
    conn = sqlite3.connect(db_path)
    try:
        function_names = {
            row[0] for row in conn.execute(
                "SELECT function_name FROM functions WHERE function_enabled = 1"
            ).fetchall()
        }
        boolean_names = {
            row[0] for row in conn.execute(
                "SELECT setting_name FROM settings_boolean"
            ).fetchall()
        }
        value_names = {
            row[0] for row in conn.execute(
                "SELECT setting_name FROM settings_values"
            ).fetchall()
        }
        harness_names = {
            row[0] for row in conn.execute(
                "SELECT harness_name FROM harnesses WHERE harness_enabled = 1"
            ).fetchall()
        }
    finally:
        conn.close()

    suggestions = []

    def add(title, rationale, next_step):
        suggestions.append((title, rationale, next_step))

    if "get_function_body" not in function_names:
        add(
            "Add get_function_body",
            "The agent can inspect and list functions, but it cannot read an existing body for debugging or review.",
            "Implement a read-only function that returns the full body for a named function."
        )
    if "delete_function" not in function_names:
        add(
            "Add delete_function",
            "There is no supported path for cleanly removing obsolete functions once they become harmful or redundant.",
            "Add a deletion helper that requires explicit confirmation and logs the removal reason."
        )
    if "get_bash_log_filtered" not in function_names:
        add(
            "Add get_bash_log_filtered",
            "The current bash-log view is likely too coarse for daemon and cron troubleshooting.",
            "Support filters for exit code, command text, and time window so operators can inspect failures faster."
        )
    if "health_check" not in function_names:
        add(
            "Add health_check",
            "The agent needs a cheap probe for model endpoints when running unattended.",
            "Return endpoint status and latency for Kobold and Ollama before the next runtime issue becomes a mystery."
        )
    if "restart_service" not in function_names:
        add(
            "Add restart_service",
            "A daemon-oriented agent should be able to recover a failed backend without manual intervention.",
            "Add a guarded service restart helper with a small whitelist of allowed service names."
        )

    if "BASH_TIMEOUT_SECONDS" not in value_names:
        add(
            "Add BASH_TIMEOUT_SECONDS",
            "Bash execution timeouts should be operator-configurable instead of hard-coded.",
            "Seed a sane range like 5 to 300 seconds and read it at runtime."
        )
    if "LOG_RETENTION_DAYS" not in value_names:
        add(
            "Add LOG_RETENTION_DAYS",
            "The audit log will keep growing unless the retention policy is explicit.",
            "Store a retention window in settings_values and prune old rows on a schedule."
        )
    if "HEALTH_CHECK_INTERVAL_SECONDS" not in value_names:
        add(
            "Add HEALTH_CHECK_INTERVAL_SECONDS",
            "Health probes should be schedulable without code changes.",
            "Keep the polling cadence in settings_values so daemon and cron deployments can tune it."
        )
    if "DRY_RUN_MODE" not in boolean_names:
        add(
            "Add DRY_RUN_MODE",
            "Safe testing is hard if destructive actions always execute for real.",
            "Use a boolean guard that logs intent but suppresses writes and shell execution."
        )

    if "BASH_NO_NETWORK_EXPOSURE" not in harness_names:
        add(
            "Add BASH_NO_NETWORK_EXPOSURE",
            "The current harness set protects against destructive shell commands, but not data exfiltration.",
            "Deny curl, wget, nc, scp, and similar tools unless an allowlist explicitly permits them."
        )
    if "FUNCTION_SCHEMA_VALIDATION" not in harness_names:
        add(
            "Add FUNCTION_SCHEMA_VALIDATION",
            "Upserted function bodies should be sanity-checked before execution time.",
            "Validate bodies with AST checks and reject obvious hazards before they enter the roster."
        )

    if not suggestions:
        suggestions.append((
            "Tighten observability",
            "The current snapshot does not expose an obvious missing capability, so the best gains are likely operational.",
            "Focus next on logging, health checks, and prompt quality improvements."
        ))

    lines = [
        "LOCAL FALLBACK REPORT",
        "The local model output was unusable, so this deterministic report was generated from the live database snapshot.",
        ""
    ]
    for idx, (title, rationale, next_step) in enumerate(suggestions[:5], start=1):
        lines.append(f"{idx}. {title} - {rationale} {next_step}")

    return "\n".join(lines)


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
    prompt   = _build_local_evolve_prompt(snapshot) if mode == "local" else _build_evolve_prompt(snapshot)

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
        if mode == "local" and _is_low_quality_response(response):
            print("  [evolve] Local response looked unusable; generating fallback report.")
            response = _build_local_fallback_report(db_path)
        print(f"  [evolve] Response received ({len(response)} chars).")
        _write_output(response, output_file, base_dir, mode)
        # Print a brief preview so the operator gets immediate value
        preview = response[:400].replace("\n", "\n  ")
        print(f"\n  Preview:\n  {preview}{'...' if len(response) > 400 else ''}\n")
