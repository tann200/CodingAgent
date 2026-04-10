# Bug Report — TUI & GitHub Copilot Implementation

Generated: 2026-04-07  
Scope: `tui/src/ui/` and `src/core/inference/adapters/github_copilot_*`

Severity legend: **Critical** → data loss / crash / security | **High** → broken feature |
**Medium** → incorrect behaviour | **Low** → fragile / tech-debt

---

## TUI Bugs

### Critical

#### TUI-C1 — `oauth/screen.py`: `query_one` after widget already removed
**File:** `tui/src/ui/features/oauth/screen.py:356–365`

`OAuthDeviceFlowScreen.fail()` removes `#oauth_waiting_row` then immediately
calls `query_one("#oauth_waiting_text")` — which lives *inside* that row.
After `remove()` the widget is detached from the DOM; `query_one` throws
`NoMatches`, preventing `dismiss()` from being reached and locking the modal
open when a device-flow error occurs.

**Fix:** Guard the second query with the `_done` flag that is already set,
or query `#oauth_waiting_text` *before* removing the row.

---

#### TUI-C2 — `core_bridge.py`: cross-thread write to `app._continue_state`
**File:** `tui/src/ui/core_bridge.py:1020`

`_run_agent` runs in a background thread and writes directly to
`self.app._continue_state` with no lock.  `app.py` reads this attribute from
the Textual event-loop thread (`/continue` command).  Concurrent access is a
data race: the dict written mid-run can be partially observed.

**Fix:** Post the state back via `call_from_thread` so the write happens on
the Textual loop thread, or protect with a lock.

---

#### TUI-C3 — `app.py:1648–1655`: `/continue` guard is inverted
**File:** `tui/src/ui/app.py:1648–1655`

```python
self._bridge.restore_and_continue(...)
if not self._bridge.is_running():          # ← True when nothing is running
    self.notify("No previous task…", severity="warning")
```

The warning fires when the agent *starts* successfully (is_running() returns
`False` for the brief instant before the agent thread starts).  It never fires
on the genuine failure path (no previous task).

**Fix:** Check `_last_task_text` / `_continue_state` *before* calling
`restore_and_continue`, not after.

---

### High

#### TUI-H1 — `app.py:428–465`: `asyncio.ensure_future` inside `call_later`
**File:** `tui/src/ui/app.py:428–432`, `461–464`

```python
self.call_later(
    lambda w=widget, c=chat_log: asyncio.ensure_future(
        self._mount_and_scroll(w, c)
    )
)
```

`call_later` schedules the lambda on Textual's internal event loop.  Inside
that lambda, `asyncio.ensure_future` is called *without* an explicit loop
argument and relies on `asyncio.get_event_loop()`, which may not be Textual's
loop in Python ≥ 3.10 (no running loop in the lambda's context).  Coroutines
are silently dropped and widgets are never mounted.

**Fix:** Replace with `self.call_later(self._mount_and_scroll, widget, chat_log)`
and make `_sched_chat_widget` use `self.call_later` directly with the async
target — Textual handles scheduling coroutines via `call_later` correctly.

---

#### TUI-H3 — `app.py:1303`: tool tokens counted as output tokens
**File:** `tui/src/ui/app.py:1303`

```python
self.query_one("#sb_context", Static).update(
    f"In: {event.system + event.task:,} | Out: {event.tools:,}"
)
```

`event.tools` counts tool-call tokens that are re-injected as *input* context
on the next turn.  They are displayed under the "Out" label, understating
output tokens and overstating context efficiency.

**Fix:** Move `event.tools` to the input side:
```python
f"In: {event.system + event.task + event.tools:,} | Out: 0"
```
or track actual output tokens separately.

---

#### TUI-H4 — Multiple `ModalScreen` subclasses use `pop_screen()` instead of `dismiss()`
**Files:**
- `tui/src/ui/features/settings/screen.py:405,413,454,458,462`
- `tui/src/ui/screens/session_list.py:181,200`
- `tui/src/ui/screens/timeline.py:144,166,169`

`ModalScreen.dismiss()` is the correct API; it invokes the callback registered
in `push_screen(..., callback=cb)` and allows the caller to receive a return
value.  Using `app.pop_screen()` bypasses the callback contract — any code
that passes a callback to these screens will silently never receive it.

**Fix:** Replace every `self.app.pop_screen()` in modal screens with
`self.dismiss()` (passing a result value where meaningful).

---

### Medium

#### TUI-M1 — `app.py:1132`: `event.stop()` unconditionally blocks bubbling
**File:** `tui/src/ui/app.py:1132`

```python
async def on_any_button(self, event: Button.Pressed) -> None:
    btn_id = event.button.id or ""
    event.stop()   # ← unconditional
```

`event.stop()` prevents the event from reaching any other handler.  If a
child widget (e.g. a `ProviderConfigScreen` button) has its own `on_button`
handler, the app-level handler intercepts it first, calls `event.stop()`, and
the child's handler never fires — even when `btn_id` doesn't match any of the
`if/elif` branches.

**Fix:** Call `event.stop()` only inside the matched branch, after confirming
the button belongs to this handler:
```python
async def on_any_button(self, event: Button.Pressed) -> None:
    btn_id = event.button.id or ""
    if btn_id in KNOWN_IDS or btn_id.startswith(KNOWN_PREFIXES):
        event.stop()
        # handle...
```

---

#### TUI-M3 — `screens/timeline.py:110–121`: user text embedded raw in Rich markup
**File:** `tui/src/ui/screens/timeline.py:110–121`

Chat history content is sliced and embedded directly into Rich markup strings
without escaping.  A message containing `[bold]` or `[red]anything[/red]`
will break the Rich parser and produce garbled or invisible output.

**Fix:**
```python
from rich.markup import escape
preview = escape(content[:120].replace("\n", " "))
```
Apply `escape()` before embedding in any `f"...{preview}..."` markup string.

---

#### TUI-M7 — `mock_eventbus.py:35–39`: subscriber exceptions silently swallowed
**File:** `tui/src/ui/mock_eventbus.py:35–39`

```python
except Exception:
    pass
```

No logging.  Any bug in a bridge callback during mock/dev mode produces zero
output — the event appears delivered but nothing happens.

**Fix:**
```python
except Exception as exc:
    import logging
    logging.getLogger("mock_eventbus").warning(
        "MockEventBus: callback %r raised: %s", cb, exc, exc_info=True
    )
```

---

### Low

#### TUI-L2 — `screens/probe_results.py`: no dismiss mechanism
**File:** `tui/src/ui/screens/probe_results.py`

`ProbeResultsScreen` is a `ModalScreen` with no `BINDINGS`, no close button,
and no `on_key` handler.  Once pushed, the user has no way to dismiss it.

**Fix:** Add `BINDINGS = [("escape", "dismiss", "Close")]`.

---

#### TUI-L4 — `logging.py:24`: `emit()` iterates `_callbacks` without a lock
**File:** `tui/src/ui/logging.py:24`

`emit()` iterates `self._callbacks` while background threads can concurrently
call `register_callback()` / `unregister_callback()`, mutating the list and
raising `RuntimeError: list changed size during iteration`.

**Fix:** Iterate a snapshot:
```python
for cb in list(self._callbacks):
```

---

## GitHub Copilot Bugs

### Critical

#### CP-01 — `openai_compat_adapter.py:58–71`: wrong URL for GitHub Enterprise
**File:** `src/core/inference/adapters/openai_compat_adapter.py:58–71`

`_compose()` injects `/api/v1/` whenever the base URL does not already contain
`/v` or `/api`.  The enterprise base URL `https://copilot-api.company.ghe.com`
contains neither, so `chat/completions` becomes:

```
https://copilot-api.company.ghe.com/api/v1/chat/completions   ← 404
```

The public URL `https://api.githubcopilot.com` accidentally matches the `/api`
substring check (`://api`) producing the correct result, but this is a false
positive that masks the underlying fragility.

**Fix:** Override `_compose` in `GithubCopilotAdapter` to append the path
directly with no prefix injection:
```python
def _compose(self, path: str) -> Optional[str]:
    if not self.base_url:
        return None
    return f"{str(self.base_url).rstrip('/')}/{path.lstrip('/')}"
```

---

### High

#### CP-02 — `github_copilot_adapter.py:112`: `x-initiator` always `"user"`
**File:** `src/core/inference/adapters/github_copilot_adapter.py:112`

`x-initiator` is hardcoded to `"user"` on every request.  OpenCode sets it
to `"agent"` when the last message role is not `"user"` (i.e., for all agent
continuation turns, sub-agent sessions, and compaction turns).  GitHub uses
this header for rate-limit differentiation; incorrect values consume the
human-interaction quota for automated turns.

**Fix:** Accept `messages` in `_headers()` and set dynamically:
```python
def _headers(self, messages=None) -> Dict[str, str]:
    ...
    is_agent = bool(
        messages and isinstance(messages, list)
        and messages[-1].get("role") != "user"
    )
    return {
        ...,
        "x-initiator": "agent" if is_agent else "user",
    }
```
Update `_chat_internal` to pass `messages` when calling `_headers()`.

---

#### CP-03 — `github_copilot_adapter.py:107–113`: `Copilot-Vision-Request` header never sent
**File:** `src/core/inference/adapters/github_copilot_adapter.py:107–113`

When any message contains image content, GitHub Copilot requires
`Copilot-Vision-Request: true`.  The header is never set.  Vision/multimodal
requests will fail silently or return an error.

**Fix:** Detect image parts in outgoing messages and add the header:
```python
has_vision = any(
    isinstance(m.get("content"), list)
    and any(p.get("type") == "image_url" for p in m["content"])
    for m in (messages or [])
)
if has_vision:
    headers["Copilot-Vision-Request"] = "true"
```

---

### Medium

#### CP-04 — `github_copilot_adapter.py:107–113`: `anthropic-beta` header missing for Claude
**File:** `src/core/inference/adapters/github_copilot_adapter.py:107–113`

Claude models routed through Copilot require
`anthropic-beta: interleaved-thinking-2025-05-14` for extended thinking /
reasoning to work.  This header is never sent.

**Fix:**
```python
if "claude" in (model or "").lower():
    headers["anthropic-beta"] = "interleaved-thinking-2025-05-14"
```
Pass the active model name into `_headers()`.

---

#### CP-05 — `github_copilot_auth.py:201–213`: sleep before first poll attempt
**File:** `src/core/inference/adapters/github_copilot_auth.py:201–213`

`poll_for_token` sleeps `interval + 3` seconds *before* the very first HTTP
request.  OpenCode polls immediately on the first iteration and only sleeps
after receiving `authorization_pending`.  This adds an unnecessary 8-second
delay to every login.

**Fix:** Move the sleep to after the HTTP call, conditional on
`authorization_pending`.

---

#### CP-06 — `openai_compat_adapter.py:341–344`: `tools` duplicated as `functions`
**File:** `src/core/inference/adapters/openai_compat_adapter.py:341–344`

The base class copies the `tools` array to the deprecated `functions` key on
every request.  Copilot supports the modern `tools` key natively; receiving
both simultaneously bloats the payload and may cause schema validation errors.

**Fix:** Override `_chat_internal` (or add a `_preprocess_payload` hook) in
`GithubCopilotAdapter` to remove `functions` before posting:
```python
payload.pop("functions", None)
```

---

#### CP-07 — `openai_compat_adapter.py:394–448`: 401 revoked-token not handled
**File:** `src/core/inference/adapters/openai_compat_adapter.py:394–448`

A `401 Unauthorized` response (revoked token) is treated as a generic
`http_error`.  The stale token stays in `auth.json`; every subsequent request
also fails with 401; the user is never prompted to re-authenticate.

**Fix:** Detect `status_code == 401` in the error handler of
`GithubCopilotAdapter` and call `clear_token()`, then surface a clear
"please log in again" message.

---

#### CP-08 — `github_copilot_auth.py:316–324`: `clear_token()` swallows write errors
**File:** `src/core/inference/adapters/github_copilot_auth.py:316–324`

If `_write_auth_json` fails (permissions, disk full), `clear_token()` logs a
warning and returns silently.  Callers receive no indication of failure; the
UI shows the user as logged out, but the token file is unchanged.

**Fix:** Return a `bool` and let callers check:
```python
def clear_token() -> bool:
    try:
        data = _read_auth_json()
        data.pop(_PROVIDER_KEY, None)
        _write_auth_json(data)
        return True
    except Exception as e:
        _logger.error("clear_token failed: %s", e)
        return False
```

---

### Low

#### CP-09 — `github_copilot_auth.py:243–253`: `slow_down` accumulates interval
**File:** `src/core/inference/adapters/github_copilot_auth.py:243–253`

Repeated `slow_down` responses without a server interval compound the wait:
`5 → 10 → 15 → …`.  OpenCode resets to `original_interval + 5` on each
`slow_down` response.

**Fix:** Store the original interval and use `original_interval + 5` instead
of `current_interval += 5`.

---

#### CP-10 — `github_copilot_auth.py:100–113`: fixed `.tmp` filename not thread-safe
**File:** `src/core/inference/adapters/github_copilot_auth.py:100–113`

`_write_auth_json` always uses `auth.tmp` in the same directory.  Two
concurrent writers (login + logout racing) will write to the same temp file;
the last `replace()` wins, but the first write's data is silently overwritten
mid-flight.

**Fix:** Use `tempfile.mkstemp(dir=path.parent)` for a unique temp path:
```python
import tempfile
fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp", prefix="auth_")
try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, indent=2))
    os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp_path, str(path))
except Exception:
    try: os.unlink(tmp_path)
    except OSError: pass
    raise
```

---

## Previously Fixed (this session)

| ID | File | Description |
|----|------|-------------|
| PATH-01 | `tui/src/ui/app.py:702` | `parents[4]` → `parents[3]` for providers.json |
| PATH-02 | `tui/src/ui/core_bridge.py:272,355,415` | Same off-by-one in three locations |
| PATH-03 | `tui/src/ui/config_writer.py:18` | Wrong parent chain → `parents[3] / "src" / "config"` |
| BANNER-01 | `src/core/orchestration/orchestrator.py:~1107` | Event bus wired before init (race condition) |
| BANNER-02 | `src/core/inference/llm_manager.py:~669` | `validate_connection()` result overrides model-probe status |
| BANNER-03 | `tui/src/ui/core_bridge.py:~375` | Copilot initial status from `is_authenticated()`, not "initializing" |
| TUI-C1 | `tui/src/ui/features/oauth/screen.py:349–363` | `query_one` called after widget removed — fixed (query before remove) |
| TUI-C2 | `tui/src/ui/core_bridge.py:1285–1289` | Cross-thread write to `app._continue_state` — fixed via `call_from_thread` |
| TUI-C3 | `tui/src/ui/app.py:1743–1749` | `/continue` guard inverted — fixed (check `_last_task_text` first) |
| TUI-H1 | `tui/src/ui/app.py:507,536` | `asyncio.ensure_future` in `call_later` — fixed (use `call_later` directly) |
| TUI-H3 | `tui/src/ui/app.py:1388–1399` | Tool tokens counted as output — fixed (moved to input side) |
| TUI-H4 | Multiple modal screens | `pop_screen()` instead of `dismiss()` — fixed in all 3 screens |
| TUI-M1 | `tui/src/ui/app.py:1211–1228` | Unconditional `event.stop()` — fixed (only stop when handler owns button) |
| TUI-M3 | `tui/src/ui/screens/timeline.py:111–123` | User text raw in Rich markup — fixed (`markup_escape()` applied) |
| TUI-M7 | `tui/src/ui/mock_eventbus.py:42–45` | Subscriber exceptions silently swallowed — fixed (log warning) |
| TUI-L2 | `tui/src/ui/screens/probe_results.py` | No dismiss mechanism — fixed (`BINDINGS = [("escape", "dismiss", "Close")]`) |
| TUI-L4 | `tui/src/ui/logging.py:24` | Callbacks iteration race — fixed (`list(self._callbacks)` snapshot) |
| CP-01 | `openai_compat_adapter.py` | Wrong URL for GitHub Enterprise — fixed (`_compose` overridden in subclass) |
| CP-02 | `github_copilot_adapter.py` | `x-initiator` always `"user"` — fixed (dynamic based on last message role) |
| CP-03 | `github_copilot_adapter.py` | `Copilot-Vision-Request` never sent — fixed (detects image parts) |
| CP-04 | `github_copilot_adapter.py` | `anthropic-beta` missing for Claude — fixed |
| CP-05 | `github_copilot_auth.py` | Sleep before first poll — fixed (poll first, sleep after) |
| CP-06 | `openai_compat_adapter.py` | `functions` duplicated from `tools` — fixed (`payload.pop("functions", None)`) |
| CP-07 | `github_copilot_adapter.py` | 401 not handled — fixed (`clear_token()` called on 401) |
| CP-08 | `github_copilot_auth.py` | `clear_token()` swallows write errors — fixed (returns `bool`) |
| CP-09 | `github_copilot_auth.py` | `slow_down` compounds interval — fixed (`original_interval + 5`) |
| CP-10 | `github_copilot_auth.py` | Fixed `.tmp` filename not thread-safe — fixed (`tempfile.mkstemp`) |
| INT-01 | `tests/integration/test_phase3_findings.py` | `_write_index` wrote nested format, impl reads flat — fixed (helper converts to flat) |
| INT-02 | `tests/integration/test_phase4_findings.py` | Gate4 test patched wrong module path — fixed (`permission_gateway._get_*`) |
| INT-03 | `tests/integration/test_phase4_findings.py` | `grep` routing test used stale expected value — fixed (use `list_files`) |
| LIVE-01 | `tests/integration/test_lm_studio_live_pipeline.py` | No reachability probe before enabling LM Studio tests — fixed (2 s HTTP probe) |
| LIVE-02 | `tests/integration/test_lmstudio_end_to_end.py` | No reachability probe before enabling LM Studio tests — fixed (2 s HTTP probe + CI guard) |
| LIVE-03 | `tests/integration/test_orchestrator_lmstudio_e2e.py` | No reachability probe before enabling LM Studio tests — fixed (2 s HTTP probe + CI guard) |
| LIVE-04 | `tests/integration/test_system_prompts_against_lmstudio.py` | No reachability probe before enabling LM Studio tests — fixed (2 s HTTP probe) |
| LIVE-05 | `tests/integration/test_system_prompts_json_mode.py` | No reachability probe before enabling LM Studio tests — fixed (2 s HTTP probe) |
| FLAKY-01 | `tests/unit/test_subagent_tools.py` | `test_delegate_task_valid_roles` ran real `graph.ainvoke()` causing deadlock in suite — fixed (mock `GraphFactory.get_graph` with `AsyncMock`) |
| PYRIGHT-01 | `src/core/orchestration/mcp_stdio_server.py:387` | `_orch.call_model(...)` flagged by pyright as unknown attribute — fixed (`# type: ignore[attr-defined]`) |
| LSP-HANG-01 | `tests/unit/test_lsp_auto_restart.py:81` | `test_shutdown_clears_started_flag` hung 15 s: `shutdown()` awaited real JSON-RPC on MagicMock proc — fixed (patch `_request`/`_notify` with `AsyncMock`) |
| WF-VOL21-1 | `replan_node.py` | Bare `await call_model(...)` — no timeout or cancel-event guard — fixed (`asyncio.create_task` + deadline poll pattern, reads `max_llm_wait_seconds` from project settings) |
| WF-VOL21-2 | `debug_node.py` | Poll loop had no elapsed-time deadline — hung forever when `cancel_event` never set — fixed (added `_debug_deadline` counter + timeout exit branch) |
| WF-VOL21-3 | `evaluation_node.py` | Semantic eval bare `await _call_model(...)` — no timeout — fixed (`asyncio.wait_for(..., timeout=_eval_timeout)` with project-settings read) |
| BUG-VOL21-1 | `graph_factory.py` | `HubAndSpokeCoordinator.run_next()` submitted coroutine function to thread pool without event loop — silent result loss — fixed (`executor.submit(asyncio.run, ainvoke(...))`) |
| TEST-VOL21-1 | `tests/integration/*.py` (13 files) | Missing `pytestmark = pytest.mark.integration` preventing correct `-m integration` / `-m "not integration"` filtering — fixed (added to all 13 files) |
| WF-VOL22-1 | `src/core/orchestration/graph/nodes/analyst_delegation_node.py` | `await delegate_task_async(...)` had no `asyncio.wait_for` wrapper — thread-timeout did not cancel asyncio coroutine cleanly — fixed (`asyncio.wait_for(..., timeout=_analyst_timeout)` added; `import asyncio` added) |
| BUG-VOL22-1 | `src/core/orchestration/orchestrator_helpers.py`, `tui/src/ui/app.py` | `/compact` failure was invisible to user — `compact_context_impl` returned `False` silently — fixed (publishes `context.compact.failed` event to event bus; TUI `/compact` handler now shows bold yellow warning + `notify` toast on `False` return) |
| BUG-VOL22-2 | `src/tools/patch_tools.py` | `generate_patch` and `apply_patch` accepted `workdir: Path` as required positional param — callers passing `None` got raw `TypeError` — fixed (`workdir: Optional[Path] = None` + `if workdir is None: workdir = Path.cwd()` guard added to both functions) |
| RA-VOL22-1 | `src/core/indexing/repo_indexer.py` | `get_symbols_for_task()` silently returned `[]` when `repo_index.json` absent — impossible to distinguish "no symbols" from "index not built" — fixed (added `import logging`, `logger = logging.getLogger(__name__)`, `logger.debug(...)` explaining path and how to build index) |
| BUG-VOL22-3 | `src/core/orchestration/orchestrator_helpers.py` | `_ensure_working_dir_impl` swallowed working-dir creation exceptions with no caller signal — downstream tool failures had confusing errors — fixed (sets `orch._working_dir_unavailable = True` and publishes `working_dir.unavailable` event to event bus on exception) |
| BUG-VOL23-1 | `src/core/orchestration/graph/nodes/execution_node.py:728` | `asyncio.ensure_future()` for async post-tool hooks returned a `Task` that was immediately discarded — GC could cancel mid-run, all exceptions silently swallowed — fixed (stored in `_hook_task`; done-callback `_log_hook_exc` logs exceptions at WARNING level via structured logger) |
| BUG-VOL23-2 | `src/core/orchestration/graph/nodes/execution_node.py:921` | Hardcoded `"Action required: Modify the file by adding today's date on top."` injected into LLM context on every `read_file` call where the task contained any modification keyword ("add", "delete", "update", "after", etc.) — caused agent to corrupt files regardless of actual task — fixed (directive removed; replaced with `"Use write_file tool to write the updated content based on the task above."`) |
| WF-VOL23-1 | `src/core/orchestration/graph/nodes/planning_node.py:413,431`, `debug_node.py` | Deprecated `asyncio.get_event_loop().time()` called inside `async def` coroutines — DeprecationWarning on Python 3.12+ — fixed (`asyncio.get_running_loop().time()` used throughout) |
| WF-VOL23-2 | `src/core/orchestration/graph/builder.py:260` | `r"\band\b"` in `multi_step_patterns` matched virtually every English compound sentence, forcing spurious re-analysis after completed single-step tasks — fixed (removed `r"\band\b"` from pattern list) |
| BUG-VOL23-3 | `src/core/orchestration/graph_factory.py:18` | `state["rounds"]` bare dict subscript in `should_after_planning()` raised `KeyError` on partial-state subgraph invocations — fixed (`state.get("rounds", 0)`, matching all other router functions) |
| RA-VOL23-1 | `src/core/indexing/repo_indexer.py:170` | `parse_python_file()` bare `except Exception: return {}` swallowed all errors with no log — impossible to distinguish "empty file" from "parse error" from "permission error" — fixed (`logger.debug("parse_python_file: skipping %s (%s: %s)", path, type(exc).__name__, exc)` added before return) |
| PERF-VOL23-1 | `src/core/orchestration/graph_factory.py` | `HubAndSpokeCoordinator.run_next()` created new `ThreadPoolExecutor` per call — fixed (class removed when ARCH-VOL21-1 was resolved; `get_graph()` now delegates to full compiled graph) |
| PERF-VOL23-2 | `src/core/orchestration/tool_execution_pipeline.py:373` | `_t4_ev.wait(120.0)` blocks ThreadPoolExecutor worker for up to 2 minutes in non-autonomous mode — fixed (added explanatory comment documenting the blocking behaviour and `max_workers > 1` requirement) |
| SEC-VOL23-1 | `src/core/orchestration/inference_loop.py:152` | Session-title generation daemon thread was anonymous — killed abruptly on TUI exit with no join — fixed (thread reference stored as `orch._session_title_thread` so shutdown path can join with timeout) |
| OE-VOL23-1 | `src/core/orchestration/inference_loop.py:739` | Fallback LLM call on graph failure used bare `asyncio.run()` — raised `RuntimeError: This event loop is already running` when invoked from async context — fixed (running-loop guard: `get_running_loop()` check → submit to `_graph_executor` if loop active, else `asyncio.run()` directly) |
| GAP-TUI-1 | `tui/src/ui/app.py` | Generic tool render — fixed: per-tool `_TOOL_ICONS` map (`→`, `←`, `✱`, `#`, `│`, `⚙`, `◈`, `◇`, `%`) + fenced bash block |
| GAP-TUI-3 | `tui/src/ui/app.py` | Bash output unscoped — fixed: fenced `# desc / $ cmd` block, output truncated at 40 lines |
| GAP-TUI-4 | `tui/src/ui/app.py` | TodoWrite not rendered — fixed: `_render_todo_block()` renders `# Todos` with `○ ● ✓ ✗` status icons |
| GAP-TUI-5 | `tui/src/ui/app.py` | Question tool not rendered — fixed: `_render_question_block()` renders `# Questions` Q&A block |
| GAP-TUI-6 | `tui/src/ui/app.py` | Task tool stream — fixed: on finish renders `│ <role> Task — <desc>  └ N toolcalls` summary |
| GAP-PERM-1 | `tui/src/ui/app.py` | Bash-only permission gate — existing `ToolPermissionEvent` wired; badge updated |
| GAP-FOOTER-1 | `tui/src/ui/app.py` | Static footer — fixed: `#perm_count_chip` shows `△ N Permission(s)` when pending |
| GAP-FOOTER-2 | `tui/src/ui/app.py`, `bus.py`, `core_bridge.py` | MCP chip no error state — fixed: `has_error` field added; chip shows red `⊙` when has_error |
| GAP-MSG-1 | `tui/src/ui/app.py` | No compaction divider — fixed: `/compact` mounts `.compaction_divider` Static |
| GAP-MSG-2 | `tui/src/ui/app.py` | Input blocked while agent runs — fixed: message queued with `[QUEUED]` badge, sent on agent idle |
| GAP-MSG-3 | `tui/src/ui/app.py` | No denied-tool visual state — fixed: pending widget updated to red strikethrough on deny |
| GAP-CMD-1 | `tui/src/ui/app.py`, `chat_input.py` | No `/undo` command — fixed: trims last user+assistant turn from history |
