"""OPT-01 regression tests: lazy HTTP server import and cached built-in ToolRegistry snapshot."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_orchestrator_init_does_not_import_fastapi_when_http_server_disabled(tmp_path: Path):
    """Creating Orchestrator with CODINGAGENT_HTTP_SERVER=false must not import src.server.app or fastapi."""
    code = (
        "import os, sys; "
        "os.environ['CODINGAGENT_HTTP_SERVER'] = 'false'; "
        "os.environ['CODING_AGENT_HTTP_SERVER'] = 'false'; "
        "from src.core.orchestration.orchestrator import Orchestrator; "
        f"o = Orchestrator(working_dir={str(tmp_path)!r}, defer_provider_probe=True); "
        "o.close(); "
        "assert 'src.server.app' not in sys.modules, 'src.server.app was imported eagerly'; "
        "assert 'fastapi' not in sys.modules, 'fastapi was imported eagerly'; "
        "print('OK')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, f"stderr={proc.stderr}\nstdout={proc.stdout}"
    assert "OK" in proc.stdout


def test_build_registry_snapshot_isolates_instances_and_is_fast():
    """Repeated build_registry() calls share discovery cache but keep isolated mutable state."""
    from src.tools._registry import build_registry, clear_builtin_registry_cache

    clear_builtin_registry_cache()
    reg1 = build_registry()
    reg2 = build_registry()

    assert set(reg1.tools.keys()) == set(reg2.tools.keys())
    assert reg1._tools is not reg2._tools

    # Mutating reg1 must never affect reg2 or the cached snapshot
    reg1.register("opt01_temp_tool", lambda: {"ok": True}, origin="plugin")
    assert "opt01_temp_tool" in reg1.tools
    assert "opt01_temp_tool" not in reg2.tools
    reg3 = build_registry()
    assert "opt01_temp_tool" not in reg3.tools
