import logging
from typing import Dict, Any, List, Optional

from src.core.orchestration.graph.state import AgentState

logger = logging.getLogger(__name__)


def validate_plan(
    plan: List[Dict[str, Any]],
    enforce_warnings: bool = False,
    strict_mode: bool = False,
) -> Dict[str, Any]:
    """
    Validate a plan before execution.

    Checks:
    - Plan has at least one step
    - Plan references files in steps
    - Plan has verification step (in strict mode)
    - Steps are properly formatted
    - No dangerous operations without safety measures

    Args:
        plan: The plan to validate
        enforce_warnings: If True, warnings will block execution (treat warnings as errors)
        strict_mode: If True, enforces verification steps and other best practices

    Returns:
        {
            "valid": bool,
            "errors": List[str],
            "warnings": List[str],
            "severity": "error" | "warning" | "ok"
        }
    """
    errors = []
    warnings = []

    if not plan:
        errors.append("Plan is empty")
        return {
            "valid": False,
            "errors": errors,
            "warnings": warnings,
            "severity": "error",
        }

    if len(plan) == 0:
        errors.append("Plan has no steps")

    # Check each step
    has_verification = False
    steps_reference_files = 0
    has_dangerous_ops = False

    # Track edit operations without prior read
    edit_ops_without_read = set()
    read_ops = set()

    for i, step in enumerate(plan):
        if not isinstance(step, dict):
            errors.append(f"Step {i} is not a dictionary")
            continue

        description = step.get("description", "")
        action = step.get("action")

        # Extract tool name from action if present
        tool_name = None
        if action and isinstance(action, dict):
            tool_name = action.get("name", "")

        # Track read operations
        if tool_name in ["read_file", "fs.read", "list_files", "list_dir", "glob"]:
            if isinstance(action, dict):
                path = action.get("arguments", {}).get("path", "")
                if path:
                    read_ops.add(path)

        # Check if step references a file
        if any(
            keyword in description.lower()
            for keyword in [
                "file",
                "read",
                "write",
                "edit",
                "modify",
                "update",
                "add",
                "remove",
                "create",
                "delete",
            ]
        ):
            steps_reference_files += 1

        # Check for edit operations
        if tool_name in ["edit_file", "write_file", "apply_patch", "delete_file"]:
            # Check if we've read this file
            if isinstance(action, dict):
                path = action.get("arguments", {}).get("path", "")
                if path and path not in read_ops:
                    edit_ops_without_read.add(path)
                    has_dangerous_ops = True

        # Check for dangerous commands in description
        dangerous_keywords = ["rm -rf", "delete all", "drop table", "truncate"]
        if any(d in description.lower() for d in dangerous_keywords):
            has_dangerous_ops = True

        # Check for verification step
        if any(
            keyword in description.lower()
            for keyword in ["test", "verify", "lint", "check", "run"]
        ):
            has_verification = True

    # Add errors for dangerous operations
    if has_dangerous_ops:
        if edit_ops_without_read:
            # In strict mode, this is an error
            if strict_mode:
                errors.append(
                    f"Edit operations without prior read: {', '.join(edit_ops_without_read)}. "
                    "Add read_file steps before edit steps."
                )
            else:
                warnings.append(
                    f"Edit operations without prior read detected: {', '.join(edit_ops_without_read)}"
                )

    # Add warnings/errors based on mode
    if not has_verification:
        if strict_mode:
            errors.append("Plan does not include a verification step (tests/lint)")
        else:
            warnings.append("Plan does not include a verification step (tests/lint)")

    if steps_reference_files == 0:
        warnings.append("Plan does not reference any files")

    if len(plan) > 20:
        warnings.append(
            f"Plan has {len(plan)} steps - consider breaking into smaller plans"
        )

    # Determine severity and validity
    if errors:
        severity = "error"
        is_valid = False
    elif warnings and (enforce_warnings or strict_mode):
        severity = "warning"
        is_valid = False  # Treat warnings as errors in strict mode
    else:
        severity = "ok"
        is_valid = True

    return {
        "valid": is_valid,
        "errors": errors,
        "warnings": warnings,
        "severity": severity,
        "has_verification": has_verification,
        "steps_with_files": steps_reference_files,
        "has_dangerous_ops": has_dangerous_ops,
    }


async def plan_validator_node(state: AgentState, config: Any) -> Dict[str, Any]:
    """
    Plan Validator Node: Validates plans before execution.

    Ensures plans are well-formed and complete before being passed to execution.
    In strict mode, enforces verification steps and read-before-edit patterns.
    """
    logger.info("=== plan_validator_node START ===")

    # Get validation config from state or use defaults
    enforce_warnings = state.get(
        "plan_enforce_warnings", True
    )  # Default True - enforce warnings as errors
    strict_mode = state.get(
        "plan_strict_mode", False
    )  # Keep False - strict is aggressive

    current_plan = state.get("current_plan")

    if not current_plan:
        logger.warning("plan_validator_node: no plan to validate")
        return {
            "plan_validation": {
                "valid": False,
                "errors": ["No plan provided"],
                "warnings": [],
                "severity": "error",
            },
            "errors": ["No plan to validate"],
        }

    validation_result = validate_plan(
        current_plan,
        enforce_warnings=enforce_warnings,
        strict_mode=strict_mode,
    )

    logger.info(f"plan_validator_node: validation result = {validation_result}")

    if not validation_result["valid"]:
        logger.warning(
            f"plan_validator_node: plan invalid - errors={validation_result['errors']}, "
            f"warnings={validation_result['warnings']}"
        )

        return {
            "plan_validation": validation_result,
            "action_failed": True,
            "errors": validation_result["errors"] + validation_result["warnings"],
        }

    if validation_result["warnings"]:
        logger.info(f"plan_validator_node: warnings - {validation_result['warnings']}")

    return {
        "plan_validation": validation_result,
        "action_failed": False,
    }
