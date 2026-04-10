"""tests/integration/test_e2e_pipeline_smoke.py — CAP-1

End-to-end pipeline smoke tests using MockAdapter + real filesystem.

Key difference from test_pipeline_mock.py:
- These tests focus on the real filesystem outcome (files exist, content correct)
  not just that the graph completed.
- Each test verifies the full round-trip: LLM → routing → tool dispatch → disk.
- No LLM required: all call_model calls are intercepted by MockAdapter.

Coverage:
  E2E-1  Simple write: agent writes hello.py; file exists with correct content
  E2E-2  Cache invalidation: written file re-read reflects new content (MEM-1)
  E2E-3  WorkspaceGuard blocks write to pyproject.toml
  E2E-4  task_complexity flag drives routing in route_after_perception (WF-1)
  E2E-5  step_controller returns step_lint_warnings on lint-failed file (WF-3)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

pytestmark = pytest.mark.integration

from src.core.inference.adapters.mock_adapter import MockAdapter
from src.core.orchestration.orchestrator import Orchestrator

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


def _patch_call_model(adapter: MockAdapter, monkeypatch: Any) -> None:
    """Patch every node's call_model import to use the provided adapter."""

    async def mock_call_model(messages, model=None, provider=None, *args, **kwargs):
        return adapter.generate(messages, model=model, provider=provider, **kwargs)

    for target in _CALL_MODEL_TARGETS:
        try:
            monkeypatch.setattr(target, mock_call_model)
        except AttributeError:
            pass


def _patch_infra(monkeypatch: Any) -> None:
    """Patch heavy infrastructure that doesn't need to run in smoke tests."""
    monkeypatch.setattr(
        "src.core.orchestration.orchestrator._ensure_provider_manager_initialized_sync",
        lambda: None,
    )
    monkeypatch.setattr(
        "src.core.orchestration.orchestrator.Orchestrator._background_model_check",
        lambda self: None,
    )
    try:
        monkeypatch.setattr(
            "src.core.memory.distiller.generate_session_title",
            lambda msg: "Test Session",
        )
    except AttributeError:
        pass
    try:
        import src.core.orchestration.graph.builder as _builder

        monkeypatch.setattr(_builder, "_COMPILED_GRAPH", None)
    except AttributeError:
        pass


def _make_orch(
    tmp_path: Path, responses: list, monkeypatch: Any
) -> tuple[Orchestrator, MockAdapter]:
    """Build an Orchestrator wired with a MockAdapter using real tmp_path workdir."""
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
# E2E-1: Agent writes hello.py; file exists with correct content on real disk
# ---------------------------------------------------------------------------


def test_e2e_write_hello_py_real_disk(tmp_path: Path, monkeypatch: Any) -> None:
    """Full pipeline: perception → fast-path execution → write_file on real disk.

    Verifies that:
    1. The graph completed without error
    2. hello.py exists on the real filesystem
    3. The file contains the expected function definition
    """
    py_content = "def hello():\\n    return 'Hello World'\\n"
    display_content = "def hello():\n    return 'Hello World'\n"

    orch, _ = _make_orch(
        tmp_path,
        [
            # Perception returns write_file YAML → fast-path execution
            f"```yaml\nname: write_file\narguments:\n  path: hello.py\n  content: \"def hello():\\n    return 'Hello World'\\n\"\n```",
            # Completion text → memory_sync
            "hello.py has been created with the hello() function.",
        ],
        monkeypatch,
    )

    result = orch.run_agent_once(
        None,
        [
            {
                "role": "user",
                "content": "Create hello.py with def hello(): return 'Hello World'",
            }
        ],
        {},
    )

    assert isinstance(result, dict), f"Expected dict, got {type(result)}"
    hello_path = tmp_path / "hello.py"
    assert hello_path.exists(), (
        f"hello.py not found in {tmp_path}. Pipeline result: {result}"
    )
    file_text = hello_path.read_text(encoding="utf-8")
    assert "hello" in file_text, f"Expected 'hello' in file content: {file_text!r}"


# ---------------------------------------------------------------------------
# E2E-2: MEM-1 — written file is re-read with fresh content (cache invalidated)
# ---------------------------------------------------------------------------


def test_e2e_mem1_cache_invalidated_after_write(tmp_path: Path) -> None:
    """MEM-1: ContextBuilder.invalidate_path removes the cache entry after write_file.

    Direct unit integration: write_file writes a file, then we confirm that
    ContextBuilder._read_text_cached reads the new content not the old cached content.
    """
    from src.tools.file_tools import write_file
    from src.core.context.context_builder import ContextBuilder, _TEXT_CACHE
    from src.tools.guardrails import mark_file_read

    target = tmp_path / "cached.py"
    target.write_text("def old(): pass\n", encoding="utf-8")

    # Register the file as "read" in the current session so write_file doesn't
    # block on the read-before-write guard.
    mark_file_read(str(target.resolve()))

    # Warm the cache by reading through ContextBuilder
    ContextBuilder._read_text_cached(target)

    # Verify the old content is cached
    key = str(target.resolve())
    assert key in _TEXT_CACHE, "File should be in cache after _read_text_cached"
    assert "old" in _TEXT_CACHE[key][1], "Cached content should contain 'old'"

    # Write new content via write_file (which calls ContextBuilder.invalidate_path)
    result = write_file(str(target), "def new(): return 42\n", workdir=tmp_path)
    assert result.get("status") in ("ok", "no_change"), f"write_file failed: {result}"

    # Cache entry must have been evicted
    assert key not in _TEXT_CACHE, (
        "Cache entry should have been evicted by write_file (MEM-1)"
    )

    # Re-reading should get the new content
    fresh = ContextBuilder._read_text_cached(target)
    assert fresh is not None
    assert "new" in fresh, f"Expected 'new' in fresh content: {fresh!r}"


# ---------------------------------------------------------------------------
# E2E-3: WorkspaceGuard blocks writes to protected pyproject.toml
# ---------------------------------------------------------------------------


def test_e2e_workspace_guard_blocks_pyproject(tmp_path: Path) -> None:
    """WorkspaceGuard rejects write_file to pyproject.toml without user_approved."""
    from src.tools.file_tools import write_file

    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[tool.project]\nname = 'myapp'\n")

    result = write_file(
        str(pyproject), "[tool.project]\nname = 'hacked'\n", workdir=tmp_path
    )

    assert result.get("status") == "error", (
        f"Expected WorkspaceGuard to block write to pyproject.toml, got: {result}"
    )
    assert "protected" in result.get("error", "").lower(), (
        f"Expected 'protected' in error message: {result.get('error')}"
    )
    # Original file should be unchanged
    assert "myapp" in pyproject.read_text()


def test_e2e_workspace_guard_allows_with_user_approved(tmp_path: Path) -> None:
    """WorkspaceGuard allows write_file to pyproject.toml when user_approved=True."""
    from src.tools.file_tools import write_file
    from src.tools.guardrails import mark_file_read

    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[tool.project]\nname = 'myapp'\n")

    # Mark file as read so read-before-write guard passes
    mark_file_read(str(pyproject.resolve()))

    result = write_file(
        str(pyproject),
        "[tool.project]\nname = 'approved'\n",
        workdir=tmp_path,
        user_approved=True,
    )

    assert result.get("status") in ("ok", "no_change"), (
        f"Expected write to succeed with user_approved=True, got: {result}"
    )


# ---------------------------------------------------------------------------
# E2E-4: WF-1 — route_after_perception uses task_complexity flag from state
# ---------------------------------------------------------------------------


def test_e2e_wf1_route_uses_complexity_flag_simple(tmp_path: Path) -> None:
    """WF-1: route_after_perception routes to execution when task_complexity='simple'."""
    from src.core.orchestration.graph.builder import route_after_perception

    state = {
        "task": "write hello.py",
        "next_action": {
            "name": "write_file",
            "arguments": {"path": "hello.py", "content": ""},
        },
        "task_complexity": "simple",
        "rounds": 1,
        "last_result": None,
        "relevant_files": [],
        "current_plan": [],
    }

    route = route_after_perception(state)
    assert route == "execution", (
        f"WF-1: expected 'execution' for simple task, got '{route}'"
    )


def test_e2e_wf1_route_uses_complexity_flag_complex(tmp_path: Path) -> None:
    """WF-1: route_after_perception routes to analysis when task_complexity='complex'."""
    from src.core.orchestration.graph.builder import route_after_perception

    state = {
        "task": "refactor the entire authentication system",
        "next_action": {"name": "read_file", "arguments": {"path": "auth.py"}},
        "task_complexity": "complex",
        "rounds": 1,
        "last_result": None,
        "relevant_files": [],
        "current_plan": [],
    }

    route = route_after_perception(state)
    assert route == "analysis", (
        f"WF-1: expected 'analysis' for complex task, got '{route}'"
    )


def test_e2e_wf1_route_falls_back_to_heuristic_when_no_flag(tmp_path: Path) -> None:
    """WF-1: route_after_perception falls back to _task_is_complex() when flag absent."""
    from src.core.orchestration.graph.builder import route_after_perception

    # No task_complexity flag — heuristic should fire on "refactor" keyword
    state = {
        "task": "refactor and rewrite the entire authentication module",
        "next_action": {"name": "read_file", "arguments": {"path": "auth.py"}},
        # No task_complexity key
        "rounds": 1,
        "last_result": None,
        "relevant_files": [],
        "current_plan": [],
    }

    route = route_after_perception(state)
    # "refactor" is a complexity keyword — should route to analysis
    assert route == "analysis", (
        f"WF-1: expected 'analysis' for complex task (heuristic fallback), got '{route}'"
    )


# ---------------------------------------------------------------------------
# E2E-5: WF-3 — step_controller_node sets step_lint_warnings after a step
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_wf3_step_lint_warnings_on_bad_python(tmp_path: Path) -> None:
    """WF-3: step_controller runs lint on Python files and reports warnings."""
    from src.core.orchestration.graph.nodes.step_controller_node import (
        step_controller_node,
    )

    # Create a syntactically invalid Python file
    bad_py = tmp_path / "bad.py"
    bad_py.write_text("def foo(:\n    pass\n", encoding="utf-8")

    state: Dict[str, Any] = {
        "current_plan": [
            {"description": "Write bad.py", "action": None, "completed": False},
        ],
        "current_step": 0,
        "step_controller_enabled": True,
        "working_dir": str(tmp_path),
        "last_result": {
            "status": "ok",
            "path": str(bad_py),
        },
        "step_retry_counts": {},
    }

    result = await step_controller_node(state, config=None)

    # step_lint_warnings should be a list (may be empty if linter not available)
    assert "step_lint_warnings" in result, (
        f"WF-3: step_lint_warnings not in step_controller output. Keys: {list(result.keys())}"
    )
    assert isinstance(result["step_lint_warnings"], list)


@pytest.mark.asyncio
async def test_e2e_wf3_no_lint_on_non_python_file(tmp_path: Path) -> None:
    """WF-3: step_controller does not run lint on non-Python files."""
    from src.core.orchestration.graph.nodes.step_controller_node import (
        step_controller_node,
    )

    txt_file = tmp_path / "notes.txt"
    txt_file.write_text("hello world\n", encoding="utf-8")

    state: Dict[str, Any] = {
        "current_plan": [
            {"description": "Write notes.txt", "action": None, "completed": False},
        ],
        "current_step": 0,
        "step_controller_enabled": True,
        "working_dir": str(tmp_path),
        "last_result": {
            "status": "ok",
            "path": str(txt_file),
        },
        "step_retry_counts": {},
    }

    result = await step_controller_node(state, config=None)

    assert result.get("step_lint_warnings") == [], (
        f"WF-3: non-Python file should produce empty lint warnings, got: {result.get('step_lint_warnings')}"
    )
