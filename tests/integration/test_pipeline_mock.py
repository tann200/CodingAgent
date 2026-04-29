"""tests/integration/test_pipeline_mock.py — S10-D

Mock-adapter pipeline tests that always run in CI (no live LLM required).
These tests use MockAdapter with node-aware response scripting to drive the
real LangGraph pipeline without a live provider.

Coverage:
  PM-1  write_file: agent writes a new file; file appears on disk
  PM-2  read_file:  agent reads a pre-existing file; marker in result
  PM-3  write-then-overwrite: agent reads then overwrites file contents
  PM-4  orchestrator e2e smoke: single turn, adapter wiring verified (DeterministicAdapter)
  PM-5  multi-step workflow: write then read, two-turn pipeline
  PM-6  fix_syntax: three-step sequence — read_file → edit_file → run_tests
"""


# ruff: noqa: E501
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

from src.core.inference.adapters.mock_adapter import MockAdapter
from src.core.orchestration.orchestrator import Orchestrator
from tests.integration.mocks.deterministic_adapter import DeterministicAdapter

# Every graph node that imports call_model at module load time.
_CALL_MODEL_TARGETS = [
    "src.core.orchestration.graph.nodes.perception_node.call_model",
    "src.core.orchestration.graph.nodes.planning_node.call_model",
    "src.core.orchestration.graph.nodes.execution_node.call_model",
    "src.core.orchestration.graph.nodes.debug_node.call_model",
    "src.core.orchestration.graph.nodes.replan_node.call_model",
    "src.core.inference.llm_manager.call_model",
    "src.core.inference.llm_manager._call_model_internal",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_call_model(adapter, monkeypatch) -> None:
    """Patch every node's call_model import to use the provided adapter."""

    async def mock_call_model(messages, model=None, provider=None, *args, **kwargs):
        return adapter.generate(messages, model=model, provider=provider, **kwargs)

    for target in _CALL_MODEL_TARGETS:
        try:
            monkeypatch.setattr(target, mock_call_model)
        except AttributeError:
            pass


def _patch_infra(monkeypatch) -> None:
    """Patch heavy infrastructure that doesn't need to run in unit-style tests."""
    monkeypatch.setattr(
        "src.core.orchestration.orchestrator._ensure_provider_manager_initialized_sync",
        lambda: None,
    )
    monkeypatch.setattr(
        "src.core.orchestration.orchestrator.Orchestrator._background_model_check",
        lambda self: None,
    )
    # Prevent the background ORCH-W5 title-generation thread from consuming
    # MockAdapter responses before perception_node gets them.  The thread runs
    # _gen_title → generate_session_title → _call_llm_sync → call_model and
    # increments the adapter's call_count, offsetting the scripted response sequence.
    try:
        monkeypatch.setattr(
            "src.core.memory.distiller.generate_session_title",
            lambda msg: "Test Session",
        )
    except AttributeError:
        pass
    # Reset the compiled graph cache so changes to routing functions (e.g. route_execution
    # _mod_kws) take effect in this test session rather than being shadowed by a cached graph.
    try:
        import src.core.orchestration.graph.builder as _builder

        monkeypatch.setattr(_builder, "_COMPILED_GRAPH", None)
    except AttributeError:
        pass


def _build_orch(tmp_path: Path, adapter, monkeypatch) -> Orchestrator:
    """Build an Orchestrator wired with a scripted adapter (DeterministicAdapter)."""
    _patch_call_model(adapter, monkeypatch)
    _patch_infra(monkeypatch)
    return Orchestrator(
        adapter=adapter,
        working_dir=str(tmp_path),
        allow_external_working_dir=True,
    )


def _build_orch_mock(
    tmp_path: Path, responses: list, monkeypatch
) -> tuple[Orchestrator, MockAdapter]:
    """Build an Orchestrator wired with a MockAdapter.

    Returns (orchestrator, adapter) so the caller can inspect adapter.call_count.
    The MockAdapter uses strict=False so the last response is repeated when the
    script is exhausted — allowing the completion-text round to terminate the loop.
    """
    adapter = MockAdapter(responses=responses, strict=False)
    _patch_call_model(adapter, monkeypatch)
    _patch_infra(monkeypatch)
    orch = Orchestrator(
        adapter=adapter,
        working_dir=str(tmp_path),
        allow_external_working_dir=True,
    )
    return orch, adapter


# ---------------------------------------------------------------------------
# PM-1  write_file: scripted agent call creates a file on disk
# ---------------------------------------------------------------------------


def test_pm1_write_file_creates_file(tmp_path, monkeypatch):
    """Perception returns a write_file YAML → fast-path execution → file created."""
    target = "greeting.txt"
    content = "Hello, mock pipeline!"

    # Response 0: perception returns a YAML tool call → sets next_action (fast-path).
    # Response 1 (repeated): plain text → perception sees no YAML → routes to memory_sync.
    orch, adapter = _build_orch_mock(
        tmp_path,
        [
            f'```yaml\nname: write_file\narguments:\n  path: {target}\n  content: "{content}"\n```',
            "I have written the file successfully.",
        ],
        monkeypatch,
    )

    result = orch.run_agent_once(
        None, [{"role": "user", "content": f"Write '{content}' to {target}"}], {}
    )

    assert isinstance(result, dict)
    target_path = tmp_path / target
    assert target_path.exists(), (
        f"Expected {target} to exist in {tmp_path}.\nOrch result: {result}"
    )
    assert content in target_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# PM-2  read_file: scripted agent reads a pre-existing file; marker in result
# ---------------------------------------------------------------------------


def test_pm2_read_file_marker_present(tmp_path, monkeypatch):
    """Perception returns a read_file YAML → execution reads file → marker in history."""
    secret = "UNIQUE_MARKER_XY7291"
    source = tmp_path / "data.txt"
    source.write_text(f"Line one\n{secret}\nLine three\n", encoding="utf-8")

    orch, _ = _build_orch_mock(
        tmp_path,
        [
            "```yaml\nname: read_file\narguments:\n  path: data.txt\n```",
            "I have read the file and found all its content.",
        ],
        monkeypatch,
    )

    result = orch.run_agent_once(
        None,
        [{"role": "user", "content": "Read data.txt and report its contents."}],
        {},
    )

    assert isinstance(result, dict)
    # The secret marker must appear in the message history via tool_execution_result.
    result_text = json.dumps(result)
    history_text = json.dumps(orch.msg_mgr.all())
    assert (secret in result_text) or (secret in history_text), (
        f"Secret marker '{secret}' not found in agent output.\n"
        f"Result keys: {list(result.keys()) if isinstance(result, dict) else type(result)}"
    )


# ---------------------------------------------------------------------------
# PM-3  write-then-overwrite: read config.py → write new content with 'new_name'
# ---------------------------------------------------------------------------


def test_pm3_overwrite_file_changes_land(tmp_path, monkeypatch):
    """
    Agent reads config.py (satisfying read-before-write guard), then writes
    new content that replaces 'old_name' with 'new_name'.

    Route:  perception(read) → execution(read_file) →
            perception(write) → execution(write_file) →
            perception(done) → memory_sync
    """
    target = tmp_path / "config.py"
    target.write_text("DEBUG = False\nNAME = 'old_name'\n", encoding="utf-8")

    orch, _ = _build_orch_mock(
        tmp_path,
        [
            # Round 0: read the file to satisfy the read-before-write guard.
            "```yaml\nname: read_file\narguments:\n  path: config.py\n```",
            # Round 1: write new content.  'replace ' in task triggers read_then_modify
            # routing after round 0, bringing us back to perception for round 1.
            '```yaml\nname: write_file\narguments:\n  path: config.py\n  content: "NAME = new_name"\n```',
            # Round 2: completion acknowledgement → no YAML → memory_sync.
            "config.py has been updated.",
        ],
        monkeypatch,
    )

    result = orch.run_agent_once(
        None,
        [
            {
                "role": "user",
                "content": "In config.py replace 'old_name' with 'new_name'.",
            }
        ],
        {},
    )

    assert isinstance(result, dict)
    disk_content = target.read_text(encoding="utf-8")
    assert "old_name" not in disk_content, (
        f"Old value still present after agent turn.\nActual:\n{disk_content}"
    )
    assert "new_name" in disk_content, (
        f"Expected 'new_name' in config.py after agent turn.\nActual:\n{disk_content}"
    )


# ---------------------------------------------------------------------------
# PM-4  Orchestrator e2e smoke (DeterministicAdapter — already passing)
# ---------------------------------------------------------------------------


def test_pm4_orchestrator_e2e_smoke(tmp_path, monkeypatch):
    """Single-turn smoke test: Orchestrator runs, returns a dict, no crash."""
    scenarios = {
        "smoke": [
            "To add a new tool you would edit src/core/tools/ and register it in the tool registry.",
        ]
    }
    adapter = DeterministicAdapter(scenarios=scenarios)
    adapter.set_scenario("smoke")
    orch = _build_orch(tmp_path, adapter, monkeypatch)

    out = orch.run_agent_once(
        None,
        [
            {
                "role": "user",
                "content": "Describe how to add a new tool to the orchestration component.",
            }
        ],
        {},
    )
    assert isinstance(out, dict), f"Expected dict, got {type(out)}"
    assert not (out.get("ok") is False and out.get("error")), (
        f"Orchestrator returned an error: {out.get('error')}"
    )


# ---------------------------------------------------------------------------
# PM-5  Multi-step workflow: write then read (two orchestrator turns)
# ---------------------------------------------------------------------------


def test_pm5_multi_step_write_then_read(tmp_path, monkeypatch):
    """Two-turn workflow: agent writes a file in turn 1, reads it in turn 2.

    The MockAdapter persists call_count across turns so responses are consumed
    sequentially:  turn-1 uses responses[0-1], turn-2 uses responses[2-3].
    """
    content = "multi-step-content-42"
    filename = "result.txt"

    orch, _ = _build_orch_mock(
        tmp_path,
        [
            # Turn 1, round 0: write the file.
            f'```yaml\nname: write_file\narguments:\n  path: {filename}\n  content: "{content}"\n```',
            # Turn 1, round 1: completion → memory_sync.
            "File written successfully.",
            # Turn 2, round 0: read the file back.
            f"```yaml\nname: read_file\narguments:\n  path: {filename}\n```",
            # Turn 2, round 1: completion → memory_sync.
            "I read the file and found the content.",
        ],
        monkeypatch,
    )

    # Turn 1: write the file
    r1 = orch.run_agent_once(
        None, [{"role": "user", "content": f"Write '{content}' to {filename}"}], {}
    )
    assert isinstance(r1, dict)
    assert (tmp_path / filename).exists(), "File should exist after turn 1"

    # Turn 2: read back the file
    r2 = orch.run_agent_once(
        None,
        [{"role": "user", "content": f"Read {filename} and report the content."}],
        {},
    )
    assert isinstance(r2, dict)
    history_text = json.dumps(orch.msg_mgr.all())
    assert content in history_text, (
        f"Expected content '{content}' in message history after turn 2."
    )


# ---------------------------------------------------------------------------
# PM-6  fix_syntax: three-step read → edit → test pipeline
# ---------------------------------------------------------------------------


def test_pm6_fix_syntax_pipeline(tmp_path, monkeypatch):
    """Three-step fix_syntax scenario: read_file → edit_file → run_tests.

    Uses MockAdapter with node-aware responses to drive the fast-path through
    all three tool calls in a single run_agent_once invocation.

    Route per call_model call:
      call 0: read_file YAML    → execution dispatches read_file
      call 1: edit_file YAML    → execution dispatches edit_file (file already read)
      call 2: run_tests YAML    → execution dispatches run_tests
      call 3: completion text   → next_action=None, last_result ok → memory_sync
    """
    monkeypatch.setattr("src.tools.tools_config._AUTONOMOUS_MODE", True)

    dummy_file = tmp_path / "src" / "dummy.py"
    dummy_file.parent.mkdir(parents=True, exist_ok=True)
    dummy_file.write_text("def foo(): pass\n", encoding="utf-8")

    orch, adapter = _build_orch_mock(
        tmp_path,
        [
            # call 0: read dummy.py first (clears read-before-write guard for edit_file)
            "```yaml\nname: read_file\narguments:\n  path: src/dummy.py\n```",
            # call 1: edit dummy.py
            (
                "```yaml\n"
                "name: edit_file\n"
                "arguments:\n"
                "  path: src/dummy.py\n"
                '  patch: "@@ -1 +1 @@\\n- def foo(): pass\\n+ def foo(): return 1\\n"\n'
                "```"
            ),
            # call 2: run_tests
            "```yaml\nname: run_tests\narguments: {}\n```",
            # call 3: completion → task complete signal → routes to memory_sync
            "Task complete. The syntax has been fixed and tests pass.",
        ],
        monkeypatch,
    )

    # Register mock implementations so execute_tool doesn't call real binaries.
    def run_tests_mock(**kwargs):
        return {"status": "ok", "output": "Tests passed", "returncode": 0}

    def bash_mock(**kwargs):
        return {
            "status": "ok",
            "stdout": "Command executed",
            "stderr": "",
            "exit_code": 0,
        }

    def search_code_mock(**kwargs):
        return {
            "status": "ok",
            "results": [{"file": "test.py", "line": 1, "content": "found"}],
        }

    def edit_file_mock(**kwargs):
        return {"status": "ok", "path": kwargs.get("path"), "edited": True}

    orch.tool_registry.register("run_tests", run_tests_mock, [], "Run test suite")
    orch.tool_registry.register("bash", bash_mock, [], "Run bash")
    orch.tool_registry.register("search_code", search_code_mock, [], "Search code")
    orch.tool_registry.register("edit_file", edit_file_mock, ["write"], "Edit file")

    result = orch.run_agent_once(
        None,
        [
            {
                "role": "user",
                "content": "Fix the syntax in src/dummy.py and run the tests.",
            }
        ],
        {},
    )

    assert isinstance(result, dict), f"Expected dict result, got {type(result)}"

    # Verify execution trace records the expected tool calls.
    trace_path = tmp_path / ".codingAgent" / "execution_trace.json"
    assert trace_path.exists(), "Execution trace must be written by run_agent_once"

    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    executed_tools = [step.get("tool") for step in trace if step.get("tool")]
    assert len(executed_tools) >= 3, (
        f"Expected at least 3 tool calls (read, edit, test); got {executed_tools}"
    )
    assert "read_file" in executed_tools, f"read_file not in trace: {executed_tools}"
    assert "edit_file" in executed_tools, f"edit_file not in trace: {executed_tools}"
    assert "run_tests" in executed_tools, f"run_tests not in trace: {executed_tools}"

    # Verify read before edit order (read_file must precede edit_file).
    tool_names = [s.get("tool") for s in trace]
    read_idx = next((i for i, t in enumerate(tool_names) if t == "read_file"), None)
    edit_idx = next((i for i, t in enumerate(tool_names) if t == "edit_file"), None)
    assert read_idx is not None and edit_idx is not None
    assert read_idx < edit_idx, (
        f"read_file must precede edit_file but got order: {tool_names}"
    )
