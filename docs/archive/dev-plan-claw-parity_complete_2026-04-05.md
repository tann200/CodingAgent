# Development Plan: claw-code-main Parity + TUI Integration
*Updated: 2026-04-03*

Four concurrent workstreams:

**Workstream A — TUI Integration:** Wire the standalone `tui/` into CodingAgent's orchestrator.
The new TUI supersedes `src/ui/` and already implements multiline input, diff approval,
session browsing, bash gates, permission prompts, streaming, reasoning display, and token
budget — they just need backend events wired up.

**Workstream B — Autonomy + claw Parity:** Make the system fully configurable for
autonomous runs, filling the remaining claw-code-main parity gaps.

**Workstream C — `src/ui/` Retirement:** Freeze, migrate tests, and delete the original UI.

**Workstream D — Python Best Practices + Agentic Architecture:** Harden the codebase for
production: type safety, structured errors, tool idempotency, observability, prompt
templates, async correctness, and god-class decomposition.

All workstreams share the EventBus and orchestrator as integration surface.

**claw Python source:** `/Users/tann200/PycharmProjects/claw-code-main/src/`
**New TUI source:** `tui/src/ui/`
**CodingAgent target:** `src/`

---

## Workstream A — TUI Integration

The new TUI communicates **exclusively via EventBus** — no `src.core` imports. The bridge
(`tui/src/ui/core_bridge.py::AgentBridge`) already calls `orchestrator.run_agent_once()`;
it just needs to be pointed at the real orchestrator and real EventBus.

The integration surface is exactly four things:
1. Swap `MockEventBus` for the real `EventBus`
2. Give `AgentBridge` a real `Orchestrator` instance
3. Align event names/payloads between the orchestrator and what the TUI expects
4. Publish the events the TUI needs that the orchestrator doesn't yet emit

---

### TUI-01 · Replace `src/ui/` as primary entry point with `tui/`

**What:** Make `tui/src/ui/app.py::AgentApp` the app that `src/main.py` launches.
`tui/src/ui/core_bridge.py::AgentBridge` is already the glue layer.

**AgentBridge contract (already implemented in `core_bridge.py`):**
```python
class AgentBridge:
    def __init__(self, app: AgentApp)
    def send_prompt(text: str) → bool          # thread-safe; calls run_agent_once in thread
    def interrupt() / force_interrupt()
    def is_running() → bool
    def approve_plan() / reject_plan()
    def bash_approved(tool_id) / bash_denied(tool_id)
    def start_new_session()
    def restore_and_continue(last_task, continue_state)
    def compact_context() → bool
```

**`_run_agent()` in `core_bridge.py` already does:**
```python
orchestrator.start_new_task()
messages = msg_mgr.messages or get_messages()
tools = orchestrator.get_tools_for_role("operational")
result = orchestrator.run_agent_once(
    system_prompt_name="operational",
    messages=messages, tools=tools,
    cancel_event=self._cancel_event
)
orchestrator.flush_execution_trace()
```
It just needs `self._orchestrator` to be a real `Orchestrator` and `self._bus` to be
the real `EventBus`.

**What to do:**
1. In `tui/src/ui/core_bridge.py::AgentBridge.__init__()`, replace the mock bus:
   ```python
   # Remove: self._bus = get_mock_event_bus()
   from src.core.orchestration.event_bus import EventBus
   from src.core.orchestration.orchestrator import Orchestrator
   self._bus = EventBus()
   self._orchestrator = Orchestrator(working_dir=Path.cwd(), event_bus=self._bus)
   ```
2. In `src/main.py::main()`, replace `CodingAgentApp` with `AgentApp` from `tui/`:
   ```python
   from tui.src.ui.app import AgentApp
   app = AgentApp()
   app.run()
   ```
3. Pass `working_dir` from `--workdir` CLI arg into `AgentBridge` constructor.

**Target files:**
- `tui/src/ui/core_bridge.py` — swap mock bus + mock orchestrator for real instances
- `src/main.py` — launch `AgentApp` instead of `CodingAgentApp`

---

### TUI-02 · Align EventBus event names to TUI spec

The TUI's `AgentBridge.setup_subscriptions()` expects specific event name strings.
The CodingAgent orchestrator publishes a different set. Map them.

**Complete event mapping** (TUI expected → CodingAgent published):

| TUI subscribes to | CodingAgent publishes | Action |
|---|---|---|
| `orchestrator.startup` | `system.startup` | rename in orchestrator |
| `tool.execute.start` | `tool.call.start` | rename in orchestrator |
| `tool.execute.finish` | `tool.call.finish` | rename in orchestrator |
| `tool.execute.error` | `tool.call.error` | add if missing |
| `file.diff.preview` | `file.diff.preview` | ✅ matches |
| `plan.progress` | `plan.progress` | ✅ matches |
| `plan.requested` | `plan.mode` | rename/add |
| `token.budget` | *(not published)* | add — TUI-09 |
| `session.new` | *(not published)* | add — TUI-01 |
| `agent.running` | *(not published)* | add — TUI-01 |
| `model.routing` | `provider.active` | rename |
| `provider.status` | `provider.status` | ✅ matches |
| `git.branch` | *(not published)* | add — TUI-07 |
| `role.transition` | `role.transition` | ✅ matches |
| `retry.attempt` | `retry.attempt` | ✅ matches |
| `retry.succeeded` | `retry.succeeded` | ✅ matches |
| `retry.failed` | `retry.failed` | ✅ matches |
| `context.degraded` | *(not published)* | add |
| `step.start` | `step.start` | ✅ matches |
| `step.finish` | `step.finish` | ✅ matches |
| `mcp.server.status` | *(not published)* | add — TUI-08 |
| `tool.permission_required` | *(not published)* | add — TUI-04 |
| `bash.approval_required` | *(not published)* | add — TUI-03 |
| `log.new` | `log.line` | rename |
| `task.file_modified` | `file.modified` | rename |

**What to do:**
Add a thin event translation shim in `tui/src/ui/core_bridge.py` OR rename the event
strings in the orchestrator. The shim approach is safer — add `_EVENT_MAP` in
`core_bridge.py`:
```python
_EVENT_MAP = {
    "orchestrator.startup":  "system.startup",
    "tool.execute.start":    "tool.call.start",
    "tool.execute.finish":   "tool.call.finish",
    "tool.execute.error":    "tool.call.error",
    "plan.requested":        "plan.mode",
    "model.routing":         "provider.active",
    "log.new":               "log.line",
    "task.file_modified":    "file.modified",
}
# In setup_subscriptions(), subscribe to mapped names
actual = _EVENT_MAP.get(tui_event, tui_event)
self._subscribe(actual, handler)
```

**Target files:**
- `tui/src/ui/core_bridge.py` — add `_EVENT_MAP`, remap subscription calls

---

### TUI-03 · Bash approval gate (TIER-3) — backend side

The TUI already has the full UI: `BashApprovalEvent` mounts a warning with Allow/Deny
buttons; `bash_approved(tool_id)` / `bash_denied(tool_id)` publish back to the bus.

Missing: the backend needs to **pause** and **wait** before executing tier-3 commands.

**Threading constraint — `bash()` is synchronous.**
`system_tools.py::bash()` is a plain sync function called from `execute_tool()` which
is itself sync (`orchestrator.py:1813: def execute_tool`). The graph calls it via the
async `_execute_tool_with_locks` wrapper in `execution_node.py`, but `bash()` itself
runs on a thread-pool executor. `asyncio.Event` **cannot** be awaited from a sync
function running in an executor thread — doing so raises `RuntimeError: no running event
loop` and would deadlock.

**Use `threading.Event` with a cross-thread callback instead:**
```python
# src/core/orchestration/orchestrator.py — module-level registry
import threading
_pending_bash: dict[str, threading.Event] = {}
_bash_denied:  set[str] = set()

def register_bash_gate(tool_id: str) -> threading.Event:
    ev = threading.Event()
    _pending_bash[tool_id] = ev
    return ev

def resolve_bash_gate(tool_id: str, approved: bool) -> None:
    if not approved:
        _bash_denied.add(tool_id)
    ev = _pending_bash.pop(tool_id, None)
    if ev:
        ev.set()
```

In `bash()` (sync, runs in executor):
```python
from src.core.orchestration.orchestrator import register_bash_gate, _bash_denied
tool_id = str(uuid.uuid4())[:8]
ev = register_bash_gate(tool_id)
event_bus.publish("bash.approval_required", {"tool_id": tool_id, "command": cmd})
approved = ev.wait(timeout=120.0)          # blocking — fine on executor thread
if not approved or tool_id in _bash_denied:
    _bash_denied.discard(tool_id)
    return {"status": "denied", "output": "Bash command denied by user."}
```

In the EventBus handlers registered by the orchestrator:
```python
# on "bash.approval_granted" / "bash.approval_denied"
resolve_bash_gate(payload["tool_id"], approved=True/False)
```

`AgentBridge.bash_approved/denied()` already publishes those events — no change needed
in the TUI.

**TIER3_PREFIXES** (copy from `core_bridge.py` into `system_tools.py` or a shared
`src/tools/_approval.py` constant):
```python
TIER3_PREFIXES = (
    "pip ", "pip3 ", "curl ", "wget ", "npm install", "npm i ",
    "cargo install", "go install", "go get",
    "apt ", "apt-get ", "yum ", "dnf ", "brew ",
    "sudo ", "su ", "chmod ", "chown ", "rm ", "del "
)
```

**Target files:**
- `src/tools/system_tools.py` — add tier-3 prefix check + `threading.Event` gate
- `src/core/orchestration/orchestrator.py` — `register_bash_gate`, `resolve_bash_gate`, event handlers
- `src/tools/_approval.py` — NEW: shared `TIER3_PREFIXES` constant (avoid duplication with `core_bridge.py`)

---

### TUI-04 · Tool permission gate — backend side

The TUI already handles `ToolPermissionEvent` with Allow/Deny buttons that post
`ToolPermissionApproved` / `ToolPermissionDenied` which the bridge converts to
`tool.permission_granted` / `tool.permission_denied` events.

Missing: the backend needs to publish `tool.permission_required` and wait.

**Threading constraint — `execute_tool()` is synchronous.**
`orchestrator.py:1813: def execute_tool` is a sync method. The same threading mismatch
as TUI-03 applies: `asyncio.Event.wait()` cannot be called from a sync function on an
executor thread. Use `threading.Event` with the same cross-thread pattern:

```python
# orchestrator.py — module-level
_pending_tool: dict[str, threading.Event] = {}
_tool_denied:  set[str] = set()

def register_tool_gate(tool_id: str) -> threading.Event:
    ev = threading.Event()
    _pending_tool[tool_id] = ev
    return ev

def resolve_tool_gate(tool_id: str, approved: bool) -> None:
    if not approved:
        _tool_denied.add(tool_id)
    ev = _pending_tool.pop(tool_id, None)
    if ev:
        ev.set()
```

In `execute_tool()`:
```python
required = get_tool_permission(tool_name)
if required in (PermissionLevel.DANGER, PermissionLevel.PROMPT) and not is_autonomous():
    tool_id = str(uuid.uuid4())[:8]
    ev = register_tool_gate(tool_id)
    self.event_bus.publish("tool.permission_required",
                           {"tool": tool_name, "args": args, "tool_id": tool_id})
    granted = ev.wait(timeout=120.0)
    if not granted or tool_id in _tool_denied:
        _tool_denied.discard(tool_id)
        return {"status": "denied", "error": f"Tool '{tool_name}' denied by user."}
```

Register EventBus handlers for `tool.permission_granted` / `tool.permission_denied`
that call `resolve_tool_gate()`.

**Target files:**
- `src/core/orchestration/orchestrator.py` — `register_tool_gate`, `resolve_tool_gate`, gate in `execute_tool()`

---

### TUI-05 · Diff accept/reject flow — blocking preview

The TUI's `SideBySideDiff` already renders Accept/Reject buttons. `Accepted` posts
`preview.confirmed`; `Rejected` posts `preview.rejected`. The bridge translates these
to `preview.confirmed` / `preview.rejected` EventBus events.

Missing: `file.diff.preview` currently fires **fire-and-forget** before the write.
It needs to **block** and await user decision.

**What to do:**
In `src/tools/file_tools.py::edit_file_atomic()`, when `autonomous_mode` is OFF:
```python
confirm_event = asyncio.Event()
_pending_previews[path_str] = confirm_event
event_bus.publish("file.diff.preview", {
    "path": path_str, "diff": diff_text, "is_new_file": is_new
})
await asyncio.wait_for(confirm_event.wait(), timeout=300.0)
if _preview_rejected.pop(path_str, False):
    return {"status": "rejected", "message": "Edit rejected by user."}
# proceed with write
```
Register `preview.confirmed` / `preview.rejected` handlers in the orchestrator that
set/flag the event.

In `autonomous_mode`, skip the wait entirely — write immediately (current behaviour).

**Target files:**
- `src/tools/file_tools.py` — add await-preview gate in `edit_file_atomic()`
- `src/core/orchestration/orchestrator.py` — register `preview.confirmed/rejected` handlers

---

### TUI-06 · Session list integration — wire to session persistence

`tui/src/ui/screens/session_list.py::SessionListScreen._load_sessions()` reads:
```python
Path.home() / ".coding_agent" / "sessions" / "session_*.json"
```
Each session JSON must have: `task_name` (or `task`), `messages` (list), `working_dir`.

The `_resume_selected()` method:
```python
bridge.history = [(m["role"], m["content"]) for m in messages]
bridge.working_dir = Path(working_dir)
```
then calls `start_new_session()` so the orchestrator continues from the loaded history.

**What to do:**
TASK-05 enriches the existing `_save_session_snapshot()` in `tui/src/ui/app.py` — it
already writes to `~/.coding_agent/sessions/session_{ts}.json` but is missing five
fields that `SessionListScreen` and resumption need. See TASK-05 in Workstream B for the
full enrichment spec.

Wire `AgentBridge.restore_and_continue(last_task, continue_state)` (already in bridge)
to call `orchestrator.start_new_task()` and inject `continue_state["messages"]` as
the initial history.

**Target files:**
- `tui/src/ui/app.py::_save_session_snapshot()` — enrich payload (TASK-05)
- `src/core/orchestration/session_store.py` — NEW: `load_session()`, `list_sessions()` for `SessionListScreen`
- `tui/src/ui/core_bridge.py::restore_and_continue()` — wire to orchestrator

---

### TUI-07 · Git branch status publishing

The TUI's sidebar has a git section wired to `GitBranchEvent(branch, dirty, ahead, behind)`.

**What to do:**
Add a `_publish_git_status()` helper to the orchestrator:
```python
def _publish_git_status(self):
    import subprocess
    try:
        branch = subprocess.check_output(
            ["git", "branch", "--show-current"], cwd=self.working_dir,
            text=True, timeout=3
        ).strip()
        status = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=self.working_dir,
            text=True, timeout=3
        )
        dirty = bool(status.strip())
        self.event_bus.publish("git.branch", {
            "branch": branch, "dirty": dirty, "ahead": 0, "behind": 0
        })
    except Exception:
        pass
```
Call it in `start_new_task()` and after each `run_agent_once()` completes.
For `ahead`/`behind`: optionally parse `git status -sb` output.

**Target files:**
- `src/core/orchestration/orchestrator.py` — add `_publish_git_status()`, call at task start/end

---

### TUI-08 · MCP server status publishing

The TUI's footer chip shows `McpServerStatusEvent(running, count, server_names)`.

**What to do:**
In `MCPStdioServer.__init__()` / `start()`, publish:
```python
event_bus.publish("mcp.server.status", {
    "running": True, "count": 1, "server_names": ["codingagent"]
})
```
On shutdown / error, publish `running=False`.

**Target files:**
- `src/core/mcp/mcp_server.py` — publish `mcp.server.status` on start/stop

---

### TUI-09 · Token budget event publishing

The TUI sidebar has a full token budget bar driven by
`TokenBudgetEvent(used, limit, percent, warning)`.

**What to do:**
After each `call_model()` in `orchestrator.py`, alongside the existing usage tracking:
```python
used = total_input + total_output          # session running total
limit = state.get("max_tokens", 32_768)
percent = min(100, int(used / limit * 100))
self.event_bus.publish("token.budget", {
    "used": used, "limit": limit,
    "percent": percent,
    "warning": percent >= 80
})
```
Also publish `token.usage` (legacy):
```python
self.event_bus.publish("token.usage", {
    "input_tokens": input_tokens,
    "output_tokens": output_tokens,
    "model": model_name,
    "cost_usd": estimate_cost_usd(input_tokens, output_tokens, model_name)
})
```

**Target files:**
- `src/core/orchestration/orchestrator.py` — publish `token.budget` + `token.usage` after `call_model()`

---

### TUI-10 · Reasoning / thinking display

The TUI has `ThinkingProcess` (collapsible, shows elapsed time) and `StreamView` for
normal streaming. `AgentBridge` already subscribes to both `response.stream_chunk` and
`response.reasoning_chunk` and translates them to `StreamChunkEvent` /
`DisplayReasoning` Textual messages.

**Detection approach — three sources, checked in order:**

1. **`reasoning_content` / `thinking` response fields** — some providers (OpenAI o1,
   some OpenRouter routes) return thinking as a separate field alongside `content`.
   Already partially handled at `openai_compat_adapter.py:482–483`.

2. **`<think>…</think>` tags in streamed content** — models like Qwen3 and
   DeepSeek-R1 embed thinking inline. `thinking_utils.py` already has
   `_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)` and
   `is_reasoning_model(model_id)`.

3. **`is_reasoning` flag on the stream delta** — some providers send a structured
   delta with a boolean field. Fall back to tag detection if absent.

**Event design — extend payload, not a new event type:**
Use a single `response.stream_chunk` event with an `is_reasoning: bool` field rather
than a second event. This keeps the bridge subscription count down and lets the bridge
route based on the field:

```python
# In core_bridge.py _on_stream_chunk handler:
if payload.get("is_reasoning"):
    self._app.post_message(DisplayReasoning(payload["chunk"], start_time=...))
else:
    self._app.post_message(StreamChunkEvent(payload["chunk"]))
```

**What to implement in the adapter:**
```python
# openai_compat_adapter.py — inside the streaming loop
_thinking_buf: list[str] = []
_content_buf:  list[str] = []

for delta in stream:
    # Source 1: structured reasoning_content field
    reasoning_delta = delta.get("reasoning_content") or delta.get("thinking", "")
    content_delta   = delta.get("content") or delta.get("text", "")

    # Source 2: <think> tag split on content_delta if no structured field
    if not reasoning_delta and "<think>" in content_delta:
        # split on opening tag — everything inside is reasoning
        before, _, rest = content_delta.partition("<think>")
        thinking_part, _, after = rest.partition("</think>")
        if before:
            event_bus.publish("response.stream_chunk",
                              {"chunk": before, "is_reasoning": False})
        if thinking_part:
            event_bus.publish("response.stream_chunk",
                              {"chunk": thinking_part, "is_reasoning": True})
        content_delta = after   # remainder is normal content

    if reasoning_delta:
        event_bus.publish("response.stream_chunk",
                          {"chunk": reasoning_delta, "is_reasoning": True})
    if content_delta:
        event_bus.publish("response.stream_chunk",
                          {"chunk": content_delta, "is_reasoning": False})
```

Publish `response.stream_start` before the loop and `response.stream_end` with the
full accumulated text after. Use `thinking_utils.is_reasoning_model(model_id)` to set
a session flag that enables the `<think>` split path, avoiding the tag scan overhead
for non-reasoning models.

**Target files:**
- `src/core/inference/adapters/openai_compat_adapter.py` — implement three-source detection, publish per-chunk with `is_reasoning`
- `tui/src/ui/core_bridge.py::_on_stream_chunk` — route on `is_reasoning` field to `StreamChunkEvent` vs `DisplayReasoning`

---

### TUI-11 · System settings handshake

On startup, `AgentApp.on_mount()` posts `RequestSystemSettings()`. The bridge needs
to respond with `SystemSettingsLoaded`. Currently wired to mock engine.

**What to do:**
In `AgentBridge.setup_subscriptions()` or `__init__()`, after orchestrator is ready,
call a `_publish_system_settings()` method:
```python
def _publish_system_settings(self):
    from src.core.config_loader import load_merged_config
    cfg = load_merged_config(self._orchestrator.working_dir)
    providers = [...]   # from providers.json
    self._bus.publish("system.settings", {
        "active_mode": cfg.get("active_mode", "lead_architect"),
        "theme": cfg.get("theme", "textual-dark"),
        "context_window": cfg.get("max_tokens", 32_768),
        "default_provider": cfg.get("default_provider", "none"),
        "default_model": cfg.get("default_model", "none"),
        "providers": providers,
        ...
    })
```
Also publish `orchestrator.startup` / `agent.running` events.

**Target files:**
- `tui/src/ui/core_bridge.py` — add `_publish_system_settings()`, called after init

---

## Workstream B — Autonomy + claw Parity

---

### AUTO-01 · `autonomous_mode` — global flag that bypasses all approval gates

**What it controls:**
When `autonomous_mode = true`:
- Bash tier-3 gate (TUI-03) auto-approves — no user prompt
- Tool permission gate (TUI-04) auto-approves DANGER/PROMPT tools
- Diff preview gate (TUI-05) auto-applies writes without confirmation
- Plan mode: plans auto-approved
- `max_turns` raised to 200 (configurable)
- Active permission mode set to `ALLOW`

**Config surface (all three ways):**
```python
# 1. In .agent/config.json:
{"autonomous_mode": true, "max_turns": 200}

# 2. CLI flag:
codingagent --autonomous --task "refactor auth module"

# 3. At runtime via /settings or /provider TUI command
```

**What to do:**
1. Add `autonomous_mode: bool = False` to `src/core/config_loader.py` bundled defaults.
2. Add `--autonomous` flag to `src/main.py::_parse_args()`.
3. Add `is_autonomous() -> bool` to `src/tools/tools_config.py`:
   ```python
   _AUTONOMOUS = False
   def is_autonomous() -> bool: return _AUTONOMOUS
   def set_autonomous(value: bool): global _AUTONOMOUS; _AUTONOMOUS = value
   ```
4. In all approval gates (TUI-03, TUI-04, TUI-05) check `is_autonomous()` first —
   skip the wait and proceed immediately.
5. Publish `agent.running` event with `{"running": True, "autonomous": True}` so
   the TUI banner can indicate autonomous mode visually.

**Target files:**
- `src/tools/tools_config.py` — add `is_autonomous()`, `set_autonomous()`
- `src/main.py` — add `--autonomous` flag
- `src/core/config_loader.py` — add `autonomous_mode` to defaults
- `tui/src/ui/core_bridge.py` — read autonomy from config, pass to bridge

---

### AUTO-02 · Per-role autonomy settings

The new TUI has three agent roles: `lead_architect`, `full_stack_engineer`, `qa_lead`.
These map to CodingAgent's `operational` / `strategic` / analysis roles. Each role
should have its own autonomy level.

**Config schema:**
```json
{
  "roles": {
    "lead_architect":      {"autonomous": false, "max_turns": 50,  "permission_mode": "prompt"},
    "full_stack_engineer": {"autonomous": true,  "max_turns": 100, "permission_mode": "workspace_write"},
    "qa_lead":             {"autonomous": true,  "max_turns": 30,  "permission_mode": "read_only"}
  }
}
```

**What to do:**
Add `get_role_config(role: str) -> dict` to `src/core/config_loader.py`.
In `AgentBridge._run_agent()`, read the active role's config and apply it before
calling `run_agent_once()`:
```python
role_cfg = config_loader.get_role_config(active_role)
set_autonomous(role_cfg.get("autonomous", False))
set_active_permission_mode(PermissionLevel(role_cfg.get("permission_mode", "prompt")))
state["max_turns"] = role_cfg.get("max_turns", 50)
```

**Target files:**
- `src/core/config_loader.py` — add `get_role_config()`
- `tui/src/ui/core_bridge.py` — apply role config before each run

---

### AUTO-03 · Role → CodingAgent node mapping

The TUI cycles through `lead_architect` / `full_stack_engineer` / `qa_lead` and sends
`RoleTransitionEvent`. CodingAgent's pipeline uses `operational` / `strategic` etc.
These need to map so the right system prompt is loaded.

**claw reference:**
- `runtime.py::_infer_permission_denials()` — gates based on role type.
- `settings.py::AGENTS` — the three role definitions.

**Mapping:**
```python
TUI_ROLE_TO_PROMPT = {
    "lead_architect":      "strategic",    # planning, design
    "full_stack_engineer": "operational",  # execution, coding
    "qa_lead":             "reviewer",     # review, testing
}
```

**What to do:**
In `AgentBridge._run_agent()`, map `active_role` to a system prompt name:
```python
prompt_name = TUI_ROLE_TO_PROMPT.get(self._active_role, "operational")
result = orchestrator.run_agent_once(system_prompt_name=prompt_name, ...)
```
Publish `role.transition` event when the TUI changes active role so CodingAgent's
`delegation_node` can react.

**Target files:**
- `tui/src/ui/core_bridge.py` — add `TUI_ROLE_TO_PROMPT` mapping, apply in `_run_agent()`

---

### TASK-01 · Wire PermissionLevel enforcement in `execute_tool()`

*(Unchanged from original plan — prerequisite for TUI-04)*

Add `_active_permission_mode` module var and `set_active_permission_mode()` to
`src/tools/tools_config.py`. In `execute_tool()` check required vs active level.

**claw ref:** `permissions.py::ToolPermissionContext.blocks()`

**Target files:**
- `src/tools/tools_config.py` — `_active_permission_mode`, `set_active_permission_mode()`
- `src/core/orchestration/orchestrator.py` — check in `execute_tool()`

---

### TASK-03 · Plugin–builtin name conflict detection

Add `_origins: Dict[str, str]` to `ToolRegistry`; raise `ValueError` when plugin
tries to overwrite a builtin.

**claw ref:** `tools.py::filter_tools_by_permission_context()` (conflict-awareness pattern)

**Target files:**
- `src/tools/_registry.py` — add `_origins`, conflict check in `register()`

---

### TASK-04 · Secure API key storage

Use `keyring` (cross-platform) for API keys; fall back to `prefs.json` with warning.
Add `src/core/credentials.py`.

**claw ref:** `prefetch.py::start_keychain_prefetch()` (keychain concept)

**Target files:**
- `src/core/credentials.py` — NEW
- `src/ui/components/settings_panel.py` — use `credentials.py`
- `tui/src/ui/config_writer.py` — use `credentials.py` instead of raw JSON

---

### TASK-05 · Session persistence — enrich existing snapshot + orchestrator-side trigger

**What already exists (do NOT rebuild from scratch):**

`tui/src/ui/app.py::_save_session_snapshot()` already writes session JSON to
`~/.coding_agent/sessions/session_{ts}.json`.  It is already triggered at:
- `on_unmount()` — app exits
- `action_quit_app()` — user presses Ctrl-Q
- `/new` slash command — user starts fresh session

Current payload written by `_save_session_snapshot()`:
```json
{
  "timestamp": "2026-04-03T04:11:00Z",
  "task_name": "...",
  "message_count": 12,
  "messages": [{"role": "...", "content": "..."}],
  "working_dir": "/path"
}
```

**What's missing** (what `SessionListScreen` and resumption need but can't find):
```json
{
  "version": 1,           ← for forward-compat schema checks
  "session_id": "...",    ← stable UUID for load_session() lookup
  "turn_count": 12,       ← from AgentState.turn_count
  "input_tokens": 4200,   ← from orchestrator._usage_buffer accumulator
  "output_tokens": 1800   ← same
}
```

**What to do:**

1. **Enrich `_save_session_snapshot()` in `tui/src/ui/app.py`** — add the five missing
   fields. Pull `turn_count` from `bridge.get_turn_count()` (add that accessor to
   `AgentBridge`). Pull token totals from `bridge.get_usage_totals()` (add accessor
   that reads `orchestrator._usage_buffer`). Generate `session_id` once in
   `AgentApp.__init__()` as `str(uuid.uuid4())` and store on `self`.

2. **Add orchestrator-side trigger** — the UI quit/unmount path already covers normal
   operation, but a task that completes without the user quitting (e.g. headless run,
   autonomous mode) never triggers the save. Add a call to
   `core_bridge.AgentBridge._save_session_snapshot()` (or a standalone
   `session_store.save_session()`) at the end of `_run_agent()` in `core_bridge.py`,
   after `orchestrator.flush_execution_trace()`.

3. **Add `src/core/orchestration/session_store.py`** — thin claw port for headless/CLI
   use (no TUI bridge required). `SessionListScreen` can use this directly for load:
   ```python
   # claw ref: session_store.py — StoredSession, save_session(), load_session()
   @dataclass
   class StoredSession:
       version: int
       session_id: str
       task_name: str
       working_dir: str
       messages: list[dict]
       turn_count: int
       input_tokens: int
       output_tokens: int
       created_at: str

   def save_session(session: StoredSession, path: Path | None = None) -> Path: ...
   def load_session(session_id: str) -> StoredSession | None: ...
   def list_sessions(limit: int = 50) -> list[StoredSession]: ...
   ```
   Write path: `~/.coding_agent/sessions/session_{session_id}.json`

**Full target schema** (after enrichment):
```json
{
  "version": 1,
  "session_id": "a3f2...",
  "task_name": "refactor auth module",
  "working_dir": "/home/user/project",
  "messages": [{"role": "user", "content": "..."}, ...],
  "message_count": 12,
  "turn_count": 12,
  "input_tokens": 4200,
  "output_tokens": 1800,
  "timestamp": "2026-04-03T04:11:00Z"
}
```

**Target files:**
- `tui/src/ui/app.py` — enrich `_save_session_snapshot()` payload; add `session_id` attr
- `tui/src/ui/core_bridge.py` — add `get_turn_count()`, `get_usage_totals()` accessors; trigger save at end of `_run_agent()`
- `src/core/orchestration/session_store.py` — NEW: `StoredSession`, `save_session()`, `load_session()`, `list_sessions()`

---

### TASK-07 · Token-based compaction trigger

Replace message-count trigger (≥50) with token-count trigger.

**claw ref:** `query_engine.py::QueryEngineConfig.max_budget_tokens`,
`compact_messages_if_needed()`

Add token estimate check to `distiller.py` before compacting.
Read threshold from `config_loader.get("compact_token_threshold", 6000)`.

**Target files:**
- `src/core/inference/distiller.py` — add token-estimate guard
- `src/core/config_loader.py` — add `compact_token_threshold` default

---

### TASK-08 · Compact summary as System message + continuation signal

Rust `compact.rs` pattern: inject `{"role": "system", "content": "<summary>...</summary>"}`
as first message, append recent messages, then append user continuation message.

**Target files:**
- `src/core/inference/distiller.py` — change injection role to system; append continuation

---

### TASK-09 · `--allowed-tools` / `--deny-tool` / `--deny-prefix` CLI flags

Direct port of `permissions.py::ToolPermissionContext` from claw.

**claw ref:** `permissions.py` — entire file (30 lines). Copy `ToolPermissionContext`
into `src/tools/permission_context.py`. Add flags to `_parse_args()`.

**Target files:**
- `src/tools/permission_context.py` — NEW (copy from claw `permissions.py`)
- `src/main.py` — add `--allowed-tools`, `--deny-tool`, `--deny-prefix`
- `src/core/orchestration/orchestrator.py` — filter registry at init

---

### TASK-10 · `init` subcommand

Scaffold `.agent/` with config, hooks, gitignore, and blank `AGENT.md`.

**claw ref:** `setup.py::WorkspaceSetup.startup_steps()`, `deferred_init.py`

**Target files:**
- `src/main.py` — add `init` subcommand

---

### TASK-11 · Deferred feature gating

**claw ref — direct port:** `deferred_init.py::DeferredInitResult` + `run_deferred_init(trusted)`.
Gate MCP server, hook runner, plugin loading on `trusted` flag.

**Target files:**
- `src/core/orchestration/deferred_init.py` — NEW
- `src/core/orchestration/orchestrator.py` — gate subsystem init

---

### TASK-12 · `max_turns` guard at Orchestrator level

Add pre-graph check in `run_agent_once()` before graph execution.

**claw ref:** `query_engine.py::submit_message()` max_turns check at entry.

**Target files:**
- `src/core/orchestration/orchestrator.py` — add pre-graph guard

---

### TASK-13 · Hook scripts run async

Replace `subprocess.run` with `asyncio.create_subprocess_exec` in `tool_hooks.py`.

**Target files:**
- `src/core/orchestration/tool_hooks.py` — async subprocess

---

### TASK-14 · Structured patch hunks in edit tools

Add `generate_unified_diff()` to `patch_tools.py` using `difflib.unified_diff`.
Include diff in `file.diff.preview` payload and tool result.

**Target files:**
- `src/tools/patch_tools.py` — add `generate_unified_diff()`
- `src/tools/file_tools.py` — call in `edit_file_atomic()`, `edit_by_line_range()`

---

### TASK-15 · Per-turn token display *(superseded by TUI-09)*

The new TUI already renders `TokenBudgetEvent` and `TokenUsageEvent` in the sidebar.
This task is now just the backend publish side — covered in TUI-09.

**Target files:**
- `src/core/orchestration/orchestrator.py` — see TUI-09

---

### TASK-16 · Alias resolution at startup

In `build_registry()`, call `reg.alias(short, canonical)` for all `TOOL_ALIASES`
so aliases appear in listings and schema export.

**claw ref:** `tools.py::get_tools()` — pool assembled once at construction.

**Target files:**
- `src/tools/_registry.py::build_registry()` — call `reg.alias()` for `TOOL_ALIASES`
- `src/core/orchestration/orchestrator.py::execute_tool()` — remove per-call lookup

---

### TASK-17 · Glob truncation reporting

Return `{"files": [...], "truncated": bool, "total_found": int}` when results exceed
`MAX_GLOB_RESULTS = 10_000`.

**Target files:**
- `src/tools/repo_tools.py`

---

### TASK-18 · Model pricing table

Add `_PRICING: dict[str, tuple[float, float]]` and `estimate_cost_usd()` to
`provider_context.py`. Used by TUI-09 cost display.

**Target files:**
- `src/core/inference/provider_context.py`

---

### TASK-19 · Multiline REPL input *(already done in new TUI)*

`tui/src/ui/components/chat_input.py::ChatTextArea` has Shift+Enter support.
No separate task needed once TUI-01 is complete.

---

### TASK-20 · `--permission-mode` CLI flag

Requires TASK-01's `set_active_permission_mode()`.

**Target files:**
- `src/main.py` — add `--permission-mode` flag

---

### TASK-21 · Outbound MCP client

Create `src/core/mcp/mcp_client.py::McpStdioClient` (stdio transport first).
Read `mcp_servers` from config; register MCP tools in `build_registry()`.

**Target files:**
- `src/core/mcp/mcp_client.py` — NEW
- `src/tools/_registry.py::build_registry()` — MCP tool registration

---

### TASK-22 · `system-prompt` debug command

Add `system-prompt` subcommand to `src/main.py` that prints the resolved system
prompt for a given working dir.

**Target files:**
- `src/main.py`

---

## Dependency Graph

```
TUI-01  (swap mock bus → real bus)
  └── TUI-02  (event name mapping)
  └── TUI-11  (settings handshake)
  └── AUTO-03 (role mapping)

TUI-03  (bash gate)   ← requires AUTO-01 (autonomous bypass)
TUI-04  (tool perm gate) ← requires TASK-01, AUTO-01
TUI-05  (diff approve) ← requires AUTO-01
TUI-06  (session list) ← requires TASK-05

AUTO-01 (autonomous flag)
  └── AUTO-02 (per-role autonomy)
  └── TUI-03, TUI-04, TUI-05 (bypass gates when set)

TASK-01  → TASK-02 (PROMPT modal, now via TUI-04)
TASK-05  → TUI-06 (session list)
TASK-07  → TASK-08 (compaction)
TASK-14  → TUI-05 (diff payload)
TUI-09   → TASK-18 (pricing table for cost display)
TASK-11  → TASK-21 (MCP client gated by mcp_prefetch)
```

---

## Task Summary

### Workstream C — `src/ui/` Retirement (do in parallel with A)

| Task | What | Effort |
|------|------|--------|
| LEGACY-01 | Add deprecation headers to `src/ui/` — freeze immediately | S |
| LEGACY-02 | Migrate 16 test files (~4 800 lines) to `core_bridge.py` + `mock_eventbus.py` | L |
| LEGACY-03 | Delete `src/ui/` after Sprint 1 gate passes | S |
| LEGACY-04 | Audit unique logic to absorb before deletion | S |

### Workstream A — TUI Integration (do first)

| Task | What | Effort |
|------|------|--------|
| TUI-01 | Swap mock → real orchestrator + EventBus in bridge | M |
| TUI-02 | Event name translation shim in `core_bridge.py` | S |
| TUI-03 | Bash tier-3 approval gate — backend pause/resume | M |
| TUI-04 | Tool permission gate — backend pause/resume | M |
| TUI-05 | Diff preview blocking flow — await accept/reject | M |
| TUI-06 | Session list schema alignment + bridge resume | S |
| TUI-07 | Git branch status publishing | S |
| TUI-08 | MCP server status publishing | S |
| TUI-09 | Token budget + usage event publishing | S |
| TUI-10 | Reasoning/thinking stream events | S |
| TUI-11 | System settings handshake on startup | S |

### Workstream B — Autonomy + Parity (interleaved)

| Task | What | Effort |
|------|------|--------|
| AUTO-01 | `autonomous_mode` flag — bypass all approval gates | S |
| AUTO-02 | Per-role autonomy config (max_turns, permission_mode) | S |
| AUTO-03 | TUI role → CodingAgent system prompt mapping | S |
| TASK-01 | Wire PermissionLevel enforcement in `execute_tool()` | S |
| TASK-03 | Plugin–builtin conflict detection | S |
| TASK-04 | Secure keychain credential storage | M |
| TASK-05 | Session persistence — enrich existing snapshot + orchestrator trigger + `session_store.py` | M |
| TASK-07 | Token-based compaction trigger | S |
| TASK-08 | Compact summary as System message | S |
| TASK-09 | `--allowed-tools` / `--deny-tool` CLI flags | S |
| TASK-10 | `init` subcommand | S |
| TASK-11 | Deferred feature gating | S |
| TASK-12 | `max_turns` guard at Orchestrator level | S |
| TASK-13 | Async hook scripts | S |
| TASK-14 | Unified diff in edit tools | S |
| TASK-16 | Alias resolution at startup | S |
| TASK-17 | Glob truncation reporting | S |
| TASK-18 | Model pricing table | S |
| TASK-20 | `--permission-mode` CLI flag | S |
| TASK-21 | Outbound MCP client (stdio) | L |
| TASK-22 | `system-prompt` debug command | S |

**Effort key:** S = <1 hr, M = 2–4 hr, L = >4 hr

---

## Workstream C — `src/ui/` Retirement

The original `src/ui/textual_app_impl.py` (4,153 lines) is a strict subset of the new TUI on
every axis. Refactoring it to remove direct `src.core` imports would reproduce what
`tui/core_bridge.py` already is, at the cost of significant effort and without gaining the
features the new TUI adds (session list, timeline, diff approval, bash gate, tool permission
gate, 3-role model, thinking widget, token budget sidebar). The correct path is a controlled
retirement over three steps that tracks Sprint 1 of Workstream A.

---

### LEGACY-01 · Freeze `src/ui/` and add deprecation notice

Do this **immediately** — before any other work — to prevent new code being added to the
wrong place.

**What to do:**
Add a module-level deprecation comment to `src/ui/app.py::create_app()` and
`src/ui/textual_app_impl.py`:

```python
# DEPRECATED — superseded by tui/src/ui/app.py (AgentApp).
# This module is kept alive only as a headless/fallback path for
# --output-format json|raw modes where no TUI is launched.
# Do not add new features here. Target removal: after Sprint 1 of Workstream A.
```

The headless `_run_headless()` path in `src/main.py` does **not** import `src/ui/` —
it calls `Orchestrator.run_agent_once()` directly. So `src/ui/` is only needed when
`main()` launches the Textual app interactively; once TUI-01 is complete that path
switches to `AgentApp`.

**What NOT to do:** Don't rename, move, or delete files yet — the test suite references
`src/ui/` and removal before test migration (LEGACY-02) would break CI.

**Target files:**
- `src/ui/app.py` — add deprecation header
- `src/ui/textual_app_impl.py` — add deprecation header

---

### LEGACY-02 · Migrate UI tests to `core_bridge.py` + `mock_eventbus.py`

**Why:** Tests that currently exercise `src/ui/` event subscriptions test the integration
between EventBus events and UI state updates. The new TUI provides a better isolation
surface: `mock_eventbus.py` + `AgentBridge` can be tested without launching a full
Textual app.

**Scope — confirmed by `grep -rl "from src\.ui\|import src\.ui" tests/`:**

All 16 files that must be migrated (total ~4 800 lines across the suite):

| File | Lines | Migration category |
|------|-------|--------------------|
| `tests/unit/test_diff_verification_bash_tui_rollback.py` | ~1 216 | Heavy TUI interaction — rewrite against `AgentBridge` + `MockEventBus` |
| `tests/unit/test_standalone_tui.py` | ~846 | Full app smoke tests — rewrite against `AgentBridge` in dev-mode |
| `tests/unit/test_tui_fixes.py` | ~671 | Widget-level assertions — rewrite as bus event assertions |
| `tests/unit/test_tui_threading.py` | ~250 | Threading correctness — keep logic, swap `CodingAgentTextualApp` for bridge |
| `tests/unit/test_ui_main_view.py` | ~200 | MainView widget tests — rewrite as `mock_app.post_message` assertions |
| `tests/unit/test_log_panel.py` | ~180 | LogPanel — rewrite as bus event → bridge state assertions |
| `tests/unit/test_settings_panel.py` | ~170 | SettingsPanel — rewrite against new `SettingsScreen` widget |
| `tests/unit/test_history_wrapper.py` | ~160 | `_HistoryWrapper` (LEGACY-04 candidate) — absorb or delete if covered |
| `tests/unit/test_provider_panel.py` | ~150 | ProviderPanel — rewrite against new `SettingsScreen` provider section |
| `tests/unit/test_dashboard.py` | ~140 | Dashboard layout — check if covered by new TUI; remove if so |
| `tests/unit/test_llm_markup_handling.py` | ~130 | Markup parsing — move to pure-function unit test, no UI dep needed |
| `tests/unit/test_app_telemetry.py` | ~120 | Telemetry events — rewrite as bus publish → `mock_bus.received` assertions |
| `tests/unit/test_github_copilot_adapter.py` | ~120 | Adapter test — remove `src.ui` import if it's incidental; test adapter directly |
| `tests/unit/test_openrouter_adapter.py` | ~110 | Same as above — adapter test with incidental UI import |
| `tests/unit/test_eventbus_thread_pool_graph_singleton.py` | ~100 | EventBus + graph wiring — keep test logic, remove `src.ui` fixture |
| `tests/unit/test_tool_safety_node_caching_plan_contracts.py` | ~90 | Node contracts — remove `src.ui` import if it's incidental |

**Migration categories:**

*Category A — full rewrite (~2 733 lines: diff_verification, standalone_tui, tui_fixes):*
Replace `CodingAgentTextualApp` with `AgentBridge` + `MockEventBus`. Use the pattern
below. These are the most expensive tests; tackle them last after the pattern is proven
on smaller files.

*Category B — light rewrites (~680 lines: tui_threading, ui_main_view, log_panel, settings_panel, provider_panel, dashboard, app_telemetry):*
Keep the assertion logic, replace the UI fixture with a bridge fixture.

*Category C — incidental imports (~280 lines: llm_markup_handling, github_copilot_adapter, openrouter_adapter, eventbus_thread_pool_graph_singleton, tool_safety_node_caching_plan_contracts):*
The `src.ui` import is not load-bearing — remove the import and test the actual subject
directly. May require zero logic changes.

*Category D — absorb or delete (~160 lines: history_wrapper):*
`_HistoryWrapper` logic should be confirmed absorbed into `tui/core_bridge.py`
(see LEGACY-04) then the test deleted, or kept as a pure-function test with no UI dep.

**Shared fixture pattern (add to `tests/conftest.py`):**
```python
import pytest
from unittest.mock import MagicMock

@pytest.fixture
def mock_bridge():
    from tui.src.ui.mock_eventbus import get_mock_event_bus, reset_mock_event_bus
    from tui.src.ui.core_bridge import AgentBridge
    reset_mock_event_bus()
    bus = get_mock_event_bus()
    mock_app = MagicMock()
    bridge = AgentBridge(mock_app)
    bridge._bus = bus
    bridge.setup_subscriptions()
    return bridge, bus, mock_app
```

**Per-test migration pattern:**
```python
# OLD
from src.ui.textual_app_impl import CodingAgentTextualApp
app = CodingAgentTextualApp(orchestrator=mock_orch)
app._on_tool_start({"tool_name": "read_file", "tool_id": "t1"})
assert app.some_widget.value == "read_file"

# NEW (using mock_bridge fixture)
def test_tool_start_updates_ui(mock_bridge):
    bridge, bus, mock_app = mock_bridge
    bus.publish("tool.execute.start", {"tool_name": "read_file", "tool_id": "t1"})
    mock_app.post_message.assert_called()
    posted = mock_app.post_message.call_args[0][0]
    assert posted.tool_name == "read_file"
```

**Execution order (recommended):**
1. Category C — incidental imports (fastest wins, zero logic changes)
2. Category D — history_wrapper (absorb or delete)
3. Category B — light rewrites (prove the fixture pattern)
4. Category A — full rewrites (last; largest; parallelise across authors if possible)

**Prerequisite:** TUI-01 complete (real bridge wired) and `mock_bridge` fixture merged.

---

### LEGACY-03 · Delete `src/ui/` after Sprint 1 validation

**Gate:** Sprint 1 tasks (TUI-01, TUI-02, TUI-11, AUTO-03, TUI-09, TUI-07) complete AND
test suite passing with `src/ui/` tests migrated.

**What to do:**
```bash
git rm -r src/ui/
```
Remove the `src/ui/` import from `src/main.py` (replaced by `tui/` import in TUI-01).

The only thing to verify before deletion:
1. `grep -r "from src.ui\|import src.ui" src/ tests/` returns nothing
2. Full test suite passes
3. `src/main.py` launches `AgentApp` from `tui/` without falling back

**Target files:**
- `src/ui/` — delete entire directory
- `src/main.py` — remove old `CodingAgentApp` import block

---

### LEGACY-04 · Absorb unique `src/ui/` logic into `tui/` if not already present

Before deleting (during LEGACY-02), audit for anything in `src/ui/textual_app_impl.py`
not already replicated in `tui/`:

| Feature | `src/ui/` | `tui/` | Action |
|---|---|---|---|
| `_HistoryWrapper` (checkpoint/extend/clear) | ✅ | ❌ | Port to `core_bridge.py` history management |
| `_compute_cost()` pricing logic | ✅ | ❌ | Move to `src/core/inference/provider_context.py` (TASK-18) |
| `_refresh_provider_info()` probe logic | ✅ | partial | Verify `ProbeResultsScreen` covers it |
| `TextualAppStub` headless stub | ✅ | ✅ `TextualAppStub` in `tui/main.py` | No action needed |
| `_safe_write()` thread-safe write | ✅ | ✅ via `call_from_thread` | No action needed |
| Frecency prompt history (full scoring) | ✅ | ✅ in `core_bridge.py` | No action needed |
| @file picker + expansion | ✅ | ✅ in `app.py` | No action needed |

The only items requiring porting before deletion are `_HistoryWrapper` checkpointing
and the `_compute_cost()` pricing table — both small, both covered by TASK-18.

---

## Recommended Execution Order

**Sprint 0 — Freeze legacy UI (do before anything else):**
LEGACY-01

**Sprint 1 — Core wiring (makes the TUI run against real backend):**
TUI-01 → TUI-02 → TUI-11 → AUTO-03 → TUI-09 → TUI-07

**Sprint 1 exit gate — then retire legacy:**
LEGACY-04 (audit) → LEGACY-02 (migrate tests) → LEGACY-03 (delete `src/ui/`)

**Sprint 2 — Autonomy + approval gates:**
AUTO-01 → AUTO-02 → TASK-01 → TUI-03 → TUI-04 → TUI-05

**Sprint 3 — Session continuity:**
TASK-05 → TUI-06 → TASK-12

**Sprint 4 — Quality + remaining parity:**
TASK-07 → TASK-08 → TASK-14 → TUI-10 → TASK-13 → TASK-16 → TASK-17 → TASK-18

**Sprint 5 — CLI surface + nice-to-have:**
TASK-09 → TASK-10 → TASK-20 → TASK-22 → TASK-21 → TASK-03 → TASK-04 → TASK-11

---

---

## Workstream D — Python Best Practices + Agentic Architecture

Grouped into eight themes. Each task names the exact files and lines identified in audit.
Tasks are roughly ordered by risk: correctness issues first, maintainability second.

---

### D-01 · Structured error types — replace ad-hoc error dicts

**Problem:** Every tool returns `{"status": "error", "error": "some string"}` or
`{"ok": False, "error": "..."}`. Callers parse strings to decide what happened.
The orchestrator, execution_node, and model all receive different key schemas from
different tools (e.g. `grep` returns `"output"` on success but `"error"` on failure).

**Findings:** `file_tools.py` lines 164, 173, 214, 267, 276, 360–369, 414, 444–457,
519–568; `openai_compat_adapter.py` lines 254–260, 276, 397, 408;
`execution_node.py` lines 448, 509, 546.

**What to do:**
Add `src/tools/_result.py`:
```python
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Any

class ErrorCode(str, Enum):
    NOT_FOUND       = "not_found"
    PERMISSION      = "permission_denied"
    VALIDATION      = "validation_error"
    TIMEOUT         = "timeout"
    PROVIDER        = "provider_error"
    CANCELLED       = "cancelled"
    RATE_LIMITED    = "rate_limited"
    UNKNOWN         = "unknown"

@dataclass(frozen=True)
class ToolResult:
    ok: bool
    output: Any = None
    error: str = ""
    error_code: ErrorCode = ErrorCode.UNKNOWN

    @classmethod
    def success(cls, output: Any) -> "ToolResult":
        return cls(ok=True, output=output)

    @classmethod
    def failure(cls, error: str, code: ErrorCode = ErrorCode.UNKNOWN) -> "ToolResult":
        return cls(ok=False, error=error, error_code=code)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "output": self.output,
                "error": self.error, "error_code": self.error_code.value}
```
Roll out progressively: start with `file_tools.py` and `system_tools.py`, then remaining
tools. The orchestrator's `execute_tool()` result normalisation already does a partial job
— consolidate it here.

**Target files:**
- `src/tools/_result.py` — NEW
- `src/tools/file_tools.py` — return `ToolResult`
- `src/tools/system_tools.py` — return `ToolResult`
- `src/core/orchestration/orchestrator.py::execute_tool()` — consume `ToolResult.to_dict()`

---

### D-02 · AgentState validation — guard writes and reads

**Problem:** `AgentState` is a 70-field TypedDict. Nodes increment fields with
`state.get("x", 0) + 1` without range checks. If one node corrupts a counter
(e.g. sets `plan_attempts` to `"three"`), downstream guards like
`plan_attempts >= 3` raise `TypeError` silently caught by the graph. Fields that
must co-exist (e.g. `current_step` requires `current_plan`) have no enforced invariants.

**Findings:** `state.py` lines 10–161; `perception_node.py` lines 53, 56, 100, 493;
`execution_node.py` lines 326–327; `builder.py` lines 26–110.

**What to do:**
Add a `validate_state(state: AgentState) -> list[str]` function to `graph/state.py`
that checks:
- numeric fields are `int | None` (not strings)
- `current_step` is within bounds of `current_plan` if both are set
- `turn_count <= max_turns` if both set
- required co-presence invariants

Call it at the entry of each node (one line: `issues = validate_state(state); if issues: logger.warning(...)`).
Do **not** raise — log and continue so a bad field doesn't crash a live run.

Also replace all 74 `Optional[X]` annotations in `state.py` with `X | None`
(Python 3.10+ union syntax) for consistency with the rest of the codebase.

**Target files:**
- `src/core/orchestration/graph/state.py` — add `validate_state()`, update `Optional` → `X | None`
- `src/core/orchestration/graph/nodes/perception_node.py` — call at entry
- `src/core/orchestration/graph/nodes/execution_node.py` — call at entry

---

### D-03 · Retry decorator — single implementation, used everywhere

**Problem:** Retry logic with exponential backoff is duplicated. `openai_compat_adapter.py`
lines 296–333 implement 3-attempt / 1s–2s backoff for HTTP errors. Any future call site
that needs retries would duplicate this.

**Findings:** `openai_compat_adapter.py:296–333`; no equivalent in `orchestrator.py`.

**What to do:**
Add `src/core/utils/retry.py`:
```python
import asyncio, functools, logging
from typing import Callable, Iterable, Type

logger = logging.getLogger(__name__)

def async_retry(
    max_attempts: int = 3,
    backoff: tuple[float, ...] = (1.0, 2.0, 4.0),
    retryable: Iterable[Type[Exception]] = (Exception,),
    retryable_codes: Iterable[int] = (429, 500, 502, 503, 504),
):
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            for attempt, wait in enumerate((*backoff, None), 1):
                try:
                    return await fn(*args, **kwargs)
                except tuple(retryable) as exc:
                    code = getattr(getattr(exc, "response", None), "status_code", None)
                    if attempt == max_attempts or (code and code not in retryable_codes):
                        raise
                    logger.debug("retry %d/%d after %.1fs: %s", attempt, max_attempts, wait, exc)
                    await asyncio.sleep(wait)
        return wrapper
    return decorator
```
Replace the hand-rolled loop in `openai_compat_adapter.py::_chat_internal()` with
`@async_retry(retryable_codes=(429, 500, 502, 503, 504))`.

**Target files:**
- `src/core/utils/retry.py` — NEW
- `src/core/inference/adapters/openai_compat_adapter.py::_chat_internal()` — use decorator

---

### D-04 · Tool idempotency — guard write tools against duplicate calls

**Problem:** `write_file()`, `edit_file_atomic()`, `git_commit()` have no idempotency
guard. If the model retries a hallucinated tool call, side effects repeat: files are
written twice, diffs fired twice, commits duplicated.

**Findings:** `file_tools.py:153–220`; `todo_tools.py`; `git_tools.py`.

**What to do:**
For write tools, check content equality before writing:
```python
# In write_file() / edit_file_atomic()
existing = target.read_text(encoding="utf-8") if target.exists() else None
if existing == new_content:
    return ToolResult.success({"message": "no-op: content unchanged", "path": str(target)})
```
For `git_commit()`, check `git status --porcelain` before committing — if clean, return
no-op. For `manage_todo()`, check whether the step is already in the requested state.

This is the minimal guard. Does not need a full content-address store.

**Target files:**
- `src/tools/file_tools.py::write_file()`, `edit_file_atomic()` — add content equality check
- `src/tools/git_tools.py::git_commit()` — add clean-tree check
- `src/tools/todo_tools.py::manage_todo()` — add step-state check

---

### D-05 · Prompt templates — replace f-string assembly with structured templates

**Problem:** Prompts assembled inline via multi-line f-strings. Prompt changes require
code edits; prompts cannot be tested in isolation; LLM-specific formatting (system /
user / assistant roles) is scattered.

**Findings:** `execution_node.py:205–210`; `perception_node.py:300–309`;
`analysis_node.py:139`; `context_builder.py` (build_prompt concatenation).

**What to do:**
Add `src/core/prompts/` package. Each prompt is a `.jinja2` or simple `.py` template
module with a `render(**kwargs) -> list[dict]` function returning a messages list:

```
src/core/prompts/
    perception.py    — render(task, history, context, turn_count, ...) -> list[dict]
    execution.py     — render(plan_step, tool_results, ...) -> list[dict]
    planning.py      — render(task, repo_summary, ...) -> list[dict]
    analysis.py      — render(task, ...) -> list[dict]
```

Each module contains the prompt text as a module-level constant (not an f-string),
with `{variable}` placeholders substituted via `.format_map()`. This makes prompt
changes diff-reviewable and testable without running the agent.

Roll out one node at a time — start with `planning_node.py` since its prompt is most
sensitive to change.

**Target files:**
- `src/core/prompts/` — NEW package
- `src/core/orchestration/graph/nodes/planning_node.py` — use `prompts.planning.render()`
- `src/core/orchestration/graph/nodes/perception_node.py` — use `prompts.perception.render()`
- `src/core/orchestration/graph/nodes/execution_node.py` — use `prompts.execution.render()`

---

### D-06 · Token counting — replace `len(s)/4` heuristic with tiktoken

**Problem:** Token budget is estimated with `math.ceil(len(s) / 4)` in `context_builder.py`.
This is ±30% inaccurate for code (which is token-dense). A context window overflow at
runtime is a hard failure.

**Findings:** `context_builder.py:61–65`; `provider_context.py` (hardcoded budget);
no pre-call token budget check before LLM calls.

**What to do:**
Add `src/core/inference/tokenizer.py`:
```python
from functools import lru_cache
from typing import Callable

@lru_cache(maxsize=4)
def _get_encoder(model_name: str):
    try:
        import tiktoken
        # Map model names to tiktoken encodings
        if "gpt" in model_name or "o1" in model_name:
            return tiktoken.encoding_for_model(model_name)
        return tiktoken.get_encoding("cl100k_base")   # default for Claude/Llama
    except Exception:
        return None

def count_tokens(text: str, model_name: str = "cl100k_base") -> int:
    enc = _get_encoder(model_name)
    if enc:
        return len(enc.encode(text, disallowed_special=()))
    return max(1, len(text) // 4)   # fallback only

def fits_in_budget(messages: list[dict], budget: int, model_name: str) -> bool:
    total = sum(count_tokens(m.get("content", ""), model_name) for m in messages)
    return total <= budget
```
Wire into `context_builder.py` to replace the lambda. Wire into `distiller.py`
token-count trigger (TASK-07) for an accurate measurement.

**Target files:**
- `src/core/inference/tokenizer.py` — NEW
- `src/core/context/context_builder.py:61–65` — replace lambda with `count_tokens()`
- `src/core/inference/distiller.py` — use `count_tokens()` for compact trigger

---

### D-07 · Correlation IDs in async operations — propagate to executor threads

**Problem:** `ContextVar`-based correlation IDs are not propagated to
`run_in_executor()` threads. Tool logs in executor threads have no correlation ID,
making distributed traces impossible to reconstruct.

**Findings:** `event_bus.py:36–55`; `perception_node.py:209–215` (`asyncio.gather`
across executor threads); `perception_node.py:354–365` (`create_task` without ID).

**What to do:**
In `event_bus.py`, add a helper that copies the current ContextVar into a new thread:
```python
import contextvars

def run_with_correlation(loop, executor, fn, *args):
    ctx = contextvars.copy_context()
    return loop.run_in_executor(executor, ctx.run, fn, *args)
```
Replace all `loop.run_in_executor(None, fn, ...)` calls in `perception_node.py` with
`run_with_correlation(loop, None, fn, ...)`. This propagates the correlation ID into
tool execution threads at zero cost.

**Target files:**
- `src/core/orchestration/event_bus.py` — add `run_with_correlation()`
- `src/core/orchestration/graph/nodes/perception_node.py` — replace `run_in_executor`

---

### D-08 · Async correctness — remove blocking I/O from async hot paths

**Problem:** `ContextBuilder.__init__()` reads agent-brain `.md` files synchronously
with `Path.read_text()`. It is instantiated inside `perception_node` and
`execution_node` on every turn, blocking the async event loop during file I/O.

**Findings:** `context_builder.py:85–93` (`_load_agent_brain()` does `Path.read_text()`
synchronously); instantiated in `perception_node.py:277` inside async function.

**What to do:**
Cache the agent-brain file contents at module import time (they don't change at
runtime) as a module-level dict:
```python
# context_builder.py — top of module
import functools
from pathlib import Path

@functools.lru_cache(maxsize=32)
def _read_role_file(path: Path) -> str:
    return path.read_text(encoding="utf-8")
```
Replace `Path.read_text()` calls inside `_load_agent_brain()` with `_read_role_file(path)`.
The LRU cache means each file is read exactly once per process.

**Target files:**
- `src/core/context/context_builder.py` — add `_read_role_file()`, replace `read_text()`

---

### D-09 · Doom-loop and cooldown — extract to reusable guards

**Problem:** Doom-loop detection (`DOOM_LOOP_THRESHOLD = 3`, consecutive identical
fingerprints) and tool cooldown (`_COOLDOWN_GAP = 3`) are hardcoded inside
`execution_node.py` as inline logic. Any other node that wants the same protection
must duplicate the code.

**Findings:** `execution_node.py:472` (`_COOLDOWN_GAP`); `execution_node.py:524`
(`DOOM_LOOP_THRESHOLD`); both are function-body locals, not module constants.

**What to do:**
Add `src/core/orchestration/loop_guards.py`:
```python
DOOM_LOOP_THRESHOLD: int = 3
COOLDOWN_GAP: int = 3

def is_doom_loop(fingerprints: list[str], threshold: int = DOOM_LOOP_THRESHOLD) -> bool:
    """True if last `threshold` fingerprints are identical."""
    if len(fingerprints) < threshold:
        return False
    return len(set(fingerprints[-threshold:])) == 1

def is_on_cooldown(tool_last_used: dict[str, int], tool_name: str,
                   current_step: int, gap: int = COOLDOWN_GAP) -> bool:
    last = tool_last_used.get(tool_name)
    return last is not None and (current_step - last) < gap
```
Move the constants to module level in `execution_node.py` and delegate to these
functions.

**Target files:**
- `src/core/orchestration/loop_guards.py` — NEW
- `src/core/orchestration/graph/nodes/execution_node.py` — use `loop_guards`

---

### D-10 · Orchestrator decomposition — split god class

**Problem:** `Orchestrator.__init__()` initialises 50+ attributes across 383 lines
and owns: tool execution, permission checks, hook running, session cost tracking,
preview mode, plan mode, rollback management, file lock management, context building,
and agent session management. Single-responsibility is violated; the class is
untestable in isolation.

**Findings:** `orchestrator.py:1200–1583`.

**What to do (incremental, not a rewrite):**
Extract three focused service objects that `Orchestrator` holds as attributes:

1. `ToolExecutionService` — owns `execute_tool()`, the hook runner, permission check,
   idempotency guard, and result normalisation. `Orchestrator` delegates to it.
2. `SessionCostTracker` — owns `_usage_buffer`, `_session_cost`, cost estimation,
   and `usage.turn_summary` publishing. `Orchestrator` calls `tracker.record(usage)`.
3. `PreviewCoordinator` — owns pending preview events, `preview.confirmed/rejected`
   subscriptions, and the blocking await logic (TUI-05). `Orchestrator` calls
   `coordinator.request_preview(path, diff)`.

Each extracted class has its own `__init__`, takes only what it needs, and can be
unit-tested with a mock EventBus. `Orchestrator` becomes a thin coordinator that
wires them together.

Do this incrementally: extract `SessionCostTracker` first (smallest, no async),
then `ToolExecutionService`, then `PreviewCoordinator`.

**Target files:**
- `src/core/orchestration/tool_execution_service.py` — NEW
- `src/core/orchestration/session_cost_tracker.py` — NEW
- `src/core/orchestration/preview_coordinator.py` — NEW
- `src/core/orchestration/orchestrator.py` — delegate to extracted services

---

### D-11 · Magic numbers → named module-level constants

**Problem:** Thresholds, caps, and timeouts embedded inline throughout nodes and tools
with no explanation. Changing a single threshold requires hunting through logic.

**Findings:** `builder.py:211,218,257`; `execution_node.py:472,524`;
`context_builder.py:64`; `tool_hooks.py:50–52`; `file_tools.py:67–70`.

**What to do:**
Promote every inline literal that controls behaviour to a named module-level constant
with a comment explaining the value. Examples:

```python
# execution_node.py
DOOM_LOOP_THRESHOLD = 3       # consecutive identical fingerprints → abort
TOOL_COOLDOWN_STEPS = 3       # steps between repeated tool calls
MAX_TOOL_CALLS_PER_TASK = 30  # hard cap independent of turn count

# file_tools.py
BASH_STDOUT_MAX_CHARS   = 16_384   # cap stdout to avoid flooding context
BASH_STDERR_MAX_CHARS   = 2_000
READ_FILE_MAX_CHARS     = 50_000   # ~12k tokens at 4 chars/token
READ_FILE_MAX_LINES     = 2_000

# tool_hooks.py
PRE_HOOK_TIMEOUT_SEC  = 10
POST_HOOK_TIMEOUT_SEC = 5
HOOK_STDERR_CAP_BYTES = 500
```

Where a constant should be configurable at runtime, add it to `config_loader.py`
bundled defaults and read it with `config_loader.get()`.

**Target files:**
- `src/core/orchestration/graph/nodes/execution_node.py` — promote inline literals
- `src/tools/file_tools.py` — promote caps to module level
- `src/core/orchestration/tool_hooks.py` — promote timeout constants
- `src/core/orchestration/graph/builder.py` — promote complexity thresholds

---

### D-12 · Consistent error handling policy across nodes

**Problem:** Exception handling is inconsistent:
- `perception_node.py:273` — suppress all exceptions silently
- `event_bus.py:119–126` — log at debug only; subscriber never knows it was called
- `execution_node.py:321–323` — log at error, continue with `action = None`
- `file_tools.py:56–57` — suppress diff-preview exception silently

**What to do:**
Adopt a three-tier policy documented in `src/core/orchestration/README.md`:

| Tier | When | Policy |
|------|------|--------|
| **Recoverable** | Tool call fails, network error | Return `ToolResult.failure()`, log at WARNING |
| **Degraded** | Optional enrichment fails (pre-retrieval, LSP) | Return empty/default, log at DEBUG |
| **Fatal** | State corruption, invariant broken | Re-raise, let LangGraph handle, log at ERROR |

Apply immediately to:
- `perception_node.py:273` — change silent pass to DEBUG log with exception text
- `event_bus.py:119` — raise level to WARNING for subscriber exceptions
- `file_tools.py:56` — add `logger.debug("diff preview failed: %s", exc)` to silent catch

**Target files:**
- `src/core/orchestration/graph/nodes/perception_node.py:273` — add debug log
- `src/core/orchestration/event_bus.py:119–126` — raise subscriber errors to WARNING
- `src/tools/file_tools.py:56–57` — add debug log to silent catch

---

### D-13 · Type annotations audit — complete missing signatures

**Problem:** Several async helper functions and inner functions lack parameter and
return type annotations, degrading IDE support and pyright/mypy coverage.

**Findings:** `perception_node.py:135–142,146–193` (helpers `_safe_call`,
`_fetch_search_code`, `_fetch_symbols`, `_fetch_references`, `_fetch_test_files`);
`context_builder.py:40–100` (multiple methods); `openai_compat_adapter.py:341`
(assertion used instead of `if ... raise`).

**What to do:**
Add return type annotations to all untyped functions in the above files. Replace the
`assert r is not None` pattern in `openai_compat_adapter.py:341` with:
```python
if r is None:
    raise RuntimeError("response unexpectedly None after retries")
```
Run `mypy --strict src/core/orchestration/graph/nodes/` as the validation gate.

**Target files:**
- `src/core/orchestration/graph/nodes/perception_node.py` — annotate helpers
- `src/core/context/context_builder.py` — annotate methods
- `src/core/inference/adapters/openai_compat_adapter.py:341` — assert → raise

---

### D-14 · Global mutable state — encapsulate module-level singletons

**Problem:** Several modules use bare global variables as singletons with no thread-
safety on initialisation. `event_bus.py:232–239` has a race on `_default_bus`;
`logger.py` uses five `global` statements; `role_tools.py` has an unguarded `_current_role`.

**Findings:** `event_bus.py:232–239`; `logger.py:49,272,287,314,420`;
`role_tools.py:14`.

**What to do:**
For the EventBus singleton, use `threading.Lock` on first assignment:
```python
_default_bus: EventBus | None = None
_bus_lock = threading.Lock()

def get_event_bus() -> EventBus:
    global _default_bus
    if _default_bus is None:
        with _bus_lock:
            if _default_bus is None:           # double-checked lock
                _default_bus = EventBus()
    return _default_bus
```
For `role_tools.py::_current_role`, add a `threading.Lock` around reads and writes.
For `logger.py`, encapsulate the five globals into a `_LoggerState` dataclass
initialised once.

**Target files:**
- `src/core/orchestration/event_bus.py:232–239` — double-checked lock on singleton
- `src/tools/role_tools.py:14` — thread-safe lock on `_current_role`
- `src/core/logger.py` — encapsulate globals in `_LoggerState`

---

### D-15 · Tool result normalisation — single format fed to model

**Problem:** Tool results reach the model in multiple schemas.
`execution_node.py:448,509,546` wraps results in `"tool_execution_result": {}`; others
pass raw dicts. `grep()` returns `"output"` + `"matches"` on success but just `"error"`
on failure. The model must pattern-match the schema each turn.

**Findings:** `execution_node.py:441–457,503–556`; `system_tools.py:84–92,134–136`;
`orchestrator.py::_format_tool_result()` (partial normalisation already exists).

**What to do:**
Extend `orchestrator.py::_format_tool_result()` (or move to `ToolExecutionService`
from D-10) so every tool result the model sees has the same envelope:
```python
{
    "tool": "tool_name",
    "ok": True | False,
    "output": <tool-specific payload>,   # always present, None on failure
    "error": "",                          # always present, "" on success
    "error_code": "none" | ErrorCode,
    "duration_ms": 42,
}
```
Update `execution_node.py` to use this envelope consistently when building the
`"tool_execution_result"` history entry.

**Target files:**
- `src/core/orchestration/orchestrator.py::_format_tool_result()` — enforce envelope
- `src/core/orchestration/graph/nodes/execution_node.py` — use normalised envelope

---

## Updated Task Summary

### Workstream D — Python Best Practices + Agentic Architecture

| Task | What | Effort | Priority |
|------|------|--------|----------|
| D-01 | Structured `ToolResult` type — replace ad-hoc error dicts | M | High |
| D-02 | `AgentState` validation + `Optional` → `X \| None` syntax | S | High |
| D-03 | `async_retry` decorator — single implementation | S | High |
| D-04 | Tool idempotency guards (`write_file`, `git_commit`, `manage_todo`) | S | High |
| D-05 | Prompt templates — `src/core/prompts/` package | L | Medium |
| D-06 | Token counting — replace `len/4` with `tiktoken` | M | Medium |
| D-07 | Correlation IDs propagated to `run_in_executor` threads | S | Medium |
| D-08 | Async correctness — cache role file I/O at import time | S | Medium |
| D-09 | Doom-loop + cooldown extracted to `loop_guards.py` | S | Medium |
| D-10 | Orchestrator decomposition — 3 extracted service classes | L | Medium |
| D-11 | Magic numbers → named module-level constants | S | Low |
| D-12 | Consistent error handling policy across nodes | S | Low |
| D-13 | Type annotation audit + assert → raise | S | Low |
| D-14 | Thread-safe singleton init for EventBus, role_tools, logger | S | Low |
| D-15 | Uniform tool result envelope fed to model | M | Medium |

**Effort key:** S = <1 hr, M = 2–4 hr, L = >4 hr

### Recommended execution order within D

**Immediately (before other workstreams):** D-02, D-12, D-14 — these fix silent failures and
race conditions that could mask bugs during integration work.

**During Sprint 1–2:** D-01, D-03, D-04, D-08, D-11 — low-risk improvements that harden
the core without restructuring.

**During Sprint 3–4:** D-06, D-07, D-09, D-15 — improve observability and model context
quality.

**Sprint 5+:** D-05, D-10, D-13 — structural improvements that benefit from the rest of
the codebase being stable first.

---

*End of development plan.*
