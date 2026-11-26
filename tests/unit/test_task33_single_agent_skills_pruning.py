"""TASK-33 tests: single-agent + skills architecture, flat toolsets, and slim scout/worktree delegation."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.config.toolsets.loader import (
    get_tools_for_toolset,
    get_toolset_for_role,
    list_available_toolsets,
)
from src.core.context.agent_brain_loading import discover_skills
from src.core.orchestration.agent_brain import get_agent_brain_manager
from src.core.orchestration.graph.runtime_policy import COMPACT_RUNTIME_POLICY
from src.tools.skill_tools import load_skill
from src.tools.subagent_tools import (
    _PARENT_ORCHESTRATOR_VAR,
    delegate_task,
    list_subagent_roles,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_subagent_tools_within_400_loc_ceiling() -> None:
    lines = (_REPO_ROOT / "src" / "tools" / "subagent_tools.py").read_text(encoding="utf-8").splitlines()
    assert len(lines) <= 400, f"subagent_tools.py is {len(lines)} LOC (ceiling is 400)"


def test_only_three_canonical_flat_toolsets_remain() -> None:
    assert list_available_toolsets() == ["compact-flat", "frontier-flat", "standard-flat"]
    toolsets_dir = _REPO_ROOT / "src" / "config" / "toolsets"
    for retired in ("analysis.yaml", "coding.yaml", "debug.yaml", "planning.yaml", "review.yaml"):
        assert not (toolsets_dir / retired).exists()

    assert "delegate_task" not in get_tools_for_toolset("compact-flat")
    assert "load_skill" in get_tools_for_toolset("compact-flat")
    assert "load_skill" in get_tools_for_toolset("standard-flat")
    assert "delegate_task" in get_tools_for_toolset("frontier-flat")

    assert get_toolset_for_role("coder") == "standard-flat"
    assert get_toolset_for_role("scout") == "compact-flat"
    assert get_toolset_for_role("frontier") == "frontier-flat"


def test_delegate_task_rejected_under_compact_policy(tmp_path: Path) -> None:
    fake_orch = SimpleNamespace(
        working_dir=str(tmp_path),
        _runtime_policy=COMPACT_RUNTIME_POLICY,
    )
    tok = _PARENT_ORCHESTRATOR_VAR.set(fake_orch)
    try:
        res = delegate_task(role="scout", subtask_description="Inspect repo", mode="scout")
    finally:
        _PARENT_ORCHESTRATOR_VAR.reset(tok)
    assert res.startswith("Error:")
    assert "compact" in res.lower()


def test_delegate_task_scout_mode_enforces_lethal_trifecta_isolation(tmp_path: Path) -> None:
    # Requesting mutating tools in scout mode must fail closed
    res_write = delegate_task(
        role="scout",
        subtask_description="Inspect repo",
        working_dir=str(tmp_path),
        allowed_tools=["read_file", "write_file"],
        mode="scout",
    )
    assert res_write.startswith("Error:")
    assert "lethal-trifecta" in res_write.lower() or "mutating" in res_write.lower()

    res_bash = delegate_task(
        role="analyst",
        subtask_description="Inspect repo",
        working_dir=str(tmp_path),
        allowed_tools=["read_file", "bash"],
        mode="scout",
    )
    assert res_bash.startswith("Error:")

    # Invalid mode rejected cleanly
    res_bad_mode = delegate_task(
        role="scout",
        subtask_description="Inspect repo",
        working_dir=str(tmp_path),
        mode="multi_agent_swarm",
    )
    assert "Invalid delegation mode" in res_bad_mode


def test_delegate_task_worktree_mode_creates_and_cleans_isolated_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    seen_workdirs: list[str] = []

    async def _fake_ainvoke(state: dict, config: dict | None = None) -> dict:
        seen_workdirs.append(str(state.get("working_dir", "")))
        return {
            "task": state.get("task", ""),
            "history": [{"role": "assistant", "content": "isolated edit complete"}],
            "errors": [],
            "last_result": {"status": "ok"},
        }

    mock_graph = MagicMock()
    mock_graph.ainvoke = _fake_ainvoke

    with patch("src.tools.subagent_tools._resolve_subagent_graph", return_value=mock_graph):
        out = delegate_task(
            role="operational",
            subtask_description="Edit in isolated worktree",
            working_dir=str(repo),
            mode="worktree",
        )

    assert "isolated edit complete" in out
    assert len(seen_workdirs) == 1
    wt_dir = Path(seen_workdirs[0])
    # Must have executed inside a separate worktree path, NOT the main repo dir
    assert wt_dir.resolve() != repo.resolve()
    # Worktree must be cleaned up automatically after completion
    assert not wt_dir.exists()


def test_skills_cover_retired_role_personas_and_brain_fallback() -> None:
    catalog = discover_skills(_REPO_ROOT)
    for skill_name in ("code_review", "debug_checklist", "explore_codebase", "refactor", "security_review", "write_tests"):
        assert skill_name in catalog
        assert len(catalog[skill_name].description) >= 15

    # Kebab-case alias lookup works seamlessly
    assert load_skill("code-review")["status"] == "ok"
    assert load_skill("security-review")["status"] == "ok"
    assert load_skill("explore-codebase")["status"] == "ok"

    # Retired role prompts fall back cleanly in AgentBrainManager
    brain = get_agent_brain_manager()
    brain.reload()
    for retired_role in ("analyst", "debugger", "reviewer", "strategic"):
        compiled = brain.compile_system_prompt(retired_role)
        assert len(compiled) > 50

    roles_info = list_subagent_roles()
    assert "scout" in roles_info.get("modes", {})
    assert "worktree" in roles_info.get("modes", {})
