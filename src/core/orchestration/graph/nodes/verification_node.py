from __future__ import annotations

import logging
import py_compile as _pyc
from pathlib import Path
from typing import Any, Dict, Mapping

from langchain_core.runnables import RunnableConfig

from src.core.orchestration.graph.nodes.node_utils import (
    _resolve_orchestrator,
    span_node as _span_node,
)
from src.core.orchestration.graph.nodes.verification_helpers import (
    build_deferred_rollback_feedback,
    check_targeted_python_syntax,
    discover_targeted_pytest_files,
    filter_unverified_files,
    has_go_project,
    has_rust_project,
    run_custom_verify_yaml,
    run_go_verification,
    run_rust_verification,
    run_targeted_ruff,
)
from src.core.orchestration.graph.state import StateLike
from src.tools import verification_tools

logger = logging.getLogger(__name__)


def _has_js_project(workdir: Path) -> bool:
    """Return True if workdir contains a package.json (JS/TS project)."""
    return (workdir / "package.json").exists()


def _step_requests_verification(state: Mapping[str, Any]) -> bool:
    """Return True if the current plan step explicitly asks for test/verify/lint."""
    current_plan = state.get("current_plan") or []
    current_step = state.get("current_step") or 0
    if current_plan and current_step < len(current_plan):
        desc = current_plan[current_step].get("description", "").lower()
        return any(
            k in desc
            for k in (
                "run_tests",
                "run_linter",
                "verify",
                "test",
                "lint",
                "run_js_tests",
                "run_ts_check",
            )
        )
    return False


_WRITE_TOOLS_ALWAYS_VERIFY = {
    "write_file",
    "edit_file",
    "edit_file_atomic",
    "edit_by_line_range",
    "bash",
    "patch_apply",
    "apply_patch",
    "create_file",
    "delete_file",
    "files_write",
    "files_edit",
    "files_delete",
    "files_patch",
}


async def verification_node(state: StateLike, config: RunnableConfig) -> Dict[str, Any]:
    """Thin OTel-span wrapper — delegates to _verification_node_impl."""
    with _span_node("verification", {"step": state.get("current_step", 0)}):
        return await _verification_node_impl(state, config)


async def _verification_node_impl(state: StateLike, config: RunnableConfig) -> Dict[str, Any]:
    """Polyglot, SHA-256-deduplicated verification node with Deferred Rollback."""
    logger.info("=== verification_node START ===")

    session_modified: set[str] = set()
    orch = _resolve_orchestrator(state, config)
    if orch and getattr(orch, "_session_modified_files", None):
        session_modified.update(orch._session_modified_files)
    if state.get("modified_files"):
        session_modified.update(state.get("modified_files"))
    if state.get("_session_modified_files"):
        session_modified.update(state.get("_session_modified_files"))

    last_action = state.get("last_action") or {}
    action_side_effects: list[str] = []
    if isinstance(last_action, Mapping):
        action_side_effects = list(last_action.get("side_effects") or [])
        action_mod_files = list(last_action.get("modified_files") or [])
        session_modified.update(action_mod_files)
        session_modified.update(action_side_effects)

    _tier = (state.get("model_tier") or "").lower()
    _last_tool = state.get("last_tool_name") or ""
    if (
        not session_modified
        and not action_side_effects
        and _tier == "small"
        and _last_tool not in _WRITE_TOOLS_ALWAYS_VERIFY
    ):
        logger.info(
            "verification_node: read-only tool with no modifications or side effects — skipping verification"
        )
        return {
            "verification_passed": True,
            "verification_result": {"status": "skipped_readonly"},
            "verification": {
                "required": False,
                "passed": True,
                "commands": [],
                "evidence": [],
            },
        }

    last_result = state.get("last_result") or {}
    need_verify = False

    # Validate file deletions immediately
    if isinstance(last_result, dict):
        r = last_result.get("result") or {}
        if isinstance(r, dict) and r.get("status") == "ok" and r.get("deleted"):
            deleted_path = r.get("path")
            if deleted_path:
                workdir = Path(state.get("working_dir", "."))
                full_path = (
                    workdir / deleted_path
                    if not Path(deleted_path).is_absolute()
                    else Path(deleted_path)
                )
                if full_path.exists():
                    logger.warning("Verification FAILED: %s still exists after deletion", deleted_path)
                    return {
                        "verification_result": {
                            "deletion_verification": "FAILED",
                            "error": f"File still exists: {deleted_path}",
                            "path": deleted_path,
                        },
                        "verification_passed": False,
                    }
                logger.info("Verification PASSED: %s successfully deleted", deleted_path)
                return {
                    "verification_result": {
                        "deletion_verification": "PASSED",
                        "path": deleted_path,
                    }
                }

    last_tool_name: str = state.get("last_tool_name") or ""
    try:
        if isinstance(last_result, dict):
            r = last_result.get("result") or last_result
            if isinstance(r, dict):
                if r.get("status") == "ok":
                    if last_tool_name in _WRITE_TOOLS_ALWAYS_VERIFY or r.get("path") or (
                        "returncode" in r and r["returncode"] == 0
                    ):
                        need_verify = True
                elif (
                    ("returncode" in r and r["returncode"] != 0)
                    or r.get("status") in ("error", "failed")
                    or last_result.get("ok") is False
                ):
                    need_verify = True
    except Exception:
        pass

    if session_modified:
        need_verify = True

    if not need_verify and _step_requests_verification(state):
        need_verify = True

    current_plan = state.get("current_plan") or []
    current_step = int(state.get("current_step") or 0)
    if current_plan:
        at_final_step = current_step >= len(current_plan) - 1
    else:
        evaluation_done = state.get("evaluation_result") in ("complete", "pass")
        next_action = state.get("next_action")
        at_final_step = evaluation_done or (next_action is None)
    run_full_suite = at_final_step or _step_requests_verification(state)

    rt = state.get("runtime")
    is_canonical_run = isinstance(rt, Mapping)
    rt_policy = rt.get("policy") if is_canonical_run and isinstance(rt.get("policy"), Mapping) else {}
    verification_mode = str(rt_policy.get("verification_mode") or "")

    verified_hashes: Dict[str, str] = dict(state.get("_verified_file_hashes") or {})
    current_hashes: Dict[str, str] = {}

    cancel_event = state.get("cancel_event")

    def _is_cancelled() -> bool:
        try:
            return bool(cancel_event and cancel_event.is_set())
        except Exception:
            return False

    results: Dict[str, Any] = {}
    if need_verify:
        if _is_cancelled():
            logger.info("verification_node: cancelled before running tools — skipping")
            return {
                "verification_result": {"cancelled": True},
                "verification_passed": False,
            }
        try:
            wd = Path(state.get("working_dir") or ".")
            unverified_paths, current_hashes, skipped_cached = filter_unverified_files(
                session_modified, verified_hashes, wd
            )

            # SPEC-P3.2: Scoped verification heuristics for non-executable assets
            non_code_exts = {
                ".md", ".markdown", ".txt", ".rst",
                ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
                ".css", ".scss", ".sass", ".less",
                ".html", ".htm", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".ico",
                ".csv", ".tsv", ".lock",
            }
            is_purely_non_code = bool(
                unverified_paths
                and all(
                    Path(p).suffix.lower() in non_code_exts
                    and ".codingagent" not in Path(p).parts
                    and Path(p).name != "verify.yaml"
                    for p in unverified_paths
                )
            )

            if (custom_results := run_custom_verify_yaml(wd, session_modified, _is_cancelled)) is not None:
                results.update(custom_results)
            elif is_purely_non_code:
                logger.info(
                    "verification_node: all %d modified paths are non-executable assets — skipping full test suite",
                    len(unverified_paths),
                )
                results["status"] = "skipped_non_code"
                results["non_code_assets"] = True
            elif _has_js_project(wd):
                js_code_exts = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
                has_js_code_modified = any(
                    Path(p).suffix.lower() in js_code_exts for p in unverified_paths
                )
                if unverified_paths and not has_js_code_modified:
                    logger.debug("verification_node: modified files contain no JS/TS executable code — skipping js_tests")
                    results["status"] = "skipped_non_code"
                elif run_full_suite and verification_mode != "targeted":
                    results["js_tests"] = verification_tools.run_js_tests(str(wd))
                    if _is_cancelled():
                        return {
                            "verification_result": {**results, "cancelled": True},
                            "verification_passed": False,
                        }
                    results["ts_check"] = verification_tools.run_ts_check(str(wd))
                    if _is_cancelled():
                        return {
                            "verification_result": {**results, "cancelled": True},
                            "verification_passed": False,
                        }
                    results["eslint"] = verification_tools.run_eslint(str(wd))
                elif verification_mode == "targeted":
                    js_paths = [
                        p for p in unverified_paths
                        if Path(p).suffix.lower() in js_code_exts
                    ]
                    if js_paths:
                        results["eslint"] = verification_tools.run_eslint(str(wd), paths=js_paths)
                    if run_full_suite and not _is_cancelled() and has_js_code_modified:
                        results["ts_check"] = verification_tools.run_ts_check(str(wd))
                else:
                    modified_path: str | None = None
                    try:
                        lr = state.get("last_result") or {}
                        r = lr.get("result") or lr
                        modified_path = r.get("path")
                    except Exception:
                        pass
                    if modified_path and Path(modified_path).suffix.lower() in js_code_exts:
                        results["eslint"] = verification_tools.run_eslint(
                            str(wd), paths=[modified_path]
                        )
            elif has_rust_project(wd):
                results.update(run_rust_verification(wd, run_full_suite, _is_cancelled))
            elif has_go_project(wd):
                results.update(run_go_verification(wd, run_full_suite, _is_cancelled))
            elif verification_mode == "targeted":
                # Targeted Python verification: NEVER os.walk the full repository
                results["syntax"] = check_targeted_python_syntax(
                    unverified_paths, skipped_cached=skipped_cached
                )
                if unverified_paths and not _is_cancelled():
                    results["linter"] = run_targeted_ruff(unverified_paths, wd)
                if run_full_suite and not _is_cancelled():
                    targeted_tests = discover_targeted_pytest_files(session_modified, wd)
                    last_failed_exists = (wd / ".pytest_cache" / "v" / "cache" / "lastfailed").exists()
                    if targeted_tests:
                        results["tests"] = verification_tools.run_tests(
                            str(wd), test_files=targeted_tests
                        )
                    elif last_failed_exists:
                        results["tests"] = verification_tools.run_tests(
                            str(wd), use_last_failed=True
                        )
                    else:
                        results["tests"] = {
                            "status": "skipped",
                            "reason": "targeted mode: no matching test file for modified module",
                        }
            else:
                py_code_exts = {".py", ".pyw"}
                has_py_code_modified = any(
                    Path(p).suffix.lower() in py_code_exts for p in unverified_paths
                )
                if unverified_paths and not has_py_code_modified:
                    logger.debug("verification_node: modified files contain no Python executable code — skipping tests")
                    results["status"] = "skipped_non_code"
                elif run_full_suite:
                    last_failed_exists = (wd / ".pytest_cache" / "v" / "cache" / "lastfailed").exists()
                    results["tests"] = verification_tools.run_tests(
                        str(wd), use_last_failed=last_failed_exists
                    )
                    if _is_cancelled():
                        return {
                            "verification_result": {**results, "cancelled": True},
                            "verification_passed": False,
                        }
                    results["linter"] = verification_tools.run_linter(str(wd))
                    if _is_cancelled():
                        return {
                            "verification_result": {**results, "cancelled": True},
                            "verification_passed": False,
                        }
                    if session_modified and not unverified_paths and skipped_cached > 0:
                        results["syntax"] = {
                            "status": "ok",
                            "checked_files": 0,
                            "skipped_cached": skipped_cached,
                            "syntax_errors": [],
                        }
                    else:
                        results["syntax"] = verification_tools.syntax_check(str(wd))
                else:
                    # SCAN-2: intermediate step — use py_compile on single modified .py file
                    _mod_path: str | None = None
                    try:
                        _lr = state.get("last_result") or {}
                        _r = _lr.get("result") or _lr
                        _mod_path = _r.get("path") or _r.get("file_path")
                    except Exception:
                        pass
                    if _mod_path and str(_mod_path).endswith(".py"):
                        try:
                            _pyc.compile(str(_mod_path), doraise=True)
                            results["syntax"] = {
                                "status": "ok",
                                "checked_files": 1,
                                "syntax_errors": [],
                            }
                        except Exception as _se:
                            results["syntax"] = {
                                "status": "fail",
                                "checked_files": 1,
                                "syntax_errors": [{"file": _mod_path, "error": str(_se)}],
                            }
        except Exception as e:
            results["error"] = str(e)

    verification_passed = True
    if "error" in results:
        verification_passed = False
    for key, val in results.items():
        if key == "step_rollback":
            continue
        if isinstance(val, Mapping) and val.get("status") == "fail":
            verification_passed = False
            break

    # Failed command or failed mutation forces verification failure
    if isinstance(last_result, dict):
        r = last_result.get("result") or last_result
        if isinstance(r, dict):
            if "returncode" in r and r["returncode"] != 0:
                verification_passed = False
                results["command"] = {
                    "status": "fail",
                    "error": f"Command failed with exit code {r['returncode']}",
                    "details": r.get("stderr") or r.get("stdout") or "",
                }
            elif (r.get("status") in ("error", "failed") or last_result.get("ok") is False) and (
                last_tool_name in _WRITE_TOOLS_ALWAYS_VERIFY or session_modified
            ):
                verification_passed = False
                results["mutation"] = {
                    "status": "fail",
                    "error": str(
                        r.get("error") or last_result.get("error") or "Mutation action failed"
                    ),
                }

    run_state = dict(state.get("run") or {})
    prev_recoveries = int(run_state.get("recoveries") or 0)
    raw_limit = rt_policy.get("recovery_limit") if isinstance(rt_policy, Mapping) else None
    recovery_limit = int(raw_limit) if raw_limit is not None else 2
    can_defer_rollback = bool(
        is_canonical_run
        and state.get("use_replan") is not False
        and prev_recoveries < recovery_limit
    )

    updated_history: list | None = None

    if not verification_passed and need_verify:
        if can_defer_rollback:
            # Deferred Rollback: keep open transaction & edits on disk for surgical fix
            attempt = prev_recoveries + 1
            run_state["recoveries"] = attempt
            run_state["status"] = "recovering"
            run_state["outcome"] = "continue"
            results["step_rollback"] = {
                "triggered": False,
                "deferred": True,
                "attempt": attempt,
                "recovery_limit": recovery_limit,
            }
            feedback = build_deferred_rollback_feedback(
                results=results,
                attempt=attempt,
                recovery_limit=recovery_limit,
                modified_files=session_modified,
            )
            updated_history = list(state.get("history") or []) + [
                {"role": "user", "content": feedback}
            ]
            logger.info(
                "verification_node: deferring rollback for recovery attempt %d/%d",
                attempt,
                recovery_limit,
            )
        else:
            # Recovery exhausted or legacy non-canonical graph: rollback immediately
            try:
                orchestrator = _resolve_orchestrator(state, config)
                if orchestrator and hasattr(orchestrator, "rollback_step_transaction"):
                    if getattr(orchestrator, "_step_snapshot_id", None):
                        rb = orchestrator.rollback_step_transaction()
                        if rb.get("ok"):
                            logger.info(
                                "verification_node: step rollback restored %d file(s)",
                                rb.get("restored_count", 0),
                            )
                            results["step_rollback"] = {
                                "triggered": True,
                                "restored_files": rb.get("restored_files", []),
                            }
                        else:
                            logger.warning(
                                "verification_node: step rollback failed: %s", rb.get("error")
                            )
            except Exception as rb_err:
                logger.warning("verification_node: step rollback error (non-fatal): %s", rb_err)
            run_state["status"] = "failed"
            run_state["outcome"] = "failed"
    elif verification_passed and need_verify:
        try:
            orchestrator = _resolve_orchestrator(state, config)
            if orchestrator and hasattr(orchestrator, "commit_step_transaction"):
                if getattr(orchestrator, "_step_snapshot_id", None):
                    orchestrator.commit_step_transaction()
        except Exception as _commit_err:
            logger.debug("verification_node: step commit (non-fatal): %s", _commit_err)

    commands_executed: list[str] = []
    evidence_items: list[dict[str, Any]] = []
    for k, v in results.items():
        if isinstance(v, dict):
            cmd = v.get("command") or k
            commands_executed.append(str(cmd))
            evidence_items.append(
                {
                    "check": k,
                    "status": v.get("status"),
                    "details": v.get("stdout") or v.get("error"),
                }
            )

    verification_state = {
        "required": bool(need_verify),
        "passed": bool(verification_passed),
        "commands": commands_executed,
        "evidence": evidence_items,
    }

    if verification_passed:
        run_state["status"] = "completed"
        run_state["outcome"] = "complete"
    elif not need_verify:
        if prev_recoveries < recovery_limit:
            run_state["status"] = "recovering"
            run_state["outcome"] = "recover"
        else:
            run_state["status"] = "failed"
            run_state["outcome"] = "failed"

    out: Dict[str, Any] = {
        "verification_result": results,
        "verification_passed": verification_passed,
        "verification": verification_state,
        "run": run_state,
    }
    if is_canonical_run:
        if updated_history is not None:
            out["history"] = updated_history
        if verification_passed and current_hashes:
            out["_verified_file_hashes"] = {**verified_hashes, **current_hashes}
    return out
