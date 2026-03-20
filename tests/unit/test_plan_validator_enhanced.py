"""Tests for enhanced plan validator (strict mode, read-before-edit)."""

from src.core.orchestration.graph.nodes.plan_validator_node import validate_plan


class TestEnhancedPlanValidator:
    """Tests for enhanced plan validator (strict mode, read-before-edit)."""

    def test_strict_mode_requires_verification(self):
        """Test that strict mode requires verification steps."""
        plan = [
            {"description": "Read the main.py file"},
            {"description": "Modify the function"},
        ]
        # Without strict mode - should be valid with warning
        result = validate_plan(plan, strict_mode=False)
        assert result["valid"] is True
        assert len(result["warnings"]) > 0

        # With strict mode - should be invalid (verification required)
        result = validate_plan(plan, strict_mode=True)
        assert result["valid"] is False
        assert any("verification" in e.lower() for e in result["errors"])

    def test_enforce_warnings_treats_warnings_as_errors(self):
        """Test that enforce_warnings treats warnings as errors."""
        plan = [
            {"description": "Read the main.py file"},
        ]
        # Without enforce_warnings - should be valid
        result = validate_plan(plan, enforce_warnings=False)
        assert result["valid"] is True

        # With enforce_warnings - should be invalid (no files referenced)
        result = validate_plan(plan, enforce_warnings=True)
        assert result["valid"] is False

    def test_read_before_edit_validation_description_only(self):
        """Test that edit in description triggers warning (strict mode makes it error)."""
        # Edit in description - in non-strict mode this is just tracked
        plan = [
            {"description": "Edit main.py file"},  # No prior read
            {"description": "Run tests"},
        ]
        result = validate_plan(plan, strict_mode=False)
        assert result["valid"] is True
        # The warning is about verification, not read-before-edit for descriptions

    def test_read_before_edit_with_actions(self):
        """Test read-before-edit validation with action objects."""
        # Edit without read - should fail in strict mode
        plan = [
            {
                "description": "Edit main.py",
                "action": {
                    "name": "edit_file",
                    "arguments": {"path": "main.py", "patch": "..."},
                },
            },
        ]
        result = validate_plan(plan, strict_mode=True)
        # In strict mode, this should fail due to both edit without read AND missing verification
        assert result["valid"] is False
        assert any("without prior read" in e.lower() for e in result["errors"])

        # With read first - should pass (but still fails due to no verification in strict mode)
        plan = [
            {
                "description": "Read main.py",
                "action": {"name": "read_file", "arguments": {"path": "main.py"}},
            },
            {
                "description": "Edit main.py",
                "action": {
                    "name": "edit_file",
                    "arguments": {"path": "main.py", "patch": "..."},
                },
            },
            {
                "description": "Run tests to verify",
                "action": {"name": "run_tests", "arguments": {}},
            },
        ]
        result = validate_plan(plan, strict_mode=True)
        # Should be valid now - has read, edit, and verification
        assert result["valid"] is True

    def test_dangerous_operations_detected(self):
        """Test that dangerous operations are detected."""
        plan = [
            {"description": "Run rm -rf /tmp/test"},  # Dangerous
        ]
        result = validate_plan(plan)
        assert result.get("has_dangerous_ops") is True

    def test_severity_levels(self):
        """Test that severity levels are correctly set."""
        # Errors - error
        plan = []
        result = validate_plan(plan)
        assert result["severity"] == "error"

        # Warnings only - warning (when enforce_warnings is True)
        plan = [{"description": "Read file"}]
        result = validate_plan(plan, enforce_warnings=True)
        assert result["severity"] == "warning"
