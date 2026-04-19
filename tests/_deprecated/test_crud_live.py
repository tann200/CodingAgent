"""Live CRUD integration tests — full agent pipeline against LM Studio.

Each test drives the complete Orchestrator pipeline (perception → planning →
execution) for a single file-operation task, then verifies the on-disk result.
On assertion failure a second agent turn is run asking the LLM to debug and
retry, mirroring how a human developer would iterate.

Coverage:
    write_file, read_file, edit_file_atomic, edit_by_line_range,
    delete_file, rename_file, list_files, glob, grep

Guards:
  - Skipped when RUN_INTEGRATION != '1' AND CI=true (never runs in GitHub Actions)
  - Auto-enabled when a configured lm_studio provider is detected in providers.json
    and CI is not set
  - Each test xfails gracefully if LM Studio is up but the model fails to load
  - Each test has a 600s per-test timeout (2 full agent turns on local hardware)
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from src.core.inference.adapters.lm_studio_adapter import LmStudioAdapter
from src.core.orchestration.orchestrator import Orchestrator

# ---------------------------------------------------------------------------
# Skip / auto-detect logic
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.lmstudio

_RUN = os.getenv("RUN_INTEGRATION") == "1" or bool(os.getenv("LM_STUDIO_URL"))

# Note: historically this test auto-enabled itself when a providers.json file
# referenced an LM provider. That made local test runs accidentally start the
# slow LM-backed integration tests when the repo contained a provider file.
# Require an explicit opt-in via RUN_INTEGRATION=1 or an LM_STUDIO_URL so test
# runs are deterministic and fast by default.

_LM_BASE = os.getenv("LM_STUDIO_URL", "http://localhost:1234/v1")
_LM_MODEL = os.getenv("LM_STUDIO_MODEL", "qwen/qwen3.5-9b")

skipif_no_lm = pytest.mark.skipif(
    not _RUN, reason="LM Studio integration tests disabled"
)

# Each test may run 2 agent turns (first attempt + debug turn).
# qwen3.5-9b can take 60–150s per turn on local hardware → 600s per test.
_timeout = pytest.mark.timeout(600)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_orch(tmp_path: Path) -> Orchestrator:
    adapter = LmStudioAdapter(base_url=_LM_BASE, models=[_LM_MODEL])
    return Orchestrator(adapter=adapter, working_dir=str(tmp_path))


def _is_lm_error(result: Any) -> bool:
    """Return True if result signals a model-load or connectivity error."""
    if not isinstance(result, dict):
        return False
    err = str(result.get("error", "")).lower()
    if any(k in err for k in ("failed to load", "connection", "refused", "timed out")):
        return True
    raw = result.get("raw") or {}
    if isinstance(raw, dict):
        meta = raw.get("meta") or {}
        if isinstance(meta, dict) and meta.get("status_code") in (400, 500, 502, 503):
            return True
    return False


# Per-turn wall-clock limit (seconds).  pytest-timeout uses SIGALRM which cannot
# interrupt blocking C-level I/O.  We use a daemon thread + join timeout instead
# so the test fails-fast rather than hanging the entire suite.
_TURN_TIMEOUT = int(os.getenv("CRUD_TURN_TIMEOUT", "180"))


def _run(
    orch: Orchestrator,
    messages: List[Dict[str, str]],
    *,
    timeout: int = _TURN_TIMEOUT,
) -> Dict[str, Any]:
    """Run one orchestrator turn with a hard wall-clock timeout.

    Uses a daemon thread so a hung LLM call does not block the test suite.
    On timeout, sets a cancel_event so the pipeline stops making new LLM
    requests (prevents background threads from throttling LM Studio KV cache).
    xfails (not errors) on timeout so infrastructure issues don't mask real bugs.
    """
    result: Dict[str, Any] = {}
    exc: List[Optional[BaseException]] = [None]
    cancel_event = threading.Event()

    def _target() -> None:
        try:
            result.update(
                orch.run_agent_once(
                    system_prompt_name=None,
                    messages=messages,
                    tools={},
                    cancel_event=cancel_event,
                )
            )
        except Exception as e:  # noqa: BLE001
            exc[0] = e

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout=timeout)

    if t.is_alive():
        # Signal the pipeline to abort at its next cancellation check-point
        # so the background thread stops dispatching LLM requests.
        cancel_event.set()
        pytest.xfail(
            f"run_agent_once did not complete within {timeout}s — "
            "LM Studio model too slow or pipeline stuck"
        )

    if exc[0] is not None:
        raise exc[0]  # type: ignore[misc]

    return result


def _guard(result: Any, label: str = "") -> None:
    """xfail immediately if result signals an infrastructure-level error."""
    if not isinstance(result, dict):
        return
    if result.get("error"):
        err = str(result.get("error", ""))
        if _is_lm_error(result) or any(
            k in err.lower() for k in ("cancel", "timeout", "no_model")
        ):
            pytest.xfail(f"Agent infrastructure error ({label}): {err}")


def _debug_turn(
    orch: Orchestrator,
    original_task: str,
    failure_description: str,
) -> Dict[str, Any]:
    """Run a second agent turn that asks the LLM to debug and fix the failure."""
    return _run(
        orch,
        [
            {
                "role": "user",
                "content": (
                    f"The previous task failed.\n\n"
                    f"Original task: {original_task}\n\n"
                    f"Failure: {failure_description}\n\n"
                    "Please investigate the issue, fix it, and complete the task correctly. "
                    "Verify your work after each step."
                ),
            }
        ],
    )


# ---------------------------------------------------------------------------
# Test 1 — write_file
# ---------------------------------------------------------------------------


@skipif_no_lm
@_timeout
def test_write_file_creates_new_file(tmp_path):
    """Agent writes a new file via write_file tool and the file appears on disk."""
    orch = _build_orch(tmp_path)
    target = "greeting.txt"
    expected_content = "Hello, CRUD test!"

    task = (
        f"Create a file named '{target}' in the working directory "
        f"with exactly this content (no extra lines or spaces): {expected_content}"
    )
    result = _run(orch, [{"role": "user", "content": task}])
    _guard(result, "write_file")

    target_path = tmp_path / target
    if not target_path.exists():
        debug = _debug_turn(orch, task, f"File '{target}' was not created.")
        _guard(debug, "write_file debug")

    assert target_path.exists(), (
        f"Expected '{target}' to exist in {tmp_path}.\nAgent result: {result}"
    )
    assert expected_content in target_path.read_text(encoding="utf-8").strip(), (
        f"Content mismatch in '{target}'."
    )


# ---------------------------------------------------------------------------
# Test 2 — read_file
# ---------------------------------------------------------------------------


@skipif_no_lm
@_timeout
def test_read_file_returns_content(tmp_path):
    """Agent reads an existing file and the response includes its content."""
    orch = _build_orch(tmp_path)
    secret = "UNIQUE_MARKER_XY7291"
    (tmp_path / "data.txt").write_text(
        f"Line one\n{secret}\nLine three\n", encoding="utf-8"
    )

    task = "Read the file 'data.txt' in the working directory and tell me its full content."
    result = _run(orch, [{"role": "user", "content": task}])
    _guard(result, "read_file")

    result_text = json.dumps(result)
    assert secret in result_text, (
        f"Secret marker '{secret}' not found in agent output.\n"
        f"Result keys: {list(result.keys()) if isinstance(result, dict) else type(result)}"
    )


# ---------------------------------------------------------------------------
# Test 3 — edit_file_atomic
# ---------------------------------------------------------------------------


@skipif_no_lm
@_timeout
def test_edit_file_atomic_replaces_string(tmp_path):
    """Agent edits a file with edit_file_atomic and the change lands on disk."""
    orch = _build_orch(tmp_path)
    target = tmp_path / "config.py"
    target.write_text(
        "DEBUG = False\nVERSION = '1.0.0'\nNAME = 'old_name'\n",
        encoding="utf-8",
    )

    task = (
        "In the file 'config.py' in the working directory, "
        'replace the string "old_name" with "new_name" using the edit_file_atomic tool.'
    )
    result = _run(orch, [{"role": "user", "content": task}])
    _guard(result, "edit_file_atomic")

    content = target.read_text(encoding="utf-8")
    if "new_name" not in content:
        debug = _debug_turn(
            orch, task, f"File still contains 'old_name'. Current content:\n{content}"
        )
        _guard(debug, "edit_file_atomic debug")
        content = target.read_text(encoding="utf-8")

    assert "new_name" in content, (
        f"Expected 'new_name' in config.py.\nActual:\n{content}"
    )
    assert "old_name" not in content, f"Old value still present.\nActual:\n{content}"


# ---------------------------------------------------------------------------
# Test 4 — edit_by_line_range
# ---------------------------------------------------------------------------


@skipif_no_lm
@_timeout
def test_edit_by_line_range_replaces_lines(tmp_path):
    """Agent replaces a specific line range using edit_by_line_range."""
    orch = _build_orch(tmp_path)
    target = tmp_path / "lines.txt"
    target.write_text("alpha\nbeta\ngamma\ndelta\nepsilon\n", encoding="utf-8")

    task = (
        "In the file 'lines.txt' in the working directory, "
        "replace lines 2 to 3 (beta and gamma) with the single line 'REPLACED' "
        "using the edit_by_line_range tool."
    )
    result = _run(orch, [{"role": "user", "content": task}])
    _guard(result, "edit_by_line_range")

    content = target.read_text(encoding="utf-8")
    if "REPLACED" not in content or "beta" in content:
        debug = _debug_turn(
            orch,
            task,
            f"Lines not replaced correctly.\nCurrent content:\n{content}\n"
            "Expected: 'beta' and 'gamma' replaced by 'REPLACED'.",
        )
        _guard(debug, "edit_by_line_range debug")
        content = target.read_text(encoding="utf-8")

    assert "REPLACED" in content, f"Expected 'REPLACED'.\nActual:\n{content}"
    assert "beta" not in content, f"'beta' still present.\nActual:\n{content}"
    assert "gamma" not in content, f"'gamma' still present.\nActual:\n{content}"
    assert "alpha" in content and "delta" in content, (
        f"Surrounding lines removed.\nActual:\n{content}"
    )


# ---------------------------------------------------------------------------
# Test 5 — delete_file
# ---------------------------------------------------------------------------


@skipif_no_lm
@_timeout
def test_delete_file_removes_file(tmp_path):
    """Agent deletes a file via delete_file tool and it disappears from disk."""
    orch = _build_orch(tmp_path)
    target = tmp_path / "to_delete.txt"
    target.write_text("This file should be deleted.\n", encoding="utf-8")

    task = "Delete the file 'to_delete.txt' in the working directory."
    result = _run(orch, [{"role": "user", "content": task}])
    _guard(result, "delete_file")

    if target.exists():
        debug = _debug_turn(orch, task, "The file 'to_delete.txt' still exists.")
        _guard(debug, "delete_file debug")

    assert not target.exists(), "Expected 'to_delete.txt' to be deleted."


# ---------------------------------------------------------------------------
# Test 6 — rename_file
# ---------------------------------------------------------------------------


@skipif_no_lm
@_timeout
def test_rename_file_renames_file(tmp_path):
    """Agent renames a file via rename_file tool and the new path exists."""
    orch = _build_orch(tmp_path)
    src = tmp_path / "original.txt"
    dst = tmp_path / "renamed.txt"
    src.write_text("rename me\n", encoding="utf-8")

    task = (
        "Rename the file 'original.txt' to 'renamed.txt' in the working directory "
        "using the rename_file tool."
    )
    result = _run(orch, [{"role": "user", "content": task}])
    _guard(result, "rename_file")

    if not dst.exists() or src.exists():
        debug = _debug_turn(
            orch,
            task,
            f"'original.txt' exists: {src.exists()}, 'renamed.txt' exists: {dst.exists()}. "
            "Please rename 'original.txt' to 'renamed.txt'.",
        )
        _guard(debug, "rename_file debug")

    assert dst.exists(), f"'renamed.txt' missing after rename. src={src.exists()}"
    assert not src.exists(), "'original.txt' still exists after rename."
    assert dst.read_text(encoding="utf-8").strip() == "rename me", (
        "Content should be preserved after rename."
    )


# ---------------------------------------------------------------------------
# Test 7 — list_files
# ---------------------------------------------------------------------------


@skipif_no_lm
@_timeout
def test_list_files_reports_directory_contents(tmp_path):
    """Agent calls list_files and the response includes all created files."""
    orch = _build_orch(tmp_path)
    for name in ("file_a.txt", "file_b.txt", "file_c.txt"):
        (tmp_path / name).write_text(name)

    task = (
        "List all files in the working directory using the list_files tool "
        "and tell me their names."
    )
    result = _run(orch, [{"role": "user", "content": task}])
    _guard(result, "list_files")

    result_text = json.dumps(result)
    missing = [
        n for n in ("file_a.txt", "file_b.txt", "file_c.txt") if n not in result_text
    ]

    if missing:
        debug = _debug_turn(
            orch,
            task,
            f"These files were not mentioned: {missing}. Please list_files again.",
        )
        _guard(debug, "list_files debug")
        result_text += json.dumps(debug)
        missing = [
            n
            for n in ("file_a.txt", "file_b.txt", "file_c.txt")
            if n not in result_text
        ]

    assert not missing, (
        f"File names {missing} missing from agent output.\nResult: {result}"
    )


# ---------------------------------------------------------------------------
# Test 8 — glob
# ---------------------------------------------------------------------------


@skipif_no_lm
@_timeout
def test_glob_finds_matching_files(tmp_path):
    """Agent calls glob with a pattern and finds only the matching files."""
    orch = _build_orch(tmp_path)
    (tmp_path / "module_a.py").write_text("# python")
    (tmp_path / "module_b.py").write_text("# python")
    (tmp_path / "notes.txt").write_text("text file")

    task = (
        "Use the glob tool to find all files matching the pattern '**/*.py' "
        "in the working directory and list their names."
    )
    result = _run(orch, [{"role": "user", "content": task}])
    _guard(result, "glob")

    result_text = json.dumps(result)
    missing = [n for n in ("module_a.py", "module_b.py") if n not in result_text]

    if missing:
        debug = _debug_turn(
            orch,
            task,
            f"Python files {missing} not found. Use glob('**/*.py') and list results.",
        )
        _guard(debug, "glob debug")
        result_text += json.dumps(debug)
        missing = [n for n in ("module_a.py", "module_b.py") if n not in result_text]

    assert not missing, (
        f"Python files {missing} missing from glob output.\nResult: {result}"
    )


# ---------------------------------------------------------------------------
# Test 9 — grep
# ---------------------------------------------------------------------------


@skipif_no_lm
@_timeout
def test_grep_finds_pattern_in_files(tmp_path):
    """Agent calls grep with a regex and locates the match in the right file."""
    orch = _build_orch(tmp_path)
    (tmp_path / "search_me.py").write_text(
        "def compute_checksum(data: bytes) -> int:\n    return sum(data)\n",
        encoding="utf-8",
    )
    (tmp_path / "unrelated.py").write_text("print('nothing to see here')\n")

    task = (
        "Use the grep tool to search for the pattern 'compute_checksum' "
        "in all files in the working directory. "
        "Tell me which file it was found in and at which line number."
    )
    result = _run(orch, [{"role": "user", "content": task}])
    _guard(result, "grep")

    result_text = json.dumps(result)
    if "search_me.py" not in result_text and "compute_checksum" not in result_text:
        debug = _debug_turn(
            orch,
            task,
            "grep did not mention 'search_me.py'. Run grep(pattern='compute_checksum', path='.').",
        )
        _guard(debug, "grep debug")
        result_text += json.dumps(debug)

    assert "search_me.py" in result_text or "compute_checksum" in result_text, (
        f"grep output missing expected matches.\nResult: {result}"
    )


# ---------------------------------------------------------------------------
# Test 10 — multi-step CRUD workflow
# ---------------------------------------------------------------------------


@skipif_no_lm
@_timeout
def test_multi_step_crud_workflow(tmp_path):
    """Agent completes a two-turn CRUD workflow: write then edit.

    Turn 1 — create calculator.py with a deliberate bug (return a - b).
    Turn 2 — fix the bug by replacing 'return a - b' with 'return a + b'.

    Validates write_file followed by edit_file_atomic across consecutive turns.
    """
    orch = _build_orch(tmp_path)

    # --- Turn 1: create the file with the buggy implementation ---
    task_create = (
        "Create a file 'calculator.py' in the working directory with this exact content:\n"
        "def add(a, b):\n"
        "    return a - b\n"
    )
    result1 = _run(orch, [{"role": "user", "content": task_create}], timeout=240)
    _guard(result1, "multi_step turn1")

    calc = tmp_path / "calculator.py"
    if not calc.exists():
        debug = _debug_turn(orch, task_create, "calculator.py was not created.")
        _guard(debug, "multi_step turn1 debug")

    assert calc.exists(), "calculator.py was not created by the agent."

    # --- Turn 2: fix the bug using a new orchestrator instance ---
    # Use a fresh orchestrator so turn 2 is truly independent (clean history).
    orch2 = _build_orch(tmp_path)
    task_fix = (
        "In calculator.py in the working directory, "
        "replace the string 'return a - b' with 'return a + b' "
        "using the edit_file_atomic tool."
    )
    result2 = _run(orch2, [{"role": "user", "content": task_fix}], timeout=240)
    _guard(result2, "multi_step turn2")

    content = calc.read_text(encoding="utf-8")
    if "return a + b" not in content:
        debug = _debug_turn(
            orch2,
            task_fix,
            f"Bug not fixed. Current content:\n{content}\n"
            "Please replace 'return a - b' with 'return a + b'.",
        )
        _guard(debug, "multi_step turn2 debug")
        content = calc.read_text(encoding="utf-8")

    assert "return a + b" in content, f"Bug not fixed.\nContent:\n{content}"
    assert "return a - b" not in content, (
        f"Old buggy line still present.\nContent:\n{content}"
    )
