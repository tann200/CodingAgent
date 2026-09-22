"""
Real integration tests for tool chain execution.

Real Orchestrator + real built-in tools (file read/write/edit, bash,
search_code) with only the LLM mocked via a scripted DeterministicAdapter.
Follows the integration_real README policy: never mock tool execution.

These replace the pre-Phase-5 templates that targeted the obsolete
`old_string`/`new_string` edit_file API, the removed `_llm_manager` adapter
injection, and the pre-hardening bash operator behavior (shell operators are
now refused by the preflight operator guard by design).
"""

import json
import time
from pathlib import Path

import pytest

from tests.integration.mocks.deterministic_adapter import DeterministicAdapter


pytestmark = pytest.mark.integration_real

# Modules that import call_model directly at module load time — all must be
# patched so the scripted adapter drives the loop (same set as
# tests/integration/test_agent_loop_plaintext_tools.py).
_CALL_MODEL_TARGETS = [
    "src.core.orchestration.graph.nodes.execution_node.call_model",
    "src.core.orchestration.graph.nodes.planning_node.call_model",
    "src.core.orchestration.graph.nodes.perception_node.call_model",
    "src.core.orchestration.graph.nodes.debug_node.call_model",
    "src.core.orchestration.graph.nodes.replan_node.call_model",
    "src.core.inference.llm_manager.call_model",
]

# DeterministicAdapter scenario: plaintext YAML tool-call blocks.
_SEARCH_READ_EDIT = [
    "```yaml\nname: search_code\narguments:\n  query: old_name\n```",
    "```yaml\nname: read_file\narguments:\n  path: app.py\n```",
    '```yaml\nname: edit_file\narguments:\n  path: app.py\n  patch: "@@ -1 +1 @@\\n-def old_name(): pass\\n+def new_name(): return 1\\n"\n```',
    "Done — renamed old_name to new_name.",
]

_EDIT_PATCH_OLD_TO_NEW = (
    '@@ -1 +1 @@\n-{"setting": "old_value"}\n+{"setting": "new_value"}\n'
)


def _make_scripted_loop(monkeypatch, tmp_path, scenario, scenario_name):
    """Wire a real Orchestrator whose LLM calls replay `scenario` steps."""
    from src.core.orchestration.orchestrator import Orchestrator

    # HS-1: autonomous approval requires an explicit operator allowlist opt-in.
    # "*" approves every gated tool so the real write/bash tools run un-prompted.
    monkeypatch.setattr("src.tools.tools_config._AUTONOMOUS_MODE", True)
    monkeypatch.setattr("src.tools.tools_config._AUTONOMOUS_APPROVE", frozenset({"*"}))

    # Prevent the ORCH-W5 background title thread from consuming adapter
    # responses before perception_node gets them.
    try:
        monkeypatch.setattr(
            "src.core.memory.distiller.generate_session_title",
            lambda msg: "Test Session",
        )
    except AttributeError:
        pass

    adapter = DeterministicAdapter(scenarios={scenario_name: scenario})
    adapter.set_scenario(scenario_name)

    async def mock_call_model(messages, model=None, provider=None, *largs, **kwargs):
        return adapter.generate(messages, model=model, provider=provider, **kwargs)

    for target in _CALL_MODEL_TARGETS:
        try:
            monkeypatch.setattr(target, mock_call_model)
        except AttributeError:
            pass

    return Orchestrator(adapter=adapter, working_dir=str(tmp_path))


def test_search_read_edit_full_loop_real(tmp_path, monkeypatch):
    """search_code → read_file → edit_file with real tools and a scripted LLM."""
    (tmp_path / "app.py").write_text("def old_name(): pass\n")

    orch = _make_scripted_loop(monkeypatch, tmp_path, _SEARCH_READ_EDIT, "search_read_edit")

    messages = [{"role": "user", "content": "Rename old_name to new_name in app.py"}]
    expected = ["search_code", "read_file", "edit_file"]
    executed = []
    trace_path = tmp_path / ".codingAgent" / "execution_trace.json"

    for _ in range(12):
        orch.run_agent_once(None, messages, {})
        time.sleep(0.05)
        try:
            trace = json.loads(trace_path.read_text()) if trace_path.exists() else []
        except Exception:
            trace = []
        executed = [step.get("tool") for step in trace]
        if executed == expected:
            break

    assert executed, "no tools were executed"
    it = iter(expected)
    for tool in executed:
        try:
            while next(it) != tool:
                continue
        except StopIteration:
            pytest.fail(f"executed tool {tool!r} not in expected sequence {expected}")

    content = (tmp_path / "app.py").read_text()
    assert "new_name" in content, "real edit_file must have renamed the function"
    assert "old_name" not in content


def test_read_before_write_enforcement_integration(tmp_path):
    """GAP-S1: edit_file without a prior read is blocked; after read it runs."""
    from src.core.orchestration.orchestrator import Orchestrator

    test_file = tmp_path / "config.json"
    test_file.write_text('{"setting": "old_value"}\n')

    orch = Orchestrator(working_dir=str(tmp_path))

    blocked = orch.execute_tool({
        "name": "edit_file",
        "arguments": {"path": "config.json", "patch": _EDIT_PATCH_OLD_TO_NEW},
    })
    assert blocked.get("ok") is False, blocked
    assert "must read" in blocked.get("error", "").lower()

    read = orch.execute_tool({"name": "read_file", "arguments": {"path": "config.json"}})
    assert read.get("ok") is True, read

    allowed = orch.execute_tool({
        "name": "edit_file",
        "arguments": {"path": "config.json", "patch": _EDIT_PATCH_OLD_TO_NEW},
    })
    assert allowed.get("ok") is not False, allowed
    assert '"setting": "new_value"' in test_file.read_text()


def test_bash_execution_contract_integration(tmp_path):
    """Benign bash runs; shell operators are refused by preflight by design."""
    from src.core.orchestration.orchestrator import Orchestrator

    orch = Orchestrator(working_dir=str(tmp_path))

    benign = orch.execute_tool({
        "name": "bash",
        "arguments": {"command": "echo bash-ok"},
    })
    assert benign.get("ok") is True, benign
    assert "bash-ok" in str(benign.get("result", {}))

    redir = orch.execute_tool({
        "name": "bash",
        "arguments": {"command": f"echo x > {tmp_path}/out.txt"},
    })
    assert redir.get("ok") is False, redir
    assert "dangerous" in redir.get("error", "").lower()
    assert not (tmp_path / "out.txt").exists()


def test_file_tools_integration(tmp_path):
    """write_file → read_file → edit_file (unified-patch API) end to end."""
    from src.core.orchestration.orchestrator import Orchestrator

    orch = Orchestrator(working_dir=str(tmp_path))

    r = orch.execute_tool({
        "name": "write_file",
        "arguments": {"path": "notes.txt", "content": "Original content\nLine 2\nLine 3"},
    })
    assert r.get("ok") is True, r
    notes = tmp_path / "notes.txt"
    assert notes.exists()

    r = orch.execute_tool({"name": "read_file", "arguments": {"path": "notes.txt"}})
    assert r.get("ok") is True, r
    assert "Original content" in str(r.get("result", r))

    patch = "@@ -1 +1 @@\n-Original content\n+Modified content\n"
    r = orch.execute_tool({
        "name": "edit_file",
        "arguments": {"path": "notes.txt", "patch": patch},
    })
    assert r.get("ok") is not False, r
    final = notes.read_text()
    assert "Modified content" in final
    assert "Original content" not in final


def test_file_write_invalidates_context_cache(tmp_path):
    """MEM-1: write_file evicts the written path from ContextBuilder caches."""
    from src.core.context.context_builder import (
        ContextBuilder,
        _TEXT_CACHE,
        _JSON_CACHE,
    )
    from src.core.orchestration.orchestrator import Orchestrator

    data = tmp_path / "data.txt"
    data.write_text("Version 1")
    key = str(Path(data).resolve())

    ContextBuilder._read_text_cached(data)
    assert key in _TEXT_CACHE, "read should prime the text cache"

    orch = Orchestrator(working_dir=str(tmp_path))
    # RBW (GAP-S1): writing an *existing* file requires a prior session read.
    read = orch.execute_tool({"name": "read_file", "arguments": {"path": "data.txt"}})
    assert read.get("ok") is True, read
    r = orch.execute_tool({
        "name": "write_file",
        "arguments": {"path": "data.txt", "content": "Version 2"},
    })
    assert r.get("ok") is True, r
    assert key not in _TEXT_CACHE, "write_file must invalidate the text cache"
    assert key not in _JSON_CACHE, "write_file must invalidate the JSON cache"
