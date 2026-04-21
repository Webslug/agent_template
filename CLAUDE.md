# CLAUDE.md — Project Memory for /home/kim/projects/template
# ============================================================
# This file is the authoritative briefing for Claude Code.
# Read this before touching any file in this project.
# ============================================================

## WHAT THIS IS

A local LLM agent framework in Python — a port and extension of a prior C# AgentLoop
implementation. Deployable as a daemon, cron job, or interactive terminal session.
Designed as a reusable, database-driven agent scaffold.

LLM backend: KoboldAI at http://localhost:5001/api/v1/generate
Active model: Gemma 4 IT (gemma-4-E4B-it-Q5_K_S.gguf, ~4GB VRAM)
Runtime: Python 3.10.12, SQLite, Linux (Lubuntu LXQT)
Sandbox: firejail --whitelist=/home/kim/projects/template

---

## SIX-FILE ARCHITECTURE — DO NOT ADD FILES

```
index.py        — constants, boot sequence, main()
db.py           — SQLite data access layer and in-memory resolvers
db_seed.py      — schema enforcement and seeding
db_functions.py — canonical SEED_FUNCTIONS roster (sole source of truth)
agent.py        — Kobold I/O, scratchpad loop, execution modes
database.db     — SQLite runtime database (deleted and reseeded at milestones)
```

No subfolders. No new files unless explicitly authorised.

---

## DATABASE SCHEMA (7 tables)

| Table              | Purpose                                          |
|--------------------|--------------------------------------------------|
| settings_boolean   | Binary switches — 0 or 1 ONLY                   |
| settings_values    | String values, endpoints, paths, intervals       |
| agent_prompts      | System prompt bodies, hot-swappable at runtime   |
| functions          | Callable agent roster, exec()'d at dispatch time |
| model_profiles     | Per-architecture anti-prompts + format           |
| project_files      | Source files registered for context injection    |
| agent_bash_logs    | Audit trail for all run_bash_command calls       |

**Critical distinction**: `settings_boolean` = binary switches only. `settings_values` = strings,
paths, endpoints, numeric ranges. This separation is rigid and must be maintained everywhere.

---

## RUNTIME TUPLE (7-element)

```python
(settings, values, prompts, functions, profiles, project_files, system_prompt)
```

Assembled in `index.py::_build_runtime_state()`. Passed into agent.py loops.
`build_runtime_fn` is passed as a callable to avoid circular import between index.py ↔ agent.py.

---

## EXECUTION MODES

Two modes — both must remain fully functional, neither is a degraded path:

- `loop_interactive` — readline loop, `!commands` intercepted locally, Kobold called for all else
- `loop_stateless`  — stdin pipe reader, suitable for cron/daemon/service deployment

---

## SCRATCHPAD LOOP — CALL/RESULT/FINAL PROTOCOL

Agent emits on its own line:
```
CALL: function_name [key=value ...]
```
System executes and returns:
```
RESULT: <output>
```
When complete:
```
FINAL: answer text
```

Max 12 scratchpad turns (`MAX_SCRATCHPAD_TURNS = 12`). Duplicate CALL guard prevents stall loops.

---

## GEMMA-SPECIFIC BEHAVIOURS (DO NOT BREAK)

1. **Thinking blocks**: Gemma wraps reasoning in `<|channel>thought...<channel|>`.
   `_extract_gemma_thought()` extracts, prints, then strips these from `raw` before scanning
   for CALL:/FINAL: directives. The strip must happen BEFORE the directive scan — Gemma emits
   CALL: on the same line as the closing thought tag.

2. **Duplicate FINAL: handling**: Gemma sometimes emits multiple FINAL: lines. Only
   `final_lines[0]` is used — never join them.

3. **`_parse_call_params` greedy `expr=`**: `expr=` captures everything to end-of-line.
   Splitting on every `=` breaks nested expressions like `timedelta(days=3)`. The greedy
   capture is intentional and must not be "simplified".

4. **Prompt format (Gemma wire format)**:
```
<start_of_turn>user
[system block]

[context header]
[conversation]<end_of_turn>
<start_of_turn>model
```
   With `THINKING_MODE=1`, the system block is prefixed with `<|think|>`.

---

## HOT-SWAP MECHANISM

Agent sets `PROMPT_RELOAD=1` in `settings_boolean` via `reload_prompt` function.
`_check_prompt_reload()` in agent.py checks this flag at the top of every turn.
On detection: rebuilds runtime via `build_runtime_fn()`, resets flag to 0.
No process restart required for prompt changes.

---

## FIREJAIL LAUNCH PATTERN

```bash
firejail --whitelist=/home/kim/projects/template python3 /home/kim/projects/template/index.py
```

**DO NOT use `--private`** — it blanks the home directory. Use `--whitelist=` only.

---

## KOBOLD LAUNCHER

`kobold.sh` — interactive numbered menu to select and launch model via koboldcpp-rolling.
Kills any running koboldcpp before launch. Located at `/home/kim/Downloads/koboldcpp-rolling`.

---

## KNOWN OPEN ISSUES (prioritised)

1. **Cosmetic bleed bug** — partial thought block content leaks into terminal print in
   `loop_interactive` before the strip completes. Fix target: `_agent_turn()`.

2. **No logging discipline** — `logs` table exists in schema but is unused throughout.
   All agent events should route through it.

3. **No DB migration path** — pre-existing databases missing newer tables (e.g. `agent_bash_logs`,
   `model_profiles`) have no upgrade path. `db_seed.py::run()` is idempotent for inserts
   but does not add columns or tables to existing DBs.

4. **Silent fallback in `_extract_tool_call`** — returns None with no visibility when
   the model emits a malformed tool call. Should log to terminal.

5. **No Ollama routing** — `ENDPOINT_OLLAMA` is seeded but `_call_kobold` never dispatches
   to it. A routing decision based on `active_profile["endpoint_key"]` is needed.

## RECENT FIXES

1. **`/evolve local` output quality hardened** — the local Gemma path now uses a stricter,
   shorter prompt and a deterministic fallback report when the model response is obviously
   unusable. This was verified by rerunning `index.py` and then invoking `/evolve local`.
   The fallback now keeps the tool useful even when Gemma drifts into gibberish.

---

## PLANNED ADDITIONS (not yet built)

- `db_admin.py` — CLI for non-technical operators to manage functions and prompts
- `watchdog.py` — supervisor daemon with restart logging
- Enriched function digest in system prompt — include parameter signatures to reduce
  hallucinated call syntax during inference
- Database migration path for schema evolution

---

## KEY RULES FOR CLAUDE CODE

- **Always deliver complete working files** — no fragments, no patches unless asked
- **Plan before coding** — clarify architecture before writing any file
- **Do not rename** established constants, table names, or column names without explicit instruction
- **`settings_boolean` vs `settings_values`** — the distinction is strict; never cross-populate
- **Database changes require reseed** — deleting `database.db` and running `python3 db_seed.py`
  is the intentional migration path at milestone boundaries
- **`db_functions.py` is the sole source of truth** for the function roster
- **Both execution modes must reach Kobold** — stateless requires piped stdin; do not degrade it
- **The circular import between index.py and agent.py is resolved** — `build_runtime_fn` is
  passed as a callable parameter, not imported. Do not introduce new cross-imports.

---

## ACTIVE MODEL PROFILES (seeded in model_profiles table)

| Profile | Format  | Thinking | Notes                              |
|---------|---------|----------|------------------------------------|
| GEMMA   | gemma   | 1        | google_gemma-3-4b-it / gemma-4 IT  |
| QWEN    | chatml  | 1        | Qwen3.5-9B                         |
| HERMES  | chatml  | 0        | Hermes-3-Llama-3.1-8B              |
| LLAMA3  | llama3  | 0        | Meta-Llama-3-8B-Instruct           |
| MISTRAL | mistral | 0        | Mistral-7B-Instruct                |
| PHI3    | phi3    | 0        | Phi-3-mini-128k-instruct           |

Active profile resolved at boot from `ACTIVE_MODEL` in `settings_values`.
Anti-prompts live in `model_profiles.anti_prompts` — NOT in `settings_values`.
