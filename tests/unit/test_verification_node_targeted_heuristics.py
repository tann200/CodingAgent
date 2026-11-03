import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
from src.core.orchestration.graph.nodes.verification_node import verification_node


@pytest.mark.asyncio
async def test_verification_node_skips_js_tests_for_non_code_edits(tmp_path):
    # Setup mock JS project with package.json
    pkg_json = tmp_path / "package.json"
    pkg_json.write_text('{"name": "test-project"}', encoding="utf-8")
    css_file = tmp_path / "styles.css"
    css_file.write_text("body { color: red; }", encoding="utf-8")

    state = {
        "modified_files": [str(css_file)],
        "working_dir": str(tmp_path),
        "current_plan": [{"description": "edit styles"}],
        "current_step": 0,
        "runtime": {"policy": {"verification_mode": "full"}},
    }

    with patch("src.core.orchestration.graph.nodes.verification_node.verification_tools") as mock_tools:
        result = await verification_node(state, {})

        # js_tests should NOT be called for css edit
        mock_tools.run_js_tests.assert_not_called()
        assert result.get("verification_passed") is True
        vr = result.get("verification_result", {})
        assert vr.get("status") == "skipped_non_code"


@pytest.mark.asyncio
async def test_verification_node_runs_js_tests_for_code_edits(tmp_path):
    pkg_json = tmp_path / "package.json"
    pkg_json.write_text('{"name": "test-project"}', encoding="utf-8")
    ts_file = tmp_path / "index.ts"
    ts_file.write_text("export const x = 1;", encoding="utf-8")

    state = {
        "modified_files": [str(ts_file)],
        "working_dir": str(tmp_path),
        "current_plan": [{"description": "edit code"}],
        "current_step": 0,
        "runtime": {"policy": {"verification_mode": "full"}},
    }

    with patch("src.core.orchestration.graph.nodes.verification_node.verification_tools") as mock_tools:
        mock_tools.run_js_tests.return_value = {"status": "ok", "tests": 5}
        mock_tools.run_ts_check.return_value = {"status": "ok"}
        mock_tools.run_eslint.return_value = {"status": "ok"}

        result = await verification_node(state, {})

        # js_tests SHOULD be called for ts code edit
        mock_tools.run_js_tests.assert_called_once()
        assert result.get("verification_passed") is True
