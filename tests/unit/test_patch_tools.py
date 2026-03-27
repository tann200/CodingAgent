"""
Tests for NEW-3: generate_patch path traversal fix, and NEW-23: test coverage for patch_tools.

NEW-3: generate_patch previously joined user-supplied paths directly without calling
       safe_resolve, allowing reading of arbitrary files outside the workdir.

NEW-23: generate_patch had zero test coverage.
"""


class TestGeneratePatch:
    """Tests for patch_tools.generate_patch — normal operation and path safety."""

    def test_normal_operation_produces_valid_diff(self, tmp_path):
        """
        generate_patch on an existing file returns status='ok' and a non-empty patch.
        """
        from src.tools.patch_tools import generate_patch

        test_file = tmp_path / "hello.py"
        test_file.write_text("def hello():\n    return 'hello'\n")

        new_content = "def hello():\n    return 'world'\n"

        result = generate_patch("hello.py", new_content, tmp_path)

        assert result["status"] == "ok", f"Expected status='ok', got {result}"
        assert "patch" in result, "Result must contain a 'patch' key"
        # The patch should be a unified diff showing the change
        assert result["patch"] != "", "Patch must be non-empty for changed content"
        assert "-    return 'hello'" in result["patch"] or "hello" in result["patch"]

    def test_result_has_status_ok_and_patch_key(self, tmp_path):
        """
        NEW-23: verify the response schema — status='ok' and 'patch' key both present.
        """
        from src.tools.patch_tools import generate_patch

        test_file = tmp_path / "script.py"
        test_file.write_text("x = 1\n")

        result = generate_patch("script.py", "x = 2\n", tmp_path)

        assert result.get("status") == "ok"
        assert "patch" in result
        assert isinstance(result["patch"], str)

    def test_file_not_found_returns_error(self, tmp_path):
        """
        generate_patch on a non-existent file must return an error dict, not raise.
        """
        from src.tools.patch_tools import generate_patch

        result = generate_patch("nonexistent.py", "new content", tmp_path)

        assert result["status"] == "error"
        assert "error" in result
        assert "not found" in result["error"].lower() or "no such" in result["error"].lower()

    def test_path_traversal_rejected(self, tmp_path):
        """
        NEW-3 regression: '../../etc/passwd' must be rejected with an error,
        NOT allowed to read a file outside the working directory.

        Before the fix: path = workdir / '../../etc/passwd' escaped the sandbox.
        After the fix: safe_resolve raises PermissionError, generate_patch returns error.
        """
        from src.tools.patch_tools import generate_patch

        result = generate_patch("../../etc/passwd", "malicious content", tmp_path)

        assert result["status"] == "error", (
            "NEW-3 regression: path traversal '../../etc/passwd' must return "
            "status='error', not status='ok'"
        )
        assert "error" in result
        # The error should mention something about permission/outside
        error_msg = result["error"].lower()
        assert any(
            word in error_msg
            for word in ["outside", "permission", "blocked", "traversal", "workdir"]
        ), f"Error message should indicate path traversal block: {result['error']}"

    def test_absolute_path_outside_workdir_rejected(self, tmp_path):
        """
        NEW-3: Absolute path pointing outside workdir must also be rejected.
        """
        from src.tools.patch_tools import generate_patch

        # Try to point to a file in /tmp (outside tmp_path)
        outside_path = str(tmp_path.parent / "outside.txt")

        result = generate_patch(outside_path, "new content", tmp_path)

        assert result["status"] == "error", (
            "Absolute path outside workdir must return status='error'"
        )

    def test_patch_shows_correct_diff_lines(self, tmp_path):
        """
        Verify the patch content is a proper unified diff (starts with --- or @@).
        """
        from src.tools.patch_tools import generate_patch

        test_file = tmp_path / "module.py"
        test_file.write_text("LINE_A\nLINE_B\nLINE_C\n")

        new_content = "LINE_A\nLINE_B_MODIFIED\nLINE_C\n"
        result = generate_patch("module.py", new_content, tmp_path)

        assert result["status"] == "ok"
        patch = result["patch"]
        # A unified diff must start with context markers
        assert "---" in patch or "@@" in patch, (
            "Patch must contain unified diff markers"
        )

    def test_identical_content_produces_empty_patch(self, tmp_path):
        """
        When new_content is identical to the existing file, patch should be empty string.
        """
        from src.tools.patch_tools import generate_patch

        original = "no changes here\n"
        test_file = tmp_path / "same.py"
        test_file.write_text(original)

        result = generate_patch("same.py", original, tmp_path)

        assert result["status"] == "ok"
        assert result["patch"] == "", (
            "Identical content must produce an empty patch"
        )

    def test_traversal_with_nested_dots(self, tmp_path):
        """
        NEW-3: Multi-level traversal like '../../../etc/shadow' must also be blocked.
        """
        from src.tools.patch_tools import generate_patch

        result = generate_patch("../../../etc/shadow", "data", tmp_path)

        assert result["status"] == "error"
