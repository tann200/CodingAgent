# CodingAgent Improvement Plan — OpenClaw Analysis

> **Sources:** `build-your-own-openclaw` tutorial series, `docs/gap-analysis-opencode-vs-codingagent-v2.md`, `docs/CODEBASE_FINDINGS.md`
> **Generated:** 2026-04-18
> **Prerequisite reading:** `docs/IMPLEMENTATION_TASKS.md` (already-completed items), `docs/CODEBASE_FINDINGS.md`

---

## Overview

Six phases ordered by risk and dependency. Complete Phase 0 before starting any other phase —
those are correctness bugs that can mask problems in every subsequent change. Each task has a
unique ID, affected files, step-by-step implementation notes, acceptance criteria, and test
requirements.

| Phase | Theme | Tasks | Effort |
|-------|-------|-------|--------|
| 0 | Critical Security & Correctness | 13 | ~8 days |
| 1 | Reliability Hardening | 3 | ~6 days |
| 2 | TUI Improvements | 9 | ~12 days |
| 3 | Permission System | 3 | ~7 days |
| 4 | Session Commands & UX | 5 | ~5 days |
| 5 | Operational Maturity | 4 | ~8 days |
| 6 | Architecture & Platform | 4 | ~14 days |

**Total estimated effort: ~60 developer-days**

---

## Phase 0 — Critical Security & Correctness

All items in this phase are sourced from `docs/CODEBASE_FINDINGS.md`. They represent bugs,
security holes, or race conditions that are already present and need fixing before any new
feature work adds more surface area.

---

### TASK-SEC-1 — Fix invalid bwrap flags in sandbox.py *(CRIT-1)*

**Summary:** The bubblewrap sandbox never activates because the flag arguments passed to `bwrap`
are syntactically wrong. This means all bash commands run unsandboxed regardless of the
`ENABLE_SANDBOX` setting.

**Why it matters:** Every bash tool call that should be isolated is running with full host
permissions. This is the most critical security gap in the codebase.

**Affected files:**
- `src/tools/sandbox.py:63` — invalid bwrap flag construction

**Implementation steps:**
1. Read the current `sandbox.py` bwrap invocation. Identify which flags are wrong
   (likely missing `--` separator, wrong binding syntax, or missing `--proc /proc`).
2. Consult `man bwrap` for correct invocation:
   ```
   bwrap --ro-bind /usr /usr --ro-bind /lib /lib --ro-bind /lib64 /lib64
         --ro-bind /bin /bin --proc /proc --dev /dev
         --bind {workdir} {workdir} --chdir {workdir}
         -- {cmd}
   ```
3. Fix the flag array. Add `--die-with-parent` to ensure sandbox subprocess dies if parent dies.
4. Add a `_probe_bwrap()` function that runs `bwrap --version` on import; if it fails, set
   `BWRAP_AVAILABLE = False` and log a warning rather than silently skipping.
5. Add a startup warning event (`system.warning`) when `ENABLE_SANDBOX=true` but
   `BWRAP_AVAILABLE=False`.

**Acceptance criteria:**
- `bwrap --version` is called on module load; result cached in `BWRAP_AVAILABLE`
- A simple sandboxed `echo hello` succeeds end-to-end
- `--die-with-parent` is present in every invocation
- `BWRAP_AVAILABLE=False` produces a `system.warning` event, not a silent no-op

**Tests:** `tests/unit/test_sandbox.py` — add `test_bwrap_flags_valid`, `test_probe_bwrap_missing`,
`test_sandbox_dies_with_parent`.

---

### TASK-SEC-2 — Add locks to file_tools.py preview globals *(HIGH-1)*

**Summary:** `_pending_previews` and `_preview_rejected` are module-level dicts/sets in
`src/tools/file_tools.py` that are read and written from multiple async contexts without any
locking. Concurrent tool calls can corrupt them silently.

**Affected files:**
- `src/tools/file_tools.py:73-74`

**Implementation steps:**
1. Replace the bare `dict` / `set` with thread-safe equivalents:
   ```python
   import threading
   _preview_lock = threading.Lock()
   _pending_previews: dict[str, str] = {}
   _preview_rejected: set[str] = set()
   ```
2. Wrap every read-modify-write of these variables in `with _preview_lock:`.
3. Alternatively, move this state into `PreviewCoordinator`
   (`src/core/orchestration/preview_coordinator.py`) which already exists and has its own
   lock. Prefer this approach to eliminate the duplicate state.
4. Update `write_file`, `edit_file`, `apply_patch`, and any preview-check helpers to go
   through `PreviewCoordinator`.

**Acceptance criteria:**
- No bare access to `_pending_previews` / `_preview_rejected` outside a lock or coordinator
- `PreviewCoordinator` is the single source of truth for preview state

**Tests:** `tests/unit/test_file_tools_concurrency.py` — launch 10 concurrent `write_file`
calls, assert no `KeyError` / `RuntimeError` due to dict size change during iteration.

---

### TASK-SEC-3 — Fix SSRF blocklist for IPv6 and decimal IPs *(HIGH-2)*

**Summary:** The SSRF blocklist in `src/tools/web_tools.py` only blocks `127.0.0.1` and
`localhost` by string match. IPv6 loopback (`::1`, `::ffff:127.0.0.1`), decimal-encoded IPs
(`2130706433` = `127.0.0.1`), and CNAME chains to internal hosts all bypass the check.

**Affected files:**
- `src/tools/web_tools.py:18-22`

**Implementation steps:**
1. Replace string-match blocklist with IP resolution + range check:
   ```python
   import ipaddress, socket

   _BLOCKED_RANGES = [
       ipaddress.ip_network("127.0.0.0/8"),    # loopback
       ipaddress.ip_network("10.0.0.0/8"),     # private
       ipaddress.ip_network("172.16.0.0/12"),  # private
       ipaddress.ip_network("192.168.0.0/16"), # private
       ipaddress.ip_network("169.254.0.0/16"), # link-local
       ipaddress.ip_network("::1/128"),        # IPv6 loopback
       ipaddress.ip_network("fc00::/7"),       # IPv6 private
   ]

   def _is_ssrf_blocked(url: str) -> bool:
       host = urllib.parse.urlparse(url).hostname
       try:
           addrs = socket.getaddrinfo(host, None)
       except socket.gaierror:
           return False
       for _, _, _, _, sockaddr in addrs:
           ip = ipaddress.ip_address(sockaddr[0])
           if any(ip in net for net in _BLOCKED_RANGES):
               return True
       return False
   ```
2. Call `_is_ssrf_blocked(url)` before every `httpx.get()` / `httpx.post()` in `web_tools.py`.
3. Raise `PermissionError("SSRF blocked: {host}")` if blocked — this maps to a tool error
   returned to the LLM.
4. Add an allowlist override via config (`web.ssrf_allowlist: ["internal.corp.example.com"]`)
   for teams running the agent inside a corporate network.

**Acceptance criteria:**
- `::1`, `::ffff:127.0.0.1`, `2130706433`, `10.0.0.1`, `169.254.169.254` (AWS metadata)
  all raise `PermissionError`
- Legitimate external URLs pass without error
- Allowlist override works

**Tests:** `tests/unit/test_web_tools_ssrf.py` — parametrize over all blocked IP forms +
allowlist bypass.

---

### TASK-SEC-4 — Fix delegation depth tracking (remove env-based counter) *(HIGH-3, HIGH-9)*

STATUS: Completed — production code no longer relies on a process-global environment
variable for delegation depth. The repo now uses two safe mechanisms:

- ContextVar (in-process, non-forgeable) for runtime nesting: `_DELEGATION_DEPTH_VAR` in
  `src/tools/subagent_tools.py`.
- AgentState["delegation_depth"] for per-graph / cross-session propagation: the parent graph
  sets `initial_state["delegation_depth"] = parent_depth + 1` when spawning children.

**What was changed / verified:**
1. Removed production reads/writes of `CODINGAGENT_DELEGATION_DEPTH` / `AGENT_DELEGATION_DEPTH`.
2. Added/verified `_DELEGATION_DEPTH_VAR: ContextVar[int]` usage for in-process nesting and
   ensured thread propagation uses `contextvars.copy_context()` where worker threads are used.
3. `delegation_node.py` reads `state.get("delegation_depth")` at spawn time and refuses to spawn
   when the value is at-or-above the configured max depth.
4. Tests that previously patched `os.environ` were updated to set the ContextVar or to pass
   `delegation_depth` on the AgentState used by tests. New unit tests cover propagation,
   enforcement, and concurrency.

**Acceptance criteria (validated):**
1. Grep for `CODINGAGENT_DELEGATION_DEPTH|AGENT_DELEGATION_DEPTH` in `src/` returns only
   non-runtime mentions (docs or explicit scrubbing code in hook runners). Hook runners that
   build subprocess env dicts scrub these keys before launching.
2. In-process nesting is tracked via ContextVar only; thread/executor usage propagates the
   ContextVar explicitly when needed.
3. Cross-session propagation uses AgentState["delegation_depth"] and `delegation_node` enforces
   the depth limit on spawn.
4. Tests updated and added to validate behavior.

**Notes / next steps:**
- `docs/IMPROVEMENT_PLAN_OPENCLAW.md` and `docs/CODEBASE_FINDINGS.md` were updated to reflect the
  new mechanism. The remaining references to the old env-vars in docs were converted to
  historical notes; the hook runners (`tool_hooks.py` and `shell_hooks.py`) still scrub the
  env keys before launching subprocesses (this scrub is intentional and safe).

---

### TASK-SEC-5 — Add locks to approval_gate.py shared state *(HIGH-4)*

**Summary:** `src/core/orchestration/approval_gate.py:55-59` uses shared dicts and sets
(pending approvals, approved IDs) that are accessed from both asyncio tasks and threads without
locking. Under concurrent tool calls this can raise `RuntimeError: dictionary changed size`.

**Affected files:**
- `src/core/orchestration/approval_gate.py:55-59`

**Implementation steps:**
1. Add `threading.Lock` to `ApprovalGate.__init__`:
   ```python
   self._lock = threading.Lock()
   ```
2. Wrap every access to `self._pending`, `self._approved`, `self._denied` with
   `with self._lock:`.
3. For async callers that need to `await` approval, use `asyncio.Event` per request ID; the
   lock only guards the dict mutations, not the await.
4. Pattern:
   ```python
   async def request_approval(self, request_id: str, ...) -> bool:
       event = asyncio.Event()
       with self._lock:
           self._pending[request_id] = event
       approved = await event.wait()  # no lock held during wait
       with self._lock:
           del self._pending[request_id]
       return approved
   ```

**Acceptance criteria:**
- No `RuntimeError: dictionary changed size` under concurrent approval requests
- Approvals and denials resolve their `asyncio.Event` correctly

**Tests:** `tests/unit/test_approval_gate_concurrency.py` — 20 concurrent approval requests,
mix of approvals/denials, assert all resolve without error.

---

### TASK-SEC-6 — Fix workspace_guard absolute path bypass *(HIGH-5)*

**Summary:** `src/core/orchestration/workspace_guard.py:62` checks that paths don't leave the
working directory, but the check is bypassed for absolute paths: if the tool passes
`/etc/passwd` directly, the guard's `path.startswith(workdir)` string check fails to catch it
if `workdir` ends with `/` or if symlinks are involved.

**Affected files:**
- `src/core/orchestration/workspace_guard.py:62`

**Implementation steps:**
1. Replace `str.startswith` with `Path.resolve()` comparison:
   ```python
   def is_within_workdir(self, path: str | Path) -> bool:
       resolved = Path(path).resolve()
       workdir = self.workdir.resolve()
       try:
           resolved.relative_to(workdir)
           return True
       except ValueError:
           return False
   ```
2. Apply this check before *every* file operation in the guard, including absolute paths.
3. Add explicit handling for symlinks: `Path.resolve()` follows symlinks, so a symlink
   pointing outside the workdir is correctly blocked.
4. Ensure the guard is called by `read_file`, `write_file`, `edit_file`, `bash`
   (for the working directory, not script content), and `glob_tool`.

**Acceptance criteria:**
- `/etc/passwd` is blocked
- `workdir/../../../etc/passwd` is blocked
- Symlinks pointing outside workdir are blocked
- Paths within workdir (including absolute paths within it) are allowed

**Tests:** `tests/unit/test_workspace_guard.py` — add parametrize over absolute, traversal,
and symlink cases.

---

### TASK-SEC-7 — Fix AB-BA deadlock in agent_session_manager *(HIGH-6)*

**Summary:** `src/core/orchestration/agent_session_manager.py:186-192` acquires two locks in
one order in some code paths and the reverse order in another path, creating a potential
deadlock if two threads hit the crossing paths concurrently.

**Affected files:**
- `src/core/orchestration/agent_session_manager.py:186-192`

**Implementation steps:**
1. Read the two lock acquisition sites. Identify the lock objects (likely `_session_lock` and
   `_registry_lock`).
2. Establish a canonical acquisition order: always acquire `_registry_lock` before
   `_session_lock`. Document this in a comment above the lock declarations.
3. Refactor both code paths to follow this order. If one path currently acquires
   `_session_lock` first, restructure it to acquire `_registry_lock` first (may require
   reading the registry value before entering the session lock scope).
4. Add a `_assert_lock_order()` debug helper (no-op in production, active in tests) that
   verifies no thread holds `_session_lock` when trying to acquire `_registry_lock`.

**Acceptance criteria:**
- Code review shows all lock acquisition sites follow the same order
- No `_session_lock` acquired while `_registry_lock` is not held
- `_assert_lock_order` passes in all unit tests

**Tests:** `tests/unit/test_session_manager_locks.py` — use `threading.Thread` to simulate the
crossing paths concurrently; verify no deadlock within 5s timeout.

---

### TASK-SEC-8 — Fix asyncio.run() inside running event loop *(HIGH-7)*

**Summary:** `src/core/orchestration/mcp_stdio_server.py:358` calls `asyncio.run()` inside a
function that may be called from an already-running event loop, which raises
`RuntimeError: This event loop is already running`.

**Affected files:**
- `src/core/orchestration/mcp_stdio_server.py:358`

**Implementation steps:**
1. Identify what `asyncio.run()` is wrapping at line 358.
2. Replace with one of:
   - `await` the coroutine directly if the caller is already async
   - `asyncio.get_event_loop().run_until_complete()` if it must be sync — but first check
     if there's a running loop with `asyncio.get_event_loop().is_running()`
   - Better: use `asyncio.ensure_future()` + `loop.run_until_complete()` from a non-async
     entry point only
3. The preferred fix is to make the surrounding function `async def` and `await` the
   coroutine, then ensure callers use `await` too.
4. Check for any other `asyncio.run()` calls in the same file and apply the same fix.

**Acceptance criteria:**
- No `asyncio.run()` call in `mcp_stdio_server.py`
- MCP server starts and handles at least one tool call in a test

**Tests:** `tests/unit/test_mcp_stdio_server.py` — add test that instantiates the server
inside a running event loop (use `asyncio.run()` at the test level with the server inside).

---

### TASK-SEC-9 — Fix shell=True with tool_hooks *(HIGH-8)*

**Summary:** `src/core/orchestration/tool_hooks.py:177-184` uses `subprocess.run(shell=True)`
with a command string that comes from the project config (`tool_hooks` entries). An attacker
who can write to the project config file (or inject a malicious `.agent-context/config.yaml`)
can execute arbitrary shell commands.

**Affected files:**
- `src/core/orchestration/tool_hooks.py:177-184`

**Implementation steps:**
1. Replace `shell=True` with a list-based invocation:
   ```python
   import shlex
   cmd_parts = shlex.split(hook_command)
   result = subprocess.run(cmd_parts, shell=False, ...)
   ```
2. Add a validation step when hooks are loaded from config: each hook command must pass
   `BashRiskLevel` analysis (from `src/tools/bash_security.py`). Commands classified as
   `BLOCKED` must be rejected with a `system.warning` event at load time, not at execution.
3. Restrict the set of allowed executables to a configurable allowlist
   (`tool_hooks.allowed_executables`). Default: `["sh", "bash", "python", "python3", "node"]`.
4. Log each hook execution with correlation ID and command hash (not full command, to avoid
   leaking secrets) to the audit trail.

**Acceptance criteria:**
- `shell=False` in all subprocess calls within `tool_hooks.py`
- Hooks with blocked patterns are rejected at load time, not execution
- Allowlist is enforced and logged

**Tests:** `tests/unit/test_tool_hooks_security.py` — blocked pattern in hook command raises
at load; shell metacharacters in hook command don't execute additional commands.

---

### TASK-SEC-10 — Fix execution_node overwriting state["task"] *(HIGH-10)*

**Summary:** `src/core/orchestration/graph/nodes/execution_node.py:876-910` — the
read-before-write path updates `state["task"]` with the content of the file just read, which
overwrites the original task description. If the read-before-write triggers mid-task (e.g., on
retry), the agent loses the original task context.

**Affected files:**
- `src/core/orchestration/graph/nodes/execution_node.py:876-910`

**Implementation steps:**
1. Read the specific code block. Identify the `state["task"] = ...` assignment.
2. The fix is to store the read content in a separate key — e.g., `state["rbw_content"]`
   or pass it through `state["planned_action"]["context"]` — rather than overwriting `task`.
3. Ensure the original `state["task"]` (the user's task description) is never modified
   inside execution_node or any node other than `start_new_task()`.
4. Add a test that verifies `state["task"]` after a read-before-write retry equals the
   original task string.

**Acceptance criteria:**
- `state["task"]` is never modified inside `execution_node`
- The file content from the read-before-write is available to the LLM via a dedicated
  state key or action context

**Tests:** `tests/unit/test_execution_node_rbw.py` — simulate read-before-write retry, assert
`state["task"]` unchanged.

---

### TASK-SEC-11 — Fix asyncio.run() in core_bridge background thread *(HIGH-11)*

**Summary:** `tui/src/ui/core_bridge.py:857` calls `asyncio.run()` on a background thread that
is created by Textual's thread scheduler. Textual already runs an asyncio event loop; calling
`asyncio.run()` on any thread that interacts with Textual internals can cause silent failures
or `RuntimeError`.

**Affected files:**
- `tui/src/ui/core_bridge.py:857`

**Implementation steps:**
1. Identify what coroutine is being wrapped in `asyncio.run()` at line 857.
2. Replace with `asyncio.run_coroutine_threadsafe(coro, loop)` where `loop` is
   the Textual app's event loop (accessible via `self.app._loop` or passed explicitly):
   ```python
   future = asyncio.run_coroutine_threadsafe(coro, self._loop)
   result = future.result(timeout=30)
   ```
3. If the coroutine needs to be fire-and-forget (no result needed), use
   `self.app.call_from_thread(async_fn, *args)` which is Textual's safe cross-thread call.
4. Audit the rest of `core_bridge.py` for any other `asyncio.run()` calls and apply the same fix.

**Acceptance criteria:**
- No `asyncio.run()` in `core_bridge.py`
- The operation that was using it works correctly in integration (TUI starts and handles
  at least one agent turn)

**Tests:** `tests/unit/test_core_bridge_threading.py` — call the fixed code path from a
non-async thread; assert no `RuntimeError`.

---

### TASK-SEC-12 — Fix Path.cwd() default evaluated at import time *(HIGH-12)*

**Summary:** `src/tools/system_tools.py:184` uses `Path.cwd()` as a default argument value,
which is evaluated once at module import time. If the working directory changes after import
(e.g., in tests), the tool uses a stale path.

**Affected files:**
- `src/tools/system_tools.py:184`

**Implementation steps:**
1. Change the default from `Path.cwd()` to `None`:
   ```python
   def some_tool(path: str | None = None) -> str:
       resolved = Path(path) if path else Path.cwd()
   ```
2. Apply this pattern to every function in `system_tools.py` that has a `Path.cwd()` default.
3. Audit all other tool files for the same pattern (grep for `= Path.cwd()`).

**Acceptance criteria:**
- No `Path.cwd()` in any default parameter value across the `src/tools/` directory
- Calling a tool with `path=None` in a test with changed `cwd` produces the correct path

**Tests:** `tests/unit/test_system_tools_cwd.py` — `os.chdir` before and after import, verify
tool uses runtime cwd.

---

### TASK-MED-1 — Fix selected medium severity issues *(MED-1 through MED-10)*

**Summary:** Address the medium-severity issues from `docs/CODEBASE_FINDINGS.md` in a single
PR to minimize review overhead.

**Affected files and fixes:**

| ID | File | Fix |
|----|------|-----|
| MED-1 | `src/tools/file_tools.py:363` | Skip `+++`/`---` header lines when counting diff line changes |
| MED-2 | `src/tools/batch_tools.py:112` | Cap `ThreadPoolExecutor` at `min(32, os.cpu_count() + 4)` |
| MED-3 | `src/tools/tools_config.py` | Add `threading.Lock` around module-global writes |
| MED-4 | `src/core/orchestration/orchestrator.py:2083-2121` | Use `contextlib.ExitStack` or try/finally to ensure temp file cleanup |
| MED-5 | `src/core/orchestration/orchestrator.py:3524` | Reuse `ThreadPoolExecutor` across `run_agent_once` calls (create in `__init__`) |
| MED-6 | `src/core/orchestration/orchestrator.py:2793` | Change `>= MAX` to `> MAX - 1` (or use `> MAX`) to fix off-by-one |
| MED-7 | `src/core/orchestration/cross_session_bus.py:434-466` | Copy subscriber list before iterating; release lock before calling callbacks |
| MED-8 | `src/core/orchestration/event_log.py:198` | Replace `assert` with `if not condition: raise ValueError(...)` |
| MED-9 | `src/core/orchestration/file_lock_manager.py:62-68` | Serialize `can_write`/`can_read` checks with the same lock used by async mutators |
| MED-10 | `src/core/orchestration/mcp_stdio_server.py:282` | Add `max_depth=10` and `max_files=1000` guards to `rglob` call |

**Acceptance criteria:** Each fix has a dedicated test asserting the specific scenario no
longer fails. All 10 fixes land in a single atomic commit.

---

## Phase 1 — Reliability Hardening

Phase 1 builds on Phase 0. These are new capabilities (not bug fixes) that improve the
system's resilience to crashes, concurrent access, and unexpected failures.

---

### TASK-REL-1 — Error Classification System

**Summary:** All exceptions are currently caught and logged as strings. There is no structured
error taxonomy, making it impossible for callers to distinguish a permission error from an LLM
timeout or a plan hallucination. This blocks precise retry logic, UI messaging, and audit
analysis.

**Affected files:**
- `src/core/errors.py` — expand `ErrorCode`, add `classify_exception()`
- `src/core/orchestration/tool_execution_pipeline.py` — map exceptions to `AgentError`
- `src/core/orchestration/graph/nodes/` — each node's `except` blocks
- `src/server/app.py` — include `error_code` in SSE/WebSocket error events
- `tui/src/ui/app.py` — render error code in the UI with human-readable label

**Implementation steps:**
1. Expand `ErrorCode` in `src/core/errors.py` to cover all failure categories:
   ```python
   class ErrorCode(str, Enum):
       # Tool execution
       TOOL_PERMISSION_DENIED     = "tool.permission_denied"
       TOOL_TIMEOUT               = "tool.timeout"
       TOOL_SANDBOX_BLOCKED       = "tool.sandbox_blocked"
       TOOL_WORKSPACE_ESCAPE      = "tool.workspace_escape"
       TOOL_SSRF_BLOCKED          = "tool.ssrf_blocked"
       TOOL_NOT_FOUND             = "tool.not_found"
       # LLM / inference
       LLM_CONTEXT_OVERFLOW       = "llm.context_overflow"
       LLM_RATE_LIMITED           = "llm.rate_limited"
       LLM_CONNECTION_FAILED      = "llm.connection_failed"
       LLM_INVALID_RESPONSE       = "llm.invalid_response"
       # Planning
       PLAN_INVALID               = "plan.invalid"
       PLAN_MAX_RETRIES_EXCEEDED  = "plan.max_retries"
       PLAN_NO_TOOLS_MATCH        = "plan.no_tools"
       # Memory
       MEMORY_INJECTION_BLOCKED   = "memory.injection_blocked"
       MEMORY_WRITE_CONFLICT      = "memory.write_conflict"
       MEMORY_COMPACTION_FAILED   = "memory.compaction_failed"
       # Delegation
       DELEGATION_DEPTH_EXCEEDED  = "delegation.depth_exceeded"
       DELEGATION_TIMEOUT         = "delegation.timeout"
       # System
       UNKNOWN                    = "system.unknown"
   ```
2. Add `classify_exception(exc: Exception) -> ErrorCode` that inspects exception type and
   message to return the appropriate code. Use a priority-ordered chain of `isinstance` checks.
3. In `tool_execution_pipeline.py`, catch exceptions at the outermost handler and convert to
   `AgentError(code=classify_exception(exc), ...)` before returning a tool result.
4. In each graph node, replace bare `except Exception as e: logger.error(e)` with:
   ```python
   except Exception as e:
       code = classify_exception(e)
       state["last_error_code"] = code.value
       publish(AgentErrorEvent(error_code=code, message=str(e)))
       raise
   ```
5. Add `last_error_code: Optional[str]` to `AgentState`.
6. In `src/server/app.py`, include `error_code` in all error SSE events.
7. In `tui/src/ui/app.py`, render `error_code` in the error notice widget with a
   human-readable label (e.g., `"llm.rate_limited"` → `"Rate limited — retrying"`).

**Acceptance criteria:**
- Every `except Exception` block in `src/core/` produces a typed `ErrorCode`
- `state["last_error_code"]` is set on any node failure
- SSE error events contain `"error_code"` field
- TUI shows human-readable error labels, not raw exception strings
- `classify_exception(ConnectionRefusedError())` returns `LLM_CONNECTION_FAILED`

**Tests:** `tests/unit/test_error_classification.py` — 20+ parametrized cases; each exception
type maps to the expected `ErrorCode`. Integration: full tool pipeline failure surfaced to SSE.

---

### TASK-REL-2 — File Locking on Memory Files

**Summary:** `memory.md` and the future `user.md` (see TASK-OPS-3) are plain text files written
from potentially concurrent async tasks. Without OS-level file locking, concurrent writes can
interleave and corrupt the file.

**Affected files:**
- New: `src/core/memory/file_lock.py`
- `src/core/orchestration/orchestrator.py` — memory save/load calls
- `src/core/memory/session_store.py`
- `src/tools/memory_tools.py`

**Implementation steps:**
1. Create `src/core/memory/file_lock.py`:
   ```python
   import fcntl, threading
   from contextlib import contextmanager
   from pathlib import Path

   # One lock object per path to avoid inter-thread contention
   _locks: dict[str, threading.Lock] = {}
   _meta_lock = threading.Lock()

   def _get_lock(path: Path) -> threading.Lock:
       key = str(path.resolve())
       with _meta_lock:
           if key not in _locks:
               _locks[key] = threading.Lock()
           return _locks[key]

   @contextmanager
   def locked_file(path: Path, mode: str = "r+"):
       lock = _get_lock(path)
       with lock:
           with open(path, mode) as fh:
               if "w" in mode or "a" in mode:
                   fcntl.flock(fh, fcntl.LOCK_EX)
               else:
                   fcntl.flock(fh, fcntl.LOCK_SH)
               try:
                   yield fh
               finally:
                   fcntl.flock(fh, fcntl.LOCK_UN)
   ```
2. Replace every `memory_path.write_text(...)` and `memory_path.read_text()` in
   `orchestrator.py`, `session_store.py`, and `memory_tools.py` with `locked_file()`.
3. For Windows compatibility, add a `try/except ImportError` that falls back to
   `msvcrt.locking` or a pure-Python threading lock (since `fcntl` is Unix-only).
4. The `AutoCompactor` and `Distiller` that read memory content should also use `locked_file`
   for their reads.

**Acceptance criteria:**
- No bare `memory_path.write_text()` without a lock
- Two threads writing to the same memory file interleave cleanly (one waits for the other)
- Works on macOS, Linux, and Windows (graceful fallback for Windows)

**Tests:** `tests/unit/test_file_lock.py` — 10 concurrent writers to the same file; read back
and verify content is one writer's output, not interleaved.

---

### TASK-REL-3 — Event Persistence & Crash Recovery

**Summary:** The `EventBus` is purely in-memory. If the server crashes after the agent
produces a response but before it is delivered to the TUI/client, the event is lost. Adding
persistence and an ACK mechanism allows recovery on restart.

**Affected files:**
- New: `src/core/orchestration/event_persistence.py`
- `src/core/orchestration/event_bus.py`
- `src/server/app.py` — ACK after successful delivery

**Implementation steps:**
1. Create `src/core/orchestration/event_persistence.py`:
   ```python
   PENDING_DIR = Path(".agent-context/events/pending")

   def persist_event(event: dict) -> Path:
       PENDING_DIR.mkdir(parents=True, exist_ok=True)
       ts = int(time.time() * 1000)
       path = PENDING_DIR / f"{ts}_{event.get('correlation_id', 'unknown')}.json"
       tmp = path.with_suffix(".tmp")
       tmp.write_text(json.dumps(event))
       tmp.replace(path)   # atomic
       return path

   def ack_event(path: Path) -> None:
       path.unlink(missing_ok=True)

   def recover_pending() -> list[dict]:
       if not PENDING_DIR.exists():
           return []
       events = []
       for p in sorted(PENDING_DIR.glob("*.json")):
           try:
               data = json.loads(p.read_text())
               data["_recovered"] = True
               data["_recovery_path"] = str(p)
               events.append(data)
           except json.JSONDecodeError:
               p.unlink(missing_ok=True)
       return events
   ```
2. In `EventBus`, call `persist_event(event)` in `publish()` for events with type
   in `PERSISTENT_EVENT_TYPES = {"agent.response", "agent.error", "tool.result"}`.
   Store the returned `path` in the event dict as `_persistence_path`.
3. In `src/server/app.py`, after each event is successfully streamed to a client
   (SSE send completes without error), call `ack_event(Path(event["_persistence_path"]))`.
4. On `EventBus.__init__` (called at server startup), call `recover_pending()` and re-publish
   each recovered event with a `recovered=True` flag. Add a dedup guard: if a recovered event's
   `correlation_id` matches one already in the current session's message history, skip it.

**Acceptance criteria:**
- A `PERSISTENT_EVENT_TYPES` event survives a simulated crash (write file, kill process,
  restart, verify event is re-published)
- ACK deletes the file; unACKed events appear in `recover_pending()`
- Recovered events have `"_recovered": true` in their payload
- Duplicate events from recovery are filtered by correlation ID

**Tests:** `tests/unit/test_event_persistence.py` — persist, crash-simulate (no ACK), recover,
assert re-published; persist + ACK, recover, assert empty; duplicate guard.

---

## Phase 2 — TUI Improvements

All items sourced from `docs/gap-analysis-opencode-vs-codingagent-v2.md`. These improve the
user experience by matching OpenCode's visual fidelity for tool calls and session state.

---

### TASK-TUI-1 — Per-tool inline icons and labels *(GAP-TUI-1)*

**Summary:** Every tool call currently renders as a generic `ToolExecutionNotice`. OpenCode
renders each tool with a distinct icon, pending text, and completed text.

**Affected files:**
- `tui/src/ui/app.py:~800-900` — `handle_tool_call_start` / `handle_tool_call_finish`
- New: `tui/src/ui/components/inline_tool.py`

**Implementation steps:**
1. Define a `TOOL_RENDER_MAP` dict in `inline_tool.py`:
   ```python
   TOOL_RENDER_MAP = {
       "read_file":    ToolRender(icon="→", pending="Reading …",     done="Read {path}"),
       "write_file":   ToolRender(icon="←", pending="Writing …",     done="Write {path}"),
       "edit_file":    ToolRender(icon="←", pending="Editing …",     done="Edit {path}"),
       "bash":         ToolRender(icon="#", pending="{desc}",         done="$ {cmd}"),
       "glob_tool":    ToolRender(icon="✱", pending='Glob "{pat}"',   done='Glob "{pat}" ({n} matches)'),
       "grep_tool":    ToolRender(icon="✱", pending='Grep "{pat}"',   done='Grep "{pat}" ({n} matches)'),
       "delegate_task":ToolRender(icon="│", pending="Delegating…",   done="{agent} Task — {desc}"),
       "manage_todo":  ToolRender(icon="⚙", pending="Updating todos…", done="Todos updated"),
   }
   ```
2. Create `InlineToolWidget(Widget)` that:
   - Shows pending state immediately on `ToolCallStartEvent`
   - Updates to done state on `ToolCallFinishEvent` with extracted args
   - Reads args from the event payload to fill `{path}`, `{cmd}`, etc.
3. In `app.py`, replace the generic notice in `handle_tool_call_start` /
   `handle_tool_call_finish` with `InlineToolWidget`.
4. Fall back to generic notice for unmapped tool names.

**Acceptance criteria:**
- `read_file` shows `→ Reading …` then `→ Read src/foo.py`
- `bash` shows `# description` then `$ command`
- Unknown tools fall back to generic label
- All icons are ASCII-safe (no emoji) for terminal compatibility

**Tests:** `tests/unit/test_inline_tool_widget.py` — each entry in `TOOL_RENDER_MAP` produces
correct pending and done text given a mock event.

---

### TASK-TUI-2 — Inline diff view for Edit/Write *(GAP-TUI-2)*

**Summary:** File change diffs are shown in a separate panel. OpenCode shows them inline
within the tool call widget, with syntax highlighting and LSP diagnostics.

**Affected files:**
- `tui/src/ui/app.py:~920` — `handle_tool_call_finish` for `edit_file`/`write_file`
- `tui/src/ui/components/diff_viewer.py` — expose `InlineDiffWidget`

**Implementation steps:**
1. Add `InlineDiffWidget(Widget)` to `diff_viewer.py`. It takes a unified diff string
   and renders it with Rich `Syntax` (language=`diff`) at a fixed width.
2. In `InlineToolWidget.on_tool_call_finish`, if the tool is `edit_file`/`write_file`/
   `apply_patch` and the event payload contains `diff`, append an `InlineDiffWidget` as
   a child.
3. Add `tui.diff_style: "auto" | "unified"` to config. In `auto` mode, use split view if
   terminal width ≥ 120; otherwise unified. In `unified` mode, always use unified.
4. Below each diff, query the `LSPManager` for diagnostics on the changed file and render
   up to 3 errors/warnings with their line numbers.

**Acceptance criteria:**
- `edit_file` tool call shows diff inline, not in a separate panel
- Diff lines are syntax-highlighted (`+` lines green, `-` lines red)
- LSP diagnostics appear below the diff (if LSP is available and has errors)
- `tui.diff_style` config is respected

**Tests:** `tests/unit/test_inline_diff_widget.py` — render a known diff string, assert
correct line count and color annotations.

---

### TASK-TUI-3 — Bash block with expandable output *(GAP-TUI-3)*

**Summary:** Bash output is currently appended as a streaming `StreamView` with no visual
grouping per-command. OpenCode shows each bash call as a fenced block with a collapsible
output region.

**Affected files:**
- `tui/src/ui/app.py` — replace bash output appending with `BashBlock` widget
- New: `tui/src/ui/components/bash_block.py`

**Implementation steps:**
1. Create `BashBlock(Widget)` with:
   - Header: `# {description}` (muted color, clickable to toggle)
   - Body: `$ {command}` in code style
   - Output: scrollable region, truncated at 40 lines with "… click to expand" hint
   - State: `collapsed: reactive(True)` — expanded on click
2. Wire into `TOOL_RENDER_MAP` (see TASK-TUI-1): `bash` → `BashBlock`.
3. Streaming output lines from `BashOutputEvent` are appended to the `BashBlock`'s
   output region as they arrive.
4. Truncate output at 40 visible lines; track `_full_output` internally for expand.
5. Add click handler on the header to toggle collapsed state.

**Acceptance criteria:**
- Each bash call gets its own `BashBlock`
- Output beyond 40 lines is hidden with expand hint
- Click on header expands/collapses
- Streaming output arrives in real time

**Tests:** `tests/unit/test_bash_block.py` — render with 5 lines (no truncation) and 50 lines
(truncation + hint visible); test toggle.

---

### TASK-TUI-4 — TodoWrite rendered as interactive list *(GAP-TUI-4)*

**Summary:** `manage_todo` tool calls are not visually differentiated. OpenCode shows a full
todo list with status icons.

**Affected files:**
- `tui/src/ui/app.py` — add `manage_todo` render handler
- New: `tui/src/ui/components/todo_list.py`

**Implementation steps:**
1. Create `TodoListWidget(Widget)` that takes a list of todo items and renders each as:
   - `○ {description}` for pending
   - `● {description}` for in_progress (bold)
   - `✓ {description}` for completed (muted)
   - `✗ {description}` for cancelled (strikethrough)
2. On `ToolCallFinishEvent` for `manage_todo`, parse the tool result JSON and extract
   the todo items list; render as `TodoListWidget`.
3. Subsequent `manage_todo` calls update the existing `TodoListWidget` for the session
   rather than creating a new one — find it by `session_id` key.

**Acceptance criteria:**
- `manage_todo` tool result shows a rendered todo list
- Status icons match the four states
- Updates to the same session replace the previous todo widget

**Tests:** `tests/unit/test_todo_list_widget.py` — render all four states; update flow
(two sequential events → widget shows latest state).

---

### TASK-TUI-5 — Question tool Q&A block *(GAP-TUI-5)*

**Summary:** `question_tool` calls render as generic tool notices. OpenCode shows a `# Questions`
block with each Q&A pair.

**Affected files:**
- `tui/src/ui/app.py` — add `question_tool` render handler
- New: `tui/src/ui/components/question_block.py`

**Implementation steps:**
1. Create `QuestionBlock(Widget)` showing:
   - Header: `# Questions`
   - For each item: question in muted color, user answer on next line in normal color
2. On `ToolCallFinishEvent` for `question_tool`, parse result JSON, extract Q&A pairs.
3. If the tool is still pending (no user answer yet), show questions with empty answer
   slots and a waiting indicator.

**Acceptance criteria:**
- Questions display before answers are given
- Answers fill in after the user responds
- Unanswered questions show a waiting indicator

**Tests:** `tests/unit/test_question_block.py` — pending state (questions only) and answered
state (Q&A pairs rendered).

---

### TASK-TUI-6 — Live subagent progress in task tool widget *(GAP-TUI-6)*

**Summary:** When a `delegate_task` call is in progress, the inline widget only shows a
spinner. OpenCode shows the live tool name from the child session and click-to-navigate.

**Affected files:**
- `tui/src/ui/app.py` — update task tool widget with live child activity
- `tui/src/ui/components/subagent_progress.py` — add live tool name + navigation hint

**Implementation steps:**
1. Subscribe `SubagentProgressWidget` to `subagent.tool_start` events filtered by
   the child `session_id`.
2. On each `subagent.tool_start`, update the widget label to:
   `↳ {tool_name} {tool_title}` (live, replaced on each tool call)
3. On `subagent.complete`, update to:
   `└ {N} toolcalls · {duration}s`
4. Add a `[Navigate]` button that switches the TUI to the child session's message stream.
5. Publish `subagent.tool_start` events from `delegation_node.py` by broadcasting each tool
   call event with an extra `parent_session_id` field.

**Acceptance criteria:**
- Live tool name updates in the parent session's chat stream
- On completion, shows tool call count and duration
- Navigate button switches to child session view

**Tests:** `tests/unit/test_subagent_progress_widget.py` — mock events, assert label updates.

---

### TASK-TUI-7 — Pending permissions counter in footer *(GAP-FOOTER-1)*

**Summary:** When permission requests are pending, the footer should show a warning count badge.

**Affected files:**
- `tui/src/ui/app.py` — subscribe to `ToolPermissionEvent`, maintain pending count
- `tui/src/ui/styles/app.tcss` — add `.permission-badge` style

**Implementation steps:**
1. Add `_pending_permissions: reactive(0)` to `CodingAgentApp`.
2. Subscribe to `ToolPermissionEvent` → increment; `ToolPermissionApproved` /
   `ToolPermissionDenied` → decrement.
3. In the footer widget, conditionally render `△ {N} Permission(s)` in `$warning` color
   when `_pending_permissions > 0`.
4. Add `permission-badge` CSS class with bold warning color.

**Acceptance criteria:**
- Badge appears immediately when a permission request is queued
- Badge disappears when all requests are resolved
- Badge count is accurate under concurrent requests

**Tests:** `tests/unit/test_footer_permissions.py` — mock two permission events, assert count 2;
approve both, assert count 0.

---

### TASK-TUI-8 — LSP and MCP counts in footer *(GAP-FOOTER-2)*

**Summary:** The footer shows static `[MCP]` text. Should show live counts with error state.

**Affected files:**
- `tui/src/ui/app.py` — update `McpServerStatusEvent` handler
- `tui/src/ui/core_bridge.py` — track LSP/MCP server states

**Implementation steps:**
1. In `core_bridge.py`, maintain `_mcp_states: dict[str, str]` (server_id → status) and
   `_lsp_states: dict[str, str]` and publish `TuiMcpCountEvent` / `TuiLspCountEvent` on change.
2. Footer renders:
   - `• {N} LSP` (N = connected LSP servers, from `lsp_manager.connected_count()`)
   - `⊙ {N} MCP` in normal color when healthy, `$error` color when any server is in error state
3. Subscribe in `app.py` to keep footer label current.

**Acceptance criteria:**
- LSP count reflects live `LSPManager.connected_count()`
- MCP count turns red when any server is in error state
- Count updates within 1 render cycle of server state change

---

### TASK-TUI-9 — Compaction divider and queued message indicator *(GAP-MSG-1, GAP-MSG-2)*

**Summary:** Two small UX additions: a visual divider when compaction occurs, and a `QUEUED`
badge on messages submitted while the agent is running.

**Affected files:**
- `tui/src/ui/app.py` — handle `context.compacted` event; message queue handling

**Implementation steps:**
1. **Compaction divider:** Subscribe to `context.compacted`. On receipt, insert a
   `CompactionDivider` widget (a simple `Rule` with label `═══ Context Compacted ═══`)
   into the chat log at the current scroll position.
2. **Queued messages:** Add a `_message_queue: list[str]` to `CodingAgentApp`. When a
   message is submitted while `self._agent_running` is True, add it to the queue and
   render it in the chat log with a `[QUEUED]` badge in the accent color.
   When `agent.complete` fires, drain the queue and send the next message.

**Acceptance criteria:**
- Compaction divider appears in the chat log when compaction occurs
- Queued messages show badge while waiting
- Badge is removed when the message is sent
- Multiple queued messages are sent in order

**Tests:** `tests/unit/test_chat_queue.py` — simulate three quick messages while running,
assert all delivered in order after completion.

---

## Phase 3 — Permission System

Extends the current bash-only permission gate to cover all tools, adds feedback on rejection,
and adds persistent "allow always" rules.

---

### TASK-PERM-1 — Per-tool permission policy *(GAP-PERM-1)*

**Summary:** Only `bash` is gated. All other tools (`write_file`, `read_file`, `delegate_task`,
`webfetch`, etc.) execute without any user-in-the-loop check. This is the most impactful
permission gap.

**Affected files:**
- `src/core/orchestration/permission_gateway.py` — extend Gate 3 to all tools
- `src/tools/` — each tool file that writes/reads/executes
- `tui/src/ui/app.py` — unify permission prompt for all tool types
- `src/core/orchestration/permission_policy.py` — add per-tool rules

**Implementation steps:**
1. Define `PermissionKind` with all tool categories (already partially done per
   `docs/IMPLEMENTATION_TASKS.md TASK-3`). Ensure it covers:
   `edit`, `write`, `bash`, `webfetch`, `delegate_task`, `read`, `glob`, `grep`,
   `websearch`, `external_directory`.
2. Add a `@requires_permission(kind: PermissionKind)` decorator that every tool applies.
   The decorator calls `permission_gateway.gate(kind, args)` before executing.
3. `PermissionGateway.gate()` checks rules in order:
   - Session allowlist (approved in this session)
   - Project rules (persisted in `.agent-context/permissions.json`)
   - Config file rules (`permission_policy.yaml`)
   - Default: `ask` for risky kinds, `allow` for reads
4. `ask` result publishes `ToolPermissionEvent` and waits for `ToolPermissionApproved` /
   `ToolPermissionDenied` from the TUI.
5. In `app.py`, unify the bash approval prompt and the new `ToolPermissionEvent` prompt
   into a single `PermissionPrompt` widget that shows tool name, kind, and arguments.

**Acceptance criteria:**
- `write_file` prompts for permission on first call (in `ask` policy mode)
- `read_file` is `allow` by default (no prompt needed)
- `delegate_task` prompts with the subtask description
- Session allowlist prevents re-prompting for the same tool+pattern in the same session
- All existing bash permission tests still pass

**Tests:** `tests/unit/test_permission_gateway_all_tools.py` — each `PermissionKind` has a
test asserting correct default policy; allowlist bypass; project rules override.

---

### TASK-PERM-2 — "Reject with feedback" flow *(GAP-PERM-2)*

**Summary:** When a user rejects a tool call, they currently have no way to provide guidance
inline. The agent receives a generic "denied" and must rely on a follow-up message.

**Affected files:**
- `tui/src/ui/features/` — add feedback text input to permission prompt
- `tui/src/ui/bus.py` — add `feedback: Optional[str]` to `ToolPermissionDenied`
- `src/core/orchestration/permission_gateway.py` — route feedback into next LLM turn
- `src/core/orchestration/graph/nodes/execution_node.py` — inject feedback as tool result

**Implementation steps:**
1. Modify `PermissionPrompt` to show a text input when the user clicks `Reject`:
   ```
   [Allow] [Reject] → opens → "Tell the agent what to do differently:" [text input] [Send]
   ```
2. `ToolPermissionDenied` gains `feedback: Optional[str]` field.
3. In `permission_gateway.py`, when the gate returns `denied` with non-empty feedback,
   format the tool result as:
   ```
   Permission denied. User feedback: {feedback}
   ```
   This is injected as the tool result string, so the agent sees it on the next LLM turn.
4. Add `permission_denied_with_feedback` as a separate `ErrorCode`.

**Acceptance criteria:**
- Rejection dialog shows text input after clicking Reject
- Feedback string is returned to the LLM as part of the tool result
- Empty feedback (plain reject) still works — `"Permission denied."` without feedback text
- `feedback` field is propagated through the event bus correctly

**Tests:** `tests/unit/test_permission_feedback.py` — deny with feedback, assert tool result
contains feedback string; deny without feedback, assert plain denial string.

---

### TASK-PERM-3 — "Allow always" with glob patterns *(GAP-PERM-3)*

**Summary:** Approvals are single-shot (per tool call). "Allow always" should persist glob
patterns to the project permission table so future calls matching the pattern are pre-approved.

**Affected files:**
- `tui/src/ui/app.py` — add "Allow always" button to permission prompt
- New: `src/core/orchestration/permission_table.py` — SQLite-backed rule table
- `src/core/orchestration/permission_gateway.py` — query table before prompting

**Implementation steps:**
1. Create `PermissionTable` (SQLite, same DB as session store):
   ```sql
   CREATE TABLE permission_rules (
     id INTEGER PRIMARY KEY,
     tool_kind TEXT NOT NULL,
     pattern TEXT NOT NULL,   -- glob or regex
     action TEXT NOT NULL,    -- "allow" | "deny"
     scope TEXT NOT NULL,     -- "project" | "session"
     created_at INTEGER
   );
   ```
2. In `PermissionGateway.gate()`, after checking the session allowlist and before `ask`,
   query `PermissionTable` for matching rules. If a rule matches, apply it without prompting.
3. In `PermissionPrompt`, add an `Allow always` button alongside `Allow`. Clicking it:
   - Opens a pattern input pre-filled with the tool argument (e.g., path for `edit`)
   - On confirm, calls `permission_table.add_rule(kind, pattern, "allow", "project")`
   - Then approves the current request
4. Show existing rules for the current tool kind in the permission prompt before the
   user decides (like OpenCode does).

**Acceptance criteria:**
- "Allow always" persists a rule to SQLite
- Subsequent calls matching the persisted glob are pre-approved without prompting
- Rules survive app restart (project scope)
- Session-scope rules are cleared on session end

**Tests:** `tests/unit/test_permission_table.py` — add rule, match, no-match; glob pattern
matching (`src/**/*.py` matches `src/foo/bar.py`); session vs project scope.

---

## Phase 4 — Session Commands & UX

---

### TASK-CMD-1 — `/undo` command *(GAP-CMD-1)*

**Summary:** There is no way to revert to a previous state in the conversation. OpenCode's
`/undo` trims all messages after the last user message, even aborting the agent if running.

**Affected files:**
- `tui/src/ui/app.py` — add `/undo` to `_handle_slash`
- `src/core/orchestration/session_manager.py` — add `trim_to_last_user_message()`

**Implementation steps:**
1. Add `SessionManager.trim_to_last_user_message(session_id)`:
   - Load the session's message list
   - Find the index of the last `role=user` message
   - Slice the list to include only messages up to and including that index
   - Persist the trimmed list (atomic write via temp file + rename)
2. In `app.py`, handle `/undo`:
   - If agent is running, abort the current run first (set abort flag, await completion)
   - Call `session_manager.trim_to_last_user_message(current_session_id)`
   - Remove the corresponding message widgets from the chat log (all after the last user bubble)
   - Show a notification: `"↩ Reverted to previous message"`
3. If there is no prior user message (first message), show: `"Nothing to undo"`.

**Acceptance criteria:**
- `/undo` while agent is idle removes last agent turn
- `/undo` while agent is running aborts it first, then trims
- `/undo` at the start of a session shows "Nothing to undo"
- Session store reflects the trim (not just UI)
- Undo is not reversible (no redo — document this)

**Tests:** `tests/unit/test_session_undo.py` — trim happy path, trim at start, trim after
3 exchanges (verify correct slice).

---

### TASK-CMD-2 — `/rename` command *(GAP-CMD-3)*

**Summary:** Sessions have auto-generated titles but no way to rename them from the TUI.

**Affected files:**
- `tui/src/ui/app.py` — add `/rename <title>` to `_handle_slash`
- `src/core/orchestration/session_manager.py` — add `rename_session()`

**Implementation steps:**
1. Add `SessionManager.rename_session(session_id, title)` — update the session metadata
   title field and persist.
2. Handle `/rename <title>` in `app.py`:
   - Validate that `<title>` is non-empty (after stripping)
   - Call `session_manager.rename_session(current_session_id, title)`
   - Update the session title display in the sidebar and header
3. Update the session list widget to reflect the new name immediately.

**Acceptance criteria:**
- `/rename My Task` changes the title in the sidebar and header
- Empty title (`/rename`) shows usage hint
- Title persists across restart

---

### TASK-CMD-3 — `/diff` command improvements

**Summary:** `/diff` exists but only shows file changes since session start. Should also support
`/diff HEAD` (vs git HEAD) and `/diff {commit}` for arbitrary comparison.

**Affected files:**
- `tui/src/ui/app.py` — extend `_slash_diff` handler

**Implementation steps:**
1. Parse the `/diff` argument:
   - `/diff` (no arg) → diff against session snapshot (existing behavior)
   - `/diff HEAD` → run `git diff HEAD` in workdir
   - `/diff {sha}` → run `git diff {sha}` in workdir
2. For git-based diffs, use `git_tools.git_diff(ref)` from the tool registry and render
   the output in the `SideBySideDiff` component.
3. Show a clear header above the diff: `Changes vs {ref}`.

**Acceptance criteria:**
- `/diff HEAD` shows unstaged changes vs git HEAD
- `/diff main` shows changes vs the `main` branch
- Invalid ref shows `"Unknown ref: {ref}"` instead of crashing

---

### TASK-CMD-4 — `/context` token usage visualization

**Summary:** `/context` exists but shows raw numbers. Should show a visual progress bar
and break down usage by category (system prompt, history, tools, memory).

**Affected files:**
- `tui/src/ui/app.py` — extend `_slash_context` handler

**Implementation steps:**
1. In `ContextBuilder`, expose `get_token_breakdown() -> dict[str, int]` that returns
   token counts per section: `system`, `history`, `tools`, `memory`, `total`, `budget`.
2. In `_slash_context`, call this method and render:
   ```
   Token Usage: 12,450 / 32,768 (38%)
   ████████░░░░░░░░░░░░░░░░░░░░░░░░ 38%
   system  : 2,100  ██
   history : 8,200  ████████
   tools   : 1,800  █
   memory  :   350  ░
   ```
3. Use Rich `Progress`-style rendering within the Textual widget.

**Acceptance criteria:**
- Progress bar reflects actual token usage
- Category breakdown is accurate
- Compact single-line format for use in footer as well

---

### TASK-CMD-5 — Message queueing while agent runs *(GAP-MSG-2)*

**Summary:** Input is disabled while the agent runs. Should queue submitted messages and
drain them after the agent completes.

**Affected files:**
- `tui/src/ui/app.py` — `_message_queue`, `on_submit`, `on_agent_complete`

**Implementation steps:**
1. Add `_message_queue: list[str] = []` to `CodingAgentApp`.
2. In `on_submit`, if `_agent_running`, append to `_message_queue` and render the user
   bubble immediately with a `[QUEUED]` badge; do not send to orchestrator.
3. In `on_agent_complete`, drain `_message_queue` one message at a time:
   - Remove the `[QUEUED]` badge from the first queued bubble
   - Send it to the orchestrator
   - Wait for completion, then repeat
4. Keep input enabled while messages are queued (user can add more to the queue).

**Acceptance criteria:**
- User can type and submit while agent is running
- Queued messages appear immediately with `[QUEUED]` badge
- Messages are sent in order, one at a time, after the agent finishes
- Queue is cleared if the session is reset

---

## Phase 5 — Operational Maturity

Patterns derived from `build-your-own-openclaw` that improve how the agent is deployed,
configured, and maintained over time.

---

### TASK-OPS-1 — Config deep-merge + complete hot-reload

**Summary:** Config hot-reload is partial. Not all subsystems subscribe to `config.reloaded`.
The config file layers (defaults / user / runtime) are not formally separated, making it easy
to accidentally overwrite user settings with runtime state.

**Affected files:**
- `src/config/config_loader.py` — formalize three-layer merge, add `changed_keys` diff
- `src/core/scheduler/worker.py` — subscribe to `config.reloaded`
- `src/core/inference/provider_manager.py` — subscribe
- `src/core/indexing/repo_indexer.py` — subscribe
- `src/core/indexing/lsp_manager.py` — subscribe

**Implementation steps:**
1. Formalize config layers in `config_loader.py`:
   - `config.defaults.yaml` — shipped defaults, never user-edited
   - `config.user.yaml` — user overrides, gitignored
   - `config.runtime.yaml` — auto-managed state (active model, session affinities, etc.)
   - `load_merged()` applies layers in order with `_deep_merge(base, override)` — override
     wins on every leaf key; lists are replaced not appended
2. On each `config.reloaded` event, compute `changed_keys` by diffing old vs new config
   (top-level keys only is sufficient). Include in the event payload.
3. Each subscriber checks `changed_keys` to decide whether to act:
   ```python
   def _on_config_reload(self, event):
       if "scheduler_jobs" in event.get("changed_keys", []):
           self._reload_jobs()
   ```
4. `SchedulerWorker._reload_jobs()` — re-reads `scheduler_jobs` from config, adds new jobs,
   removes deleted ones, updates intervals on changed ones.
5. `ProviderManager._on_config_reload()` — hot-swaps `active_model` if changed.
6. `RepoIndexer._on_config_reload()` — adds/removes watched directories.
7. `LSPManager._on_config_reload()` — restarts servers whose config changed.

**Acceptance criteria:**
- Editing `config.user.yaml` triggers `config.reloaded` within 1 second
- `changed_keys` is accurate
- Each subscriber reacts only to its relevant keys
- `config.runtime.yaml` is never committed (add to `.gitignore` if missing)

**Tests:** `tests/unit/test_config_hotreload_complete.py` — mock file write, assert each
subscriber's reload handler fires; `changed_keys` accuracy test.

---

### TASK-OPS-2 — Session source-affinity persistence

**Summary:** The `source_id → session_id` mapping is in-memory. On server restart, returning
users (Telegram, WebSocket clients) start fresh sessions instead of continuing their existing
conversations.

**Affected files:**
- `src/core/orchestration/session_registry.py` — add `persist_affinity()`, `load_affinities()`
- `src/config/config_loader.py` — `update_runtime(key, value)` helper
- `src/server/app.py` — expose `GET /sessions/sources` (admin-auth)

**Implementation steps:**
1. Add `SessionRegistry.persist_affinity(source_id, session_id)`:
   - Calls `config_loader.update_runtime(f"sources.{source_id}", session_id)`
   - `update_runtime` does an atomic read-modify-write on `config.runtime.yaml`
2. Add `SessionRegistry.load_affinities()`:
   - Called in `__init__`
   - Reads `config.runtime.yaml["sources"]`
   - Prunes entries older than `TTL_DAYS` (default 7) using the session's `updated_at`
3. On every new source→session mapping in `get_or_create_session(source_id)`, call
   `persist_affinity()`.
4. Add `GET /sessions/sources` endpoint returning the affinity map (admin-auth required).

**Acceptance criteria:**
- After server restart, a returning WebSocket client with the same `source_id` resumes
  the same session
- Affinities older than 7 days are pruned
- Admin endpoint shows current affinity map

**Tests:** `tests/unit/test_session_affinity.py` — persist, restart (re-init registry from
file), assert affinity restored; TTL expiry; concurrent affinity updates.

---

### TASK-OPS-3 — Dual memory architecture (MEMORY.md + USER.md)

**Summary:** A single `memory.md` mixes project knowledge with user preferences. During
compaction, user preferences can be evicted. Separating them protects stable personal context
from volatile project context.

**Affected files:**
- `src/core/memory/session_store.py`
- `src/core/memory/frozen_snapshot.py`
- `src/core/memory/auto_compactor.py`
- `src/tools/memory_tools.py`
- `src/core/orchestration/orchestrator.py`

**Implementation steps:**
1. Add `user_memory_path: Path` alongside `memory_path` in `SessionStore.__init__`:
   ```python
   self.memory_path = workdir / ".agent-context" / "memory.md"
   self.user_memory_path = workdir / ".agent-context" / "user.md"
   ```
2. In `FrozenSnapshot.__init__`, read both files and inject as two separate blocks:
   ```
   <project_context>
   {memory.md content}
   </project_context>

   <user_context>
   {user.md content}
   </user_context>
   ```
3. Add `target: Literal["project", "user"] = "project"` parameter to `save_memory` in
   `memory_tools.py`. Route writes to `memory_path` or `user_memory_path` accordingly.
4. `AutoCompactor` gets a `target` parameter; project memory compacts at 4400 chars limit,
   user memory compacts at 2200 chars limit (user context is typically smaller).
5. Both files go through `MemorySecurityScanner` on read and write.
6. Add guidance in the planning node prompt: "Use `save_memory(target='user')` for
   persistent user preferences, `save_memory(target='project')` for task/file knowledge."

**Acceptance criteria:**
- New `user.md` file is created alongside `memory.md` if it doesn't exist
- `save_memory(content, target="user")` writes to `user.md`
- Both are injected into system prompt as separate XML blocks
- Compaction runs independently on each with different size limits
- Security scanner runs on both

**Tests:** `tests/unit/test_dual_memory.py` — write to each store; read both; compaction
respects separate limits; injection produces two blocks; security scan on both.

---

### TASK-OPS-4 — CRON.md file-based job configuration

**Summary:** Scheduled jobs are registered only in Python code. Operators can't add, modify,
or disable jobs without code changes. The `build-your-own-openclaw` project shows how to make
jobs first-class workspace files.

**Affected files:**
- New: `src/core/scheduler/cron_loader.py`
- `src/core/scheduler/worker.py` — add `reload_from_files()`
- `src/server/app.py` — extend scheduler endpoints
- `tui/src/ui/app.py` — `/crons` slash command

**Implementation steps:**
1. Define the `CRON.md` file format:
   ```markdown
   ---
   id: daily-repo-reindex
   name: Daily Repository Re-index
   description: Refresh symbol graph and vector store
   schedule: "0 2 * * *"   # cron expression
   enabled: true
   agent_task: true         # if true, dispatches as agent session with body as task
   ---
   Reindex all repositories in the workspace. Update the symbol graph and rebuild
   the vector index. Do not modify any files.
   ```
2. Create `CronLoader`:
   ```python
   class CronLoader:
       def __init__(self, crons_dir: Path): ...
       def load_all(self) -> list[CronJobDef]: ...
       def watch(self, on_change: Callable) -> None: ...  # uses watchdog
   ```
   - Parses frontmatter (use `python-frontmatter` or manual yaml block parsing)
   - Returns list of `CronJobDef` dataclasses
3. `SchedulerWorker.reload_from_files()`:
   - Calls `CronLoader.load_all()`
   - For each job: add if new, update interval if changed, disable if `enabled: false`
   - Logs changes to `scheduler.job_changed` event
4. Jobs with `agent_task: true` dispatch `DispatchEvent(task=body, agent_role="operational")`
   into the EventBus when triggered.
5. `GET /scheduler/jobs` response includes `source: "file" | "programmatic"` and
   `file_path` for file-defined jobs.
6. Add `/crons` TUI slash command: lists all active cron jobs with ID, schedule, last run,
   enabled state.
7. Subscribe `SchedulerWorker` to `config.reloaded` (from TASK-OPS-1) to call
   `reload_from_files()` on config change.

**Acceptance criteria:**
- A `.agent-context/crons/daily-reindex.cron.md` file creates a scheduled job on startup
- Editing the file (change schedule/disable) takes effect within 60 seconds (via watchdog)
- Deleting the file removes the job
- `agent_task: true` jobs dispatch real agent sessions
- `/crons` lists all jobs with their schedule and last-run time

**Tests:** `tests/unit/test_cron_loader.py` — parse valid CRON.md; parse with missing fields
(graceful skip); `reload_from_files` adds/removes/updates jobs; `agent_task` dispatch.

---

## Phase 6 — Architecture & Platform

The largest and most architecturally significant improvements. Implement after Phases 0–5 are
stable.

---

### TASK-ARCH-1 — SOUL.md personality layer for role prompts

**Summary:** Role prompts (`src/config/agent-brain/roles/`) are monolithic — they mix
operational instructions with personality. Separating them into `AGENT.md` (what to do) +
`SOUL.md` (how to do it, tone, personality) + `BOOTSTRAP.md` (workspace context) allows
per-deployment personality tuning without touching operational logic.

**Affected files:**
- New: `src/core/inference/prompt_builder.py`
- New: `src/config/agent-brain/soul/engineer.soul.md`
- New: `src/config/agent-brain/bootstrap.md`
- `src/core/inference/context_builder.py` — delegate system prompt to `PromptBuilder`
- `src/config/agent-brain/roles/*.md` — trim personality content (move to soul files)

**Implementation steps:**
1. Create `PromptBuilder`:
   ```python
   class PromptBuilder:
       def build(self,
                 role: str,
                 soul: Optional[str],
                 bootstrap: Optional[str],
                 runtime_context: dict) -> str:
   ```
   Layer order:
   - Layer 1: Role file content (`roles/{role}.md`)
   - Layer 2: Soul file content if `soul` is specified (`soul/{soul}.soul.md`)
   - Layer 3: Bootstrap context (`bootstrap.md` with `{{variable}}` substitution)
   - Layer 4: Runtime context block (timestamp, active model, available tools, agents)
2. Create `engineer.soul.md`:
   ```markdown
   You are precise and terse. You prefer concrete examples over explanations.
   You write code before commenting on it. You never pad responses with affirmations.
   ```
3. Create `bootstrap.md` template:
   ```markdown
   ## Workspace Context
   - Working directory: {{workdir}}
   - Active model: {{model_name}} (tier: {{model_tier}})
   - Available tools: {{tool_count}} tools loaded
   - Sessions today: {{session_count}}
   - Current time: {{timestamp}}
   ```
4. In `ContextBuilder.build_system_prompt()`, call `PromptBuilder.build()` instead of
   directly loading the role file.
5. Role files are trimmed to pure operational instructions. Personality text is moved to
   `engineer.soul.md` (or a role-specific soul file).

**Acceptance criteria:**
- Replacing `soul/engineer.soul.md` changes agent tone without affecting tool behavior
- Bootstrap variables are substituted correctly
- Missing soul file degrades gracefully (soul layer simply omitted)
- All existing system prompt tests still pass

**Tests:** `tests/unit/test_prompt_builder.py` — layer composition order; variable
substitution; missing soul graceful degradation; runtime context injection.

---

### TASK-ARCH-2 — Multi-platform channel abstraction

**Summary:** CodingAgent has no channel abstraction. Adding Telegram or Discord today requires
invasive server changes. Implementing a `Channel` ABC decouples platform I/O from agent logic.

**Affected files:**
- New: `src/server/channels/` package
  - `base.py` — `Channel` ABC
  - `websocket.py` — refactor existing WebSocket into `Channel`
  - `telegram.py` — `TelegramChannel`
  - `discord.py` — `DiscordChannel`
- New: `src/server/channel_worker.py`
- `src/server/app.py` — integrate `ChannelWorker` startup/shutdown

**Implementation steps:**
1. Define `Channel[T]` ABC:
   ```python
   class Channel(ABC, Generic[T]):
       platform_name: str

       @abstractmethod
       async def run(self,
                     on_message: Callable[[str, str], Awaitable[None]]) -> None:
           """Start receiving. Call on_message(source_id, text) for each inbound."""

       @abstractmethod
       async def reply(self, content: str, source: T) -> None:
           """Send a reply to the originating source."""

       @abstractmethod
       async def stop(self) -> None: ...
   ```
2. Create `ChannelWorker`:
   - Holds a list of enabled `Channel` instances (from config)
   - For each inbound message, translates to `InboundEvent(source_id, text)`
     and publishes to `EventBus`
   - Subscribes to `OutboundEvent` and calls `channel.reply(content, source)` for the
     matching platform
3. Refactor existing WebSocket handling in `app.py` into `WebSocketChannel` implementing
   the ABC. This is a pure refactor — no behavior change.
4. Implement `TelegramChannel` using `python-telegram-bot` (async version):
   - Source IDs: `platform-telegram:{chat_id}`
   - Markdown stripping for Telegram's limited formatting
   - `allowed_user_ids` filter from config
5. Implement `DiscordChannel` using `discord.py`:
   - Source IDs: `platform-discord:{channel_id}:{user_id}`
   - `channel_id` and `guild_id` from config
6. Config:
   ```yaml
   channels:
     telegram:
       enabled: false
       bot_token: ""
       allowed_user_ids: []
     discord:
       enabled: false
       bot_token: ""
       channel_id: ""
   ```

**Acceptance criteria:**
- `WebSocketChannel` passes all existing WebSocket tests (pure refactor)
- `TelegramChannel` receives a message and routes it through the agent pipeline in an
  integration test with a mock bot API
- `DiscordChannel` same
- Adding a new channel requires only implementing `Channel` ABC + config entry

**Tests:** `tests/unit/test_channel_abstraction.py` — mock `Channel` implementation;
`ChannelWorker` routes inbound to EventBus; `OutboundEvent` routes back to correct channel.

---

### TASK-ARCH-3 — Structured metrics & observability

**Summary:** A metrics endpoint exists but has sparse instrumentation. Planning time, tool
latency, per-tool error rates, and memory compaction stats are not tracked.

**Affected files:**
- New: `src/core/observability/metrics.py`
- `src/core/orchestration/graph/nodes/*.py` — apply `@timed_metric`
- `src/core/orchestration/tool_execution_pipeline.py`
- `src/server/app.py` — extend `/metrics` payload. Prometheus scraping/export is
  intentionally out-of-scope for the shipped repository; teams requiring scraping
  should implement a small external adapter that maps the in-process metrics to
  their Prometheus client and exposes a scrapeable `/metrics` text endpoint.

**Implementation steps:**
1. Create `MetricsStore` (in-process, thread-safe):
   ```python
   class MetricsStore:
       def record_histogram(self, name: str, value_ms: float) -> None: ...
       def increment_counter(self, name: str, labels: dict = {}) -> None: ...
       def set_gauge(self, name: str, value: float) -> None: ...
       def snapshot(self) -> dict: ...  # for /metrics JSON
       def prometheus_text(self) -> str: ...  # optional helper for external adapters
   ```
   Uses `collections.deque` per histogram (rolling 1000 samples); lock on all writes.
2. Define `@timed_metric(name)` decorator:
   ```python
   def timed_metric(name: str):
       def decorator(fn):
           async def wrapper(*args, **kwargs):
               t0 = time.monotonic()
               try:
                   return await fn(*args, **kwargs)
               finally:
                   metrics.record_histogram(name, (time.monotonic() - t0) * 1000)
           return wrapper
       return decorator
   ```
3. Apply to: each graph node function, `tool_execution_pipeline.execute()`,
   `distiller.distill_context()`, `auto_compactor.compact()`.
4. Key metrics to expose:
   ```
   node.planning.duration_ms   (p50, p95, p99)
   node.execution.duration_ms
   tool.{name}.duration_ms
   tool.{name}.error_count
   tool.{name}.call_count
   llm.tokens.input             (cumulative)
   llm.tokens.output
   memory.compaction.count
   memory.compaction.chars_removed
   session.active_count         (gauge)
   scheduler.job.{id}.last_duration_ms
   ```
5. Extend `GET /metrics` JSON response with the new fields. Prometheus scraping/export
   is considered out-of-scope for the shipped repository; keep the core metrics
   in-process and expose them via the `/metrics` JSON payload. Teams that need
   Prometheus scraping can implement a small external adapter that maps the
   in-process counters to their Prometheus client and serves a `/metrics` text
   endpoint suitable for scraping.

**Acceptance criteria:**
- `GET /metrics` includes `p50`/`p95`/`p99` for planning and tool durations
- Prometheus text format scraping is supported by using an external adaptor that
  reads the `/metrics` JSON and exposes a Prometheus-compatible `/metrics` text
  endpoint. The shipped code intentionally does not include a Prometheus runtime
  dependency.
- Metrics are thread-safe under concurrent requests
- `@timed_metric` adds < 0.1ms overhead per invocation

**Tests:** `tests/unit/test_metrics_store.py` — histogram percentiles accuracy;
counter/gauge operations; prometheus text format validity (via external adapter);
thread-safety (20 concurrent writers).

---

### TASK-ARCH-4 — Agent-initiated outbound messaging (`post_message` tool)

**Summary:** Agents can only respond to incoming requests. They cannot proactively send messages
(e.g., "build completed", "found a critical issue in your code"). The `build-your-own-openclaw`
`post_message` tool enables this pattern for cron-triggered and long-running tasks.

**Affected files:**
- New: `src/tools/post_message_tool.py`
- `src/config/toolsets/cron.yaml` — new restricted toolset
- `tui/src/ui/app.py` — handle `agent.notification` event
- `src/server/app.py` — route to channel for non-TUI destinations

**Implementation steps:**
1. Create `post_message` tool:
   ```python
   @tool(permission_kind=PermissionKind.OUTBOUND_MESSAGE)
   async def post_message(
       destination: str,   # "tui", "telegram:{user_id}", "webhook:{alias}"
       content: str,
       priority: str = "normal"  # "normal" | "high" | "urgent"
   ) -> str:
   ```
   - `destination="tui"` → publish `agent.notification` event to EventBus
   - `destination="telegram:{id}"` → route through `TelegramChannel.reply()` (requires TASK-ARCH-2)
   - `destination="webhook:{alias}"` → POST to `config.webhooks.{alias}.url`
   - Returns `"Message sent"` on success, error string on failure
2. Create `src/config/toolsets/cron.yaml` — tool set for cron-triggered agent sessions:
   includes `post_message`, `read_file`, `bash` (read-only subset), `manage_todo`; excludes
   `write_file`, `edit_file`, `delegate_task`
3. In `TUI`, subscribe to `agent.notification` and render as a toast / notification banner
   with priority-colored border.
4. Restrict `post_message` to `cron` and `delegation` toolsets only. It must not appear in
   the standard `coding` toolset — prevents the agent from spamming users during normal
   interactive sessions.
5. Add `webhooks` section to config for pre-defined webhook aliases.

**Acceptance criteria:**
- `post_message(destination="tui", content="Done!")` shows a TUI notification
- Unauthorized use in the `coding` toolset returns `"Tool not available in this context"`
- Webhook destination POSTs JSON `{"content": "...", "priority": "..."}` to the configured URL
- `priority="urgent"` notifications have distinct styling

**Tests:** `tests/unit/test_post_message_tool.py` — TUI destination publishes event;
unauthorized toolset returns error; webhook posts correct payload.

---

## Dependency Graph

```
Phase 0 (all TASK-SEC-*)
    │
    ├──► Phase 1
    │    ├── TASK-REL-1 (Error Classification)
    │    ├── TASK-REL-2 (File Locking) ──────────────────► TASK-OPS-3 (Dual Memory)
    │    └── TASK-REL-3 (Event Persistence)
    │
    ├──► Phase 2 (TUI) — mostly independent, except:
    │    └── TASK-TUI-7 (permissions footer) ──────────────► TASK-PERM-1
    │
    ├──► Phase 3 (Permissions)
    │    ├── TASK-PERM-1 ──────────────────────────────────► TASK-PERM-2 ──► TASK-PERM-3
    │    └── requires TASK-REL-1 (error codes used)
    │
    ├──► Phase 4 (Session Commands) — mostly independent
    │
    ├──► Phase 5 (Operational)
    │    ├── TASK-OPS-1 (Config) ──────────────────────────► TASK-OPS-2, TASK-OPS-4
    │    ├── TASK-OPS-2 (Session Affinity)
    │    ├── TASK-OPS-3 (Dual Memory) — requires TASK-REL-2
    │    └── TASK-OPS-4 (CRON.md) — requires TASK-OPS-1
    │
    └──► Phase 6 (Architecture)
         ├── TASK-ARCH-1 (SOUL.md) — independent
         ├── TASK-ARCH-2 (Channels) — TASK-ARCH-4 depends on this
         ├── TASK-ARCH-3 (Metrics) — independent
         └── TASK-ARCH-4 (post_message) — requires TASK-ARCH-2 for non-TUI destinations
```

---

## What NOT to Change

The following CodingAgent patterns are **superior** to their openclaw equivalents and should
be preserved:

| Pattern | Reason to Keep |
|---------|---------------|
| LangGraph state machine | Verifiable routing, testable nodes, cycle detection |
| SQLite session store with WAL | Better concurrency than JSONL; ACID guarantees |
| 5-layer bash security sandbox | Far more robust than openclaw's basic execution |
| Frozen memory snapshot | No equivalent in openclaw; major prompt-cache cost savings |
| Tier-aware tool limits | Critical for local LLM support; openclaw cloud-only |
| Approval gate for destructive ops | openclaw has no user-in-the-loop safety mechanism |
| Per-role tool filtering | openclaw uses a single tool registry |
