"""
Tests for verification tools: run_tests, run_linter, syntax_check.

These tests verify the tool verification functionality.
"""



class TestVerificationTools:
    """Tests for verification_tools module."""

    def test_run_tests_import(self):
        """Test verification_tools can be imported."""
        from src.tools import verification_tools

        assert verification_tools is not None

    def test_syntax_check_import(self):
        """Test syntax check function exists."""
        from src.tools.verification_tools import syntax_check

        assert syntax_check is not None

    def test_run_linter_import(self):
        """Test run_linter function exists."""
        from src.tools.verification_tools import run_linter

        assert run_linter is not None


class TestSyntaxCheck:
    """Tests for syntax_check function."""

    def test_syntax_check_valid_python(self, tmp_path):
        """Test syntax check with valid Python file."""
        from src.tools.verification_tools import syntax_check

        # Create a valid Python file
        test_file = tmp_path / "test.py"
        test_file.write_text("def hello():\n    print('hello')\n")

        result = syntax_check(str(tmp_path))

        assert result is not None
        assert result.get("status") in ["ok", "pass", "error", "fail"]

    def test_syntax_check_invalid_python(self, tmp_path):
        """Test syntax check with invalid Python file."""
        from src.tools.verification_tools import syntax_check

        # Create an invalid Python file
        test_file = tmp_path / "test.py"
        test_file.write_text("def hello(\n    print('hello')")

        result = syntax_check(str(tmp_path))

        assert result is not None
        assert result.get("status") in ["ok", "pass", "error", "fail"]


class TestRunLinter:
    """Tests for run_linter function."""

    def test_run_linter_no_linter(self, tmp_path):
        """Test run_linter when no linter is configured."""
        from src.tools.verification_tools import run_linter

        # Should handle missing linter gracefully
        result = run_linter(str(tmp_path))

        assert result is not None
        # May return error if no linter found
        assert "status" in result


class TestRunTests:
    """Tests for run_tests function."""

    def test_run_tests_no_tests(self, tmp_path):
        """Test run_tests when no tests exist."""
        from src.tools.verification_tools import run_tests

        result = run_tests(str(tmp_path))

        assert result is not None
        assert "status" in result


class TestParsePytestSummary:
    """Unit tests for _parse_pytest_summary (pure function, no subprocess)."""

    def test_passes_extracted(self):
        from src.tools.verification_tools import _parse_pytest_summary
        output = "5 passed in 1.23s"
        passed, failed = _parse_pytest_summary(output)
        assert passed == 5
        assert failed == 0

    def test_failures_extracted(self):
        from src.tools.verification_tools import _parse_pytest_summary
        output = "2 failed, 3 passed in 0.50s"
        passed, failed = _parse_pytest_summary(output)
        assert passed == 3
        assert failed == 2

    def test_empty_output(self):
        from src.tools.verification_tools import _parse_pytest_summary
        passed, failed = _parse_pytest_summary("")
        assert passed == 0
        assert failed == 0

    def test_only_failures(self):
        from src.tools.verification_tools import _parse_pytest_summary
        output = "3 failed in 0.12s"
        passed, failed = _parse_pytest_summary(output)
        assert failed == 3


class TestExtractFailedTests:
    """Unit tests for _extract_failed_tests (pure function)."""

    def test_extracts_failed_test_names(self):
        from src.tools.verification_tools import _extract_failed_tests
        output = (
            "tests/unit/test_foo.py::test_bar FAILED\n"
            "tests/unit/test_foo.py::test_baz FAILED\n"
            "1 passed\n"
        )
        failed = _extract_failed_tests(output)
        assert len(failed) == 2

    def test_no_failures(self):
        from src.tools.verification_tools import _extract_failed_tests
        output = "5 passed in 1.23s"
        failed = _extract_failed_tests(output)
        assert failed == []


class TestParseRuffOutput:
    """Unit tests for _parse_ruff_output (pure function)."""

    def test_parses_error_lines(self):
        from src.tools.verification_tools import _parse_ruff_output
        output = "src/foo.py:10:5: E501 line too long (100 > 79 characters)\n"
        errors = _parse_ruff_output(output)
        assert len(errors) == 1
        assert errors[0]["file"] == "src/foo.py"
        assert errors[0]["line"] == 10
        assert errors[0]["code"] == "E501"

    def test_empty_output(self):
        from src.tools.verification_tools import _parse_ruff_output
        errors = _parse_ruff_output("")
        assert errors == []

    def test_severity_mapping(self):
        from src.tools.verification_tools import _parse_ruff_output
        output = (
            "foo.py:1:1: E101 indentation contains mixed spaces and tabs\n"
            "foo.py:2:1: W503 line break before binary operator\n"
        )
        errors = _parse_ruff_output(output)
        assert any(e["severity"] == "error" for e in errors)
        assert any(e["severity"] == "warning" for e in errors)


class TestSyntaxCheckStructured:
    """Structured-output tests for syntax_check."""

    def test_valid_file_returns_ok(self, tmp_path):
        from src.tools.verification_tools import syntax_check
        (tmp_path / "good.py").write_text("x = 1\n")
        result = syntax_check(str(tmp_path))
        assert result["status"] == "ok"
        assert result["checked_files"] >= 1
        assert result["syntax_errors"] == []

    def test_invalid_file_returns_fail(self, tmp_path):
        from src.tools.verification_tools import syntax_check
        (tmp_path / "bad.py").write_text("def foo(\n    pass\n")
        result = syntax_check(str(tmp_path))
        assert result["status"] == "fail"
        assert len(result["syntax_errors"]) >= 1
        assert "file" in result["syntax_errors"][0]
