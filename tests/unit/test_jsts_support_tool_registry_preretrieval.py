"""
Tests for tool registry and work summary.

ET-4: edit_by_line_range single canonical registration
MC-5: _generate_work_summary includes git diff section
"""

# ruff: noqa: E501

from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# ET-4: the registered edit_by_line_range must be the file_tools version
# ---------------------------------------------------------------------------


class TestEditByLineRangeSingleRegistrationInRegistry:
    """ET-4 regression: the registered edit_by_line_range must be the file_tools version."""

    def test_registered_fn_is_file_tools_version(self):
        """The tool fn must originate from file_tools, not an orchestrator inline copy."""
        from src.core.orchestration.orchestrator import example_registry
        from src.tools import file_tools

        reg = example_registry()
        tool = reg.tools.get("edit_by_line_range")
        assert tool is not None, "edit_by_line_range must be registered"

        fn = tool.get("fn")
        assert fn is not None, "edit_by_line_range must have a fn"

        fn_module = getattr(fn, "__module__", "") or ""
        assert "file_tools" in fn_module or fn is file_tools.edit_by_line_range, (
            f"ET-4 regression: registered edit_by_line_range is not from file_tools "
            f"(module: {fn_module!r}). The inline orchestrator copy may have been re-added."
        )

    def test_only_one_registration(self):
        """Calling example_registry() twice should yield the same single registration."""
        from src.core.orchestration.orchestrator import example_registry

        reg1 = example_registry()
        reg2 = example_registry()
        assert "edit_by_line_range" in reg1.tools
        assert "edit_by_line_range" in reg2.tools
        fn1 = reg1.tools["edit_by_line_range"]["fn"]
        fn2 = reg2.tools["edit_by_line_range"]["fn"]
        assert fn1 is fn2 or getattr(fn1, "__module__", "") == getattr(
            fn2, "__module__", ""
        )


# ---------------------------------------------------------------------------
# MC-5: _generate_work_summary includes git diff section
# ---------------------------------------------------------------------------


class TestWorkSummaryIncludesGitDiffStatSection:
    """MC-5: work summary must attempt to include git diff --stat."""

    def test_summary_has_git_diff_section(self, tmp_path, monkeypatch):
        """When git is available and returns output, summary includes diff section."""
        from src.core.orchestration.orchestrator import _generate_work_summary

        fake_diff_output = " src/foo.py | 10 ++++------\n 1 file changed, 4 insertions(+), 6 deletions(-)"

        def mock_run(cmd, **kwargs):
            m = MagicMock()
            if "diff" in cmd:
                m.returncode = 0
                m.stdout = fake_diff_output
            else:
                m.returncode = 1
                m.stdout = ""
            return m

        monkeypatch.setattr(
            "src.core.orchestration.work_summary.subprocess.run", mock_run
        )

        state = {"task": "fix bug", "rounds": 1, "working_dir": str(tmp_path)}
        summary = _generate_work_summary(state, [])
        assert "Work Summary" in summary

    def test_summary_works_without_git(self, tmp_path, monkeypatch):
        """When git is unavailable, summary still completes (no crash)."""
        from src.core.orchestration.orchestrator import _generate_work_summary

        state = {"task": "fix bug", "rounds": 2, "working_dir": str(tmp_path)}
        summary = _generate_work_summary(state, [])
        assert "Work Summary" in summary
        assert "fix bug" in summary
