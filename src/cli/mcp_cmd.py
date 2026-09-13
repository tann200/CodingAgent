"""``codingagent mcp`` — list, add, and status-check configured MCP servers.

Mirrors the TUI ``/mcp`` command for headless/scriptable use.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

from src.cli._helpers import print_json, resolve_workdir

_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_UNSAFE_RE = re.compile(r"[;&$`\\|<>]")


def run_mcp(args: Any) -> int:
    sub = getattr(args, "mcp_action", "list") or "list"
    workdir = resolve_workdir(getattr(args, "workdir", None))
    if sub == "list":
        return _list_servers(workdir, getattr(args, "json", False))
    if sub == "status":
        return _status_servers(workdir, getattr(args, "json", False))
    if sub == "add":
        return _add_server(workdir, args.name, args.cmd)
    print(f"Error: unknown mcp action '{sub}'", file=sys.stderr)
    return 2


def _configured_servers(workdir: str) -> list:
    from src.core.config_loader import get_mcp_servers

    servers = get_mcp_servers(working_dir=Path(workdir))
    out: list[dict] = []
    for s in servers if isinstance(servers, list) else []:
        if not isinstance(s, dict):
            continue
        out.append(
            {
                "name": s.get("name", "?"),
                "cmd": " ".join(s.get("cmd") or s.get("args") or []),
                "transport": s.get("transport", "stdio"),
                "auto_register_tools": s.get("auto_register_tools", True),
            }
        )
    return out


def _list_servers(workdir: str, as_json: bool) -> int:
    servers = _configured_servers(workdir)
    if as_json:
        print_json({"servers": servers})
        return 0
    if not servers:
        print("No MCP servers configured.")
        return 0
    print("Configured MCP servers:")
    for s in servers:
        auto = "auto" if s["auto_register_tools"] else "manual"
        print(f"  - {s['name']}  ({s['cmd'] or '(no cmd)'})  [{s['transport']} / {auto}]")
    return 0


def _status_servers(workdir: str, as_json: bool) -> int:
    servers = _configured_servers(workdir)
    if as_json:
        print_json({"servers": servers, "connected": [], "note": "CLI does not hold live MCP connections"})
        return 0
    if not servers:
        print("No MCP servers configured. Connection status is not tracked headlessly.")
        return 0
    print("MCP server status (configured; CLI holds no live connections):")
    for s in servers:
        print(f"  - {s['name']}")
    return 0


def _add_server(workdir: str, name: str, cmd: list) -> int:
    import json

    if not name or not _NAME_RE.match(name):
        print(
            f"Error: invalid server name '{name}': use only letters, digits, - and _",
            file=sys.stderr,
        )
        return 1
    if not cmd:
        print("Error: missing command. Usage: codingagent mcp add <name> <cmd> [args...]", file=sys.stderr)
        return 1
    if _UNSAFE_RE.search(" ".join(cmd)):
        print("Error: command contains unsafe shell characters", file=sys.stderr)
        return 1

    agent_dir = Path(workdir) / ".agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    config_path = agent_dir / "config.json"
    cfg: dict = {}
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    mcp_section = cfg.setdefault("mcp", {})
    servers_list: list = mcp_section.setdefault("servers", [])
    servers_list = [s for s in servers_list if s.get("name") != name]
    servers_list.append(
        {"name": name, "cmd": cmd, "auto_register_tools": True}
    )
    mcp_section["servers"] = servers_list
    cfg["mcp"] = mcp_section

    import logging

    _logger = logging.getLogger("cli.mcp")
    try:
        from src.core.io_utils import atomic_write_json

        ok = atomic_write_json(config_path, cfg, logger=_logger)
    except Exception:
        ok = False
    if not ok:
        config_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    print(f"MCP server added: {name}")
    print(f"  cmd: {' '.join(cmd)}")
    print(f"  Config saved to {config_path}")
    print("Restart the agent session to connect to the new server.")
    return 0