# CodingAgent — Core Principles

This document records the foundational design constraints and goals for this system.
It is the authoritative reference for any architectural decisions.

---

## 1. Model Compatibility

- The system must work with **local models** (Qwen, Gemma, Mistral via LM Studio or Ollama)
  **and** frontier models (Claude, GPT-4, Gemini) using the same codebase.
- Local/offline operation is first-class — the system must be fully functional with no internet access.
- Model capability is expressed via **tiers** (NANO / SMALL / MEDIUM / FRONTIER / REASONING).
  Guardrails, tool availability, and context budgets adapt to the tier; the agent never hard-codes
  a specific model name in logic.
- Tool calling falls back to plaintext/JSON parsing when native function-calling is unsupported
  (required for many local models).
- Thinking/reasoning tokens are opt-in per provider; disabled by default for local models.

## 2. Architectural Simplicity

- Prefer functions over classes. Classes are justified only when shared mutable state is required
  across multiple callers.
- No unnecessary abstraction layers. If a feature can be expressed as a module-level function
  called directly, prefer that over a class hierarchy.
- Avoid framework lock-in. The orchestration graph (LangGraph) is a tool, not the architecture.
  Business logic must not bleed into graph node implementations.
- The TUI (Textual) must remain swappable. All core logic lives in `src/`; `tui/` is a pure
  presentation layer that communicates exclusively via the EventBus.
- Dependencies come from **uv**. All runtime dependencies are declared in `pyproject.toml` and
  locked via `uv.lock`. No undeclared implicit imports.

## 3. OS Agnosticism

- All file paths use `pathlib.Path`. No raw string path concatenation.
- Platform-conditional code is isolated to `src/core/paths.py` and clearly labelled.
- The system runs on macOS, Linux, and Windows without modification.
- GPU / hardware detection falls back gracefully on all three platforms (no silent degradation).
- Shell execution uses `sh` on Unix, `cmd` on Windows. No hardcoded `/bin/bash`.

## 4. Guardrails and Workflows

- Five-gate permission architecture (plan-mode → explore-mode → policy → level → interactive)
  is the canonical permission model. No gate may be bypassed silently.
- Plan mode blocks all write tools until the user explicitly approves the plan. This is the
  primary safety mechanism for smaller/less reliable models.
- Bash commands are classified (BLOCKED / DANGEROUS / SAFE / WORKSPACE_WRITE) before execution.
  BLOCKED patterns are never executed regardless of user approval.
- Loop guards (doom-loop, cooldown, read-before-write) are always active. They are the last
  line of defence against runaway model behaviour.
- Interactive approval gates use `AsyncGate` with a 120 s timeout. Approval state is persisted
  to `PermissionTable` so "allow always" decisions survive session restarts.
- Rollback snapshots are taken before every write operation. Rollback is automatic on
  verification failure.

## 5. Memory and Context Retention

- The canonical context directory is `.codingAgent/` (project-local). Legacy directories
  (`.agent-context/`, `.localAgent/`, `.agent/`) are read-only fallbacks and will be migrated
  away from over time.
- Session storage defaults to SQLite (WAL mode) with a JSONL fallback. The backend is
  configurable via `CODING_AGENT_STORAGE_BACKEND`.
- Context compaction uses **tail-preserving compaction**: the most recent N messages are kept
  verbatim; older history is summarised. The resume instruction tells the model to continue
  without acknowledging the summary.
- File summaries, repo index, and vector embeddings (LanceDB) are stored in `.codingAgent/`
  and provide persistent project-level understanding across sessions.
- Cross-session global memory is stored at `~/.coding_agent/memory.md`.

## 6. Learning Loop

- `TrajectoryLogger` records complete agent runs (task → plan → tool sequence → outcome) as
  JSON files in `.agent-context/trajectories/`.
- `MistakeMemory` and `DecisionMemory` store past failures and decisions for retrieval.
- **The learning loop must be closed**: trajectory data must be queried at prompt construction
  time to inject relevant past mistakes into the system prompt. This is currently a gap.
- Named failure scenarios (`RecoveryRecipe`) encode structured recovery actions for known error
  classes. These are a compiled-in form of learned behaviour.
- The execution trace (`execution_trace.json`) and doom-loop guard together form a runtime
  feedback signal that modifies agent behaviour in the current session.

## 7. TUI Usability Standards

- The TUI must be on par with Claude Code / Aider / OpenCode in usability.
- Required affordances: slash commands, `@file` token expansion, inline permission approval,
  diff viewer, thinking panel, token budget indicator, session list, subagent progress.
- Keyboard shortcuts must be consistent and documented in `/help`.
- The TUI communicates with the orchestrator exclusively via the typed EventBus.
  No direct imports from `src.core.*` in TUI widget code.
- All long-running operations are non-blocking. The UI never freezes.

## 8. Offline-First

- The system starts and operates fully without internet access when configured to use a local
  provider (LM Studio, Ollama).
- No dependency on cloud services for core functionality. Cloud providers (Groq, GitHub Copilot,
  Anthropic) are opt-in.
- Hardware capability detection runs offline and selects the appropriate model tier.
- LanceDB vector store and LSP integration operate locally.

---

## Resolved Gaps

| # | Gap | Resolved in |
|---|-----|-------------|
| G1 | Learning loop not closed — `MistakeMemory`/`TrajectoryLogger` data not injected into prompts | `6bc3e24`, `2f753a1` — SQLite FTS5 `mistakes` table; `<past_mistakes>` block in `build_prompt()`; auto-promotion in `add_error`; wired in `debug_node` and `tool_execution_pipeline` |
| G2 | `uv.lock` missing — builds are not reproducible | `83cb799` — migrated to `uv`; `uv.lock` (84 packages) committed |
| G3 | `python = ">=3.11,<3.12"` upper bound too tight — must support 3.12+ | `83cb799` — `requires-python = ">=3.11"` (no upper bound) |
| G4 | `anthropic`, `lancedb`, `tiktoken` not declared as dependencies — silent import failures | `83cb799` — optional extras `[anthropic]`, `[vector]`, `[tokenizer]` in `pyproject.toml` |
| G5 | Legacy context dirs (`.agent-context/`, `.localAgent/`, `.agent/`) still written to | `b787708` — `agent_context_path()` is sole resolver; all fallbacks removed across 30+ files |

## Open Gaps (as of 2026-05-02)

These are known gaps against the principles above, prioritised for implementation:

| # | Gap | Priority |
|---|-----|----------|
| G6 | ~~Windows GPU detection missing in `hardware_capability_profile.py`~~ **Resolved** `a46f756` — `_detect_vram_windows()`, `GlobalMemoryStatusEx` | P2 |
| G7 | No container/namespace sandbox — bash_security patterns are the only barrier | P2 |
| G8 | WebSocket session endpoint not implemented | P2 |
| G9 | MCP tool registry integration with orchestrator routing incomplete | P2 |
| G10 | Evaluation suite minimal — no golden-set regression, no pass@k | P2 |
| G11 | ~~`bash_security.py` patterns assume Unix shell syntax — no PowerShell equivalent~~ **Resolved** `a46f756` — `analyze_powershell_command()` + `_PS_BLOCKED_PATTERNS` | P3 |
| G12 | Streaming diff preview (character-by-character) not implemented | P3 |
