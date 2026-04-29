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


# tiny debug helper — only active when CODING_AGENT_DEBUG env var is set
def _dbg(msg: str) -> None:
    if os.getenv("CODING_AGENT_DEBUG"):
        try:
            print(msg, flush=True)
        except Exception:
            pass
        try:
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            p = os.path.join(root, "tmp_debug_main.log")
            with open(p, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except Exception:
            pass


# Ensure project root is on sys.path when executed as script
if __package__ is None:
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _root not in sys.path:
        sys.path.insert(0, _root)
        _dbg(f"[src.main] Inserted project root into sys.path: {_root}")


def _parse_args(argv: list) -> argparse.Namespace:
    import argparse

    parser = argparse.ArgumentParser(
        prog="codingagent",
        description="CodingAgent — AI coding assistant",
        add_help=True,
    )

    subparsers = parser.add_subparsers(dest="subcommand")

    # TASK-10: `init` subcommand — scaffold .agent/ workspace
    init_parser = subparsers.add_parser(
        "init",
        help="Scaffold a .codingAgent/ workspace directory with config, hooks, and AGENT.md.",
    )
    init_parser.add_argument(
        "--dir",
        metavar="DIR",
        default=None,
        help="Target directory to initialise (default: cwd).",
    )
    init_parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Overwrite existing files.",
    )

    # TASK-22: `system-prompt` debug subcommand
    sp_parser = subparsers.add_parser(
        "system-prompt",
        help="Print the resolved system prompt for a given working directory and exit.",
    )
    sp_parser.add_argument(
        "--dir",
        metavar="DIR",
        default=None,
        help="Working directory to resolve prompt for (default: cwd).",
    )
    sp_parser.add_argument(
        "--role",
        metavar="ROLE",
        default="operational",
        help="Role name to resolve (default: operational).",
    )

    # Global (non-subcommand) flags
    parser.add_argument(
        "--output-format",
        choices=["pretty", "json", "raw"],
        default="pretty",
        help=(
            "Output format: pretty (default TUI), json (structured JSON per turn), "
            "raw (plain assistant text only)."
        ),
    )
    parser.add_argument(
        "--task",
        metavar="TASK",
        help="Run a single task non-interactively and exit.",
    )
    parser.add_argument(
        "--workdir",
        metavar="DIR",
        help="Working directory (default: cwd).",
    )
    parser.add_argument(
        "--sandbox-level",
        choices=["off", "workspace", "full"],
        help="Bash sandbox strictness (overrides CODINGAGENT_SANDBOX_LEVEL env var).",
    )
    parser.add_argument(
        "--autonomous",
        action="store_true",
        default=False,
        help=(
            "Run in autonomous mode: DANGER and PROMPT-level tools execute without "
            "interactive user approval."
        ),
    )
    # TASK-20: permission-mode flag
    parser.add_argument(
        "--permission-mode",
        metavar="MODE",
        default=None,
        choices=["read_only", "workspace_write", "danger", "prompt", "allow"],
        help=(
            "Active permission mode: read_only, workspace_write, danger, prompt, or allow. "
            "Overrides the default WORKSPACE_WRITE mode."
        ),
    )
    # TASK-09: Tool permission filters (direct port of claw permissions.py)
    parser.add_argument(
        "--allowed-tools",
        metavar="TOOL",
        nargs="+",
        default=None,
        help=(
            "Allowlist: only these tool names are available to the agent. "
            "All other tools are blocked."
        ),
    )
    parser.add_argument(
        "--deny-tool",
        metavar="TOOL",
        nargs="+",
        default=None,
        help="Block specific tool names.",
    )
    parser.add_argument(
        "--deny-prefix",
        metavar="PREFIX",
        nargs="+",
        default=None,
        help="Block any tool whose name starts with PREFIX (case-insensitive).",
    )
    # UX-2: --validate-config — validate .agent/settings.json without running the agent
    parser.add_argument(
        "--validate-config",
        action="store_true",
        default=False,
        help=(
            "Validate .agent/settings.json (and settings.local.json) for the "
            "working directory and print a summary.  Exits 0 on success, 1 on error."
        ),
    )
    # UX-3: --dry-run — show plan + affected files without executing writes or bash
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help=(
            "Preview the plan and affected files without executing any write or "
            "destructive tools.  Read-only tools still run normally."
        ),
    )
    # CP-14: --resume-session — resume from a previous session snapshot
    parser.add_argument(
        "--resume-session",
        metavar="SESSION_ID",
        default=None,
        help=(
            "Resume from a previous session by its session_id or path to session file. "
            "Loads the persisted message history and continues the task."
        ),
    )
    # v2 Phase 2: --thinking flag for thinking mode control
    parser.add_argument(
        "--thinking",
        metavar="MODE",
        default=None,
        choices=["auto", "on", "off"],
        help=(
            "Control thinking/reasoning mode: auto (default, on for reasoning models), "
            "on (always enable), off (disable for small models). "
            "For models like Qwen3 that emit <think> blocks."
        ),
    )
    # Allow unknown flags so future args don't break older wrappers
    known, _ = parser.parse_known_args(argv)
    return known


def _run_init(target_dir: Optional[str], force: bool) -> int:
    """TASK-10: Scaffold a .codingAgent/ workspace directory.

    Creates the following layout inside *target_dir* (defaults to cwd):

        .codingAgent/
            AGENT.md            — blank project-level instructions file
            config.json         — default agent configuration
            hooks/
                pre_tool.sh     — empty pre-tool hook stub (chmod +x)
                post_tool.sh    — empty post-tool hook stub (chmod +x)
            .gitignore          — ignore runtime artefacts

    Mirrors claw's ``WorkspaceSetup.startup_steps()`` concept but runs once at
    ``codingagent init`` rather than at every session start.
    """
    import json as _json
    import stat as _stat

    from pathlib import Path as _Path

    root = _Path(target_dir).resolve() if target_dir else _Path.cwd()
    try:
        from src.tools.tools_config import get_context_dir_name

        ctx_name = get_context_dir_name()
    except Exception:
        ctx_name = ".codingAgent"

    agent_dir = root / ctx_name

    _dbg(f"[src.main] init: scaffolding {agent_dir}")

    # ------------------------------------------------------------------ layout
    _AGENT_MD = """\
# AGENT.md — Project-level instructions for CodingAgent

Add project-specific context, conventions, and constraints here.
The agent reads this file at the start of every session.
"""

    _CONFIG_JSON: dict = {
        "version": 1,
        "autonomous": False,
        "permission_mode": "workspace_write",
        "max_turns": 50,
        "compact_token_threshold": 6000,
        "sandbox_level": "workspace",
    }

    _PRE_TOOL_SH = """\
#!/usr/bin/env bash
# pre_tool.sh — runs before every tool call
# Environment variables set by CodingAgent:
#   TOOL_NAME   — name of the tool about to execute
#   TOOL_ARGS   — JSON-encoded tool arguments
#
# Exit non-zero to abort the tool call.
exit 0
"""

    _POST_TOOL_SH = """\
#!/usr/bin/env bash
# post_tool.sh — runs after every tool call
# Environment variables set by CodingAgent:
#   TOOL_NAME   — name of the tool that was executed
#   TOOL_RESULT — JSON-encoded tool result
exit 0
"""

    _GITIGNORE = """\
# CodingAgent runtime artefacts — do not commit
*.log
__pycache__/
"""

    files: list[tuple[_Path, str]] = [
        (agent_dir / "AGENT.md", _AGENT_MD),
        (agent_dir / "config.json", _json.dumps(_CONFIG_JSON, indent=2)),
        (agent_dir / "hooks" / "pre_tool.sh", _PRE_TOOL_SH),
        (agent_dir / "hooks" / "post_tool.sh", _POST_TOOL_SH),
        (agent_dir / ".gitignore", _GITIGNORE),
    ]

    created: list[str] = []
    skipped: list[str] = []

    try:
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "hooks").mkdir(exist_ok=True)

        for fpath, content in files:
            if fpath.exists() and not force:
                skipped.append(str(fpath.relative_to(root)))
                continue
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text(content, encoding="utf-8")
            # Make shell hooks executable
            if fpath.suffix == ".sh":
                current = fpath.stat().st_mode
                fpath.chmod(current | _stat.S_IXUSR | _stat.S_IXGRP | _stat.S_IXOTH)
            created.append(str(fpath.relative_to(root)))

        print(f"Initialised .codingAgent/ workspace at {root}")
        if created:
            print("Created:")
            for f in created:
                print(f"  {f}")
        if skipped:
            print("Skipped (already exist — use --force to overwrite):")
            for f in skipped:
                print(f"  {f}")
        return 0
    except Exception as exc:
        print(f"Error during init: {exc}", file=sys.stderr)
        return 1


def _run_validate_config(workdir: Optional[str]) -> int:
    """UX-2: Validate .codingAgent/settings.json for the working directory.

    Loads and parses both settings files, reports recognised keys and their
    resolved values, flags unknown keys, and exits 0 on success or 1 if the
    file cannot be parsed at all.
    """
    import json as _json
    from pathlib import Path as _Path

    base = _Path(workdir).resolve() if workdir else _Path.cwd()
    try:
        from src.tools.tools_config import get_context_dir_name

        ctx_name = get_context_dir_name()
    except Exception:
        import os

        ctx_name = os.getenv("CODINGAGENT_CONTEXT_DIR") or ".codingAgent"

    # Build a list of candidate context directories to search. Prefer the
    # configured directory, but allow legacy names so this command works in
    # older workspaces created before the canonical name change.
    candidates = [base / ctx_name, base / ".agent-context", base / ".agent"]

    settings_files = []
    for cand in candidates:
        settings_files.append(cand / "settings.json")
        settings_files.append(cand / "settings.local.json")

    any_found = False
    any_error = False

    for sf in settings_files:
        if not sf.exists():
            continue
        any_found = True
        rel = sf.relative_to(base)
        print(f"\n{rel}")
        print("-" * len(str(rel)))
        try:
            raw = sf.read_text(encoding="utf-8")
            data = _json.loads(raw)
        except Exception as exc:
            print(f"  ERROR: could not parse JSON — {exc}", file=sys.stderr)
            any_error = True
            continue

        if not isinstance(data, dict):
            print(
                f"  ERROR: expected a JSON object, got {type(data).__name__}",
                file=sys.stderr,
            )
            any_error = True
            continue

        _KNOWN_KEYS = {
            "model",
            "permissionMode",
            "permission_mode",
            "maxTurns",
            "max_turns",
            "budgetCeiling",
            "budget_ceiling_usd",
            "hooks",
            "mcpServers",
            "enableSemanticEvaluation",
            "enable_semantic_evaluation",
            "maxLlmWaitSeconds",
            "max_llm_wait_seconds",
        }
        for k, v in data.items():
            tag = "" if k in _KNOWN_KEYS else "  [unknown key]"
            print(f"  {k}: {_json.dumps(v)}{tag}")

    if not any_found:
        print(f"No settings files found under {base / ctx_name}")
        print("Run 'codingagent init' to create a workspace.")
        return 0

    print()
    try:
        from src.core.orchestration.project_settings import load_project_settings

        ps = load_project_settings(workdir)
        print("Resolved settings:")
        print(f"  model                    = {ps.model!r}")
        print(f"  permission_mode          = {ps.permission_mode!r}")
        print(f"  max_turns                = {ps.max_turns!r}")
        print(f"  budget_ceiling_usd       = {ps.budget_ceiling_usd!r}")
        print(f"  enable_semantic_eval     = {ps.enable_semantic_evaluation!r}")
        print(f"  max_llm_wait_seconds     = {ps.max_llm_wait_seconds!r}")
        print(f"  hooks                    = {ps.hooks!r}")
        print(f"  mcp_servers              = {list(ps.mcp_servers.keys())!r}")
    except Exception as exc:
        print(f"ERROR resolving settings: {exc}", file=sys.stderr)
        any_error = True

    return 1 if any_error else 0


def _run_system_prompt(workdir: Optional[str], role: str) -> int:
    """TASK-22: Print the resolved system prompt for a given working dir and exit."""
    try:
        from src.core.context.context_builder import ContextBuilder

        cb = ContextBuilder(working_dir=workdir)
        # Build a minimal prompt with empty conversation/tools to show the system block
        messages = cb.build_prompt(
            role_name=role,
            active_skills=[],
            task_description="<system-prompt debug — no task>",
            tools=[],
            conversation=[],
        )
        # Print only the system message(s)
        for msg in messages:
            if msg.get("role") == "system":
                print(msg["content"])
        return 0
    except Exception as exc:
        print(f"Error resolving system prompt: {exc}", file=sys.stderr)
        return 1


def _run_headless(
    task: str,
    output_format: str,
    workdir: Optional[str],
    dry_run: bool = False,
    resume_session: Optional[str] = None,
) -> int:
    """Run a single task without the TUI and print the result.

    Parameters
    ----------
    task:
        The user task string to pass to the agent.
    output_format:
        One of ``pretty``, ``json``, or ``raw``.
    workdir:
        Optional working directory override.
    dry_run:
        When ``True`` the orchestrator is started in dry-run mode: write and
        destructive tool calls are intercepted and logged as previews instead
        of being executed.
    resume_session:
        Optional session_id or path to session file to resume from.
    """
    import json as _json

    _messages = None
    if resume_session:
        try:
            from pathlib import Path as _Path
            from src.core.orchestration.session_store import load_session, StoredSession

            # Check if it's a file path
            _session_path = _Path(resume_session)
            if _session_path.exists() and _session_path.suffix == ".json":
                _data = _json.loads(_session_path.read_text(encoding="utf-8"))
                _stored = StoredSession(
                    version=int(_data.get("version", 1)),
                    session_id=str(_data.get("session_id") or ""),
                    task_name=str(_data.get("task_name", "")),
                    working_dir=str(_data.get("working_dir", "")),
                    messages=list(_data.get("messages", [])),
                    message_count=int(_data.get("message_count", 0)),
                    turn_count=int(_data.get("turn_count", 0)),
                    input_tokens=int(_data.get("input_tokens", 0)),
                    output_tokens=int(_data.get("output_tokens", 0)),
                    created_at=str(_data.get("created_at", "")),
                )
                _messages = _stored.messages
                print(
                    f"[SESSION] Resumed session {_stored.session_id}", file=sys.stderr
                )
            else:
                # Try loading by session_id
                _stored = load_session(resume_session)
                if _stored:
                    _messages = _stored.messages
                    print(
                        f"[SESSION] Resumed session {_stored.session_id}",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"[SESSION] Session '{resume_session}' not found",
                        file=sys.stderr,
                    )
        except Exception as _sess_err:
            print(f"[SESSION] Failed to load session: {_sess_err}", file=sys.stderr)

    try:
        from src.core.orchestration.orchestrator import Orchestrator

        orch = Orchestrator(working_dir=workdir, dry_run=dry_run)
        result = orch.run_agent_once(
            system_prompt_name="operational",
            messages=_messages if _messages else [{"role": "user", "content": task}],
            tools={},
        )

        if dry_run and output_format not in ("json",):
            dry_calls = result.get("dry_run_intercepted", [])
            if dry_calls:
                print("[DRY RUN] The following write/destructive tool calls were")
                print("[DRY RUN] intercepted and NOT executed:\n")
                for entry in dry_calls:
                    print(
                        f"  {entry.get('would_call', entry.get('tool', '?'))}"
                        f"({_json.dumps(entry.get('args', {}), ensure_ascii=False)})"
                    )
                print()

        if output_format == "json":
            print(_json.dumps(result, ensure_ascii=False, default=str))
        elif output_format == "raw":
            print(result.get("assistant_message", ""))
        else:
            # pretty
            msg = result.get("assistant_message", "")
            summary = result.get("work_summary", "")
            print(msg)
            if summary:
                print("\n---")
                print(summary)
        return 0
    except Exception as exc:
        _dbg(f"[src.main] headless run failed: {exc}")
        if output_format == "json":
            import json as _json

            print(_json.dumps({"error": str(exc)}))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1


def main(argv: Optional[list] = None) -> int:
    argv = argv or sys.argv[1:]
    _dbg(f"[src.main] main() starting; argv={argv}")

    args = _parse_args(argv)

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

    # AUTO-01: Apply autonomous mode flag
    if args.autonomous:
        try:
            from src.tools.tools_config import set_autonomous

            set_autonomous(True)
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

    # CP-13 / CP-8: Load per-project settings and apply them (project settings
    # are applied *before* CLI flags so that CLI always wins).
    try:
        from src.core.orchestration.project_settings import load_project_settings

        _workdir_for_ps = getattr(args, "workdir", None)
        _ps = load_project_settings(_workdir_for_ps)

        # CP-8: apply permissionMode from project settings (CLI flag overrides below)
        if _ps.permission_mode and not getattr(args, "permission_mode", None):
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

    # Non-interactive (headless) mode when --task is supplied or output format is not pretty
    if args.task or args.output_format in ("json", "raw"):
        task = args.task or ""
        if not task:
            task = sys.stdin.read().strip()
        return _run_headless(
            task,
            args.output_format,
            args.workdir,
            dry_run=getattr(args, "dry_run", False),
            resume_session=getattr(args, "resume_session", None),
        )

    try:
        import sys as _sys
        from pathlib import Path as _Path

        _working_dir = _Path(args.workdir) if args.workdir else None

        # TUI-01: Launch the new Textual TUI (tui/src/ui/app.py::AgentApp).
        # src/ui/ has been retired (LEGACY-03); only the new TUI is supported.
        #
        # The tui/ package's internal imports use bare `from src.ui.xxx` paths,
        # which require tui/ to be on sys.path AND for `src` in sys.modules to
        # resolve to tui/src/ rather than the project-root src/ package.
        # We temporarily redirect sys.modules['src'] to tui/src/ for the import,
        # then restore it so the rest of the project keeps working.
        import importlib.util as _ilu

        _tui_root = str(_Path(__file__).parent.parent / "tui")
        _proj_root = str(_Path(__file__).parent.parent)
        if _tui_root not in _sys.path:
            _sys.path.insert(0, _tui_root)

        # Load tui/src as a temporary module to shadow the project-root src package
        _tui_src_init = _Path(_tui_root) / "src" / "__init__.py"
        _tui_src_spec = _ilu.spec_from_file_location(
            "src",
            str(_tui_src_init),
            submodule_search_locations=[str(_Path(_tui_root) / "src")],
        )
        if _tui_src_spec is None or _tui_src_spec.loader is None:
            raise ImportError(f"Cannot load TUI src spec from {_tui_src_init}")
        _tui_src_mod = _ilu.module_from_spec(_tui_src_spec)
        _tui_src_spec.loader.exec_module(_tui_src_mod)  # type: ignore[union-attr]

        _saved_src = _sys.modules.get("src")
        _sys.modules["src"] = _tui_src_mod
        try:
            from src.ui.app import AgentApp  # type: ignore[import]
            from src.ui.core_bridge import AgentBridge  # type: ignore[import]  # noqa: F401
        finally:
            # Restore the project-root src package.
            # IMPORTANT: also remove _tui_root from sys.path so that runtime
            # imports inside the TUI (e.g. `from src.core.inference...`) resolve
            # to the project-root src/ package, not tui/src/.  The TUI's own
            # src.ui.* modules are already in sys.modules at this point.
            if _saved_src is not None:
                _sys.modules["src"] = _saved_src
            elif "src" in _sys.modules:
                del _sys.modules["src"]
            try:
                _sys.path.remove(_tui_root)
            except ValueError:
                pass
            # Ensure project root is on sys.path for src.core.* imports
            if _proj_root not in _sys.path:
                _sys.path.insert(0, _proj_root)

        app = AgentApp()
        # Inject working_dir into the bridge after the app creates it,
        # or pass it in before run() so the bridge constructor picks it up.
        # AgentApp creates its bridge in on_mount; pass via class attribute.
        if _working_dir is not None:
            app._initial_working_dir = _working_dir  # type: ignore[attr-defined]
        _dbg("[src.main] Delegating startup to AgentApp (new TUI)")
        app.run()
        _dbg("[src.main] AgentApp.run() returned")
        return 0
    except Exception as e:
        _dbg(f"[src.main] Failed to start app: {e}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
