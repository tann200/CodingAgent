"""Tests for src.core.orchestration.project_settings (CP-13 / CP-8)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from src.core.orchestration.project_settings import (
    ProjectSettings,
    _deep_merge,
    _parse,
    load_project_settings,
)


# ---------------------------------------------------------------------------
# _deep_merge
# ---------------------------------------------------------------------------


class TestDeepMerge:
    def test_scalar_override(self) -> None:
        base = {"a": 1, "b": 2}
        _deep_merge(base, {"b": 99, "c": 3})
        assert base == {"a": 1, "b": 99, "c": 3}

    def test_nested_dict_merged(self) -> None:
        base = {"hooks": {"PreToolUse": ["x"]}}
        _deep_merge(base, {"hooks": {"PostToolUse": ["y"]}})
        assert base["hooks"] == {"PreToolUse": ["x"], "PostToolUse": ["y"]}

    def test_nested_scalar_wins(self) -> None:
        base = {"hooks": {"PreToolUse": ["x"]}}
        _deep_merge(base, {"hooks": {"PreToolUse": ["z"]}})
        assert base["hooks"]["PreToolUse"] == ["z"]

    def test_empty_override_no_change(self) -> None:
        base = {"a": 1}
        _deep_merge(base, {})
        assert base == {"a": 1}

    def test_list_replaced_not_extended(self) -> None:
        base = {"items": [1, 2, 3]}
        _deep_merge(base, {"items": [4, 5]})
        assert base["items"] == [4, 5]


# ---------------------------------------------------------------------------
# _parse
# ---------------------------------------------------------------------------


class TestParse:
    def test_empty_dict(self) -> None:
        s = _parse({})
        assert s.model is None
        assert s.permission_mode is None
        assert s.max_turns is None
        assert s.hooks == {}
        assert s.mcp_servers == {}

    def test_model_parsed(self) -> None:
        s = _parse({"model": "gpt-4o"})
        assert s.model == "gpt-4o"

    def test_model_stripped(self) -> None:
        s = _parse({"model": "  claude-3-5-sonnet  "})
        assert s.model == "claude-3-5-sonnet"

    def test_model_empty_string_ignored(self) -> None:
        s = _parse({"model": "   "})
        assert s.model is None

    def test_permission_mode_camel(self) -> None:
        s = _parse({"permissionMode": "workspaceWrite"})
        assert s.permission_mode == "workspace_write"

    def test_permission_mode_snake(self) -> None:
        s = _parse({"permissionMode": "read_only"})
        assert s.permission_mode == "read_only"

    def test_permission_mode_danger(self) -> None:
        s = _parse({"permissionMode": "dangerFullAccess"})
        assert s.permission_mode == "danger"

    def test_permission_mode_prompt(self) -> None:
        s = _parse({"permissionMode": "prompt"})
        assert s.permission_mode == "prompt"

    def test_permission_mode_allow(self) -> None:
        s = _parse({"permissionMode": "allow"})
        assert s.permission_mode == "allow"

    def test_permission_mode_unknown_ignored(self) -> None:
        s = _parse({"permissionMode": "super_unsafe_mode"})
        assert s.permission_mode is None

    def test_max_turns_int(self) -> None:
        s = _parse({"maxTurns": 25})
        assert s.max_turns == 25

    def test_max_turns_string_coerced(self) -> None:
        s = _parse({"maxTurns": "100"})
        assert s.max_turns == 100

    def test_max_turns_invalid_ignored(self) -> None:
        s = _parse({"maxTurns": "not_a_number"})
        assert s.max_turns is None

    def test_max_turns_snake_case(self) -> None:
        s = _parse({"max_turns": 42})
        assert s.max_turns == 42

    def test_hooks_parsed(self) -> None:
        raw = {"hooks": {"PreToolUse": ["./pre.sh"], "PostToolUse": ["./post.sh"]}}
        s = _parse(raw)
        assert s.hooks == {"PreToolUse": ["./pre.sh"], "PostToolUse": ["./post.sh"]}

    def test_hooks_partial(self) -> None:
        s = _parse({"hooks": {"PreToolUse": ["./x.sh"]}})
        assert "PreToolUse" in s.hooks
        assert "PostToolUse" not in s.hooks

    def test_hooks_non_dict_ignored(self) -> None:
        s = _parse({"hooks": "invalid"})
        assert s.hooks == {}

    def test_mcp_servers_parsed(self) -> None:
        data = {"mcpServers": {"myserver": {"command": "uvx", "args": ["mcp-server"]}}}
        s = _parse(data)
        assert "myserver" in s.mcp_servers

    def test_raw_preserved(self) -> None:
        raw = {"model": "gpt-4", "extra_key": 42}
        s = _parse(raw)
        assert s.raw["extra_key"] == 42


# ---------------------------------------------------------------------------
# load_project_settings (integration with filesystem)
# ---------------------------------------------------------------------------


class TestLoadProjectSettings:
    def test_no_files_returns_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            s = load_project_settings(tmp)
            assert s.model is None
            assert s.permission_mode is None
            assert s.max_turns is None

    def test_settings_json_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = Path(tmp) / ".agent"
            agent_dir.mkdir()
            (agent_dir / "settings.json").write_text(
                json.dumps({"model": "claude-opus-4", "maxTurns": 30}),
                encoding="utf-8",
            )
            s = load_project_settings(tmp)
            assert s.model == "claude-opus-4"
            assert s.max_turns == 30

    def test_settings_local_json_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = Path(tmp) / ".agent"
            agent_dir.mkdir()
            (agent_dir / "settings.json").write_text(
                json.dumps({"model": "gpt-4o", "permissionMode": "read_only"}),
                encoding="utf-8",
            )
            (agent_dir / "settings.local.json").write_text(
                json.dumps({"model": "gpt-4o-mini"}),
                encoding="utf-8",
            )
            s = load_project_settings(tmp)
            assert s.model == "gpt-4o-mini"
            # permission_mode from base file is preserved
            assert s.permission_mode == "read_only"

    def test_local_overrides_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = Path(tmp) / ".agent"
            agent_dir.mkdir()
            (agent_dir / "settings.json").write_text(
                json.dumps({"hooks": {"PreToolUse": ["./base.sh"]}}),
                encoding="utf-8",
            )
            (agent_dir / "settings.local.json").write_text(
                json.dumps({"hooks": {"PreToolUse": ["./local.sh"]}}),
                encoding="utf-8",
            )
            s = load_project_settings(tmp)
            assert s.hooks["PreToolUse"] == ["./local.sh"]

    def test_missing_agent_dir_returns_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            s = load_project_settings(tmp)
            assert isinstance(s, ProjectSettings)
            assert s.model is None

    def test_malformed_json_silently_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = Path(tmp) / ".agent"
            agent_dir.mkdir()
            (agent_dir / "settings.json").write_text(
                "{not valid json}", encoding="utf-8"
            )
            s = load_project_settings(tmp)
            assert s.model is None  # no crash

    def test_non_dict_json_silently_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = Path(tmp) / ".agent"
            agent_dir.mkdir()
            (agent_dir / "settings.json").write_text("[1, 2, 3]", encoding="utf-8")
            s = load_project_settings(tmp)
            assert s.model is None

    def test_defaults_to_cwd(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monkeypatch.chdir(tmp)
            agent_dir = Path(tmp) / ".agent"
            agent_dir.mkdir()
            (agent_dir / "settings.json").write_text(
                json.dumps({"model": "from-cwd"}), encoding="utf-8"
            )
            s = load_project_settings(None)
            assert s.model == "from-cwd"

    def test_permission_mode_from_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = Path(tmp) / ".agent"
            agent_dir.mkdir()
            (agent_dir / "settings.json").write_text(
                json.dumps({"permissionMode": "workspaceWrite"}),
                encoding="utf-8",
            )
            s = load_project_settings(tmp)
            assert s.permission_mode == "workspace_write"


# ---------------------------------------------------------------------------
# budgetCeiling (CAP-1 / ROB-3)
# ---------------------------------------------------------------------------


class TestBudgetCeiling:
    def test_budget_ceiling_camel_case(self) -> None:
        s = _parse({"budgetCeiling": 1.5})
        assert s.budget_ceiling_usd == pytest.approx(1.5)

    def test_budget_ceiling_snake_case(self) -> None:
        s = _parse({"budget_ceiling_usd": 2.0})
        assert s.budget_ceiling_usd == pytest.approx(2.0)

    def test_budget_ceiling_integer_value(self) -> None:
        s = _parse({"budgetCeiling": 5})
        assert s.budget_ceiling_usd == pytest.approx(5.0)

    def test_budget_ceiling_string_float(self) -> None:
        s = _parse({"budgetCeiling": "0.50"})
        assert s.budget_ceiling_usd == pytest.approx(0.50)

    def test_budget_ceiling_invalid_string_ignored(self) -> None:
        s = _parse({"budgetCeiling": "not-a-number"})
        assert s.budget_ceiling_usd is None

    def test_budget_ceiling_missing_is_none(self) -> None:
        s = _parse({})
        assert s.budget_ceiling_usd is None

    def test_budget_ceiling_from_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = Path(tmp) / ".agent"
            agent_dir.mkdir()
            (agent_dir / "settings.json").write_text(
                json.dumps({"budgetCeiling": 0.25}),
                encoding="utf-8",
            )
            s = load_project_settings(tmp)
            assert s.budget_ceiling_usd == pytest.approx(0.25)

    def test_budget_ceiling_local_override_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = Path(tmp) / ".agent"
            agent_dir.mkdir()
            (agent_dir / "settings.json").write_text(
                json.dumps({"budgetCeiling": 1.0}), encoding="utf-8"
            )
            (agent_dir / "settings.local.json").write_text(
                json.dumps({"budgetCeiling": 5.0}), encoding="utf-8"
            )
            s = load_project_settings(tmp)
            assert s.budget_ceiling_usd == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# enableSemanticEvaluation (PERF-2)
# ---------------------------------------------------------------------------


class TestEnableSemanticEvaluation:
    def test_default_is_true(self) -> None:
        s = _parse({})
        assert s.enable_semantic_evaluation is True

    def test_explicit_true_bool(self) -> None:
        s = _parse({"enableSemanticEvaluation": True})
        assert s.enable_semantic_evaluation is True

    def test_explicit_false_bool(self) -> None:
        s = _parse({"enableSemanticEvaluation": False})
        assert s.enable_semantic_evaluation is False

    def test_false_string(self) -> None:
        s = _parse({"enableSemanticEvaluation": "false"})
        assert s.enable_semantic_evaluation is False

    def test_zero_string(self) -> None:
        s = _parse({"enableSemanticEvaluation": "0"})
        assert s.enable_semantic_evaluation is False

    def test_true_string(self) -> None:
        s = _parse({"enableSemanticEvaluation": "true"})
        assert s.enable_semantic_evaluation is True

    def test_snake_case_key(self) -> None:
        s = _parse({"enable_semantic_evaluation": False})
        assert s.enable_semantic_evaluation is False

    def test_from_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = Path(tmp) / ".agent"
            agent_dir.mkdir()
            (agent_dir / "settings.json").write_text(
                json.dumps({"enableSemanticEvaluation": False}), encoding="utf-8"
            )
            s = load_project_settings(tmp)
            assert s.enable_semantic_evaluation is False


# ---------------------------------------------------------------------------
# maxLlmWaitSeconds (WF-5)
# ---------------------------------------------------------------------------


class TestMaxLlmWaitSeconds:
    def test_default_is_120(self) -> None:
        s = _parse({})
        assert s.max_llm_wait_seconds == 120

    def test_explicit_int(self) -> None:
        s = _parse({"maxLlmWaitSeconds": 60})
        assert s.max_llm_wait_seconds == 60

    def test_zero_disables(self) -> None:
        s = _parse({"maxLlmWaitSeconds": 0})
        assert s.max_llm_wait_seconds == 0

    def test_string_coerced(self) -> None:
        s = _parse({"maxLlmWaitSeconds": "30"})
        assert s.max_llm_wait_seconds == 30

    def test_invalid_ignored_keeps_default(self) -> None:
        s = _parse({"maxLlmWaitSeconds": "not-a-number"})
        assert s.max_llm_wait_seconds == 120

    def test_snake_case_key(self) -> None:
        s = _parse({"max_llm_wait_seconds": 45})
        assert s.max_llm_wait_seconds == 45

    def test_from_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = Path(tmp) / ".agent"
            agent_dir.mkdir()
            (agent_dir / "settings.json").write_text(
                json.dumps({"maxLlmWaitSeconds": 300}), encoding="utf-8"
            )
            s = load_project_settings(tmp)
            assert s.max_llm_wait_seconds == 300
