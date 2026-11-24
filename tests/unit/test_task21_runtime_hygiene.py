"""TASK-21 regression tests: runtime state hygiene, bounded telemetry,
single per-turn schema resolution, and persistent MCP daemon event loop.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# 1. Read-only bundled src/config/providers.json + UserPrefs layering
# ---------------------------------------------------------------------------


class TestReadOnlyBundledProvidersJson:
    def test_persist_active_provider_writes_user_prefs_not_bundled_json(
        self, tmp_path, monkeypatch
    ):
        from src.core.inference.llm_manager import (
            ProviderManager,
            _set_provider_active,
        )
        from src.core.inference.provider_config import (
            _BUNDLED_PROVIDERS_CONFIG,
        )
        from src.core.user_prefs import UserPrefs

        prefs_path = tmp_path / "prefs.json"
        monkeypatch.setenv("CODINGAGENT_PREFS", str(prefs_path))

        before_bytes = _BUNDLED_PROVIDERS_CONFIG.read_bytes()

        pm = ProviderManager()  # targets bundled src/config/providers.json
        assert pm._persist_active_provider("ollama") is True
        assert _BUNDLED_PROVIDERS_CONFIG.read_bytes() == before_bytes
        assert pm.get_active_provider_name() == "ollama"

        loaded_prefs = UserPrefs.load()
        assert loaded_prefs.selected_model_provider == "ollama"

        _set_provider_active("github_copilot", True)
        assert _BUNDLED_PROVIDERS_CONFIG.read_bytes() == before_bytes
        assert pm.get_active_provider_name() == "github_copilot"

    def test_model_routing_and_status_layer_user_prefs_over_bundled(
        self, tmp_path, monkeypatch
    ):
        from src.cli.status_cmd import _active_provider
        from src.core.inference.llm_manager import ProviderManager
        from src.core.inference.provider_config import (
            _BUNDLED_PROVIDERS_CONFIG,
            apply_user_prefs_to_providers,
            canonical_provider_name,
        )
        from src.core.user_prefs import UserPrefs

        prefs_path = tmp_path / "prefs.json"
        monkeypatch.setenv("CODINGAGENT_PREFS", str(prefs_path))
        before_bytes = _BUNDLED_PROVIDERS_CONFIG.read_bytes()

        pm = ProviderManager()
        dummy_adapter = SimpleNamespace(default_model="old-model")
        pm._providers["ollama"] = dummy_adapter

        pm._on_model_routing({"provider": "ollama", "selected": "qwen3:14b"})

        assert _BUNDLED_PROVIDERS_CONFIG.read_bytes() == before_bytes
        assert dummy_adapter.default_model == "qwen3:14b"

        prefs = UserPrefs.load()
        assert prefs.selected_model_provider == "ollama"
        assert prefs.selected_model_name == "qwen3:14b"

        prefs.update_provider_config("ollama", base_url="http://127.0.0.1:11435")
        layered = apply_user_prefs_to_providers(
            [
                {"name": "lm_studio", "type": "lm_studio", "active": True},
                {
                    "name": "ollama",
                    "type": "ollama",
                    "active": False,
                    "base_url": "http://localhost:11434",
                },
            ],
            canonical_provider_name,
        )
        by_name = {entry["name"]: entry for entry in layered}
        assert by_name["lm_studio"]["active"] is False
        assert by_name["ollama"]["active"] is True
        assert by_name["ollama"]["default_model"] == "qwen3:14b"
        assert by_name["ollama"]["base_url"] == "http://127.0.0.1:11435"

        status = _active_provider()
        assert canonical_provider_name(status["provider"]) == "ollama"
        assert status["model"] == "qwen3:14b"


# ---------------------------------------------------------------------------
# 2. Bounded timings.json ring buffer
# ---------------------------------------------------------------------------


def test_timings_json_bounded_across_500_step_transactions(tmp_path):
    from src.core.orchestration.orchestrator_helpers import (
        DEFAULT_TIMINGS_MAX_ENTRIES,
        begin_step_transaction_impl,
    )

    orch = SimpleNamespace(working_dir=tmp_path, _step_snapshot_id=None)
    step_ids = []
    for _ in range(500):
        step_ids.append(begin_step_transaction_impl(orch))

    timings_path = tmp_path / ".codingAgent" / "timings.json"
    assert timings_path.exists()
    entries = json.loads(timings_path.read_text(encoding="utf-8"))
    assert isinstance(entries, list)
    assert len(entries) == DEFAULT_TIMINGS_MAX_ENTRIES
    assert len(entries) <= 200
    assert entries[0]["step_id"] == step_ids[-DEFAULT_TIMINGS_MAX_ENTRIES]
    assert entries[-1]["step_id"] == step_ids[-1]


# ---------------------------------------------------------------------------
# 3. Single resolve_effective_tool_schemas() per turn in frontier_loop_node
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_frontier_loop_resolves_effective_tool_schemas_once_per_turn(tmp_path):
    import src.core.orchestration.graph.nodes.frontier_loop_node as fln

    orch = MagicMock()
    orch.working_dir = tmp_path
    orch.cancel_event = None
    orch.active_role = "operational"
    orch.execute_tool.return_value = {"status": "ok", "output": "file contents"}

    real_schema = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]

    responses = [
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"path": "a.py"}',
                                },
                            }
                        ],
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Done reading a.py.",
                    }
                }
            ]
        },
    ]

    call_idx = {"n": 0}

    async def _fake_call_model(**kwargs):
        assert kwargs.get("tools") == real_schema
        idx = call_idx["n"]
        call_idx["n"] += 1
        return responses[idx]

    with (
        patch.object(
            fln,
            "resolve_effective_tool_schemas",
            return_value=real_schema,
        ) as spy_schemas,
        patch.object(fln, "call_model", side_effect=_fake_call_model),
        patch(
            "src.core.orchestration.provider_capabilities.resolve_provider_capabilities",
            return_value={"supports_native_tools": True},
        ),
        patch(
            "src.core.context.context_builder.ContextBuilder.build_prompt",
            return_value=[{"role": "system", "content": "sys"}, {"role": "user", "content": "read"}],
        ),
    ):
        state = {
            "task": "Read a.py and summarize it",
            "history": [],
            "working_dir": str(tmp_path),
            "model_tier": "frontier",
            "max_turns": 5,
            "tool_call_count": 0,
            "max_tool_calls": 10,
            "provider": "openai",
            "model": "gpt-4o",
        }
        result = await fln.frontier_loop_node(
            state, {"configurable": {"orchestrator": orch}}
        )

    assert result.get("_frontier_loop_turns") == 2
    assert spy_schemas.call_count == 2


# ---------------------------------------------------------------------------
# 4. Persistent MCP daemon event loop across run_deferred_init_sync & close
# ---------------------------------------------------------------------------


def test_mcp_tool_call_after_run_deferred_init_sync_uses_live_loop_and_closes_cleanly(
    tmp_path, monkeypatch
):
    from src.core.mcp.manager import McpServerManager
    from src.core.orchestration.orchestrator import Orchestrator

    lifecycle = {
        "connect_loop": None,
        "call_loop": None,
        "disconnected": False,
        "calls": [],
    }

    class _LoopBoundMcpClient:
        def __init__(self, name: str, cmd: list[str], env=None):
            self.name = name
            self.cmd = cmd
            self._connected = False
            self._queue: asyncio.Queue[str] | None = None
            self._worker: asyncio.Task | None = None

        def add_notification_handler(self, _cb):
            return None

        async def connect(self) -> None:
            loop = asyncio.get_running_loop()
            lifecycle["connect_loop"] = loop
            self._queue = asyncio.Queue()

            async def _bg() -> None:
                while True:
                    await asyncio.sleep(60)

            self._worker = asyncio.create_task(_bg())
            self._connected = True

        async def call_tool(self, tool_name: str, arguments: dict):
            loop = asyncio.get_running_loop()
            lifecycle["call_loop"] = loop
            assert loop is lifecycle["connect_loop"], (
                "MCP call_tool must run on the same live event loop used by connect()"
            )
            assert not loop.is_closed()
            assert self._queue is not None
            await self._queue.put(arguments.get("msg", ""))
            echoed = await self._queue.get()
            lifecycle["calls"].append((tool_name, echoed))
            return SimpleNamespace(ok=True, content=f"pong:{echoed}", error=None)

        async def register_tools(self, registry) -> int:
            async def _call(**kwargs):
                res = await self.call_tool("ping", kwargs)
                if res.ok:
                    return {"ok": True, "output": res.content}
                return {"ok": False, "error": res.error}

            registry.register(
                name=f"{self.name}/ping",
                fn=_call,
                description="Ping MCP demo server",
                origin="mcp",
            )
            return 1

        async def disconnect(self) -> None:
            if self._worker is not None:
                self._worker.cancel()
                try:
                    await self._worker
                except asyncio.CancelledError:
                    pass
            self._connected = False
            lifecycle["disconnected"] = True

    monkeypatch.setattr("src.core.mcp.manager.McpStdioClient", _LoopBoundMcpClient)
    monkeypatch.setattr(
        "src.core.mcp.manager.get_mcp_config",
        lambda _wd=None: {"auto_register_tools": True},
    )
    monkeypatch.setattr(
        "src.core.mcp.manager.get_mcp_servers",
        lambda _wd=None: [
            {
                "name": "demo",
                "transport": "stdio",
                "cmd": ["python", "-c", "pass"],
            }
        ],
    )

    orch = Orchestrator(working_dir=str(tmp_path), trusted=True)
    try:
        from src.core.orchestration.permission_policy import (
            Behavior,
            PermissionRule,
            get_permission_policy,
        )

        # Explicitly approve the MCP tool via a session ALLOW rule so the live
        # permission gate passes without blocking for interactive UI input.
        policy = get_permission_policy()
        orig_rules = policy._rules
        policy._rules = (
            PermissionRule(pattern="demo/ping", behavior=Behavior.ALLOW),
        ) + tuple(orig_rules)
        try:
            assert isinstance(orch.mcp_manager, McpServerManager)
            init_res = orch.run_deferred_init_sync(trusted=True)
            assert init_res.mcp_started is True
            assert lifecycle["connect_loop"] is not None
            assert not lifecycle["connect_loop"].is_closed()

            bg_thread = orch.mcp_manager._bg_thread
            assert bg_thread is not None and bg_thread.is_alive()

            # Execute the registered MCP tool through the live Orchestrator dispatch boundary
            tool_res = orch.execute_tool({"name": "demo/ping", "arguments": {"msg": "hello"}})
            assert tool_res.get("ok") is True
            assert tool_res.get("output") == "pong:hello" or (
                isinstance(tool_res.get("result"), dict)
                and tool_res["result"].get("output") == "pong:hello"
            )
            assert lifecycle["call_loop"] is lifecycle["connect_loop"]
            assert lifecycle["calls"] == [("ping", "hello")]
        finally:
            policy._rules = orig_rules
    finally:
        orch.close()

    assert lifecycle["disconnected"] is True
    assert orch.mcp_manager._bg_loop is None
    assert orch.mcp_manager._bg_thread is None
    assert bg_thread is not None and not bg_thread.is_alive()
