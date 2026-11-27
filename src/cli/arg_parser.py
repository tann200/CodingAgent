"""CLI argument parser for ``codingagent`` (extracted from ``src/main.py``)."""

from __future__ import annotations

import argparse


def parse_args(argv: list) -> argparse.Namespace:
    import argparse

    parser = argparse.ArgumentParser(
        prog="codingagent",
        description="CodingAgent — AI coding assistant",
        add_help=True,
        allow_abbrev=False,
    )

    subparsers = parser.add_subparsers(dest="subcommand")

    # TASK-10: `init` subcommand — scaffold .codingAgent/ workspace
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

    # Phase 3.7: `session` subcommand — list / show / export session snapshots
    session_parser = subparsers.add_parser(
        "session",
        help="List, show, or export persisted session snapshots (headless /sessions, /timeline, /share).",
    )
    session_sub = session_parser.add_subparsers(dest="session_action")
    session_list = session_sub.add_parser("list", help="List saved sessions (most recent first).")
    session_list.add_argument("--limit", type=int, default=50)
    session_list.add_argument("--json", action="store_true")
    session_show = session_sub.add_parser("show", help="Show a session's message timeline.")
    session_show.add_argument("session_id")
    session_show.add_argument("--json", action="store_true")
    session_export = session_sub.add_parser("export", help="Export a session to markdown.")
    session_export.add_argument("session_id")
    session_export.add_argument("--out", metavar="FILE", default=None)
    session_export.add_argument("--json", action="store_true")

    # Phase 3.7: `status` subcommand — agent/provider/model runtime info
    status_parser = subparsers.add_parser(
        "status",
        help="Print agent/provider/model/runtime status (headless /status).",
    )
    status_parser.add_argument("--workdir", metavar="DIR", default=None)
    status_parser.add_argument("--json", action="store_true")

    # Phase 3.7: `mcp` subcommand — list / status / add MCP servers
    mcp_parser = subparsers.add_parser(
        "mcp",
        help="List, status-check, or add MCP servers (headless /mcp).",
    )
    mcp_parser.add_argument("--workdir", metavar="DIR", default=None,
                            help="Working directory for mcp operations.")
    mcp_sub = mcp_parser.add_subparsers(dest="mcp_action")
    mcp_list = mcp_sub.add_parser("list", help="List configured MCP servers.")
    mcp_list.add_argument("--json", action="store_true")
    mcp_status = mcp_sub.add_parser("status", help="Show configured MCP servers (no live connections headlessly).")
    mcp_status.add_argument("--json", action="store_true")
    mcp_add = mcp_sub.add_parser("add", help="Add an MCP server to .codingAgent/config.json.")
    mcp_add.add_argument("name")
    mcp_add.add_argument("cmd", nargs=argparse.REMAINDER)

    # Phase 3.7: `diff` subcommand — working-directory diff since HEAD/snapshot
    diff_parser = subparsers.add_parser(
        "diff",
        help="Show the working-directory diff (headless /diff).",
    )
    diff_parser.add_argument("--workdir", metavar="DIR", default=None)
    diff_parser.add_argument("--json", action="store_true")
    diff_parser.add_argument("--path", metavar="PATH", default=None,
                             help="Restrict the diff to a specific path.")

    # Phase 3.8: `doctor` subcommand — diagnose local model providers & validate tool schemas
    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Diagnose local model providers (Ollama, LM Studio, vLLM), inspect hardware limits, and validate native tool schemas.",
    )
    doctor_parser.add_argument(
        "--provider",
        metavar="NAME",
        default=None,
        help="Specific provider to diagnose (e.g. ollama, lm_studio, vllm, or all).",
    )
    doctor_parser.add_argument(
        "--model",
        metavar="MODEL_ID",
        default=None,
        help="Specific model identifier to probe and validate.",
    )
    doctor_parser.add_argument(
        "--endpoint",
        metavar="URL",
        default=None,
        help="Custom server URL override to probe (e.g. http://localhost:11434).",
    )
    doctor_parser.add_argument(
        "--validate-tools",
        action="store_true",
        default=False,
        help="Send a test ping with flat tool schemas to verify server native tool acceptance.",
    )
    doctor_parser.add_argument(
        "--workdir",
        metavar="DIR",
        default=None,
        help="Working directory to inspect.",
    )
    doctor_parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output diagnostic report as structured JSON.",
    )

    # Phase 3.8: `skill` subcommand — list, inspect, and export agent skills
    skill_parser = subparsers.add_parser(
        "skill",
        help="List, inspect, and export packaged agent skills for Agensi, Claude Code, and Cursor.",
    )
    skill_sub = skill_parser.add_subparsers(dest="skill_action")

    skill_list = skill_sub.add_parser("list", help="List all available agent skills.")
    skill_list.add_argument("--json", action="store_true")

    skill_show = skill_sub.add_parser("show", help="Show instructions for a specific skill.")
    skill_show.add_argument("name", help="Name of the skill to inspect.")
    skill_show.add_argument("--json", action="store_true")

    skill_export = skill_sub.add_parser("export", help="Export skill(s) to target agent harness.")
    skill_export.add_argument("name", nargs="?", default=None, help="Name of the skill to export (or omit with --all).")
    skill_export.add_argument("--all", action="store_true", help="Export all available skills.")
    skill_export.add_argument(
        "--target",
        choices=["claude", "cursor", "agensi", "dir"],
        default="agensi",
        help="Target agent harness format (default: agensi).",
    )
    skill_export.add_argument("--out", metavar="DIR", default=None, help="Destination directory (default: current workspace).")
    skill_export.add_argument("--json", action="store_true")

    skill_val = skill_sub.add_parser("validate", help="Validate skill metadata and structure.")
    skill_val.add_argument("name", nargs="?", default=None, help="Name of skill to validate (or omit with --all).")
    skill_val.add_argument("--all", action="store_true", help="Validate all available skills.")
    skill_val.add_argument("--json", action="store_true")

    # TASK-28: `context-report` subcommand — offline prompt & tool-schema token budget report
    ctx_report_parser = subparsers.add_parser(
        "context-report",
        help="Print offline token breakdown for static system prompt, instructions, skills, and tool schemas.",
    )
    ctx_report_parser.add_argument(
        "--tier",
        choices=("compact-flat", "standard-flat", "frontier-flat"),
        default="compact-flat",
        help="Flat toolset tier to measure (default: compact-flat).",
    )
    ctx_report_parser.add_argument(
        "--workdir",
        metavar="DIR",
        default=None,
        help="Working directory to inspect.",
    )
    ctx_report_parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output context report as JSON.",
    )

    # TASK-28: `bench-diff` subcommand — benchmark-driven regression gate
    bench_diff_parser = subparsers.add_parser(
        "bench-diff",
        help="Compare baseline and candidate evaluation JSONs (exit 0 improved, 1 regressed, 2 noise, 3 error).",
    )
    bench_diff_parser.add_argument("baseline", help="Baseline JSON file or directory.")
    bench_diff_parser.add_argument("candidate", help="Candidate JSON file or directory.")
    bench_diff_parser.add_argument(
        "--threshold",
        type=float,
        default=0.02,
        help="Minimum pass-rate delta for improved/regressed classification (default: 0.02).",
    )
    bench_diff_parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output diff verdict as JSON.",
    )

    # TASK-35: `local-launch` subcommand — bounded GGUF memory fit & llama-server script generator
    ll_parser = subparsers.add_parser(
        "local-launch",
        help="Inspect a local .gguf file, compute safe n_ctx & KV quant, and generate a llama-server launch script.",
    )
    ll_parser.add_argument("model_path", help="Path to local .gguf model file.")
    ll_parser.add_argument(
        "--script",
        action="store_true",
        default=False,
        help="Print a copy-pasteable POSIX bash launch script.",
    )
    ll_parser.add_argument(
        "--autostart",
        action="store_true",
        default=False,
        help="Start llama-server directly if the model fits available memory.",
    )
    ll_parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1).")
    ll_parser.add_argument("--port", type=int, default=8080, help="Bind port (default: 8080).")
    ll_parser.add_argument(
        "--kv-quant",
        choices=("auto", "f16", "bf16", "q8_0", "q4_0"),
        default="auto",
        help="KV-cache quantization override (default: auto).",
    )
    ll_parser.add_argument(
        "--ctx",
        type=int,
        default=None,
        help="Optional maximum target context window (never exceeds hardware fit).",
    )
    ll_parser.add_argument(
        "--memory-gb",
        type=float,
        default=None,
        help="Override detected unified RAM / VRAM in GiB.",
    )
    ll_parser.add_argument("--cache-reuse", type=int, default=256, help="llama-server --cache-reuse value (default: 256).")
    ll_parser.add_argument("--parallel", type=int, default=1, help="llama-server --parallel slot count (default: 1).")
    ll_parser.add_argument("--json", action="store_true", default=False, help="Output fit report and command as JSON.")

    # Global (non-subcommand) flags
    parser.add_argument(
        "--output-format",
        choices=["pretty", "json", "raw", "stream", "stream-json"],
        default="pretty",
        help=(
            "Output format: pretty (default TUI), json (structured JSON per turn), "
            "raw (plain assistant text only), stream (incremental token streaming to stdout), "
            "stream-json (live NDJSON lifecycle events terminated by authoritative result envelope)."
        ),
    )
    parser.add_argument(
        "--output",
        choices=["text", "json", "stream-json"],
        default=None,
        help=(
            "Headless output mode (TASK-36): text (plain assistant text), "
            "json (authoritative result JSON), or stream-json (live NDJSON lifecycle stream)."
        ),
    )
    parser.add_argument(
        "--private",
        action="store_true",
        default=False,
        help=(
            "Ephemeral private session (TASK-36): disables SQLite session, memory, and trace "
            "disk writes while allowing normal workspace edits."
        ),
    )
    parser.add_argument(
        "--parallel",
        action="store_true",
        default=False,
        help=(
            "Run inside a timestamped temporary git worktree (TASK-36) and locally squash-merge "
            "changes on exit (never auto-pushes; prompts [a]bort/[l]eave/[h]elp on conflict)."
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
        "--sandbox-expose",
        metavar="PATH",
        action="append",
        default=None,
        help=(
            "Explicitly expose a host path read-only inside the OS sandbox "
            "(overrides default credential-directory masking; repeatable)."
        ),
    )
    parser.add_argument(
        "--autonomous",
        action="store_true",
        default=False,
        help=(
            "Run in autonomous mode: DANGER and PROMPT-level tools execute without "
            "interactive user approval — but ONLY for tools listed via "
            "--autonomous-approve (or the CODINGAGENT_AUTONOMOUS_APPROVE env var). "
            "Unlisted gated tools are denied, never silently approved."
        ),
    )
    # HS-1: operator override for autonomous approval suppression
    parser.add_argument(
        "--autonomous-approve",
        metavar="TOOL",
        nargs="+",
        default=None,
        help=(
            "Explicit operator override listing canonical tool names that "
            "autonomous mode may auto-approve without prompting; pass '*' to allow "
            "every gated tool. Equivalent to CODINGAGENT_AUTONOMOUS_APPROVE."
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
    # UX-2: --validate-config — validate .codingAgent/settings.json without running the agent
    parser.add_argument(
        "--validate-config",
        action="store_true",
        default=False,
        help=(
            "Validate .codingAgent/settings.json (and settings.local.json) for the "
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
            "For models like Qwen3 that emit  thinking blocks."
        ),
    )
    # TASK-29: --reasoning / --effort portable reasoning effort level
    parser.add_argument(
        "--reasoning",
        "--effort",
        dest="reasoning",
        metavar="LEVEL",
        default=None,
        choices=["off", "low", "medium", "high", "unset"],
        help="Control portable reasoning effort / thinking token budget (off|low|medium|high|unset).",
    )
    # Phase 3.7: --provider / --model headless overrides (mirror TUI /provider + /model)
    parser.add_argument(
        "--provider",
        metavar="NAME",
        default=None,
        help="Provider to use for --task runs (live model.routing switch before run).",
    )
    parser.add_argument(
        "--model",
        metavar="MODEL_ID",
        default=None,
        help="Model id to use for --task runs (live model.routing switch before run).",
    )
    # Phase 3.7: --continue — re-run the most recent task (mirror TUI /continue)
    parser.add_argument(
        "--continue",
        dest="continue_task",
        action="store_true",
        default=False,
        help=(
            "Re-run the most recent user task from the latest saved session. "
            "Overrides --task."
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=False,
        help="Enable debug logging (sets CODINGAGENT_DEBUG=1).",
    )
    parser.add_argument(
        "--trusted",
        action="store_true",
        default=None,
        help="Mark the workspace as trusted, enabling plugins and MCP servers.",
    )
    parser.add_argument(
        "--untrusted",
        action="store_false",
        dest="trusted",
        help="Mark the workspace as untrusted, disabling plugins and MCP servers.",
    )
    parser.add_argument(
        "--plugin-dir",
        metavar="DIR",
        default=None,
        help="Directory to load plugins from (requires trusted workspace).",
    )
    # Fail closed on unrecognized flags so typos on security flags (e.g.
    # --untrsted or --permision-mode) never degrade silently.
    return parser.parse_args(argv)

_parse_args = parse_args
