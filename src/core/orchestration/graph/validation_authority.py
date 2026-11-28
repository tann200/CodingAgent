"""Validation authority: pipefail wrapping, shell-operator demotion, and red->green debt tracking (TASK-34)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

_PIPEFAIL_SHELLS: frozenset[str] = frozenset({"bash", "zsh", "ksh", "/bin/bash", "/usr/bin/env bash", "/bin/zsh"})

_TRAILING_REDIRECT_RE = re.compile(
    r"(?:\s+(?:2>&1|1>&2|2>\s*/dev/null|>\s*/dev/null))+\s*$"
)

_VALIDATION_CMD_RE = re.compile(
    r"(?:^|\s|/)(?:"
    r"pytest|py\.test|unittest|nose2|"
    r"ruff|mypy|flake8|pylint|pyright|black|isort|"
    r"cargo\s+(?:test|check|clippy|build)|"
    r"go\s+(?:test|vet|build)|"
    r"(?:npm|yarn|pnpm|bun)\s+(?:run\s+)?(?:test|lint|build|check|typecheck)|"
    r"vitest|jest|mocha|tsc|eslint|"
    r"make\s+(?:test|check|lint|build)"
    r")(?:\s|$)",
    re.IGNORECASE,
)


def wrap_with_pipefail(command: str, shell: str = "bash") -> str:
    """Prefix a POSIX validation shell command with ``set -o pipefail; `` when supported."""
    cmd = (command or "").strip()
    if not cmd:
        return cmd
    sh_name = (shell or "").strip().lower()
    base = sh_name.rsplit("/", 1)[-1].split()[-1] if sh_name else ""
    if base not in ("bash", "zsh", "ksh"):
        return cmd
    if re.match(r"^set\s+-[A-Za-z]*o\s+pipefail\b|^set\s+-[A-Za-z]*e[A-Za-z]*o\s+pipefail\b", cmd):
        return cmd
    return f"set -o pipefail; {cmd}"


def normalize_validation_command(command: str) -> str:
    """Normalize a validation command for identity comparison (strips trailing bare 2>&1 / 2>/dev/null)."""
    cmd = (command or "").strip()
    # Strip optional leading set -o pipefail;
    cmd = re.sub(r"^set\s+-o\s+pipefail\s*;\s*", "", cmd)
    # Iteratively strip trailing bare stderr/stdout redirections
    prev = None
    while prev != cmd:
        prev = cmd
        cmd = _TRAILING_REDIRECT_RE.sub("", cmd).strip()
    return " ".join(cmd.split())


def is_validation_command(command: str) -> bool:
    """Return True if *command* looks like a recognized test, lint, typecheck, or build command."""
    norm = normalize_validation_command(command)
    return bool(norm and _VALIDATION_CMD_RE.search(norm))


def _scan_unquoted_operators(command: str) -> Tuple[List[str], List[str]]:
    """Quote-aware scan splitting on unquoted control operators (``;``, ``||``, ``&&``, ``&``).

    Returns ``(operators_found, segments)``. Ignores ``>&`` / ``<&`` redirections
    like ``2>&1`` and operators inside single quotes, double quotes, or escaped chars.
    """
    ops: List[str] = []
    segments: List[str] = []
    buf: List[str] = []
    in_single = False
    in_double = False
    i = 0
    n = len(command)

    while i < n:
        ch = command[i]
        if in_single:
            buf.append(ch)
            if ch == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            if ch == "\\" and i + 1 < n:
                buf.append(ch)
                buf.append(command[i + 1])
                i += 2
                continue
            buf.append(ch)
            if ch == '"':
                in_double = False
            i += 1
            continue

        if ch == "\\" and i + 1 < n:
            buf.append(ch)
            buf.append(command[i + 1])
            i += 2
            continue
        if ch == "'":
            in_single = True
            buf.append(ch)
            i += 1
            continue
        if ch == '"':
            in_double = True
            buf.append(ch)
            i += 1
            continue

        if ch in ("\n", "\r"):
            ops.append("\n")
            segments.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        if ch == ";":
            ops.append(";")
            segments.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        if ch == "|" and i + 1 < n and command[i + 1] == "|":
            ops.append("||")
            segments.append("".join(buf).strip())
            buf = []
            i += 2
            continue
        if ch == "&":
            # Skip file-descriptor redirections like 2>&1, >&2, &>, <&0
            prev_ch = command[i - 1] if i > 0 else ""
            next_ch = command[i + 1] if i + 1 < n else ""
            if prev_ch in (">", "<") or next_ch == ">":
                buf.append(ch)
                i += 1
                continue
            if next_ch == "&":
                ops.append("&&")
                segments.append("".join(buf).strip())
                buf = []
                i += 2
                continue
            ops.append("&")
            segments.append("".join(buf).strip())
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        segments.append(tail)
    return ops, [s for s in segments if s]


def classify_shell_validation(command: str) -> Dict[str, Any]:
    """Classify a shell validation command's authority.

    - Commands containing unquoted ``;``, ``||``, or background ``&`` are demoted
      to ``authority="unconfirmed"`` (can mask earlier failures, so they cannot
      clear required verification or red->green debt).
    - ``&&``-chained commands keep ``authority="confirmed"`` and record each
      normalized stage individually.
    """
    stripped = re.sub(r"^set\s+-o\s+pipefail\s*;\s*", "", (command or "").strip())
    ops, segments = _scan_unquoted_operators(stripped)
    demoting_ops = [op for op in ops if op in (";", "||", "&", "\n")]
    # Unquoted leading shell negation (`! pytest ...`) inverts non-zero exit codes to 0.
    if any(seg == "!" or re.match(r"^!\s+", seg) for seg in segments):
        demoting_ops.append("!")
    stages = [normalize_validation_command(seg) for seg in segments if seg]
    if demoting_ops:
        return {
            "authority": "unconfirmed",
            "demoting_operators": sorted(set(demoting_ops)),
            "stages": stages,
            "reason": (
                f"Validation command demoted to unconfirmed because it contains "
                f"unquoted failure-masking operator(s): {', '.join(sorted(set(demoting_ops)))}"
            ),
        }
    return {
        "authority": "confirmed",
        "demoting_operators": [],
        "stages": stages,
        "reason": None,
    }


@dataclass
class ValidationDebtTracker:
    """Opportunistic red->green validation debt tracker for a single task."""

    red_debt: Set[str] = field(default_factory=set)
    passed_after_mutation: Set[str] = field(default_factory=set)
    has_mutated: bool = False

    def record_mutation(self) -> None:
        """Mark that a workspace file was mutated, invalidating prior green passes."""
        self.has_mutated = True
        self.passed_after_mutation.clear()

    def record_command(
        self,
        command: str,
        *,
        passed: bool,
        after_mutation: Optional[bool] = None,
        exit_code: Optional[int] = None,
        skipped: bool = False,
    ) -> Dict[str, Any]:
        """Record a shell validation command outcome and update red->green debt.

        Non-test-failure outcomes — ``skipped=True``, pytest exit code ``5``
        (no tests collected), or shell ``126``/``127`` (command not found / not
        executable) — never create red->green baseline debt.
        """
        info = classify_shell_validation(command)
        is_after = self.has_mutated if after_mutation is None else bool(after_mutation)
        stages = info["stages"] or [normalize_validation_command(command)]
        val_stages = [s for s in stages if is_validation_command(s)]

        _is_non_failure_exit = skipped or (exit_code in (5, 126, 127))

        if not passed and not is_after and not _is_non_failure_exit:
            # Red baseline failure observed before first mutation -> create red->green debt
            for st in val_stages:
                self.red_debt.add(st)
        elif passed and is_after and info["authority"] == "confirmed":
            # Confirmed green run after last mutation -> pay down matching debt
            try:
                from src.core.orchestration.graph.nodes.verification_helpers import (
                    normalize_validation_command_key,
                )
            except Exception:
                normalize_validation_command_key = normalize_validation_command  # type: ignore[assignment]

            passed_keys: Set[str] = set()
            for st in val_stages:
                self.passed_after_mutation.add(st)
                self.red_debt.discard(st)
                k = normalize_validation_command_key(st)
                if k:
                    passed_keys.add(k)
            norm_full = normalize_validation_command(command)
            if norm_full:
                self.passed_after_mutation.add(norm_full)
                self.red_debt.discard(norm_full)
                k_full = normalize_validation_command_key(norm_full)
                if k_full:
                    passed_keys.add(k_full)

            # Match wrapper-normalized keys (e.g. `env CI=1 .venv/bin/pytest a.py` == `pytest a.py`)
            # and allow a broad/untargeted pytest/unittest run (no specific .py target) to clear
            # narrower Python test debt, without letting an unrelated single-file test clear it.
            def _is_broad_python_test(cmd_key: str) -> bool:
                low = cmd_key.lower()
                if not any(tok in low for tok in ("pytest", "unittest")):
                    return False
                # Targeted single-file / nodeid / -k filter is narrow, not a full suite run
                if ".py" in low or "::" in low or " -k " in low or " -k=" in low:
                    return False
                return True

            has_broad_py = any(_is_broad_python_test(k) for k in passed_keys)
            for existing in list(self.red_debt):
                ex_key = normalize_validation_command_key(existing)
                if ex_key and ex_key in passed_keys:
                    self.red_debt.discard(existing)
                    continue
                if has_broad_py and any(tok in existing.lower() for tok in ("pytest", "unittest")):
                    self.red_debt.discard(existing)

        return {
            **info,
            "unpaid_debt": self.unpaid_debt(),
        }

    def mark_debt_paid_by_verifier(self) -> None:
        """Clear unpaid test debt when verification_node runs and passes all tests."""
        if self.has_mutated:
            self.passed_after_mutation |= set(self.red_debt)
            self.red_debt.clear()

    def unpaid_debt(self) -> List[str]:
        return sorted(self.red_debt)


def get_or_create_debt_tracker(orch: Any) -> ValidationDebtTracker:
    """Return the per-orchestrator ``ValidationDebtTracker``, creating it lazily."""
    tracker = getattr(orch, "_validation_debt_tracker", None)
    if not isinstance(tracker, ValidationDebtTracker):
        tracker = ValidationDebtTracker()
        if orch is not None:
            try:
                setattr(orch, "_validation_debt_tracker", tracker)
            except Exception:
                pass
    return tracker
