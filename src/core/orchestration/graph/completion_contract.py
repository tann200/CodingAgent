"""Evidence-based completion contracts for agent tasks (audit Phase 1 Item 1).

Distinguishes between:
1. Mutating tasks: require side-effect evidence (e.g. modified files, successful file/shell mutations).
2. Inspection/read-only tasks: require read actions or inspection findings.
3. Informational tasks: require non-empty answer content.

Rejects false completion when:
- Model output is empty / whitespace with no tools called.
- A mutating task produces only text without any files modified or mutation tools executed.
- Modifying tools or shell commands reported failure.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Set


class TaskType(str, Enum):
    MUTATING = "mutating"
    READ_ONLY = "read_only"
    INFORMATIONAL = "informational"


_MUTATION_REGEX = re.compile(
    r"\b(?:"
    r"create|write|edit|modify|update|patch|delete|remove|fix|refactor|implement|add|replace|rename"
    r")\b.*(?:"
    r"\.(?:py|ts|js|json|md|txt|ya?ml|html|css|sh|rs|go|java|c|cpp)|"
    r"\b(?:file|code|function|class|method|test|module|endpoint|bug|script|directory|folder|package|repo)\b"
    r")|"
    r"\b(?:fix|resolve)\s+(?:the\s+)?(?:bug|issue|error|failure|test|typo|crash)\b|"
    r"\b(?:implement|build|develop)\s+(?:a\s+|the\s+)?(?:feature|function|class|endpoint|component|api)\b|"
    r"\b(?:add|insert)\s+(?:a\s+|the\s+)?(?:function|class|method|test|endpoint|import|line)\b",
    re.IGNORECASE | re.DOTALL,
)

_READ_ONLY_REGEX = re.compile(
    r"\b(?:"
    r"find|search|grep|locate|where\s+is|inspect|look\s+at|show\s+me|list|read|view|check\s+if"
    r")\b",
    re.IGNORECASE,
)

_MODIFYING_TOOL_NAMES = frozenset(
    {
        "write_file",
        "edit_file",
        "create_file",
        "delete_file",
        "rename_file",
        "apply_patch",
        "patch",
        "bash",
    }
)


@dataclass
class CompletionContractVerdict:
    is_complete: bool
    task_type: TaskType
    failure_reason: Optional[str] = None
    error_code: Optional[str] = None
    evidence: Optional[Dict[str, Any]] = None


def classify_task(
    task: str,
    current_plan: Optional[List[Dict[str, Any]]] = None,
    affected_files: Optional[List[str]] = None,
) -> TaskType:
    """Classify a task into MUTATING, READ_ONLY, or INFORMATIONAL."""
    if affected_files and len(affected_files) > 0:
        return TaskType.MUTATING

    if current_plan:
        for step in current_plan:
            action = step.get("action") or step.get("tool") or ""
            desc = step.get("description") or ""
            if action in _MODIFYING_TOOL_NAMES:
                return TaskType.MUTATING
            if _MUTATION_REGEX.search(desc) or _MUTATION_REGEX.search(action):
                return TaskType.MUTATING

    cleaned_task = (task or "").strip()
    if _MUTATION_REGEX.search(cleaned_task):
        return TaskType.MUTATING

    if _READ_ONLY_REGEX.search(cleaned_task):
        return TaskType.READ_ONLY

    return TaskType.INFORMATIONAL


def evaluate_completion_contract(
    task: str,
    content_text: Optional[str] = None,
    tool_calls_count: int = 0,
    modified_files: Optional[Set[str] | List[str]] = None,
    current_plan: Optional[List[Dict[str, Any]]] = None,
    affected_files: Optional[List[str]] = None,
    last_result: Optional[Mapping[str, Any]] = None,
    errors: Optional[List[str]] = None,
) -> CompletionContractVerdict:
    """Evaluate whether completion criteria are genuinely satisfied by concrete evidence."""
    cleaned_task = (task or "").strip()
    if not cleaned_task:
        return CompletionContractVerdict(
            is_complete=True,
            task_type=TaskType.INFORMATIONAL,
        )

    task_type = classify_task(
        task=cleaned_task,
        current_plan=current_plan,
        affected_files=affected_files,
    )

    # 1. Reject empty response when no actions were taken
    text_content = (content_text or "").strip()
    has_completed_plan = bool(current_plan and all(step.get("completed", False) for step in current_plan))
    if content_text is not None and not text_content and tool_calls_count == 0 and not has_completed_plan:
        return CompletionContractVerdict(
            is_complete=False,
            task_type=task_type,
            failure_reason="Model returned empty response without tool calls or explanation",
            error_code="empty_response",
        )

    # 2. Mutating tasks require evidence of successful modification
    if task_type == TaskType.MUTATING:
        mod_set = set(modified_files or [])
        has_modified_files = len(mod_set) > 0

        # Check last result status if present
        last_ok: Optional[bool] = None
        last_status: Optional[str] = None
        if isinstance(last_result, Mapping):
            last_ok = last_result.get("ok")
            last_status = last_result.get("status")
            if last_ok is None and isinstance(last_result.get("result"), Mapping):
                res_inner = last_result.get("result")
                last_ok = res_inner.get("ok")
                last_status = res_inner.get("status")

        last_failed = (last_ok is False) or (last_status in ("error", "failed"))
        if last_failed:
            err_msg = ""
            if isinstance(last_result, Mapping):
                err_msg = str(last_result.get("error") or last_result.get("result", {}).get("error") or "Mutation action failed")
            return CompletionContractVerdict(
                is_complete=False,
                task_type=TaskType.MUTATING,
                failure_reason=err_msg or "Mutation action failed",
                error_code="mutation_failed",
            )

        if tool_calls_count == 0:
            return CompletionContractVerdict(
                is_complete=False,
                task_type=TaskType.MUTATING,
                failure_reason="Task requested code or file modifications, but no mutating tools were executed (text-only response).",
                error_code="unverified_text_only_completion",
            )

        if not has_modified_files:
            # Check if last_result was a successful non-file mutation or successful command
            if last_ok is True or last_status == "ok":
                return CompletionContractVerdict(
                    is_complete=True,
                    task_type=TaskType.MUTATING,
                    evidence={"last_result": last_result},
                )
            return CompletionContractVerdict(
                is_complete=False,
                task_type=TaskType.MUTATING,
                failure_reason="Task requested code or file modifications, but no files were modified.",
                error_code="unverified_mutation_missing",
            )

        return CompletionContractVerdict(
            is_complete=True,
            task_type=TaskType.MUTATING,
            evidence={"modified_files": list(mod_set)},
        )

    # 3. Read-only tasks
    if task_type == TaskType.READ_ONLY:
        if not text_content and tool_calls_count == 0:
            return CompletionContractVerdict(
                is_complete=False,
                task_type=TaskType.READ_ONLY,
                failure_reason="No inspection output or results returned",
                error_code="empty_inspection",
            )
        return CompletionContractVerdict(
            is_complete=True,
            task_type=TaskType.READ_ONLY,
            evidence={"text_length": len(text_content), "tool_calls_count": tool_calls_count},
        )

    # 4. Informational tasks
    if content_text is None or text_content or has_completed_plan or tool_calls_count > 0:
        return CompletionContractVerdict(
            is_complete=True,
            task_type=TaskType.INFORMATIONAL,
            evidence={"text_length": len(text_content), "has_completed_plan": has_completed_plan},
        )

    return CompletionContractVerdict(
        is_complete=False,
        task_type=TaskType.INFORMATIONAL,
        failure_reason="Empty response for informational query",
        error_code="empty_response",
    )
