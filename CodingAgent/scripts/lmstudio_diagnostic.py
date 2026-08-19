#!/usr/bin/env python3
"""
Lightweight LM Studio diagnostic script.

Sends sample chat/completions requests (varying prompt size and concurrency)
and reports timing, HTTP status, and basic response metadata. Also attempts
to capture system memory usage when psutil is installed.

Usage:
  python3 scripts/lmstudio_diagnostic.py --base-url http://localhost:1234/v1 \
      --model qwen/qwen3.5-9b --concurrency 2 --iterations 3

This is intended as a quick smoke-test to reproduce KV/slot pressure and
client disconnect issues observed when very large prompts or many concurrent
requests are sent to a local LM Studio server.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

import requests

try:
    import psutil as _psutil

    _HAS_PSUTIL = True
except Exception:
    _psutil = None  # type: ignore[assignment]
    _HAS_PSUTIL = False


def sample_mem() -> Dict[str, Any]:
    if not _HAS_PSUTIL or _psutil is None:
        return {}
    vm = _psutil.virtual_memory()
    return {
        "total": vm.total,
        "available": vm.available,
        "used": vm.used,
        "percent": vm.percent,
    }


def make_prompt(words: int) -> str:
    # generate an innocuous repeating prompt of ~words words
    return "Tell me about: " + "word " * max(0, words - 2)


def run_request(
    base_url: str, model: str, prompt: str, timeout: float = 60.0
) -> Dict[str, Any]:
    ep = base_url.rstrip("/")
    # Accept either /v1 host or bare host — mirror adapter compose behavior
    if ep.endswith("/v1"):
        url = f"{ep}/chat/completions"
    else:
        url = f"{ep}/v1/chat/completions"

    payload = {"model": model, "messages": [{"role": "user", "content": prompt}]}
    t0 = time.time()
    try:
        r = requests.post(url, json=payload, timeout=timeout)
        latency = time.time() - t0
        try:
            body = r.json()
        except Exception:
            body = {"text": r.text[:200]}
        return {
            "ok": True,
            "status_code": r.status_code,
            "latency": latency,
            "body_summary": (body if isinstance(body, dict) else str(body)),
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "latency": time.time() - t0}


def worker_task(
    base_url: str,
    model: str,
    prompt_words: int,
    timeout: float,
) -> Dict[str, Any]:
    mem_before = sample_mem()
    prompt = make_prompt(prompt_words)
    result = run_request(base_url, model, prompt, timeout=timeout)
    mem_after = sample_mem()
    return {
        "prompt_words": prompt_words,
        "result": result,
        "mem_before": mem_before,
        "mem_after": mem_after,
    }


def run_diagnostics(
    base_url: str,
    model: str,
    concurrency: int,
    iterations: int,
    prompt_sizes: List[int],
    timeout: float,
):
    summary: Dict[str, Any] = {
        "base_url": base_url,
        "model": model,
        "concurrency": concurrency,
        "iterations": iterations,
        "prompt_sizes": prompt_sizes,
        "psutil": _HAS_PSUTIL,
        "runs": [],
    }

    # Check models endpoint first
    try:
        models_ep = base_url.rstrip("/")
        if models_ep.endswith("/v1"):
            models_url = f"{models_ep}/models"
        else:
            models_url = f"{models_ep}/v1/models"
        r = requests.get(models_url, timeout=10)
        summary["models_endpoint"] = {
            "status_code": r.status_code,
            "ok": r.status_code == 200,
        }
    except Exception as e:
        summary["models_endpoint"] = {"ok": False, "error": str(e)}

    total_runs = iterations * len(prompt_sizes)
    run_count = 0

    for it in range(iterations):
        # spawn concurrency requests per prompt size
        tasks: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            futures = []
            for size in prompt_sizes:
                # launch one request per size concurrently
                futures.append(ex.submit(worker_task, base_url, model, size, timeout))

            for fut in as_completed(futures):
                res = fut.result()
                res["iteration"] = it
                run_count += 1
                summary["runs"].append(res)
                print(
                    f"[{run_count}/{total_runs}] size={res['prompt_words']} latency={res['result'].get('latency'):.3f} ok={res['result'].get('ok')}"
                )
                sys.stdout.flush()
        # small pause between iterations
        time.sleep(0.5)

    return summary


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="LM Studio diagnostic smoke tester")
    p.add_argument(
        "--base-url",
        required=True,
        help="LM Studio base URL (e.g. http://localhost:1234/v1)",
    )
    p.add_argument("--model", required=True, help="Model id (e.g. qwen/qwen3.5-9b)")
    p.add_argument(
        "--concurrency", type=int, default=2, help="Parallel requests per iteration"
    )
    p.add_argument("--iterations", type=int, default=3, help="Number of iterations")
    p.add_argument(
        "--prompt-sizes",
        type=int,
        nargs="*",
        default=[20, 400, 1200],
        help="Prompt size in words (space-separated list)",
    )
    p.add_argument(
        "--timeout", type=float, default=60.0, help="Per-request timeout seconds"
    )
    p.add_argument("--out", default="lmstudio_diag.json", help="Output JSON file")
    args = p.parse_args(argv)

    start = time.time()
    summary = run_diagnostics(
        args.base_url,
        args.model,
        args.concurrency,
        args.iterations,
        args.prompt_sizes,
        args.timeout,
    )
    summary["duration_seconds"] = time.time() - start

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)

    print(f"Wrote summary to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
