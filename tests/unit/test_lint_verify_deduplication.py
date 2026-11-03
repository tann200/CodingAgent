import pytest
from unittest.mock import MagicMock, patch
from src.tools._lint_verify import (
    mark_content_prevalidated,
    is_content_prevalidated,
    verify_candidate_content,
)


def test_mark_and_check_prevalidated(tmp_path):
    target = tmp_path / "foo.py"
    content = "x = 1\n"
    assert not is_content_prevalidated(target, content)

    mark_content_prevalidated(target, content)
    assert is_content_prevalidated(target, content)

    # Different content returns False
    assert not is_content_prevalidated(target, "x = 2\n")


def test_verify_candidate_content_fast_paths_when_prevalidated(tmp_path):
    target = tmp_path / "new_file.py"
    content = "def test():\n    return True\n"

    mark_content_prevalidated(target, content)

    with patch("src.tools.lint_dispatch.quick_lint") as mock_lint:
        # For a new file whose content is pre-validated, quick_lint should NOT be called at all
        res = verify_candidate_content(target, content, tmp_path)
        assert res is None
        mock_lint.assert_not_called()


def test_verify_candidate_content_runs_full_check_when_not_prevalidated(tmp_path):
    target = tmp_path / "existing.py"
    target.write_text("x = 1\n", encoding="utf-8")
    content = "x = 2\n"

    with patch("src.tools.lint_dispatch.quick_lint") as mock_lint:
        mock_lint.return_value = {"lint_errors": []}
        res = verify_candidate_content(target, content, tmp_path)
        assert res is None
        # Must run baseline + candidate check
        assert mock_lint.call_count >= 1
