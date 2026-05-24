"""
mock_engine.py — TUI System Specification v2.0 full-coverage simulation.

Publishes every event category defined in §4.5:
  - orchestrator.startup, provider.status.changed, provider.models.list,
    model.routing, model.response, model.token
  - tool.execute.start / finish / error  (full ACP schema: sessionUpdate, status,
    toolCallId, title, rawInput / content)
  - file.diff.preview, file.modified (with tool + workdir), file.deleted
  - preview.pending — exercises the _on_preview_pending bridge path (→ DiffPreviewEvent)
  - plan.progress — ACP schema (currentStep/totalSteps/stepDescription) AND
    legacy schema (step/total/description) to validate dual-schema support §12.3
  - plan.requested — exercises the plan approval UI (§14.1)
  - session.registered, session.hydrated, session.health_alert
  - task.queue.updated — queued (startup), in_progress (mid-run), completed (end)
  - log.new — written DIRECTLY to console panel (§16.4)
  - ui.notification, token.budget.update, token.budget.warning
  - retry.attempt, retry.succeeded — Phase 1 (VectorStore fallback)
  - retry.attempt × 2, retry.failed — Phase 3 (ConnectionRefused)
  - context.degraded — Phase 3 (execution trace compacted)
  - Bash tier-3 approval gate (§16.1): tool.execute.start fires, bridge converts
    to BashApprovalEvent; after delay mock resolves with tool.execute.finish
  - git.branch — Phase 2 (branch/dirty/ahead populated in sidebar)
"""

import asyncio
import time
import uuid
from pathlib import Path
from textual import work, on

from tui.tui_src.ui.bus import SystemSettingsLoaded, AgentFinalResponse
from tui.tui_src.ui.events import (
    RequestSystemSettings,
    SaveProviderCredentials,
    UpdateRoleModel,
)
from tui.tui_src.ui.app import AgentApp

try:
    from tui.tui_src.ui.logging import get_logger
except Exception:
    try:
        from .logging import get_logger
    except Exception:
        import logging

        def get_logger(name: str) -> logging.Logger:
            return logging.getLogger(name)


logger = get_logger("mock_engine")

PROVIDERS_JSON_PATH = (
    Path(__file__).resolve().parents[3] / "src" / "config" / "providers.json"
)

MOCK_DEFAULT_SETTINGS = {
    "active_mode": "lead_architect",
    "theme": "textual-dark",
    "default_model": "qwen2.5-coder-14b",
    "default_provider": "local_lm_studio",
    "lead_architect_provider": "local_lm_studio",
    "lead_architect_model": "qwen2.5-coder-14b",
    "full_stack_engineer_provider": "local_lm_studio",
    "full_stack_engineer_model": "qwen2.5-coder-14b",
    "qa_lead_provider": "local_lm_studio",
    "qa_lead_model": "qwen2.5-coder-14b",
    "context_window": 32000,
    "providers": {},
}


def _load_providers() -> list:
    if PROVIDERS_JSON_PATH.exists():
        try:
            import json

            with open(PROVIDERS_JSON_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            # providers.json is a top-level list, not {"providers": [...]}
            entries = data if isinstance(data, list) else (data.get("providers") or [])
            return [
                {
                    "name": p.get("name") or p.get("type") or "",
                    "type": p.get("type") or "",
                    "models": p.get("models") or [],
                    "active": p.get("active", False),
                }
                for p in entries
                if isinstance(p, dict)
            ]
        except (Exception,) as e:
            logger.error(f"Failed to load providers.json: {e}")
    return []


def _tid() -> str:
    """Short unique tool-call ID."""
    return str(uuid.uuid4())[:8]


def _su(status: str = "in_progress") -> dict:
    """Minimal ACP sessionUpdate dict — required 'always-present' field per §4.5."""
    return {"updateType": "tool", "status": status, "timestamp": time.time()}


async def _stream(bus, text: str, delay: float = 0.014) -> None:
    """Stream text word-by-word via model.token events."""
    words = text.split()
    for i, word in enumerate(words):
        partial = i < len(words) - 1
        bus.publish("model.token", {"text": word + " ", "partial": partial})
        await asyncio.sleep(delay)
    bus.publish("model.token", {"text": "", "partial": False})


class MockSimulationApp(AgentApp):
    def on_mount(self):
        super().on_mount()
        self.run_simulation()

    @on(RequestSystemSettings)
    def handle_request_settings(self, _: RequestSystemSettings) -> None:
        logger.info("Mock backend: sending SystemSettingsLoaded")
        providers = _load_providers()
        self.post_message(
            SystemSettingsLoaded(
                settings_dict=dict(MOCK_DEFAULT_SETTINGS),
                available_providers=providers,
            )
        )

    @on(SaveProviderCredentials)
    def handle_save_creds(self, event: SaveProviderCredentials) -> None:
        logger.info(f"Mock backend: saving credentials for {event.provider_id}")
        self.notify(f"API key saved for {event.provider_id}")

    @on(UpdateRoleModel)
    def handle_role_model_update(self, event: UpdateRoleModel) -> None:
        logger.info(f"Mock backend: role {event.role} → {event.model_id}")

    # ── Main simulation ────────────────────────────────────────────────────

    @work(exclusive=True)
    async def run_simulation(self):
        """
        Exercises every §4.5 event the TUI subscribes to.
        Phases: Startup → Lead Architect → Full Stack Engineer → QA Lead.
        """
        bus = self._bridge._bus
        await asyncio.sleep(0.8)

        # ══════════════════════════════════════════════════════════════════
        # PHASE 0 — Core startup sequence (§10.1)
        # ══════════════════════════════════════════════════════════════════

        # orchestrator.startup — requires time + working_dir (§4.5)
        bus.publish(
            "orchestrator.startup",
            {
                "time": time.time(),
                "working_dir": "/workspace/project",
            },
        )
        await asyncio.sleep(0.15)

        # session.new — fires first so bridge clears any stale chat/sidebar state
        bus.publish("session.new", {"timestamp": time.time()})
        await asyncio.sleep(0.1)

        # Session lifecycle events
        bus.publish("session.registered", {"session_id": "mock-session-001"})
        await asyncio.sleep(0.1)

        # Provider discovery
        bus.publish(
            "provider.status.changed",
            {
                "provider": "local_lm_studio",
                "status": "connected",
            },
        )
        bus.publish(
            "provider.models.list",
            {
                "provider": "local_lm_studio",
                "models": ["qwen2.5-coder-14b", "deepseek-coder-v2", "llama-3-8b"],
            },
        )
        bus.publish(
            "model.routing",
            {
                "provider": "local_lm_studio",
                "selected": "qwen2.5-coder-14b",
            },
        )
        await asyncio.sleep(0.2)

        # session.hydrated — backend restores previous state
        bus.publish(
            "session.hydrated",
            {
                "session_id": "mock-session-001",
                "history": [],
                "working_dir": "/workspace/project",
            },
        )
        await asyncio.sleep(0.1)

        # task.queue.updated — 3 tasks queued at startup (§4.5 task.queue.updated)
        bus.publish(
            "task.queue.updated",
            {
                "pending_count": 3,
                "queue_size": 3,
                "new_status": "queued",
            },
        )
        await asyncio.sleep(0.1)

        # log.new — §16.4: must render DIRECTLY to console, never via logger
        bus.publish(
            "log.new",
            {
                "level": "INFO",
                "logger": "orchestrator",
                "message": "Agent session initialised — all services ready",
            },
        )
        bus.publish(
            "log.new",
            {
                "level": "INFO",
                "logger": "provider",
                "message": "local_lm_studio connected at http://127.0.0.1:1234/v1",
            },
        )
        await asyncio.sleep(0.2)

        # ══════════════════════════════════════════════════════════════════
        # PHASE 1 — LEAD ARCHITECT: Planning & analysis
        # ══════════════════════════════════════════════════════════════════

        bus.publish(
            "role.transition",
            {
                "from_role": "system",
                "to_role": "lead_architect",
                "run_id": "mock-001",
            },
        )
        await asyncio.sleep(0.3)

        bus.publish(
            "token.budget.update", {"used": 800, "limit": 32000, "percent": 2.5}
        )
        bus.publish(
            "ui.notification",
            {
                "level": "info",
                "message": "Analysing project structure…",
                "source": "lead_architect",
            },
        )
        await asyncio.sleep(0.15)

        bus.publish(
            "log.new",
            {
                "level": "DEBUG",
                "logger": "tool_registry",
                "message": "Loading tools for role: strategic (23 tools available)",
            },
        )

        # ── Tool 1: list_files — full ACP schema ──────────────────────────
        t1 = _tid()
        bus.publish(
            "tool.execute.start",
            {
                "sessionUpdate": _su("in_progress"),
                "toolCallId": t1,
                "title": "list_files",
                "status": "in_progress",
                "rawInput": {"path": "src/"},
            },
        )
        await asyncio.sleep(0.5)
        bus.publish(
            "tool.execute.finish",
            {
                "sessionUpdate": _su("completed"),
                "toolCallId": t1,
                "title": "list_files",
                "status": "completed",
                "ok": True,
                "content": [
                    {
                        "text": (
                            "📁 src/\n"
                            "  📁 ui/\n"
                            "    📄 app.py\n"
                            "    📄 core_bridge.py\n"
                            "    📄 mock_engine.py\n"
                            "    📄 bus.py\n"
                            "    📄 events.py\n"
                            "  📁 core/\n"
                            "    📄 providers.json\n"
                            "  📄 README.md\n"
                        )
                    }
                ],
            },
        )
        await asyncio.sleep(0.2)

        # ── Tool 2: search_code — FAILS (exercises tool.execute.error) ────
        t2 = _tid()
        bus.publish(
            "tool.execute.start",
            {
                "sessionUpdate": _su("in_progress"),
                "toolCallId": t2,
                "title": "search_code",
                "status": "in_progress",
                "rawInput": {"query": "EventBus subscribe unsubscribe pattern"},
            },
        )
        await asyncio.sleep(0.35)
        bus.publish(
            "tool.execute.error",
            {
                "sessionUpdate": _su("error"),
                "toolCallId": t2,
                "title": "search_code",
                "status": "error",
                "error": "VectorStore not initialised — run initialize_repo_intelligence first",
            },
        )
        await asyncio.sleep(0.15)

        bus.publish(
            "log.new",
            {
                "level": "WARNING",
                "logger": "search",
                "message": "VectorStore unavailable, falling back to grep",
            },
        )

        # retry.attempt → retry.succeeded (§4.5 retry events, exercises RetryAttemptEvent)
        bus.publish(
            "retry.attempt",
            {
                "attempt_number": 1,
                "max_attempts": 3,
                "error_type": "VectorStoreNotInitialised",
                "provider": "local_lm_studio",
                "run_id": "mock-001",
            },
        )
        await asyncio.sleep(0.3)
        bus.publish(
            "retry.succeeded",
            {
                "attempt_number": 2,
                "provider": "local_lm_studio",
                "run_id": "mock-001",
            },
        )
        await asyncio.sleep(0.1)

        # ── Tool 3: grep (fallback) ────────────────────────────────────────
        t3 = _tid()
        bus.publish(
            "tool.execute.start",
            {
                "sessionUpdate": _su("in_progress"),
                "toolCallId": t3,
                "title": "grep",
                "status": "in_progress",
                "rawInput": {
                    "pattern": "get_event_bus|_subscribe",
                    "path": "src/ui/",
                    "include": "*.py",
                },
            },
        )
        await asyncio.sleep(0.4)
        bus.publish(
            "tool.execute.finish",
            {
                "sessionUpdate": _su("completed"),
                "toolCallId": t3,
                "title": "grep",
                "status": "completed",
                "ok": True,
                "content": [
                    {
                        "text": (
                            "Found 12 matches:\n"
                            "  src/ui/core_bridge.py:43:    def _get_event_bus():\n"
                            "  src/ui/core_bridge.py:47:        from src.core.orchestration.event_bus import get_event_bus\n"
                            "  src/ui/core_bridge.py:101:    def _subscribe(self, event: str, cb: Callable) -> None:\n"
                            "  src/ui/core_bridge.py:103:        self._subscriptions.append((event, cb))\n"
                            "  src/ui/mock_eventbus.py:12:def get_mock_event_bus():\n"
                            "  … 7 more matches\n"
                        )
                    }
                ],
            },
        )
        await asyncio.sleep(0.2)

        # ── Stream: architect analysis ─────────────────────────────────────
        await _stream(
            bus,
            (
                "The project has a complete EventBus bridge architecture. "
                "The bridge subscribes to 37 events from §4.5 and translates them "
                "into Textual messages. I will now propose a plan to add the "
                "remaining spec-required simulation coverage to mock_engine.py."
            ),
        )

        # model.response fires after streaming completes (§4.5)
        bus.publish(
            "model.response",
            {
                "provider": "local_lm_studio",
                "model": "qwen2.5-coder-14b",
                "tokens": 347,
            },
        )
        bus.publish(
            "token.budget.update", {"used": 5200, "limit": 32000, "percent": 16.3}
        )
        await asyncio.sleep(0.2)

        # plan.progress — ACP schema, step 1 of 4
        bus.publish(
            "plan.progress",
            {
                "currentStep": 1,
                "totalSteps": 4,
                "stepDescription": "Analyse EventBus subscription coverage",
            },
        )
        await asyncio.sleep(0.2)

        # ── plan.requested — triggers plan approval UI (§14.1) ────────────
        bus.publish(
            "plan.requested",
            {
                "plan_text": (
                    "Step 1: Audit all §4.5 events against current mock_engine.py\n"
                    "Step 2: Rewrite mock_engine.py with full ACP schema compliance\n"
                    "Step 3: Add missing events: plan.requested, tool.execute.error,\n"
                    "        log.new, session.*, provider.models.list, model.response,\n"
                    "        file.deleted, legacy plan.progress schema\n"
                    "Step 4: Verify with linter + tests — update replit.md"
                ),
            },
        )
        # Give user time to interact with approve/reject buttons
        await asyncio.sleep(3.5)

        # ══════════════════════════════════════════════════════════════════
        # PHASE 2 — FULL STACK ENGINEER: Implementation
        # ══════════════════════════════════════════════════════════════════

        bus.publish(
            "role.transition",
            {
                "from_role": "lead_architect",
                "to_role": "full_stack_engineer",
                "run_id": "mock-001",
            },
        )
        await asyncio.sleep(0.3)

        bus.publish(
            "log.new",
            {
                "level": "INFO",
                "logger": "orchestrator",
                "message": "Switching to operational role — write tools now available",
            },
        )

        # plan.progress — legacy schema (tests §12.3 dual-schema support)
        bus.publish(
            "plan.progress",
            {
                "step": 2,
                "total": 4,
                "description": "Rewriting mock_engine.py with full spec coverage",
            },
        )
        await asyncio.sleep(0.15)

        bus.publish(
            "token.budget.update", {"used": 9100, "limit": 32000, "percent": 28.4}
        )

        # ── Tool: read_file (required before write) ────────────────────────
        t_read = _tid()
        bus.publish(
            "tool.execute.start",
            {
                "sessionUpdate": _su("in_progress"),
                "toolCallId": t_read,
                "title": "read_file",
                "status": "in_progress",
                "rawInput": {"path": "src/ui/mock_engine.py"},
            },
        )
        await asyncio.sleep(0.4)
        bus.publish(
            "tool.execute.finish",
            {
                "sessionUpdate": _su("completed"),
                "toolCallId": t_read,
                "title": "read_file",
                "status": "completed",
                "ok": True,
                "content": [
                    {
                        "text": (
                            "File: src/ui/mock_engine.py\n"
                            "──────────────────────────\n"
                            "[328 lines]\n"
                            "… [288 more lines]\n"
                        )
                    }
                ],
            },
        )
        await asyncio.sleep(0.2)

        # ── file.diff.preview BEFORE write (§6.4) ─────────────────────────
        bus.publish(
            "file.diff.preview",
            {
                "path": "src/ui/mock_engine.py",
                "diff": (
                    "--- a/src/ui/mock_engine.py\n"
                    "+++ b/src/ui/mock_engine.py\n"
                    "@@ -1,7 +1,8 @@\n"
                    ' """\n'
                    "-mock_engine.py — basic spec simulation.\n"
                    "+mock_engine.py — TUI System Specification v2.0 full-coverage simulation.\n"
                    ' """\n'
                    "+import time\n"
                    " import asyncio\n"
                    " import json\n"
                    "-import uuid\n"
                    "+import uuid\n"
                    "@@ -51,10 +52,22 @@\n"
                    "-def _tool_id() -> str:\n"
                    "-    return str(uuid.uuid4())[:8]\n"
                    "+def _tid() -> str:\n"
                    '+    """Short unique tool-call ID."""\n'
                    "+    return str(uuid.uuid4())[:8]\n"
                    "+\n"
                    "+def _su(status: str = 'in_progress') -> dict:\n"
                    '+    """Minimal ACP sessionUpdate dict — §4.5 required field."""\n'
                    '+    return {"updateType": "tool", "status": status, "timestamp": time.time()}\n'
                    "+\n"
                    "+async def _stream(bus, text: str, delay: float = 0.014) -> None:\n"
                    '+    """Stream text word-by-word via model.token events."""\n'
                    "+    words = text.split()\n"
                    "+    for i, word in enumerate(words):\n"
                    "+        partial = (i < len(words) - 1)\n"
                    "+        bus.publish('model.token', {'text': word + ' ', 'partial': partial})\n"
                    "+        await asyncio.sleep(delay)\n"
                    "+    bus.publish('model.token', {'text': '', 'partial': False})\n"
                ),
                "is_new_file": False,
            },
        )
        await asyncio.sleep(0.3)

        # ── Tool: write_file ───────────────────────────────────────────────
        t_write = _tid()
        bus.publish(
            "tool.execute.start",
            {
                "sessionUpdate": _su("in_progress"),
                "toolCallId": t_write,
                "title": "write_file",
                "status": "in_progress",
                "rawInput": {
                    "path": "src/ui/mock_engine.py",
                    "content": "<3 241 chars>",
                },
            },
        )
        await asyncio.sleep(0.7)
        bus.publish(
            "tool.execute.finish",
            {
                "sessionUpdate": _su("completed"),
                "toolCallId": t_write,
                "title": "write_file",
                "status": "completed",
                "ok": True,
                "content": [
                    {"text": "✓ Modified src/ui/mock_engine.py  [+142 / -31 lines]\n"}
                ],
            },
        )
        # file.modified — must include tool + workdir (§4.5)
        bus.publish(
            "file.modified",
            {
                "path": "src/ui/mock_engine.py",
                "tool": "write_file",
                "workdir": "/workspace/project",
            },
        )
        await asyncio.sleep(0.2)

        # ── file.diff.preview for core_bridge.py ──────────────────────────
        bus.publish(
            "file.diff.preview",
            {
                "path": "src/ui/core_bridge.py",
                "diff": (
                    "--- a/src/ui/core_bridge.py\n"
                    "+++ b/src/ui/core_bridge.py\n"
                    "@@ -119,6 +119,9 @@\n"
                    "         self._subscribe('tool.execute.finish',       self._on_tool_finish)\n"
                    "         self._subscribe('tool.execute.error',        self._on_tool_error)\n"
                    "+        self._subscribe('tool.invoked',              lambda p: None)\n"
                    " \n"
                    "         # file\n"
                    "         self._subscribe('file.diff.preview',         self._on_diff_preview)\n"
                    "         self._subscribe('file.modified',             self._on_file_modified)\n"
                    "+        self._subscribe('file.deleted',              self._on_file_deleted)\n"
                ),
                "is_new_file": False,
            },
        )
        await asyncio.sleep(0.3)

        # preview.pending — alternative confirmation path (§4.5, tests _on_preview_pending)
        # Shows that bridge correctly routes preview.pending → DiffPreviewEvent
        bus.publish(
            "preview.pending",
            {
                "path": "src/ui/events.py",
                "diff": (
                    "--- a/src/ui/events.py\n"
                    "+++ b/src/ui/events.py\n"
                    "@@ -88,4 +88,10 @@\n"
                    " class BashDenied(Message):\n"
                    "     def __init__(self, tool_id: str) -> None:\n"
                    "         self.tool_id = tool_id\n"
                    "         super().__init__()\n"
                    "+\n"
                    "+\n"
                    "+class ContextCompacted(Message):\n"
                    '+    """UI notifies backend that context was manually compacted."""\n'
                    "+    def __init__(self) -> None:\n"
                    "+        super().__init__()\n"
                ),
                "is_new_file": False,
            },
        )
        await asyncio.sleep(0.25)

        # task.queue.updated — 1 task in progress, 2 remaining (§4.5)
        bus.publish(
            "task.queue.updated",
            {
                "pending_count": 2,
                "queue_size": 2,
                "new_status": "in_progress",
            },
        )
        await asyncio.sleep(0.1)

        # ── Tool: edit_file_atomic ─────────────────────────────────────────
        t_edit = _tid()
        bus.publish(
            "tool.execute.start",
            {
                "sessionUpdate": _su("in_progress"),
                "toolCallId": t_edit,
                "title": "edit_file_atomic",
                "status": "in_progress",
                "rawInput": {
                    "path": "src/ui/core_bridge.py",
                    "old_string": "self._subscribe('tool.invoked',              lambda p: None)",
                    "new_string": "self._subscribe('tool.invoked',              self._on_tool_invoked)",
                },
            },
        )
        await asyncio.sleep(0.45)
        bus.publish(
            "tool.execute.finish",
            {
                "sessionUpdate": _su("completed"),
                "toolCallId": t_edit,
                "title": "edit_file_atomic",
                "status": "completed",
                "ok": True,
                "content": [
                    {"text": "✓ Modified src/ui/core_bridge.py  [+1 / -1 lines]\n"}
                ],
            },
        )
        bus.publish(
            "file.modified",
            {
                "path": "src/ui/core_bridge.py",
                "tool": "edit_file_atomic",
                "workdir": "/workspace/project",
            },
        )
        await asyncio.sleep(0.2)

        # ── Tool: manage_todo (create) ─────────────────────────────────────
        t_todo = _tid()
        bus.publish(
            "tool.execute.start",
            {
                "sessionUpdate": _su("in_progress"),
                "toolCallId": t_todo,
                "title": "manage_todo",
                "status": "in_progress",
                "rawInput": {
                    "action": "create",
                    "steps": [
                        "Audit §4.5 event coverage",
                        "Rewrite mock_engine.py",
                        "Update core_bridge.py",
                        "Run verification checks",
                    ],
                },
            },
        )
        await asyncio.sleep(0.25)
        bus.publish(
            "tool.execute.finish",
            {
                "sessionUpdate": _su("completed"),
                "toolCallId": t_todo,
                "title": "manage_todo",
                "status": "completed",
                "ok": True,
                "content": [
                    {
                        "text": (
                            "📋 TODO  (0/4 done)\n\n"
                            "  ⬜ 1. Audit §4.5 event coverage\n"
                            "  ⬜ 2. Rewrite mock_engine.py\n"
                            "  ⬜ 3. Update core_bridge.py\n"
                            "  ⬜ 4. Run verification checks\n"
                        )
                    }
                ],
            },
        )
        await asyncio.sleep(0.2)

        # ── Bash tier-3 approval gate (§16.1) ─────────────────────────────
        # Bridge intercepts this and posts BashApprovalEvent instead.
        # Approval UI appears; mock auto-resolves after 2.5 s.
        t_bash = _tid()
        bus.publish(
            "tool.execute.start",
            {
                "sessionUpdate": _su("in_progress"),
                "toolCallId": t_bash,
                "title": "bash",
                "status": "in_progress",
                "rawInput": {"command": "pip install textual==0.89.0 --quiet"},
            },
        )
        await asyncio.sleep(2.5)  # let user interact with Approve/Deny buttons
        bus.publish(
            "tool.execute.finish",
            {
                "sessionUpdate": _su("completed"),
                "toolCallId": t_bash,
                "title": "bash",
                "status": "completed",
                "ok": True,
                "content": [
                    {
                        "text": (
                            "$ pip install textual==0.89.0 --quiet\n"
                            "Successfully installed textual-0.89.0\n"
                        )
                    }
                ],
            },
        )
        await asyncio.sleep(0.2)

        # ── Stream: implementation summary ────────────────────────────────
        await _stream(
            bus,
            (
                "Both files updated. "
                "The mock engine now publishes all 37 EventBus events from §4.5 "
                "using the correct ACP schema — sessionUpdate, status, toolCallId, "
                "title, rawInput on start; sessionUpdate, status, content on finish. "
                "The bridge dual-schema handler for plan.progress also validated."
            ),
            delay=0.013,
        )

        bus.publish(
            "model.response",
            {
                "provider": "local_lm_studio",
                "model": "qwen2.5-coder-14b",
                "tokens": 289,
            },
        )
        bus.publish(
            "token.budget.update", {"used": 19800, "limit": 32000, "percent": 61.9}
        )
        await asyncio.sleep(0.2)

        # ── Tool: git_status ──────────────────────────────────────────────
        t_git = _tid()
        bus.publish(
            "tool.execute.start",
            {
                "sessionUpdate": _su("in_progress"),
                "toolCallId": t_git,
                "title": "git_status",
                "status": "in_progress",
                "rawInput": {},
            },
        )
        await asyncio.sleep(0.3)
        bus.publish(
            "tool.execute.finish",
            {
                "sessionUpdate": _su("completed"),
                "toolCallId": t_git,
                "title": "git_status",
                "status": "completed",
                "ok": True,
                "content": [
                    {
                        "text": (
                            "Branch: main\n\n"
                            "M  src/ui/mock_engine.py\n"
                            "M  src/ui/core_bridge.py\n"
                        )
                    }
                ],
            },
        )
        # git.branch — update sidebar git widget
        bus.publish(
            "git.branch",
            {
                "branch": "main",
                "dirty": True,
                "ahead": 2,
                "behind": 0,
            },
        )
        await asyncio.sleep(0.2)

        # plan.progress — ACP, step 3
        bus.publish(
            "plan.progress",
            {
                "currentStep": 3,
                "totalSteps": 4,
                "stepDescription": "Running QA verification checks",
            },
        )
        await asyncio.sleep(0.2)

        # ══════════════════════════════════════════════════════════════════
        # PHASE 3 — QA LEAD: Verification
        # ══════════════════════════════════════════════════════════════════

        bus.publish(
            "role.transition",
            {
                "from_role": "full_stack_engineer",
                "to_role": "qa_lead",
                "run_id": "mock-001",
            },
        )
        await asyncio.sleep(0.3)

        bus.publish(
            "log.new",
            {
                "level": "INFO",
                "logger": "orchestrator",
                "message": "QA verification phase — linter + tests + syntax check",
            },
        )
        bus.publish(
            "token.budget.update", {"used": 24600, "limit": 32000, "percent": 76.9}
        )
        await asyncio.sleep(0.15)

        # session.health_alert (§4.5) — tests SessionHealthEvent handler
        bus.publish(
            "session.health_alert",
            {
                "level": "warning",
                "title": "Context window at 77%",
                "message": "Consider running /compact before the next task to free context",
            },
        )
        await asyncio.sleep(0.2)

        # ── Tool: run_linter (finds warnings) ─────────────────────────────
        t_lint = _tid()
        bus.publish(
            "tool.execute.start",
            {
                "sessionUpdate": _su("in_progress"),
                "toolCallId": t_lint,
                "title": "run_linter",
                "status": "in_progress",
                "rawInput": {"workdir": ".", "fix": False},
            },
        )
        await asyncio.sleep(0.65)
        bus.publish(
            "tool.execute.finish",
            {
                "sessionUpdate": _su("completed"),
                "toolCallId": t_lint,
                "title": "run_linter",
                "status": "completed",
                "ok": True,
                "content": [
                    {
                        "text": (
                            "❌ Linter: 2 warnings\n\n"
                            "  src/ui/mock_engine.py:12:1  W0611  'json' imported but unused (removed in rewrite)\n"
                            "  src/ui/core_bridge.py:291:80  E501  line too long (88 > 79 chars)\n"
                        )
                    }
                ],
            },
        )
        await asyncio.sleep(0.2)

        # ── Tool: syntax_check ────────────────────────────────────────────
        t_syn = _tid()
        bus.publish(
            "tool.execute.start",
            {
                "sessionUpdate": _su("in_progress"),
                "toolCallId": t_syn,
                "title": "syntax_check",
                "status": "in_progress",
                "rawInput": {"workdir": "."},
            },
        )
        await asyncio.sleep(0.5)
        bus.publish(
            "tool.execute.finish",
            {
                "sessionUpdate": _su("completed"),
                "toolCallId": t_syn,
                "title": "syntax_check",
                "status": "completed",
                "ok": True,
                "content": [{"text": "✅ Syntax OK  (41 files checked)\n"}],
            },
        )
        await asyncio.sleep(0.2)

        # retry.attempt × 2 → retry.failed — simulates exhausted provider retries (§4.5)
        bus.publish(
            "retry.attempt",
            {
                "attempt_number": 1,
                "max_attempts": 2,
                "error_type": "ConnectionRefused",
                "provider": "remote_api",
                "run_id": "mock-001",
            },
        )
        await asyncio.sleep(0.25)
        bus.publish(
            "retry.attempt",
            {
                "attempt_number": 2,
                "max_attempts": 2,
                "error_type": "ConnectionRefused",
                "provider": "remote_api",
                "run_id": "mock-001",
            },
        )
        await asyncio.sleep(0.2)
        bus.publish(
            "retry.failed",
            {
                "total_attempts": 2,
                "error_type": "ConnectionRefused",
                "provider": "remote_api",
                "run_id": "mock-001",
            },
        )
        await asyncio.sleep(0.15)

        # ── Tool: run_tests ───────────────────────────────────────────────
        t_test = _tid()
        bus.publish(
            "tool.execute.start",
            {
                "sessionUpdate": _su("in_progress"),
                "toolCallId": t_test,
                "title": "run_tests",
                "status": "in_progress",
                "rawInput": {"workdir": ".", "test_files": ["tests/"]},
            },
        )
        await asyncio.sleep(0.8)
        bus.publish(
            "tool.execute.finish",
            {
                "sessionUpdate": _su("completed"),
                "toolCallId": t_test,
                "title": "run_tests",
                "status": "completed",
                "ok": True,
                "content": [{"text": "✅ Tests passed  (52 passed, 0 failed)\n"}],
            },
        )
        await asyncio.sleep(0.2)

        # ── Tool: manage_todo (check all done) ────────────────────────────
        t_todo2 = _tid()
        bus.publish(
            "tool.execute.start",
            {
                "sessionUpdate": _su("in_progress"),
                "toolCallId": t_todo2,
                "title": "manage_todo",
                "status": "in_progress",
                "rawInput": {"action": "read"},
            },
        )
        await asyncio.sleep(0.2)
        bus.publish(
            "tool.execute.finish",
            {
                "sessionUpdate": _su("completed"),
                "toolCallId": t_todo2,
                "title": "manage_todo",
                "status": "completed",
                "ok": True,
                "content": [
                    {
                        "text": (
                            "📋 TODO  (4/4 done)\n\n"
                            "  ✅ 1. Audit §4.5 event coverage\n"
                            "  ✅ 2. Rewrite mock_engine.py\n"
                            "  ✅ 3. Update core_bridge.py\n"
                            "  ✅ 4. Run verification checks\n"
                        )
                    }
                ],
            },
        )
        await asyncio.sleep(0.2)

        # plan.progress — final step, legacy schema
        bus.publish(
            "plan.progress",
            {
                "step": 4,
                "total": 4,
                "description": "All checks passed — spec compliance verified",
            },
        )
        await asyncio.sleep(0.2)

        # context.degraded — bridge compacted context to free space (§4.5)
        bus.publish(
            "context.degraded",
            {
                "target_window": 24000,
                "reason": "Execution trace compacted — 8,000 tokens freed",
            },
        )
        await asyncio.sleep(0.15)

        # ── Stream: QA summary ────────────────────────────────────────────
        await _stream(
            bus,
            (
                "Verification complete. "
                "Linter found 2 non-blocking warnings; syntax check clean on 41 files; "
                "all 52 unit tests pass. "
                "The mock engine now exercises every event category in §4.5 "
                "with full ACP schema compliance. "
                "Plan approval UI, bash tier-3 gate, tool error path, "
                "and all session events confirmed working."
            ),
            delay=0.015,
        )

        bus.publish(
            "model.response",
            {
                "provider": "local_lm_studio",
                "model": "qwen2.5-coder-14b",
                "tokens": 512,
            },
        )
        await asyncio.sleep(0.3)

        # Token budget — red zone + warning
        bus.publish(
            "token.budget.update", {"used": 28900, "limit": 32000, "percent": 90.3}
        )
        bus.publish(
            "token.budget.warning", {"used": 28900, "limit": 32000, "percent": 90.3}
        )
        await asyncio.sleep(0.15)

        # file.deleted — exercises _on_file_deleted in bridge
        bus.publish(
            "file.deleted",
            {
                "path": "/workspace/project/.agent-context/tmp_analysis.md",
                "workdir": "/workspace/project",
            },
        )
        await asyncio.sleep(0.1)

        # task.queue.updated — all tasks complete, queue drained (§4.5)
        bus.publish(
            "task.queue.updated",
            {
                "pending_count": 0,
                "queue_size": 0,
                "new_status": "completed",
            },
        )
        await asyncio.sleep(0.1)

        bus.publish(
            "log.new",
            {
                "level": "INFO",
                "logger": "mock_engine",
                "message": "Simulation complete — all §4.5 events exercised",
            },
        )

        bus.publish(
            "ui.notification",
            {
                "level": "success",
                "message": "All checks passed — TUI is now fully spec-compliant",
                "source": "qa_lead",
            },
        )
        await asyncio.sleep(0.3)

        # ── Final response ─────────────────────────────────────────────────
        self.post_message(
            AgentFinalResponse(
                content=(
                    "## Spec-Compliance Achieved\n\n"
                    "Every event category from §4.5 is now exercised by the mock engine:\n\n"
                    "| Category | Events covered |\n"
                    "|---|---|\n"
                    "| Provider / Model | `orchestrator.startup`, `provider.status.changed`, "
                    "`provider.models.list`, `model.routing`, `model.response`, `model.token` |\n"
                    "| Tool execution | `tool.execute.start` / `finish` / `error` "
                    "(full ACP schema: `sessionUpdate`, `status`, `toolCallId`, `title`, `rawInput`) |\n"
                    "| File | `file.diff.preview`, `file.modified` (with `tool`+`workdir`), "
                    "`file.deleted`, `preview.pending` |\n"
                    "| Planning | `plan.progress` — ACP schema **and** legacy schema; `plan.requested` |\n"
                    "| Session | `session.new`, `session.registered`, `session.hydrated`, "
                    "`session.health_alert` |\n"
                    "| Role | `role.transition` — via EventBus bridge translator |\n"
                    "| Logging | `log.new` → direct to console panel (§16.4 compliant) |\n"
                    "| Notifications | `ui.notification` |\n"
                    "| Token budget | `token.budget.update`, `token.budget.warning` |\n"
                    "| Retry / resilience | `retry.attempt`, `retry.succeeded`, `retry.failed` |\n"
                    "| Context | `context.degraded` |\n"
                    "| Task queue | `task.queue.updated` |\n"
                    "| Git | `git.branch` |\n\n"
                    "### Compliance checklist\n"
                    "- [x] §9 Threading: `_agent_lock`, `_cancel_event`, `_history_lock`\n"
                    "- [x] §10 Lifecycle: startup → `session.request_state` → hydration\n"
                    "- [x] §11 Slash commands: all 10 implemented\n"
                    "- [x] §12 All 8 UI panels + token colour coding\n"
                    "- [x] §14 Plan approval UI (`plan.requested` → Approve/Reject buttons)\n"
                    "- [x] §15 History persistence: atomic JSON write\n"
                    "- [x] §16 Security: bash tier-3 gate, no recursive `log.new`, "
                    "read-before-write display\n"
                )
            )
        )

        bus.publish(
            "token.budget.update", {"used": 29700, "limit": 32000, "percent": 92.8}
        )
        logger.info("Mock simulation completed — all §4.5 events exercised")


if __name__ == "__main__":
    app = MockSimulationApp()
    app.run()
