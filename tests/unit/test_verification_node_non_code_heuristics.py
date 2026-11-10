import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
from src.core.orchestration.graph.nodes.verification_node import verification_node


@pytest.mark.asyncio
async def test_verification_skips_full_suite_for_non_code_assets(tmp_path: Path):
    doc_file = tmp_path / "README.md"
    doc_file.write_text("# Documentation\n", encoding="utf-8")
    css_file = tmp_path / "styles.css"
    css_file.write_text("body { color: black; }\n", encoding="utf-8")
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text('{"theme": "dark"}\n', encoding="utf-8")

    state = {
        "working_dir": str(tmp_path),
        "modified_files": [str(doc_file), str(css_file), str(cfg_file)],
        "last_tool_name": "write_file",
        "last_result": {"status": "ok", "path": str(doc_file)},
        "evaluation_result": "complete",
        "next_action": None,
    }

    with patch("src.tools.verification_tools.run_tests") as mock_tests, \
         patch("src.tools.verification_tools.run_js_tests") as mock_js_tests, \
         patch("src.tools.verification_tools.run_linter") as mock_linter:

        result = await verification_node(state, config=MagicMock())

        # Neither test suite nor linter should be invoked for pure asset changes
        mock_tests.assert_not_called()
        mock_js_tests.assert_not_called()
        mock_linter.assert_not_called()

        assert result.get("verification_passed") is True
        vr = result.get("verification_result") or {}
        assert vr.get("status") == "skipped_non_code"
        assert vr.get("non_code_assets") is True
