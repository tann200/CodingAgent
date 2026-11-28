"""Application entrypoint moved into src package.

This module now delegates entirely to `src.ui.app.CodingAgentApp` which
is responsible for choosing Textual vs headless behavior. This centralizes
startup logic and avoids duplicated Textual detection.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Optional

# Ensure project root is on sys.path when executed as script before src.* imports.
if __package__ is None:
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _root not in sys.path:
        sys.path.insert(0, _root)

from src.cli.arg_parser import parse_args as _parse_args_impl  # noqa: E402
from src.cli.headless_runner import (  # noqa: E402
    flatten_content as _flatten_content,  # noqa: F401
    load_last_user_task as _load_last_user_task_impl,
    run_headless as _run_headless_impl,
)
from src.cli.init_cmd import (  # noqa: E402
    run_init as _run_init_impl,
    run_system_prompt as _run_system_prompt,
    run_validate_config as _run_validate_config,
)


# tiny debug helper — only active when CODINGAGENT_DEBUG env var is set
# CODING_AGENT_DEBUG is the legacy name kept for backward compatibility.
def _dbg(msg: str) -> None:
    if os.getenv("CODINGAGENT_DEBUG") or os.getenv("CODING_AGENT_DEBUG"):
        try:
            print(msg, flush=True)
        except Exception:
            pass
        try:
            import tempfile
            p = os.path.join(tempfile.gettempdir(), "codingagent_debug_main.log")
            with open(p, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except Exception:
            pass


def _err(msg: str) -> None:
    """Print *msg* to stderr unconditionally (startup failures must be visible)."""
    try:
        print(f"[codingagent] {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


def _check_tui_imports() -> Optional[str]:
    """Verify TUI module imports before attempting to start the app.

    Returns an error message string if a known import-time issue is detected,
    or None if everything looks healthy.
    """
    try:
        # Check that textual.app.App metaclass is compatible with mixin protocols.
        # AgentAppProtocol intentionally avoids inheriting from typing.Protocol
        # because _ProtocolMeta conflicts with App's _MessagePumpMeta metaclass.
        # If someone accidentally adds `Protocol` back, catch it early.
        from textual.app import App
        app_meta = type(App)

        from tui.tui_src.ui.components.status_bar import StatusBarMixin
        mixin_meta = type(StatusBarMixin)

        if not issubclass(mixin_meta, app_meta) and not issubclass(app_meta, mixin_meta):
            return (
                f"Metaclass conflict: {mixin_meta.__name__} and {app_meta.__name__} "
                f"are incompatible. AgentAppProtocol must NOT inherit from typing.Protocol."
            )
    except Exception:
        pass  # Import checks are best-effort; don't block startup.
    return None


def _parse_args(argv: list) -> argparse.Namespace:
    return _parse_args_impl(argv)


def _run_init(target_dir: Optional[str], force: bool) -> int:
    return _run_init_impl(target_dir=target_dir, force=force, dbg_fn=_dbg)


def _load_last_user_task(workdir: Optional[str]) -> str:
    return _load_last_user_task_impl(workdir, dbg_fn=_dbg)


def _run_headless(
    task: str,
    output_format: str,
    workdir: Optional[str],
    dry_run: bool = False,
    resume_session: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    trusted: Optional[bool] = None,
    plugin_dir: Optional[str] = None,
    private: bool = False,
) -> int:
    return _run_headless_impl(
        task=task,
        output_format=output_format,
        workdir=workdir,
        dry_run=dry_run,
        resume_session=resume_session,
        provider=provider,
        model=model,
        trusted=trusted,
        plugin_dir=plugin_dir,
        private=private,
        sys_mod=sys,
        dbg_fn=_dbg,
    )



def main(argv: Optional[list] = None) -> int:
    argv = argv or sys.argv[1:]
    _dbg(f"[src.main] main() starting; argv={argv}")

    args = _parse_args(argv)

    # Apply --debug flag: set env var so _dbg() activates everywhere
    if getattr(args, "debug", False):
        os.environ["CODINGAGENT_DEBUG"] = "1"

    # Run pre-startup health checks (metaclass compatibility, etc.)
    _check_result = _check_tui_imports()
    if _check_result is not None:
        _err(f"Pre-flight check failed: {_check_result}")

    # TASK-10: Dispatch `init` subcommand
    if getattr(args, "subcommand", None) == "init":
        return _run_init(
            target_dir=getattr(args, "dir", None),
            force=getattr(args, "force", False),
        )

    # TASK-22: Dispatch `system-prompt` subcommand
    if getattr(args, "subcommand", None) == "system-prompt":
        return _run_system_prompt(
            workdir=getattr(args, "dir", None),
            role=getattr(args, "role", "operational"),
        )

    # Phase 3.7: Dispatch `session` subcommand
    if getattr(args, "subcommand", None) == "session":
        from src.cli.session_cmd import run_session

        return run_session(args)

    # Phase 3.7: Dispatch `status` subcommand
    if getattr(args, "subcommand", None) == "status":
        from src.cli.status_cmd import run_status

        return run_status(args)

    # Phase 3.7: Dispatch `mcp` subcommand
    if getattr(args, "subcommand", None) == "mcp":
        from src.cli.mcp_cmd import run_mcp

        return run_mcp(args)

    # Phase 3.7: Dispatch `diff` subcommand
    if getattr(args, "subcommand", None) == "diff":
        from src.cli.diff_cmd import run_diff

        return run_diff(args)

    # Phase 3.8: Dispatch `doctor` subcommand
    if getattr(args, "subcommand", None) == "doctor":
        from src.cli.doctor_cmd import run_doctor

        return run_doctor(args)

    # Phase 3.8: Dispatch `skill` subcommand
    if getattr(args, "subcommand", None) == "skill":
        from src.cli.skill_cmd import run_skill

        return run_skill(args)

    # TASK-28: Dispatch `context-report` subcommand
    if getattr(args, "subcommand", None) == "context-report":
        from src.cli.context_report_cmd import run_context_report

        return run_context_report(args)

    # TASK-28: Dispatch `bench-diff` subcommand
    if getattr(args, "subcommand", None) == "bench-diff":
        from src.core.evaluation.cli import _cmd_bench_diff

        return _cmd_bench_diff(args)

    # TASK-35: Dispatch `local-launch` subcommand
    if getattr(args, "subcommand", None) == "local-launch":
        from src.cli.local_launch_cmd import run_local_launch

        return run_local_launch(args)

    # UX-2: Dispatch --validate-config flag
    if getattr(args, "validate_config", False):
        return _run_validate_config(getattr(args, "workdir", None))

    # Apply sandbox level override from CLI flag
    if args.sandbox_level:
        try:
            from src.tools.sandbox import set_sandbox_level

            set_sandbox_level(args.sandbox_level)
        except Exception:
            pass

    if getattr(args, "sandbox_expose", None):
        existing_expose = os.environ.get("CODINGAGENT_SANDBOX_EXPOSE", "").strip()
        parts = [p for p in existing_expose.split(os.pathsep) if p] if existing_expose else []
        parts.extend(str(p) for p in args.sandbox_expose if p)
        os.environ["CODINGAGENT_SANDBOX_EXPOSE"] = os.pathsep.join(parts)

    # AUTO-01: Apply autonomous mode flag
    if args.autonomous:
        try:
            from src.tools.tools_config import set_autonomous

            set_autonomous(True)
            # HS-1: autonomous approval requires an explicit operator allowlist.
            approve = getattr(args, "autonomous_approve", None)
            if approve:
                from src.tools.tools_config import set_autonomous_approve

                set_autonomous_approve(set(approve))
        except Exception:
            pass

    # TASK-20: Apply --permission-mode flag
    if getattr(args, "permission_mode", None):
        try:
            from src.tools.tools_config import (
                PermissionLevel,
                set_active_permission_mode,
            )

            set_active_permission_mode(PermissionLevel(args.permission_mode))
            _dbg(f"[src.main] Permission mode set to: {args.permission_mode}")
        except Exception as _pm_err:
            _dbg(f"[src.main] Could not apply permission mode: {_pm_err}")

    # Phase 2: Apply --thinking flag
    if getattr(args, "thinking", None):
        try:
            from src.core.inference.thinking_utils import set_active_thinking_mode

            set_active_thinking_mode(args.thinking)
            _dbg(f"[src.main] Thinking mode set to: {args.thinking}")
        except Exception as _th_err:
            _dbg(f"[src.main] Could not apply thinking mode: {_th_err}")

    # TASK-29: Apply --reasoning / --effort flag
    if getattr(args, "reasoning", None):
        try:
            from src.core.inference.thinking_utils import set_active_reasoning_effort

            set_active_reasoning_effort(args.reasoning)
            _dbg(f"[src.main] Reasoning effort set to: {args.reasoning}")
        except Exception as _re_err:
            _dbg(f"[src.main] Could not apply reasoning effort: {_re_err}")

    # CP-13 / CP-8: Load per-project settings and apply them (project settings
    # are applied *before* CLI flags so that CLI always wins).
    try:
        from src.core.orchestration.deferred_init import is_trusted as _is_ws_trusted
        from src.core.orchestration.project_settings import load_project_settings

        _workdir_for_ps = getattr(args, "workdir", None)
        _explicit_trust = getattr(args, "trusted", None)
        _ws_trusted = _explicit_trust if _explicit_trust is not None else _is_ws_trusted()
        _ps = load_project_settings(_workdir_for_ps, trusted=_ws_trusted)

        # CP-8: apply permissionMode from project settings ONLY in trusted workspaces
        # (CLI flag overrides below; untrusted repos must never escalate permissions).
        if _ps.permission_mode and not getattr(args, "permission_mode", None):
            if not _ws_trusted:
                _dbg(
                    "[src.main] Ignoring project permissionMode override from untrusted workspace"
                )
            else:
                from src.tools.tools_config import (
                    PermissionLevel,
                    set_active_permission_mode,
                )

                try:
                    set_active_permission_mode(PermissionLevel(_ps.permission_mode))
                    _dbg(
                        f"[src.main] Project permissionMode applied: {_ps.permission_mode}"
                    )
                except ValueError:
                    _dbg(
                        f"[src.main] Unknown project permissionMode: {_ps.permission_mode}"
                    )

        # CP-13: stash active settings for orchestrator / perception_node
        import src.core.orchestration.project_settings as _psmod

        _psmod._ACTIVE_SETTINGS = _ps  # type: ignore[attr-defined]
        if _ps.model:
            _dbg(f"[src.main] Project model override: {_ps.model}")
        if _ps.max_turns is not None:
            _dbg(f"[src.main] Project maxTurns override: {_ps.max_turns}")
        if _ps.budget_ceiling_usd is not None:
            _dbg(f"[src.main] Project budgetCeiling: ${_ps.budget_ceiling_usd:.4f}")
    except Exception as _ps_err:
        _dbg(f"[src.main] Could not load project settings: {_ps_err}")

    # TASK-09: Build tool permission context from allow/deny CLI flags
    try:
        from src.tools.permission_context import ToolPermissionContext

        _permission_ctx = ToolPermissionContext.from_iterables(
            allow_names=getattr(args, "allowed_tools", None),
            deny_names=getattr(args, "deny_tool", None),
            deny_prefixes=getattr(args, "deny_prefix", None),
        )
        if not _permission_ctx.is_empty():
            # Publish as a module-level singleton so the orchestrator can read it
            import src.tools.permission_context as _pc_mod

            _pc_mod._ACTIVE_CONTEXT = _permission_ctx  # type: ignore[attr-defined]
            _dbg(
                f"[src.main] Tool permission context: allow={_permission_ctx.allow_names}, "
                f"deny={_permission_ctx.deny_names}, prefixes={_permission_ctx.deny_prefixes}"
            )
    except Exception as _pctx_err:
        _dbg(f"[src.main] Could not apply permission context: {_pctx_err}")

    # TASK-36: Apply --private ephemeral mode and --output mapping
    _is_private = bool(getattr(args, "private", False))
    if _is_private:
        os.environ["CODINGAGENT_PRIVATE"] = "1"

    _output_override = getattr(args, "output", None)
    _effective_output_format = args.output_format
    if _output_override == "text":
        _effective_output_format = "raw"
    elif _output_override in ("json", "stream-json"):
        _effective_output_format = _output_override

    # TASK-36: Optional --parallel timestamped git worktree isolation
    _parallel_session = None
    _effective_workdir = args.workdir
    if getattr(args, "parallel", False):
        from src.cli.headless_stream import ParallelWorktreeSession

        try:
            _parallel_session = ParallelWorktreeSession(_effective_workdir or os.getcwd())
            _effective_workdir = str(_parallel_session.create())
            _dbg(f"[src.main] --parallel worktree created at {_effective_workdir}")
        except Exception as _par_err:
            _err(f"--parallel failed: {_par_err}")
            return 1

    try:
        # Non-interactive (headless) mode when --task is supplied, --continue
        # requests a re-run of the last task, or output format is not pretty
        if (
            args.task
            or getattr(args, "continue_task", False)
            or _effective_output_format in ("json", "raw", "stream", "stream-json")
        ):
            task = ""
            if getattr(args, "continue_task", False):
                task = _load_last_user_task(_effective_workdir)
                if not task:
                    _err("--continue: no saved session found to re-run (run a --task first).")
                    return 1
            elif args.task:
                task = args.task
            if not task:
                task = sys.stdin.read().strip()
            return _run_headless(
                task,
                _effective_output_format,
                _effective_workdir,
                dry_run=getattr(args, "dry_run", False),
                resume_session=getattr(args, "resume_session", None),
                provider=getattr(args, "provider", None),
                model=getattr(args, "model", None),
                trusted=getattr(args, "trusted", None),
                plugin_dir=getattr(args, "plugin_dir", None),
                private=_is_private,
            )

        try:
            from pathlib import Path as _Path

            _working_dir = _Path(_effective_workdir) if _effective_workdir else None

            # TUI-01: Launch the Textual TUI (tui/tui_src/ui/app.py::AgentApp).
            # P1-3: tui/src/ was renamed to tui/tui_src/ so it has a unique package
            # name `tui_src` — no sys.path shadowing required.
            from tui.tui_src.ui.app import AgentApp  # type: ignore[import]

            app = AgentApp(
                working_dir=_working_dir,
                trusted=getattr(args, "trusted", None),
                plugin_dir=getattr(args, "plugin_dir", None),
            )
            _dbg("[src.main] Delegating startup to AgentApp (new TUI)")
            app.run()
            _dbg("[src.main] AgentApp.run() returned")
            return 0
        except Exception as e:
            _dbg(f"[src.main] Failed to start app: {e}")
            _err(f"Failed to start TUI: {e}")
            return 2
    finally:
        if _parallel_session is not None:
            try:
                _parallel_session.finalize()
            except Exception as _fin_err:
                _err(f"--parallel finalize error: {_fin_err}")


if __name__ == "__main__":
    raise SystemExit(main())
