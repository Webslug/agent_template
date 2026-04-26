# Roadmap — Forward Engineering Backlog
# /home/kim/projects/template
# Authored: April 2026
# Status: PLANNED — none of the below is implemented

---

## Preamble

The current six-file architecture is intentionally lean. Speed is a direct consequence
of that leanness: no trimming, no summarisation, no vector lookups, no memory overhead.
This document plans the next tier of capability without compromising that principle.

Hardware will improve. Context windows will grow. The 64k ceiling of today is a
temporary constraint, not a permanent law. Designs below must degrade gracefully
on current hardware and scale without refactoring on future hardware.

---

## BLOCK 1 — Context Management (Trimming & Summarisation)

### Problem

`_agent_turn()` in `agent.py` builds `conversation` by raw string concatenation across
up to `MAX_SCRATCHPAD_TURNS = 12` cycles. Each CALL/RESULT pair appends verbatim to the
growing string. A large bash stdout or file read can spike context by thousands of tokens
in a single turn. No trimming guard exists. No token budget is enforced.

This is currently not a problem because:
  - Gemma 4 IT is fast and the context window is 64k
  - Most sessions are short and function outputs are concise
  - The speed benefit of the current design is real and must be preserved

### Design Principle

  **Do not trim by default. Trim only when a budget threshold is approached.**
  The trimmer is a pressure-relief valve, not a standard pipeline stage.

### Planned Implementation

#### 1a — Token Budget Guard (settings_values entry)

Add to `settings_values`:

  SCRATCHPAD_TOKEN_BUDGET = 48000

This is the soft ceiling for the `conversation` string before trimming fires.
Approximation: `len(conversation) / 3.5` gives a rough token estimate without
requiring a tokeniser library. Cheap, fast, good enough for a guard rail.

In `_agent_turn()`, before each Kobold call:

```python
BUDGET = int(db.resolve_value(values, "SCRATCHPAD_TOKEN_BUDGET", fallback=48000))
approx_tokens = len(conversation) // 3   # conservative estimate
if approx_tokens > BUDGET:
    conversation = _trim_conversation(conversation, BUDGET)
```

Add to `settings_boolean`:

  SCRATCHPAD_TRIM_ENABLED = 1   # master switch; 0 = legacy unbounded behaviour

#### 1b — Trimmer Strategy (`_trim_conversation`)

The trimmer is not a summariser. It is a **window slider**.

Strategy (in order of preference):

  1. Drop the oldest RESULT: blocks first — they are the most verbose and least
     relevant to the current turn's reasoning.
  2. Preserve the original user_input (turn 0) always — it is the mission statement.
  3. Preserve the most recent N CALL:/RESULT: pairs (N = SCRATCHPAD_KEEP_TAIL,
     default 3, stored in settings_values).
  4. If still over budget after dropping old results, drop old CALL: lines too.
  5. Never drop FINAL: lines — they are the agent's committed answers.

This is a sliding window over the scratchpad, not a destructive summarisation.
No LLM call required. No latency added unless the budget is actually breached.

#### 1c — Summarisation (Future, Hardware-Gated)

Summarisation is a **separate optional tier**, gated behind:

  SCRATCHPAD_SUMMARISE_ENABLED = 0   (default OFF)

When enabled, if trimming alone cannot bring the context under budget (i.e. a
single RESULT block exceeds the budget by itself), a secondary Kobold call is
made with a compression prompt:

  "Summarise the following tool output in under 200 words, preserving all
   numeric values, file paths, error codes, and key findings:"

The compressed result replaces the raw RESULT: block in the conversation string.

This adds latency and a second inference call. It is appropriate only for
autonomous daemon deployments processing large file outputs, not interactive use.
Hardware gate: only activate when context window > 128k tokens is available,
or when VRAM allows two concurrent model loads.

#### 1d — Schema Changes Required

No new tables. Two additions to existing tables:

  settings_values:   SCRATCHPAD_TOKEN_BUDGET    = "48000"
  settings_values:   SCRATCHPAD_KEEP_TAIL       = "3"
  settings_boolean:  SCRATCHPAD_TRIM_ENABLED    = 1
  settings_boolean:  SCRATCHPAD_SUMMARISE_ENABLED = 0

#### 1e — File Touch Points

  agent.py   — _agent_turn(): add budget check and call _trim_conversation()
  agent.py   — add _trim_conversation(conversation, budget) helper
  db_seed.py — seed the four new settings rows
  CLAUDE.md  — update scratchpad protocol section

---

## BLOCK 2 — Hard Dispatcher Enforcement in `_execute_function`

### Problem

Harnesses constrain via prompt injection only. The model reads the CONSTRAINTS block
and is expected to self-enforce. A sufficiently confident or confused model can still
emit a forbidden bash command; `_execute_function` will execute it; the audit log
captures the aftermath. The harness boundary is advisory, not structural.

### Design Principle

  **Prompt harnesses remain the first line. Dispatcher enforcement is the wall.**
  Two independent layers. Neither replaces the other.

### Planned Implementation

#### 2a — Blocklist Table (new DB table or harnesses extension)

Option A (preferred): Extend `harnesses` with a `harness_type` column:

  harness_type TEXT NOT NULL DEFAULT 'prompt'
  Values: 'prompt' | 'dispatch'

  'prompt'   — current behaviour: injected into system prompt only
  'dispatch' — enforced at dispatcher level AND injected into prompt

Option B: New table `dispatch_rules (id, rule_pattern, rule_action, rule_enabled)`.

Option A is preferred because it reuses the existing harness admin surface and
keeps the table count stable.

#### 2b — Dispatcher Check Pattern

In `_execute_function()`, before `exec()`:

```python
def _is_dispatched_blocked(fn_name, call_str, harnesses):
    """
    Check call_str against all dispatch-type harnesses.
    Returns (True, rule_name) if blocked, (False, None) if clear.
    """
    dispatch_harnesses = [
        h for h in harnesses
        if h.get("harness_type") == "dispatch" and h.get("harness_enabled") == 1
    ]
    for h in dispatch_harnesses:
        # Simple pattern match — rule contains the forbidden token
        # e.g. harness_rule = "BLOCK: rm -rf"
        forbidden = h["harness_rule"].replace("BLOCK:", "").strip()
        if forbidden.lower() in call_str.lower():
            return True, h["harness_name"]
    return False, None
```

On block: log to `logs` table with `log_code=2`, return a clean refusal string
to the scratchpad — do not raise an exception, do not crash the turn.

#### 2c — Seeded Dispatch Rules (initial roster)

The following should be seeded as `harness_type='dispatch'` rules:

  DISPATCH_BLOCK_RM_RF        — pattern: "rm -rf"
  DISPATCH_BLOCK_DD           — pattern: "dd if="
  DISPATCH_BLOCK_CHMOD_777    — pattern: "chmod 777"
  DISPATCH_BLOCK_SUDO         — pattern: "sudo "
  DISPATCH_BLOCK_CURL_PIPE    — pattern: "curl | bash" and "wget | bash"
  DISPATCH_BLOCK_DROP_TABLE   — pattern: "DROP TABLE"
  DISPATCH_BLOCK_ALTER_TABLE  — pattern: "ALTER TABLE"

These are the irreversible-or-escalating patterns. The list is intentionally
conservative. Adding more is a DB operation, no code change required.

#### 2d — Schema Changes Required

  harnesses table: ADD COLUMN harness_type TEXT NOT NULL DEFAULT 'prompt'

Migration note: existing rows default to 'prompt' — no data loss, no reseeding
required for current installations. New dispatch rows added in db_seed.py as
idempotent inserts.

#### 2e — File Touch Points

  db_seed.py  — add harness_type column to schema, seed dispatch rules
  agent.py    — _execute_function(): add _is_dispatched_blocked() pre-check
  db.py       — fetch_all_harnesses() already returns all columns; no change
                needed if harness_type column is added to existing table
  CLAUDE.md   — update harness enforcement model section

---

## BLOCK 3 — Agent Memory (Compact Context Injection)

### Problem

The agent has no memory between sessions. Each boot starts cold. Recurring context
(who the operator is, what the project does, what was decided last week) must be
re-established from scratch every session or baked permanently into the system prompt.
Baking it into the prompt is brittle — it requires a DB edit and a prompt reload
for every update.

### Design Principle

  **Memory is a compacted, operator-curated insert into the system prompt.**
  It is not a vector database. It is not a retrieval system.
  It is a flat text file or DB table that gets injected at assembly time.
  Keep it dumb. Keep it fast. The complexity ceiling is a chroma lookup — no higher.

### Tier 1 — `memory.md` Flat File (Immediate, Zero Dependencies)

A plain text file at the project root: `memory.md`.

Contents: operator-written or agent-written bullet points. Short. Factual. Dated.

  # Agent Memory — last updated 2026-04-23
  - Operator: Emily. Preferred name in responses: Emily.
  - Project: /home/kim/projects/template — local LLM agent framework.
  - Active model: Gemma 4 IT via KoboldAI on localhost:5001.
  - Known working functions: run_bash_command, get_current_datetime, ...
  - Last session: diagnosed scratchpad stall on nested timedelta expression.

At prompt assembly time in `db.assemble_system_prompt()`, if `memory.md` exists
and `MEMORY_INJECT_ENABLED = 1` in `settings_boolean`, its contents are appended
to the system prompt under a `## AGENT MEMORY` header.

Token cost: negligible for a well-maintained memory file (target < 500 tokens).

Updating: the agent can write to `memory.md` via `run_bash_command` (already
sandboxed by firejail whitelist). The operator can edit it directly.

New settings:

  settings_boolean: MEMORY_INJECT_ENABLED = 1
  settings_values:  MEMORY_FILE_PATH = "memory.md"
  settings_values:  MEMORY_MAX_CHARS = "2000"   (hard truncation at injection)

#### Tier 1 File Touch Points

  db.py       — assemble_system_prompt(): read memory.md and inject if enabled
  db_seed.py  — seed MEMORY_INJECT_ENABLED, MEMORY_FILE_PATH, MEMORY_MAX_CHARS
  agent.py    — no changes required (prompt is pre-assembled before agent sees it)
  memory.md   — create empty stub at project root

### Tier 2 — `memory` Table in SQLite (Medium Term)

Replace the flat file with a DB table for structured querying and agent-writable
records without filesystem writes:

  memory (
      id           INTEGER PRIMARY KEY AUTOINCREMENT,
      memory_key   TEXT    NOT NULL,          -- short label, e.g. "operator_name"
      memory_value TEXT    NOT NULL,          -- the fact
      memory_scope TEXT    NOT NULL DEFAULT 'session',  -- 'session' | 'persistent'
      memory_date  DATETIME NOT NULL,
      memory_enabled INTEGER DEFAULT 1
  )

Agent-callable functions:

  write_memory(key, value)  — upsert a persistent memory row
  read_memory(key)          — retrieve a specific memory value
  list_memories()           — return all enabled persistent memories

At prompt assembly, all `memory_scope='persistent'` and `memory_enabled=1` rows
are serialised and injected. Session-scope rows are injected only for the
current session identifier (future: SESSION_ID in settings_values).

This tier requires a new table and three new seeded functions. No external
dependencies. Still no vector search.

#### Tier 2 File Touch Points

  db_seed.py      — create memory table, seed write_memory / read_memory / list_memories
  db_functions.py — add three memory functions to SEED_FUNCTIONS
  db.py           — assemble_system_prompt(): query memory table instead of file
  CLAUDE.md       — update memory section

### Tier 3 — Vector Memory via ChromaDB (Long Term, Hardware-Gated)

ChromaDB is already installed (`chromadb version 1.5.6`). This tier is planned
but not prioritised until Tier 1 or 2 is operational and a clear retrieval need
emerges.

Architecture:

  - A `chroma_store/` subdirectory at the project root (whitelisted in firejail)
  - Each agent turn's FINAL: answer is embedded and stored with a timestamp
  - At session start, the top-K most semantically similar memories to the
    current user input are retrieved and injected into the prompt
  - Embedding model: a small local model (e.g. nomic-embed-text via Ollama)
    or sentence-transformers on CPU

Token cost: variable — top-K retrieval results (K=3 default) injected as a
`## RELEVANT MEMORIES` block. Target < 300 tokens per retrieval.

Gate condition: do not implement until:
  1. Tier 1 memory is operational and its limits are felt in practice
  2. A retrieval use case exists that flat key/value lookup cannot serve
  3. The embedding latency is benchmarked and acceptable on this hardware

#### Tier 3 File Touch Points (when gated)

  New file: memory_chroma.py   — vector store interface (keep it isolated)
  agent.py  — _agent_turn(): inject retrieved memories pre-call
  db_seed.py — seed MEMORY_VECTOR_ENABLED, MEMORY_VECTOR_K, CHROMA_STORE_PATH
  CLAUDE.md — update memory architecture section

---

## Implementation Order (Recommended)

  Phase 1 (next sprint):
    - BLOCK 2: harness_type column + 7 dispatch blocklist rules (low risk, high value)
    - BLOCK 3 Tier 1: memory.md flat file injection (trivial, immediate payoff)
    - BLOCK 1a/1b: token budget guard + window slider (add the settings, wire the check)

  Phase 2 (when Phase 1 is stable):
    - BLOCK 3 Tier 2: memory SQLite table + write/read/list functions
    - BLOCK 1c: summarisation (only if large outputs become a real problem)
    - logs table write_log() wiring across agent.py / index.py / evolve.py

  Phase 3 (hardware-gated / future):
    - BLOCK 3 Tier 3: ChromaDB vector memory
    - watchdog.py supervisor daemon
    - db_admin.py operator CLI

---

## Open Issues (Carried Forward from Current State)

  1. Cosmetic thought-bleed in loop_interactive (print after thought extraction)
  2. No Ollama routing despite endpoint being seeded in model_profiles
  3. _extract_tool_call silent fallback — no visibility on extraction failures
  4. No migration path for pre-existing DBs lacking newer tables
  5. Logging discipline absent despite logs table existing in schema
  6. Stagger timers are ephemeral — lost on process exit, no recovery path
