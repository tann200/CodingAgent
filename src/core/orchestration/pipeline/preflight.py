"""Tool execution pipeline — Stage 1: Preflight, lookup, syntax sandbox & snapshots."""

from __future__ import annotations

import ast
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Collection, Dict, Optional

from src.core.orchestration.loop_guards import format_unknown_tool_error
from src.core.orchestration.pipeline.permissions import (
    _ACTIVE_AGENT_CTX,
    _check_agent_permission_override,
    get_effective_workdir,
)
from src.core.orchestration.tool_constants import _write_permission_audit

logger = logging.getLogger("src.core.orchestration.tool_execution_pipeline")

try:
    from src.core.logger import logger as guilogger  # type: ignore[assignment]
except Exception:
    guilogger = logger  # type: ignore[assignment]

_PATH_ARG_KEYS = ("path", "file_path", "src_path", "src", "old_path", "dir_path", "directory")


def _extract_path_arg(args: dict) -> Optional[str]:
    """Extract the primary path argument from a tool's args dict."""
    for key in _PATH_ARG_KEYS:
        val = args.get(key)
        if val:
            return val
    return None


def _check_read_before_write(
    orch: Any,
    name: str,
    args: dict,
    workspace_guard_tools: Collection[str],
) -> Optional[Dict[str, Any]]:
    """Enforce read-before-write for write tools."""
    if name not in workspace_guard_tools:
        return None
    path_arg = _extract_path_arg(args)
    if not path_arg:
        return None

    try:
        eff_wd = get_effective_workdir(orch) or Path(".")
        target = Path(eff_wd) / path_arg
        resolved_path = str(target.resolve())
        from src.tools.guardrails import _get_ctx_read_files, _global_lock, _global_read_files

        with _global_lock:
            in_guardrails = (resolved_path in _global_read_files) or (resolved_path in _get_ctx_read_files())
        if in_guardrails:
            orch._session_read_files.add(resolved_path)

        if target.exists() and resolved_path not in orch._session_read_files:
            from src.core.orchestration.tool_output_reducer import is_surgical_preview_edit_allowed
            if is_surgical_preview_edit_allowed(orch, name, args, resolved_path):
                return None
            return {
                "ok": False,
                "error": (
                    f"Security/Logic violation: You must read '{path_arg}' "
                    "before writing to it. Use read_file first to inspect the current content."
                ),
            }
    except Exception as exc:
        logger.error("read-before-write security check failed for %r: %s", name, exc)
        return {"ok": False, "error": f"Security check (read-before-write) failed closed: {exc}"}
    return None


def _check_workspace_scope_guard(
    orch: Any,
    name: str,
    args: dict,
    workspace_guard_tools: Collection[str],
) -> Optional[Dict[str, Any]]:
    """Block writes to files outside the plan's affected_files set."""
    if name not in workspace_guard_tools:
        return None
    path_arg = _extract_path_arg(args)
    if not path_arg:
        return None

    try:
        affected_files = getattr(orch, "_affected_files", None) or []
        if not affected_files:
            return None

        workdir = str(get_effective_workdir(orch) or ".").rstrip("/\\")

        def _norm(p: str) -> str:
            p = str(p).replace("\\", "/")
            p = str((Path(workdir) / p).resolve())
            if p.startswith(workdir + "/"):
                p = p[len(workdir) + 1 :]
            elif p.lstrip("/\\").startswith(workdir.lstrip("/\\") + "/"):
                stripped = p.lstrip("/\\")
                p = stripped[len(workdir.lstrip("/\\")) + 1 :]
            return p.lstrip("/\\")

        target_norm = _norm(path_arg)
        affected_set = {_norm(str(item)) for item in affected_files}
        if target_norm not in affected_set:
            try:
                from src.core.orchestration.event_bus import get_event_bus

                get_event_bus().publish(
                    "scope.violation",
                    {"tool": name, "path": path_arg, "allowed": list(affected_set)},
                )
            except Exception:
                pass
            return {
                "ok": False,
                "error": (
                    f"File '{path_arg}' is outside the task scope. "
                    f"The current plan authorises writes to: {sorted(affected_set)}. "
                    "Use ask_user to confirm expanding the scope."
                ),
            }
    except Exception as exc:
        logger.error("workspace scope security check failed for %r: %s", name, exc)
        return {
            "ok": False,
            "error": f"Security check (workspace-scope) failed closed: {exc}",
        }
    return None


def _run_preflight_and_lookup(
    orch: Any,
    name: str,
    args: dict,
    tool_call: Dict[str, Any],
    tool_call_id: str,
) -> tuple[Optional[Dict[str, Any]], Any]:
    """Run preflight check, tool registry lookup, and agent/role tool restrictions."""
    preflight: Any = {"ok": True}
    if hasattr(orch, "preflight_check"):
        try:
            preflight = orch.preflight_check(tool_call)
        except Exception as pf_exc:
            return {
                "ok": False,
                "error": f"Preflight check failed closed for '{name}': {pf_exc}",
            }, None
    if isinstance(preflight, dict) and not preflight.get("ok", True):
        if preflight.get("error") == "tool_not_found":
            return {
                "ok": False,
                "error": format_unknown_tool_error(name, orch),
                "details": preflight.get("message"),
                "suggestions": preflight.get("suggestions", []),
            }, None
        return {
            "ok": False,
            "error": preflight.get("message") or preflight.get("error") or "preflight_failed",
        }, None

    if isinstance(preflight, dict):
        corrected = preflight.get("corrected_name")
        if isinstance(corrected, str) and corrected and corrected != name:
            try:
                from src.tools.tools_config import resolve_tool_alias

                corrected = resolve_tool_alias(corrected)
            except Exception:
                pass
            name = corrected
            tool_call["_resolved_name"] = corrected

    tool = orch.tool_registry.get(name)
    if not tool:
        return {"ok": False, "error": format_unknown_tool_error(name, orch)}, None

    active_agent = None
    try:
        active_agent = _ACTIVE_AGENT_CTX.get() or getattr(orch, "active_agent", None)
        if active_agent is not None and not active_agent.is_tool_permitted(name):
            _write_permission_audit(orch.working_dir, name, args, "deny", "spawn_allowed_tools")
            return {
                "ok": False,
                "error": (
                    f"Tool '{name}' is not permitted for the active delegated agent "
                    "(allowed_tools restriction). Check the agent's allowed_tools list."
                ),
            }, None
    except Exception as agent_exc:
        return {
            "ok": False,
            "error": f"Delegated agent tool restriction check failed closed for '{name}': {agent_exc}",
        }, None

    try:
        role_manager = getattr(orch, "role_manager", None)
        if role_manager is not None and active_agent is None:
            current_role = role_manager.get_current_role()
            if current_role is not None:
                from src.core.orchestration.role_config import is_tool_allowed_for_role

                if not is_tool_allowed_for_role(name, current_role):
                    _write_permission_audit(orch.working_dir, name, args, "deny", "role_tool_restriction")
                    return {
                        "ok": False,
                        "error": (
                            f"Tool '{name}' is not permitted for role '{current_role}'. "
                            f"Role '{current_role}' has structural tool restrictions — "
                            "check the role's allowed_tools / denied_tools config."
                        ),
                    }, None
    except Exception as role_exc:
        return {
            "ok": False,
            "error": f"Role tool restriction check failed closed for '{name}': {role_exc}",
        }, None

    agent_override = _check_agent_permission_override(orch, name, args)
    if agent_override is not None:
        return agent_override, None

    return None, tool


def _compute_candidate_content(orch: Any, name: str, args: dict, path_arg: str) -> Optional[str]:
    """Compute candidate replacement content for edit tools when ``content`` is absent."""
    new_content = args.get("content")
    if new_content or name not in ("edit_file_atomic", "edit_code_block", "multiedit"):
        return new_content if isinstance(new_content, str) else None
    try:
        target_p = Path(path_arg)
        if not target_p.is_absolute() and getattr(orch, "working_dir", None):
            target_p = Path(orch.working_dir) / target_p
        if not target_p.is_file():
            return None
        try:
            orig_text = target_p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            orig_text = target_p.read_text(encoding="utf-8", errors="replace")

        if name == "edit_file_atomic":
            old_s, new_s = args.get("old_string"), args.get("new_string")
            if isinstance(old_s, str) and isinstance(new_s, str) and old_s in orig_text:
                return orig_text.replace(old_s, new_s, 1)
        elif name == "edit_code_block":
            old_s, new_s = args.get("block_to_find"), args.get("new_block")
            if isinstance(old_s, str) and isinstance(new_s, str) and old_s in orig_text:
                return orig_text.replace(old_s, new_s, 1)
        elif name == "multiedit" and isinstance(args.get("edits"), list):
            curr = orig_text
            for ed in args["edits"]:
                if isinstance(ed, dict):
                    old_s, new_s = ed.get("old_string"), ed.get("new_string")
                    if isinstance(old_s, str) and isinstance(new_s, str) and old_s in curr:
                        curr = curr.replace(old_s, new_s, 1)
            return curr
    except Exception:
        pass
    return None


def _validate_candidate_syntax(orch: Any, path_arg: str, new_content: str) -> Optional[Dict[str, Any]]:
    """Run pre-write syntax checks for Python, JS, and TypeScript candidates."""
    p_str = str(path_arg)
    if p_str.endswith(".py"):
        try:
            ast.parse(new_content)
        except SyntaxError as syn:
            hint = ""
            if "line continuation" in str(syn.msg or "").lower():
                hint = " (Hint: pass real newline characters, not literal backslash-n '\\n' sequences.)"
            return {
                "ok": False,
                "error": f"Sandbox validation error: new content has a syntax error at line {syn.lineno}: {syn.msg}{hint}",
            }
        return None

    if p_str.endswith((".js", ".mjs", ".cjs")):
        from src.tools.sandbox import run_sandboxed
        tmp_path = None
        try:
            wd = Path(getattr(orch, "working_dir", None) or ".")
            if run_sandboxed(["node", "--version"], cwd=wd, capture_output=True, timeout=3, network=False).returncode == 0:
                with tempfile.NamedTemporaryFile(suffix=".js", mode="w", delete=False, encoding="utf-8") as tmp:
                    tmp.write(new_content)
                    tmp_path = tmp.name
                check = run_sandboxed(["node", "--check", tmp_path], cwd=Path(tmp_path).parent, capture_output=True, text=True, timeout=10, network=False)
                if check.returncode != 0:
                    return {"ok": False, "error": f"Sandbox validation error: JS syntax error: {(check.stderr or check.stdout or 'syntax error').strip()[:300]}"}
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
        return None

    if p_str.endswith((".ts", ".tsx")):
        from src.tools.sandbox import run_sandboxed

        tsc_bin = shutil.which("tsc")
        if not tsc_bin:
            return None
        tmp_path = None
        try:
            ext = ".tsx" if p_str.endswith(".tsx") else ".ts"
            with tempfile.NamedTemporaryFile(suffix=ext, mode="w", delete=False, encoding="utf-8") as tmp:
                tmp.write(new_content)
                tmp_path = tmp.name
            check = run_sandboxed(
                [tsc_bin, "--noEmit", "--allowJs", "--target", "es2020", tmp_path],
                cwd=Path(tmp_path).parent, capture_output=True, text=True, timeout=10, network=False,
            )
            if check.returncode != 0:
                err = (check.stdout or check.stderr or "syntax error").strip()
                if "error TS" in err:
                    return {"ok": False, "error": f"Sandbox validation error: TS syntax error: {err[:300]}"}
        except Exception:
            pass
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
    return None


def _run_sandbox_and_snapshot(
    orch: Any, name: str, args: dict, tool: Any, path_arg: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Validate candidate content (sandbox) and take a rollback snapshot for write tools."""
    try:
        if "write" in tool.get("side_effects", []) and path_arg:
            new_content = _compute_candidate_content(orch, name, args, path_arg)
            if new_content:
                syntax_err = _validate_candidate_syntax(orch, path_arg, new_content)
                if syntax_err is not None:
                    return syntax_err
                try:
                    from src.tools._lint_verify import mark_content_prevalidated

                    target_p = Path(path_arg)
                    if not target_p.is_absolute() and getattr(orch, "working_dir", None):
                        target_p = Path(orch.working_dir) / target_p
                    mark_content_prevalidated(target_p, new_content)
                except Exception:
                    pass
    except Exception as exc:
        guilogger.error(f"Sandbox validation failed (fail-closed): {exc}")
        return {"ok": False, "error": f"Sandbox validation aborted: {exc}. Write operation blocked for safety."}

    from src.tools.rollback_tools import WRITE_TOOL_NAMES

    paths_to_snap: list[str] = [str(path_arg)] if path_arg else []
    for key in ("paths", "files", "target_files", "file_paths"):
        val = args.get(key)
        if isinstance(val, (list, tuple)):
            for item in val:
                if isinstance(item, str) and item not in paths_to_snap:
                    paths_to_snap.append(item)
                elif isinstance(item, dict) and "path" in item and str(item["path"]) not in paths_to_snap:
                    paths_to_snap.append(str(item["path"]))
    dst_arg = args.get("new_path") or args.get("dst_path") or args.get("dst") or args.get("new_name")
    if dst_arg and str(dst_arg) not in paths_to_snap:
        paths_to_snap.append(str(dst_arg))

    if ("write" in tool.get("side_effects", []) or name in WRITE_TOOL_NAMES) and paths_to_snap:
        try:
            if getattr(orch, "_step_snapshot_id", None):
                for p_snap in paths_to_snap:
                    orch.rollback_manager.append_to_snapshot(orch._step_snapshot_id, p_snap)
                orch._current_snapshot_id = orch._step_snapshot_id
            else:
                orch._current_snapshot_id = orch.rollback_manager.snapshot_files(paths_to_snap, snapshot_id=None)
        except Exception as snap_err:
            guilogger.warning(f"Snapshot failed (non-blocking): {snap_err}")

    try:
        from src.core.orchestration.change_log import begin_change
        begin_change(orch, name, args, orch.working_dir)
    except Exception as exc:
        guilogger.debug(f"change_log: begin_change skipped (non-blocking): {exc}")
    return None
