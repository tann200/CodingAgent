"""benchmarks/bench_pipeline.py — CAP-5: Pipeline performance benchmarks.

Runs 5 representative scenarios through the full agent pipeline using
MockAdapter (no real LLM required) and records wall time + approximate
token count for each.

Usage::

    python benchmarks/bench_pipeline.py           # human-readable table
    python benchmarks/bench_pipeline.py --json    # machine-readable JSON

CI integration::

    pytest benchmarks/bench_pipeline.py -v       # runs as a pytest suite

Each scenario is a lightweight perception → execution → memory_sync pass.
The benchmark records:
  - wall_time_s  : elapsed seconds for run_agent_once()
  - input_tokens : tokens sent to the mock LLM (estimated from message length)
  - output_tokens: tokens in the mock response
  - result_ok    : whether the pipeline completed without error
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(_REPO_ROOT))


def _patch_infra() -> list:
    """Return a list of active mock patches for heavy infrastructure."""
    patches = [
        patch(
            "src.core.orchestration.orchestrator._ensure_provider_manager_initialized_sync",
            lambda: None,
        ),
        patch(
            "src.core.orchestration.orchestrator.Orchestrator._background_model_check",
            lambda self: None,
        ),
    ]
    try:
        import src.core.orchestration.graph.builder as _builder

        # Use patch.object so the original value is restored by p.stop().
        # Direct assignment (_builder._COMPILED_GRAPH = None) is never cleaned up
        # and destroys any already-compiled graph for the duration of the process.
        patches.append(patch.object(_builder, "_COMPILED_GRAPH", None))
    except Exception:
        pass
    return patches


def _approx_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token."""
    return max(1, len(text) // 4)


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

SCENARIOS: List[Dict[str, Any]] = [
    {
        "name": "fast_path_write",
        "task": "Create hello.py with def hello(): return 'Hello World'",
        "responses": [
            "```yaml\nname: write_file\narguments:\n  path: hello.py\n  content: \"def hello():\\n    return 'Hello World'\\n\"\n```",
            "hello.py has been created.",
        ],
    },
    {
        "name": "fast_path_read",
        "task": "What does hello.py contain?",
        "responses": [
            "```yaml\nname: read_file\narguments:\n  path: hello.py\n```",
            "hello.py contains a hello() function that returns 'Hello World'.",
        ],
        "pre_create": {"hello.py": "def hello():\n    return 'Hello World'\n"},
    },
    {
        "name": "fast_path_grep",
        "task": "Find all Python files that define a main function",
        "responses": [
            "```yaml\nname: grep\narguments:\n  pattern: 'def main'\n  path: .\n```",
            "Found 2 files with main functions.",
        ],
    },
    {
        "name": "fast_path_edit",
        "task": "Add a docstring to hello.py",
        "responses": [
            "```yaml\nname: read_file\narguments:\n  path: hello.py\n```",
            '```yaml\nname: edit_file\narguments:\n  path: hello.py\n  old_string: "def hello():"\n  new_string: "def hello():\\n    \\"\\"\\"Return a greeting.\\"\\"\\"\\n"\n```',
            "Docstring added to hello.py.",
        ],
        "pre_create": {"hello.py": "def hello():\n    return 'Hello World'\n"},
    },
    {
        "name": "fast_path_list",
        "task": "List all files in the project",
        "responses": [
            "```yaml\nname: list_files\narguments:\n  path: .\n```",
            "Found 3 files in the project.",
        ],
    },
]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_scenario(scenario: Dict[str, Any], tmp_path: Path) -> Dict[str, Any]:
    """Run a single scenario and return timing + token metrics."""
    from src.core.inference.adapters.mock_adapter import MockAdapter
    from src.core.orchestration.orchestrator import Orchestrator

    # Optionally pre-create files
    for fname, content in scenario.get("pre_create", {}).items():
        fpath = tmp_path / fname
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text(content, encoding="utf-8")

    responses = list(scenario["responses"])
    adapter = MockAdapter(responses=responses, strict=False)

    async def mock_call_model(messages, model=None, provider=None, *a, **kw):
        return adapter.generate(messages, model=model, provider=provider)

    _CALL_MODEL_TARGETS = [
        "src.core.orchestration.graph.nodes.perception_node.call_model",
        "src.core.orchestration.graph.nodes.planning_node.call_model",
        "src.core.orchestration.graph.nodes.execution_node.call_model",
        "src.core.orchestration.graph.nodes.debug_node.call_model",
        "src.core.orchestration.graph.nodes.replan_node.call_model",
        "src.core.inference.llm_manager.call_model",
        "src.core.inference.llm_manager._call_model_internal",
    ]

    infra_patches = _patch_infra()
    for p in infra_patches:
        p.start()

    cm_patches = []
    for target in _CALL_MODEL_TARGETS:
        try:
            p = patch(target, new=mock_call_model)
            p.start()
            cm_patches.append(p)
        except AttributeError:
            pass

    try:
        orch = Orchestrator(
            adapter=adapter,
            working_dir=str(tmp_path),
            allow_external_working_dir=True,
        )

        messages = [{"role": "user", "content": scenario["task"]}]
        t0 = time.perf_counter()
        result = orch.run_agent_once(None, messages, {})
        elapsed = time.perf_counter() - t0

        # Estimate tokens from responses
        total_out_tokens = sum(_approx_tokens(str(r)) for r in scenario["responses"])
        total_in_tokens = _approx_tokens(scenario["task"]) * len(scenario["responses"])

        return {
            "scenario": scenario["name"],
            "wall_time_s": round(elapsed, 4),
            "input_tokens": total_in_tokens,
            "output_tokens": total_out_tokens,
            "result_ok": isinstance(result, dict) and not result.get("error"),
        }
    except Exception as exc:
        return {
            "scenario": scenario["name"],
            "wall_time_s": -1.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "result_ok": False,
            "error": str(exc),
        }
    finally:
        for p in cm_patches:
            try:
                p.stop()
            except Exception:
                pass
        for p in infra_patches:
            try:
                p.stop()
            except Exception:
                pass


def run_all(tmp_base: Path | None = None) -> List[Dict[str, Any]]:
    """Run all 5 scenarios and return metrics list.

    Parameters
    ----------
    tmp_base:
        Optional base directory for temporary scenario workdirs.  When given,
        sub-directories are created under *tmp_base* instead of the system
        temp dir so the caller controls cleanup.
    """
    import tempfile

    results = []
    for scenario in SCENARIOS:
        if tmp_base is not None:
            import uuid as _uuid

            td_path = tmp_base / f"bench_{_uuid.uuid4().hex[:8]}"
            td_path.mkdir(parents=True, exist_ok=True)
            metrics = run_scenario(scenario, td_path)
        else:
            with tempfile.TemporaryDirectory() as td:
                metrics = run_scenario(scenario, Path(td))
        results.append(metrics)
    return results


# ---------------------------------------------------------------------------
# pytest entry points (CAP-5: runnable as pytest suite)
# ---------------------------------------------------------------------------

try:
    import pytest  # noqa: E402

    @pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["name"] for s in SCENARIOS])
    def test_bench_scenario(scenario: Dict[str, Any], tmp_path: Path) -> None:
        """CAP-5: Each scenario must complete in under 30 s and succeed."""
        metrics = run_scenario(scenario, tmp_path)
        assert metrics["result_ok"], (
            f"Scenario '{metrics['scenario']}' failed: {metrics.get('error', 'unknown')}"
        )
        assert metrics["wall_time_s"] < 30.0, (
            f"Scenario '{metrics['scenario']}' took {metrics['wall_time_s']:.2f}s (limit: 30s)"
        )

except ImportError:
    pass  # pytest not installed — CLI-only mode, test functions not defined


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run pipeline performance benchmarks")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")
    args = parser.parse_args()

    results = run_all()

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        header = (
            f"{'Scenario':<25} {'Time (s)':>10} {'In tok':>8} {'Out tok':>8} {'OK':>5}"
        )
        print(header)
        print("-" * len(header))
        for r in results:
            status = "✓" if r["result_ok"] else "✗"
            print(
                f"{r['scenario']:<25} {r['wall_time_s']:>10.4f} "
                f"{r['input_tokens']:>8} {r['output_tokens']:>8} {status:>5}"
            )
        total = sum(r["wall_time_s"] for r in results if r["wall_time_s"] > 0)
        print(f"\nTotal wall time: {total:.4f}s")
        failures = [r for r in results if not r["result_ok"]]
        if failures:
            print(f"\nFailed scenarios: {[r['scenario'] for r in failures]}")
            sys.exit(1)
