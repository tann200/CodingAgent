"""Workspace initialization, config validation, and system-prompt CLI helpers."""

from __future__ import annotations

import sys
from typing import Callable, Optional


def run_init(target_dir: Optional[str], force: bool, *, dbg_fn: Optional[Callable[[str], None]] = None) -> int:
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

    if dbg_fn is not None:
        dbg_fn(f"[src.main] init: scaffolding {agent_dir}")

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
#   TOOL_NAME        — name of the tool about to execute
#   TOOL_ARGS_JSON   — JSON-encoded tool arguments
#
# Exit non-zero to abort the tool call.
exit 0
"""

    _POST_TOOL_SH = """\
#!/usr/bin/env bash
# post_tool.sh — runs after every tool call
# Environment variables set by CodingAgent:
#   TOOL_NAME        — name of the tool that was executed
#   TOOL_RESULT_JSON — JSON-encoded tool result
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


def run_validate_config(workdir: Optional[str]) -> int:
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

    # Only the canonical .codingAgent directory is used; legacy dirs are no longer created.
    candidates = [base / ctx_name]

    settings_files = []
    for cand in candidates:
        settings_files.append(cand / "config.json")
        settings_files.append(cand / "config.local.json")
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
            "version",
            "autonomous",
            "model",
            "permissionMode",
            "permission_mode",
            "maxTurns",
            "max_turns",
            "compact_token_threshold",
            "sandbox_level",
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


def run_system_prompt(workdir: Optional[str], role: str) -> int:
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

