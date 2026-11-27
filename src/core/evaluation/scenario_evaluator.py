"""
Scenario Evaluation Framework for CodingAgent.

This module provides a standardized way to evaluate the agent on coding tasks,
similar to SWE-bench style evaluations.
"""

import json
import logging
import math
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, Any, List, Optional, Callable
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


def _align_agent_working_dir(agent: Any, scenario_dir: Path) -> None:
    """Point agent-backed runs at the scenario directory when supported."""
    if not hasattr(agent, "working_dir"):
        return
    try:
        current = getattr(agent, "working_dir")
        aligned = scenario_dir if isinstance(current, Path) or current is None else str(scenario_dir)
        setattr(agent, "working_dir", aligned)
        rb = getattr(agent, "rollback_manager", None)
        if rb is not None and hasattr(rb, "workdir"):
            try:
                from src.tools.tools_config import agent_context_path

                rb.workdir = Path(scenario_dir)
                rb.snapshot_dir = agent_context_path(rb.workdir) / "snapshots"
                rb.snapshot_dir.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
    except Exception:
        return
    try:
        ensure_working_dir = getattr(agent, "_ensure_working_dir", None)
        if callable(ensure_working_dir):
            ensure_working_dir()
    except Exception as exc:
        logger.debug("scenario_evaluator: _ensure_working_dir failed: %s", exc)


def _run_agent_for_scenario(agent: Any, scenario: "Scenario", scenario_dir: Path) -> None:
    """Execute one scenario against an agent-like object.

    Supports the existing direct-agent ``run(...)`` contract and the real
    ``Orchestrator.run_agent_once(...)`` path used by benchmarks.
    """
    _align_agent_working_dir(agent, scenario_dir)

    if hasattr(agent, "run"):
        agent.run(scenario.task, working_dir=str(scenario_dir))
        return

    if hasattr(agent, "run_agent_once") and callable(getattr(agent, "run_agent_once")):
        tools = {}
        try:
            tools = agent.get_tools_for_role("operational")
        except Exception:
            tools = getattr(agent, "tools", {}) or {}

        agent.run_agent_once(
            system_prompt_name="operational",
            messages=[{"role": "user", "content": scenario.task}],
            tools=tools,
        )
        return

    if callable(agent):
        agent(scenario.task)
        return

    logger.warning(
        "ScenarioEvaluator: agent has no supported execution surface for '%s'",
        scenario.name,
    )


@dataclass
class Scenario:
    """A test scenario for evaluation."""

    name: str
    description: str
    task: str
    setup_files: Dict[str, str] = field(default_factory=dict)
    expected_files: Dict[str, str] = field(default_factory=dict)
    test_command: Optional[str] = None
    cleanup_command: Optional[str] = None
    # M5: Metadata for filtering and reporting
    difficulty: str = "medium"
    category: str = "general"
    tags: List[str] = field(default_factory=list)


@dataclass
class ScenarioResult:
    """Result of a scenario evaluation."""

    scenario_name: str
    status: str  # "pass", "fail", "error"
    start_time: datetime
    end_time: datetime
    duration_seconds: float
    tool_calls: List[Dict] = field(default_factory=list)
    files_created: List[str] = field(default_factory=list)
    files_modified: List[str] = field(default_factory=list)
    error: Optional[str] = None
    verification_output: Optional[str] = None
    tool_calls_total: int = 0
    malformed_tool_calls: int = 0
    edit_accurate: bool = False
    verification_attempted: bool = False
    verification_passed: bool = False
    prompt_tokens: int = 0
    completion_tokens: int = 0
    prefill_latency_ms: float = 0.0


class ScenarioEvaluator:
    """
    Evaluates CodingAgent on standardized scenarios.

    Usage:
        evaluator = ScenarioEvaluator()

        # Add scenarios
        evaluator.add_scenario(Scenario(
            name="simple_function",
            description="Create a simple function",
            task="Create a function hello() that returns 'Hello World'",
            expected_files={"hello.py": "def hello"},
            test_command="python -c 'from hello import hello; assert hello() == \"Hello World\"'",
        ))

        # Run evaluation
        results = evaluator.run_evaluation(agent_factory)

        # Get summary
        summary = evaluator.get_summary(results)
    """

    def __init__(self, workdir: Optional[str] = None, *, debug: Optional[bool] = None):
        self._own_tempdir = workdir is None
        self.workdir = Path(workdir) if workdir else Path(tempfile.mkdtemp())
        self.scenarios: List[Scenario] = []
        self.results: List[ScenarioResult] = []
        if debug is None:
            self.debug = os.getenv("CODINGAGENT_EVAL_DEBUG", "").strip().lower() in (
                "1",
                "true",
                "yes",
            )
        else:
            self.debug = bool(debug)

    def _emit_debug(self, msg: str) -> None:
        if self.debug:
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"[EVAL {ts}] {msg}", flush=True)

    def cleanup(self) -> None:
        """Remove the temporary work directory if it was created by this instance."""
        if self._own_tempdir and self.workdir.exists():
            import shutil as _shutil
            _shutil.rmtree(self.workdir, ignore_errors=True)

    def __del__(self) -> None:
        self.cleanup()

    def add_scenario(self, scenario: Scenario):
        """Add a scenario to the evaluation suite."""
        self.scenarios.append(scenario)
        logger.info(f"Added scenario: {scenario.name}")

    def add_scenarios_from_file(self, filepath: str):
        """Load scenarios from a JSON file."""
        with open(filepath, "r") as f:
            data = json.load(f)
            for item in data.get("scenarios", []):
                self.add_scenario(
                    Scenario(
                        name=item["name"],
                        description=item.get("description", ""),
                        task=item["task"],
                        setup_files=item.get("setup_files", {}),
                        expected_files=item.get("expected_files", {}),
                        test_command=item.get("test_command"),
                        cleanup_command=item.get("cleanup_command"),
                        difficulty=item.get("difficulty", "medium"),
                        category=item.get("category", "general"),
                        tags=item.get("tags", []),
                    )
                )

    def _setup_scenario(self, scenario: Scenario) -> Path:
        """Setup scenario files in a temporary directory."""
        _wd = self.workdir.resolve()
        scenario_dir = _wd / scenario.name
        scenario_dir.resolve().relative_to(_wd)  # fails if traversal escapes workdir
        scenario_dir.mkdir(parents=True, exist_ok=True)

        for filename, content in scenario.setup_files.items():
            filepath = (scenario_dir / filename).resolve()
            filepath.relative_to(_wd)  # fails if traversal escapes workdir
            filepath.parent.mkdir(parents=True, exist_ok=True)
            filepath.write_text(content)

        return scenario_dir

    def _verify_scenario_detailed(
        self, scenario: Scenario, scenario_dir: Path
    ) -> tuple[bool, str, bool, bool, bool]:
        """Verify scenario results and return ``(passed, output, edit_ok, verify_ran, verify_ok)``."""
        output = ""
        _wd = self.workdir.resolve()

        edit_ok = True
        for filename, expected_content in scenario.expected_files.items():
            filepath = (scenario_dir / filename).resolve()
            filepath.relative_to(_wd)  # fails if traversal escapes workdir
            if not filepath.exists():
                return False, f"Expected file not found: {filename}", False, False, False

            actual_content = filepath.read_text()
            if expected_content.strip() not in actual_content.strip():
                return False, f"File content mismatch: {filename}", False, False, False

        verify_ran = bool(scenario.test_command)
        verify_ok = False
        if scenario.test_command:
            try:
                cmd_parts = shlex.split(scenario.test_command)
                if cmd_parts and cmd_parts[0] == "python":
                    cmd_parts[0] = sys.executable
                result = subprocess.run(
                    cmd_parts,
                    shell=False,
                    cwd=scenario_dir,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                output = result.stdout + result.stderr
                if result.returncode != 0:
                    return False, f"Test command failed: {output}", edit_ok, True, False
                verify_ok = True
            except subprocess.TimeoutExpired:
                return False, "Test command timed out", edit_ok, True, False
            except Exception as e:
                return False, f"Test command error: {str(e)}", edit_ok, True, False

        return True, output, edit_ok, verify_ran, verify_ok

    def _verify_scenario(
        self, scenario: Scenario, scenario_dir: Path
    ) -> tuple[bool, str]:
        """Verify scenario results."""
        passed, output, _, _, _ = self._verify_scenario_detailed(scenario, scenario_dir)
        return passed, output

    def run_scenario(
        self,
        scenario: Scenario,
        agent_factory: Callable[[], Any],
    ) -> ScenarioResult:
        """Run a single scenario."""
        start_time = datetime.now()
        scenario_dir = None
        agent = None
        metrics: Dict[str, Any] = {
            "tool_calls_total": 0,
            "malformed_tool_calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "prefill_latency_ms": 0.0,
        }
        subscriptions: list[tuple[Any, str, Callable]] = []

        try:
            # Setup
            scenario_dir = self._setup_scenario(scenario)

            # Run agent on task (C7 fix — agent_factory result is now actually invoked)
            logger.info(f"Running scenario: {scenario.name}")
            agent = agent_factory()

            turn_state: Dict[str, Any] = {
                "llm_calls": 0,
                "llm_seconds": 0.0,
                "tool_starts": {},
            }
            if self.debug:
                _ad = getattr(agent, "_adapter", None) or getattr(agent, "adapter", None)
                _prov = getattr(_ad, "name", None) or getattr(agent, "_provider_name", None) or "?"
                _mdl = getattr(_ad, "default_model", None) or getattr(agent, "model", None) or "?"
                self._emit_debug(
                    f">>> {scenario.name} | provider={_prov} model={_mdl} workdir={scenario_dir}"
                )

            bus = getattr(agent, "event_bus", None) if agent is not None else None
            if bus is not None and hasattr(bus, "subscribe"):
                def _payload_field(payload: Any, key: str, default: Any = None) -> Any:
                    if isinstance(payload, dict):
                        return payload.get(key, default)
                    return getattr(payload, key, default)

                def _on_tool_start(payload: Any) -> None:
                    metrics["tool_calls_total"] += 1
                    if self.debug:
                        tname = _payload_field(payload, "tool", None) or _payload_field(payload, "tool_name", "?")
                        tc_id = str(_payload_field(payload, "tool_call_id", "") or "")
                        args = _payload_field(payload, "args", None) or _payload_field(payload, "arguments", None)
                        arg_hint = ""
                        if isinstance(args, dict):
                            for k in ("path", "file_path", "pattern", "command", "action", "name"):
                                if k in args and args[k]:
                                    arg_hint = f" {k}={str(args[k])[:60]!r}"
                                    break
                            if not arg_hint and args:
                                arg_hint = f" keys={list(args.keys())[:4]}"
                        key = tc_id or str(tname)
                        turn_state["tool_starts"][key] = (str(tname), datetime.now())
                        self._emit_debug(f"  TOOL start #{metrics['tool_calls_total']}: {tname}{arg_hint}")

                def _on_tool_finish(payload: Any) -> None:
                    if not self.debug:
                        return
                    tc_id = str(_payload_field(payload, "tool_call_id", "") or "")
                    started = turn_state["tool_starts"].pop(tc_id, None) if tc_id else None
                    tname = (
                        _payload_field(payload, "tool", None)
                        or _payload_field(payload, "tool_name", None)
                        or (started[0] if started else None)
                        or _payload_field(payload, "title", "?")
                    )
                    if started is None:
                        started = turn_state["tool_starts"].pop(str(tname), None)
                    status_val = _payload_field(payload, "status", None)
                    ok_val = _payload_field(payload, "ok", None)
                    if ok_val is None and status_val is not None:
                        ok_val = str(status_val).lower() == "completed"
                    ms = int((datetime.now() - started[1]).total_seconds() * 1000) if started else -1
                    err_val = _payload_field(payload, "error", None)
                    err_str = f" err={str(err_val)[:80]}" if err_val else ""
                    self._emit_debug(f"  TOOL done  {tname} ok={ok_val} ({ms}ms){err_str}")

                def _on_tool_error(payload: Any) -> None:
                    err = str(payload.get("error", "") if isinstance(payload, dict) else payload).lower()
                    if any(
                        tok in err
                        for tok in ("malformed", "invalid argument", "validation", "missing required", "json", "unknown tool")
                    ):
                        metrics["malformed_tool_calls"] += 1
                    if self.debug:
                        tname = _payload_field(payload, "tool", None) or _payload_field(payload, "tool_name", "?")
                        self._emit_debug(f"  TOOL error {tname}: {err[:120]}")

                def _payload_int(payload: Any, key: str) -> int:
                    if isinstance(payload, dict):
                        val = payload.get(key)
                    else:
                        val = getattr(payload, key, None)
                    try:
                        return int(val or 0)
                    except (TypeError, ValueError):
                        return 0

                def _payload_float(payload: Any, key: str) -> float:
                    if isinstance(payload, dict):
                        val = payload.get(key)
                    else:
                        val = getattr(payload, key, None)
                    try:
                        return float(val or 0.0)
                    except (TypeError, ValueError):
                        return 0.0

                def _on_model_response(payload: Any) -> None:
                    pt = _payload_int(payload, "prompt_tokens")
                    ct = _payload_int(payload, "completion_tokens")
                    metrics["prompt_tokens"] += pt
                    metrics["completion_tokens"] += ct
                    lat = _payload_float(payload, "latency")
                    if metrics["prefill_latency_ms"] == 0.0 and lat > 0.0:
                        metrics["prefill_latency_ms"] = lat * 1000.0
                    turn_state["llm_calls"] += 1
                    turn_state["llm_seconds"] += lat
                    if self.debug:
                        m_name = _payload_field(payload, "model", "") or ""
                        self._emit_debug(
                            f"  LLM  call #{turn_state['llm_calls']}: {lat:.2f}s | tok=({pt}p/{ct}c) | model={m_name}"
                        )

                def _on_usage_turn_summary(payload: Any) -> None:
                    if metrics["prompt_tokens"] == 0 and metrics["completion_tokens"] == 0:
                        metrics["prompt_tokens"] += (
                            _payload_int(payload, "prompt_tokens")
                            or _payload_int(payload, "input_tokens")
                        )
                        metrics["completion_tokens"] += (
                            _payload_int(payload, "completion_tokens")
                            or _payload_int(payload, "output_tokens")
                        )

                for topic, fn in (
                    ("tool.execute.start", _on_tool_start),
                    ("tool.execute.finish", _on_tool_finish),
                    ("tool.execute.error", _on_tool_error),
                    ("model.response", _on_model_response),
                    ("usage.turn_summary", _on_usage_turn_summary),
                ):
                    try:
                        bus.subscribe(topic, fn)
                        subscriptions.append((bus, topic, fn))
                    except Exception:
                        pass

                try:
                    from src.core.orchestration.event_bus import get_event_bus as _get_global_eb

                    global_bus = _get_global_eb()
                    if global_bus is not None and global_bus is not bus and hasattr(global_bus, "subscribe"):
                        global_bus.subscribe("model.response", _on_model_response)
                        subscriptions.append((global_bus, "model.response", _on_model_response))
                except Exception:
                    pass

            # TASK-39: In isolated temporary evaluation workdirs with no interactive
            # TUI attached, allow sandboxed `run_tests`, `bash`, and `delegate_task`
            # via scoped autonomous approval so the agent doesn't hang 120s waiting
            # for a non-existent approval button. Always restore prior state.
            _prev_autonomous: Optional[bool] = None
            _prev_approve: Optional[set[str]] = None
            try:
                from src.tools import tools_config as _tc

                _prev_autonomous = _tc.is_autonomous()
                _prev_approve = set(_tc.get_autonomous_approve())
                if not _prev_autonomous:
                    _tc.set_autonomous(True)
                    _tc.set_autonomous_approve(
                        _prev_approve | {"run_tests", "bash", "delegate_task"}
                    )
            except Exception:
                _prev_autonomous = None

            try:
                _run_agent_for_scenario(agent, scenario, scenario_dir)
            except Exception as agent_err:
                logger.warning(
                    f"ScenarioEvaluator: agent raised during scenario '{scenario.name}': {agent_err}"
                )
                end_time = datetime.now()
                return ScenarioResult(
                    scenario_name=scenario.name,
                    status="error",
                    start_time=start_time,
                    end_time=end_time,
                    duration_seconds=(end_time - start_time).total_seconds(),
                    error=str(agent_err),
                    **metrics,
                )
            finally:
                if _prev_autonomous is not None:
                    try:
                        from src.tools import tools_config as _tc

                        _tc.set_autonomous(_prev_autonomous)
                        _tc.set_autonomous_approve(_prev_approve or set())
                    except Exception:
                        pass

            # TASK-39: Fallback to agent cost_tracker totals if event-bus metrics remained 0
            if (
                metrics["prompt_tokens"] == 0
                and metrics["completion_tokens"] == 0
                and agent is not None
            ):
                tracker = getattr(agent, "cost_tracker", None) or getattr(agent, "_cost_tracker", None)
                if tracker is not None:
                    try:
                        metrics["prompt_tokens"] = int(getattr(tracker, "total_prompt_tokens", 0) or 0)
                        metrics["completion_tokens"] = int(getattr(tracker, "total_completion_tokens", 0) or 0)
                    except Exception:
                        pass

            # Verify
            v_start = datetime.now()
            passed, verification_output, edit_ok, verify_ran, verify_ok = (
                self._verify_scenario_detailed(scenario, scenario_dir)
            )
            v_ms = int((datetime.now() - v_start).total_seconds() * 1000)

            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()
            if self.debug:
                v_snip = ""
                if not passed and verification_output:
                    v_snip = f" | {str(verification_output).strip().splitlines()[-1][:120]}"
                self._emit_debug(
                    f"<<< {scenario.name}: {'PASS' if passed else 'FAIL'} in {duration:.2f}s "
                    f"(llm={turn_state['llm_calls']} calls/{turn_state['llm_seconds']:.2f}s, "
                    f"tools={metrics['tool_calls_total']}, verify={v_ms}ms, "
                    f"tok={metrics['prompt_tokens']}p/{metrics['completion_tokens']}c){v_snip}"
                )

            return ScenarioResult(
                scenario_name=scenario.name,
                status="pass" if passed else "fail",
                start_time=start_time,
                end_time=end_time,
                duration_seconds=duration,
                verification_output=verification_output,
                edit_accurate=edit_ok,
                verification_attempted=verify_ran,
                verification_passed=verify_ok,
                **metrics,
            )

        except Exception as e:
            end_time = datetime.now()
            return ScenarioResult(
                scenario_name=scenario.name,
                status="error",
                start_time=start_time,
                end_time=end_time,
                duration_seconds=(end_time - start_time).total_seconds(),
                error=str(e),
                **metrics,
            )
        finally:
            for b, topic, fn in subscriptions:
                if hasattr(b, "unsubscribe"):
                    try:
                        b.unsubscribe(topic, fn)
                    except Exception:
                        pass
            if agent is not None:
                close_fn = getattr(agent, "close", None)
                if callable(close_fn):
                    try:
                        close_fn()
                    except Exception as close_err:
                        logger.debug(f"ScenarioEvaluator: agent.close() failed: {close_err}")
            # Cleanup
            if scenario_dir and scenario.cleanup_command:
                try:
                    cleanup_parts = shlex.split(scenario.cleanup_command)
                    if cleanup_parts and cleanup_parts[0] == "python":
                        cleanup_parts[0] = sys.executable
                    subprocess.run(
                        cleanup_parts,
                        shell=False,
                        cwd=scenario_dir,
                        capture_output=True,
                        timeout=30,
                    )
                except Exception as e:
                    logger.warning(f"Cleanup failed for {scenario.name}: {e}")

    def run_evaluation(
        self,
        agent_factory: Callable[[], Any],
    ) -> List[ScenarioResult]:
        """Run all scenarios."""
        self.results = []

        logger.info(f"Starting evaluation with {len(self.scenarios)} scenarios")

        for scenario in self.scenarios:
            result = self.run_scenario(scenario, agent_factory)
            self.results.append(result)
            logger.info(f"Scenario {scenario.name}: {result.status}")

        return self.results

    def get_summary(
        self, results: Optional[List[ScenarioResult]] = None
    ) -> Dict[str, Any]:
        """Get evaluation summary."""
        results = results or self.results

        passed = sum(1 for r in results if r.status == "pass")
        failed = sum(1 for r in results if r.status == "fail")
        errors = sum(1 for r in results if r.status == "error")

        total_duration = sum(r.duration_seconds for r in results)
        total_tool_calls = sum(getattr(r, "tool_calls_total", 0) for r in results)
        total_malformed = sum(getattr(r, "malformed_tool_calls", 0) for r in results)
        edit_accurate_count = sum(1 for r in results if getattr(r, "edit_accurate", False))
        verify_attempted = [r for r in results if getattr(r, "verification_attempted", False)]
        verify_passed_count = sum(1 for r in verify_attempted if getattr(r, "verification_passed", False))
        total_prompt_tokens = sum(getattr(r, "prompt_tokens", 0) for r in results)
        total_completion_tokens = sum(getattr(r, "completion_tokens", 0) for r in results)
        latencies = [float(getattr(r, "prefill_latency_ms", 0.0)) for r in results if getattr(r, "prefill_latency_ms", 0.0) > 0]

        return {
            "total": len(results),
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "pass_rate": passed / len(results) if results else 0,
            "pass_at_1": passed / len(results) if results else 0.0,
            "edit_accuracy": edit_accurate_count / len(results) if results else 0.0,
            "malformed_tool_call_rate": (
                total_malformed / total_tool_calls if total_tool_calls > 0 else 0.0
            ),
            "verification_pass_rate": (
                verify_passed_count / len(verify_attempted) if verify_attempted else 0.0
            ),
            "total_prompt_tokens": total_prompt_tokens,
            "total_completion_tokens": total_completion_tokens,
            "avg_prefill_latency_ms": (
                sum(latencies) / len(latencies) if latencies else 0.0
            ),
            "total_duration_seconds": total_duration,
            "average_duration_seconds": total_duration / len(results) if results else 0,
        }

    def get_summary_by_category(
        self, results: Optional[List[ScenarioResult]] = None
    ) -> Dict[str, Any]:
        """M5: Break summary down by scenario category and difficulty."""
        results = results or self.results
        # Build name→scenario map for metadata lookup
        sc_map = {s.name: s for s in self.scenarios}

        by_category: Dict[str, Dict[str, int]] = {}
        by_difficulty: Dict[str, Dict[str, int]] = {}
        for r in results:
            sc = sc_map.get(r.scenario_name)
            cat = sc.category if sc else "unknown"
            diff = sc.difficulty if sc else "unknown"

            by_category.setdefault(cat, {"pass": 0, "fail": 0, "error": 0})
            by_difficulty.setdefault(diff, {"pass": 0, "fail": 0, "error": 0})
            by_category[cat][r.status] = by_category[cat].get(r.status, 0) + 1
            by_difficulty[diff][r.status] = by_difficulty[diff].get(r.status, 0) + 1

        return {
            "by_category": by_category,
            "by_difficulty": by_difficulty,
        }

    def export_results(self, filepath: str):
        """Export results to JSON file."""
        data = {
            "summary": self.get_summary(),
            "results": [
                {
                    "scenario_name": r.scenario_name,
                    "status": r.status,
                    "start_time": r.start_time.isoformat(),
                    "end_time": r.end_time.isoformat(),
                    "duration_seconds": r.duration_seconds,
                    "error": r.error,
                    "verification_output": r.verification_output,
                }
                for r in self.results
            ],
        }

        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)

        logger.info(f"Results exported to {filepath}")



from src.core.evaluation.default_scenarios import get_default_scenarios  # noqa: E402


def run_benchmark(
    agent_factory: Callable[[], Any],
    scenarios: Optional[List[Scenario]] = None,
) -> Dict[str, Any]:
    """Run a quick benchmark with default scenarios."""
    evaluator = ScenarioEvaluator()

    if scenarios is None:
        scenarios = get_default_scenarios()

    for scenario in scenarios:
        evaluator.add_scenario(scenario)

    results = evaluator.run_evaluation(agent_factory)
    summary = evaluator.get_summary(results)

    return summary


# ---------------------------------------------------------------------------
# G10: pass@k — unbiased estimator (Chen et al. 2021, HumanEval paper)
# ---------------------------------------------------------------------------

def pass_at_k(n: int, c: int, k: int) -> float:
    """Unbiased estimator for pass@k.

    Given *n* independent samples of a scenario, *c* of which passed, return
    the probability that at least one of *k* randomly chosen samples passes.

    Formula:  pass@k = 1 - C(n-c, k) / C(n, k)

    Args:
        n: total number of attempts.
        c: number of passing attempts (c <= n).
        k: number of samples to consider (k <= n).

    Returns:
        Estimated probability in [0, 1].

    Raises:
        ValueError: if k > n or c > n or any argument is negative.
    """
    if n < 0 or c < 0 or k < 0:
        raise ValueError(f"n, c, k must be non-negative; got n={n}, c={c}, k={k}")
    if c > n:
        raise ValueError(f"c ({c}) cannot exceed n ({n})")
    if k > n:
        raise ValueError(f"k ({k}) cannot exceed n ({n})")
    if k == 0:
        return 0.0
    if n == 0:
        return 0.0
    # Use log-space to avoid overflow for large n.
    # log C(n-c, k) = log_comb(n-c, k); log C(n, k) = log_comb(n, k)
    if n - c < k:
        # Fewer failures than k — at least one pass is guaranteed.
        return 1.0
    log_numerator = math.lgamma(n - c + 1) - math.lgamma(k + 1) - math.lgamma(n - c - k + 1)
    log_denominator = math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)
    return float(1.0 - math.exp(log_numerator - log_denominator))


def run_pass_at_k(
    scenario: "Scenario",
    agent_factory: Callable[[], Any],
    n: int,
    k: int,
    workdir: Optional[str] = None,
) -> Dict[str, Any]:
    """Run *scenario* n times and return pass@k alongside raw counts.

    Each run gets its own isolated temporary directory so side-effects from
    one attempt cannot influence the next.

    Args:
        scenario: The scenario to evaluate.
        agent_factory: Callable that returns a fresh agent for each attempt.
        n: Total number of independent attempts.
        k: k for the pass@k estimate (must be <= n).
        workdir: Optional parent directory for temporary run directories.

    Returns:
        dict with keys ``n``, ``c`` (passes), ``k``, ``pass_at_k``,
        ``results`` (list of ScenarioResult).
    """
    if k > n:
        raise ValueError(f"k ({k}) must be <= n ({n})")

    import shutil

    created_temp = workdir is None
    base = Path(workdir) if workdir else Path(tempfile.mkdtemp())
    results: List["ScenarioResult"] = []

    try:
        for attempt in range(n):
            attempt_dir = base / f"{scenario.name}_attempt_{attempt}"
            attempt_dir.mkdir(parents=True, exist_ok=True)
            evaluator = ScenarioEvaluator(workdir=str(attempt_dir))
            result = evaluator.run_scenario(scenario, agent_factory)
            results.append(result)

        c = sum(1 for r in results if r.status == "pass")
        return {
            "scenario": scenario.name,
            "n": n,
            "c": c,
            "k": k,
            "pass_at_k": pass_at_k(n, c, k),
            "results": results,
        }
    finally:
        if created_temp:
            shutil.rmtree(base, ignore_errors=True)
