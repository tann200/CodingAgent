"""CLI handler for ``codingagent local-launch <model.gguf>`` (TASK-35).

Inspects a local ``.gguf`` file using the pure-stdlib bounded GGUF reader,
computes hardware-safe ``--ctx-size`` and KV-cache quantization flags for
``llama-server``, and either prints a copy-pasteable launch script (default /
``--script``) or optionally starts ``llama-server`` when ``--autostart`` is
explicitly requested.
"""

from __future__ import annotations

import json
import platform
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core.inference.gguf_header import (
    GgufHeaderError,
    GgufHeaderInfo,
    GgufMemoryFit,
    estimate_gguf_memory_fit,
    read_gguf_header,
)
from src.core.inference.hardware_capability_profile import detect_hardware


def build_llama_server_command(
    gguf_path: str | Path,
    fit: GgufMemoryFit,
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    cache_reuse: int = 256,
    parallel: int = 1,
) -> List[str]:
    """Build a safe ``llama-server`` argv list for *gguf_path* and *fit*."""
    cmd: List[str] = [
        "llama-server",
        "-m",
        str(Path(gguf_path)),
        "--host",
        str(host),
        "--port",
        str(int(port)),
        "--ctx-size",
        str(max(4096, int(fit.recommended_n_ctx))),
        "--jinja",
        "--cache-reuse",
        str(max(0, int(cache_reuse))),
        "-fa",
        "on",
        "--parallel",
        str(max(1, int(parallel))),
    ]
    q = (fit.kv_quant or "f16").strip().lower()
    if q in ("q8_0", "q4_0"):
        cmd.extend(["-ctk", q, "-ctv", q])
    return cmd


def format_launch_script(
    cmd: List[str],
    info: GgufHeaderInfo,
    fit: GgufMemoryFit,
) -> str:
    """Format a copy-pasteable POSIX shell script with ``shlex.quote`` safety."""
    grouped: List[str] = []
    i = 0
    while i < len(cmd):
        tok = cmd[i]
        if i > 0 and tok.startswith("-") and i + 1 < len(cmd) and not cmd[i + 1].startswith("-"):
            grouped.append(f"{shlex.quote(tok)} {shlex.quote(cmd[i + 1])}")
            i += 2
        else:
            grouped.append(shlex.quote(tok))
            i += 1
    quoted = " \\\n  ".join(grouped)
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        f"# Model: {info.model_name} (arch={info.architecture}, weights={fit.weight_gb:.2f} GiB)",
        f"# Fit: {fit.reason}",
        f"exec {quoted}",
    ]
    return "\n".join(lines) + "\n"


def build_local_launch_plan(
    gguf_path: str | Path,
    *,
    memory_gb: Optional[float] = None,
    os_name: Optional[str] = None,
    kv_quant: str = "auto",
    target_n_ctx: Optional[int] = None,
    host: str = "127.0.0.1",
    port: int = 8080,
    cache_reuse: int = 256,
    parallel: int = 1,
) -> Dict[str, Any]:
    """Inspect *gguf_path* and return structured fit metadata, argv, and script."""
    info = read_gguf_header(gguf_path)
    hw = detect_hardware()
    eff_os = os_name or str(getattr(hw, "os", platform.system().lower()))
    if memory_gb is not None and memory_gb > 0:
        eff_mem = float(memory_gb)
    else:
        ram_gb = float(getattr(hw, "ram_gb", 0.0) or 0.0)
        vram_gb = float(getattr(hw, "vram_gb", 0.0) or 0.0)
        eff_mem = max(vram_gb, ram_gb if eff_os.lower() == "darwin" else 0.0)
        if eff_mem <= 0:
            eff_mem = ram_gb

    fit = estimate_gguf_memory_fit(
        info,
        total_memory_gb=eff_mem,
        os_name=eff_os,
        kv_quant=kv_quant,
        target_n_ctx=target_n_ctx,
    )
    cmd = build_llama_server_command(
        gguf_path,
        fit,
        host=host,
        port=port,
        cache_reuse=cache_reuse,
        parallel=parallel,
    )
    script = format_launch_script(cmd, info, fit)
    return {
        "model_path": str(Path(gguf_path)),
        "model_name": info.model_name,
        "architecture": info.architecture,
        "version": info.version,
        "block_count": info.block_count,
        "attention_kv_layers": info.attention_kv_layers,
        "head_count_kv": info.head_count_kv,
        "model_max_ctx": info.context_length,
        "fits": fit.fits,
        "available_memory_gb": fit.available_memory_gb,
        "os_reserve_gb": fit.os_reserve_gb,
        "weight_gb": fit.weight_gb,
        "compute_buffer_gb": fit.compute_buffer_gb,
        "kv_budget_gb": fit.kv_budget_gb,
        "kv_quant": fit.kv_quant,
        "kv_mb_per_1k_tokens": fit.kv_mb_per_1k_tokens,
        "recommended_n_ctx": fit.recommended_n_ctx,
        "reason": fit.reason,
        "command": cmd,
        "command_line": " ".join(shlex.quote(arg) for arg in cmd),
        "script": script,
    }


def run_local_launch(args: Any) -> int:
    """Execute ``codingagent local-launch <model.gguf>``."""
    model_path = getattr(args, "model_path", None)
    if not model_path:
        print("local-launch: missing <model.gguf> path", file=sys.stderr)
        return 2

    try:
        plan = build_local_launch_plan(
            model_path,
            memory_gb=getattr(args, "memory_gb", None),
            kv_quant=getattr(args, "kv_quant", "auto") or "auto",
            target_n_ctx=getattr(args, "ctx", None),
            host=getattr(args, "host", "127.0.0.1") or "127.0.0.1",
            port=int(getattr(args, "port", 8080) or 8080),
            cache_reuse=int(getattr(args, "cache_reuse", 256) or 256),
            parallel=int(getattr(args, "parallel", 1) or 1),
        )
    except GgufHeaderError as exc:
        print(f"local-launch: invalid or unsafe GGUF file: {exc}", file=sys.stderr)
        return 2

    as_json = bool(getattr(args, "json", False))
    as_script = bool(getattr(args, "script", False))
    autostart = bool(getattr(args, "autostart", False))

    if as_json:
        print(json.dumps(plan, indent=2))
        return 0 if plan["fits"] else 1

    if as_script and not autostart:
        print(plan["script"], end="")
        return 0 if plan["fits"] else 1

    if autostart:
        if not plan["fits"]:
            print(
                f"local-launch: refusing to autostart OOM configuration: {plan['reason']}",
                file=sys.stderr,
            )
            return 1
        binary = shutil.which("llama-server")
        if not binary:
            print(
                "local-launch: 'llama-server' not found in PATH. Install llama.cpp or run with --script.",
                file=sys.stderr,
            )
            return 1
        cmd = [binary] + plan["command"][1:]
        print(f"Starting llama-server: {plan['command_line']}")
        proc = subprocess.Popen(cmd)
        print(f"llama-server started (pid={proc.pid}) on http://{getattr(args, 'host', '127.0.0.1')}:{getattr(args, 'port', 8080)}")
        return 0

    print(f"Model:        {plan['model_name']} ({plan['architecture']}, GGUF v{plan['version']})")
    print(f"Weights:      {plan['weight_gb']:.2f} GiB  |  KV layers: {plan['attention_kv_layers']}/{plan['block_count']}")
    print(f"Memory fit:   {'OK' if plan['fits'] else 'DOES NOT FIT'} — {plan['reason']}")
    print(f"KV cache:     {plan['kv_quant']} ({plan['kv_mb_per_1k_tokens']:.2f} MiB / 1K tokens, budget {plan['kv_budget_gb']:.2f} GiB)")
    print(f"Recommended:  --ctx-size {plan['recommended_n_ctx']}")
    print("")
    print("Launch command:")
    print(f"  {plan['command_line']}")
    return 0 if plan["fits"] else 1


__all__ = [
    "build_llama_server_command",
    "build_local_launch_plan",
    "format_launch_script",
    "run_local_launch",
]
