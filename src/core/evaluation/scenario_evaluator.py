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
                        args = _payload_field(payload, "args", None) or _payload_field(payload, "arguments", None)
                        arg_hint = ""
                        if isinstance(args, dict):
                            for k in ("path", "file_path", "pattern", "command", "action", "name"):
                                if k in args and args[k]:
                                    arg_hint = f" {k}={str(args[k])[:60]!r}"
                                    break
                            if not arg_hint and args:
                                arg_hint = f" keys={list(args.keys())[:4]}"
                        turn_state["tool_starts"][str(tname)] = datetime.now()
                        self._emit_debug(f"  TOOL start #{metrics['tool_calls_total']}: {tname}{arg_hint}")

                def _on_tool_finish(payload: Any) -> None:
                    if not self.debug:
                        return
                    tname = _payload_field(payload, "tool", None) or _payload_field(payload, "tool_name", "?")
                    ok_val = _payload_field(payload, "ok", None)
                    t_start = turn_state["tool_starts"].pop(str(tname), None)
                    ms = int((datetime.now() - t_start).total_seconds() * 1000) if t_start else -1
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


# Built-in scenarios
def get_default_scenarios() -> List[Scenario]:
    """Get a set of default evaluation scenarios.

    P4-T1: Extended from 3 trivial scenarios to 23 covering:
    single-file edits, multi-file refactoring, bug fix, task disambiguation,
    tool failure recovery, plan adherence, context boundary, and delegation.
    """
    return [
        # ── Original 3 ──────────────────────────────────────────────────────
        Scenario(
            name="simple_function",
            description="Create a simple function that returns a string",
            task="Create a file called hello.py with a function hello() that returns 'Hello World'",
            expected_files={"hello.py": "def hello"},
            test_command="python -c \"from hello import hello; assert hello() == 'Hello World'\"",
            difficulty="easy",
            category="single_file_edit",
        ),
        Scenario(
            name="class_definition",
            description="Create a class with methods",
            task="Create a file calculator.py with a Calculator class that has add and subtract methods",
            expected_files={"calculator.py": "class Calculator:"},
            test_command='python -c "from calculator import Calculator; c = Calculator(); assert c.add(1, 2) == 3"',
            difficulty="easy",
            category="single_file_edit",
        ),
        Scenario(
            name="test_creation",
            description="Create a simple unit test",
            task="Create a test file test_math.py with a test for a math function",
            expected_files={"test_math.py": "import pytest"},
            test_command="python -m pytest -q test_math.py",
            difficulty="easy",
            category="single_file_edit",
        ),

        # ── Single-file edits ────────────────────────────────────────────────
        Scenario(
            name="rename_function",
            description="Rename a function in an existing file",
            task="Rename the function `compute_total` to `calculate_total` in utils.py",
            setup_files={
                "utils.py": (
                    "def compute_total(items):\n"
                    "    return sum(items)\n"
                ),
            },
            expected_files={"utils.py": "def calculate_total"},
            test_command=(
                "python -c \""
                "import utils; "
                "assert not hasattr(utils, 'compute_total') and utils.calculate_total([1, 2, 3]) == 6\""
            ),
            difficulty="easy",
            category="single_file_edit",
            tags=["rename"],
        ),
        Scenario(
            name="add_docstring",
            description="Add a docstring to an existing function",
            task="Add a Google-style docstring to the function `parse_config` in config.py that describes what it does and its parameters.",
            setup_files={
                "config.py": (
                    "def parse_config(path):\n"
                    "    with open(path) as f:\n"
                    "        return f.read()\n"
                ),
            },
            expected_files={"config.py": '"""'},
            test_command=(
                "python -c \""
                "import ast; "
                "tree = ast.parse(open('config.py').read()); "
                "fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'parse_config'); "
                "assert ast.get_docstring(fn)\""
            ),
            difficulty="easy",
            category="single_file_edit",
            tags=["docstring"],
        ),
        Scenario(
            name="fix_off_by_one",
            description="Fix an off-by-one error in a loop",
            task=(
                "Fix the off-by-one error in the `get_last_n` function in list_utils.py. "
                "The function should return the last n items of a list but currently misses the last element."
            ),
            setup_files={
                "list_utils.py": (
                    "def get_last_n(items, n):\n"
                    "    return items[len(items)-n:len(items)-1]\n"  # bug: -1 drops last
                ),
            },
            expected_files={"list_utils.py": "def get_last_n"},
            test_command=(
                "python -c \""
                "from list_utils import get_last_n; "
                "assert get_last_n([1,2,3,4,5], 3) == [3,4,5], 'off-by-one not fixed'\""
            ),
            difficulty="medium",
            category="single_file_edit",
            tags=["bug_fix", "off_by_one"],
        ),
        Scenario(
            name="add_type_hints",
            description="Add type hints to an untyped function",
            task="Add Python type hints to all parameters and the return type of the `merge_dicts` function in merge.py.",
            setup_files={
                "merge.py": (
                    "def merge_dicts(a, b):\n"
                    "    result = {}\n"
                    "    result.update(a)\n"
                    "    result.update(b)\n"
                    "    return result\n"
                ),
            },
            expected_files={"merge.py": "->"},
            test_command=(
                "python -c \""
                "import inspect, merge; "
                "sig = inspect.signature(merge.merge_dicts); "
                "assert all(p.annotation is not inspect.Parameter.empty for p in sig.parameters.values()); "
                "assert sig.return_annotation is not inspect.Parameter.empty; "
                "assert merge.merge_dicts({'a': 1}, {'b': 2}) == {'a': 1, 'b': 2}\""
            ),
            difficulty="easy",
            category="single_file_edit",
            tags=["type_hints"],
        ),
        Scenario(
            name="add_error_handling",
            description="Add try/except error handling to a function",
            task=(
                "Add proper error handling to `read_json_file` in file_reader.py. "
                "It should raise a ValueError with a descriptive message if the file "
                "does not exist or contains invalid JSON."
            ),
            setup_files={
                "file_reader.py": (
                    "import json\n\n"
                    "def read_json_file(path):\n"
                    "    with open(path) as f:\n"
                    "        return json.load(f)\n"
                ),
            },
            expected_files={"file_reader.py": "ValueError"},
            test_command=(
                "python -c \""
                "from file_reader import read_json_file; "
                "open('bad.json', 'w').write('{bad'); "
                "open('good.json', 'w').write('{\\\"ok\\\": true}'); "
                "assert read_json_file('good.json') == {'ok': True}; "
                "errs = 0\n"
                "try:\n read_json_file('missing.json')\nexcept ValueError:\n errs += 1\n"
                "try:\n read_json_file('bad.json')\nexcept ValueError:\n errs += 1\n"
                "assert errs == 2\""
            ),
            difficulty="medium",
            category="single_file_edit",
            tags=["error_handling"],
        ),

        # ── Multi-file refactoring ───────────────────────────────────────────
        Scenario(
            name="move_class_update_imports",
            description="Move a class to a new module and update all import references",
            task=(
                "Move the `UserValidator` class from validators.py into a new file "
                "user_validator.py. Update the import in app.py so it imports "
                "`UserValidator` from `user_validator` instead of `validators`."
            ),
            setup_files={
                "validators.py": (
                    "class UserValidator:\n"
                    "    def validate(self, user):\n"
                    "        return bool(user.get('name'))\n"
                ),
                "app.py": (
                    "from validators import UserValidator\n\n"
                    "def process(user):\n"
                    "    v = UserValidator()\n"
                    "    return v.validate(user)\n"
                ),
            },
            expected_files={
                "user_validator.py": "class UserValidator",
                "app.py": "from user_validator import UserValidator",
            },
            test_command=(
                "python -c \""
                "from user_validator import UserValidator; "
                "from app import process; "
                "assert process({'name': 'A'}) is True and process({}) is False\""
            ),
            difficulty="medium",
            category="multi_file",
            tags=["refactor", "move_class"],
        ),
        Scenario(
            name="split_module",
            description="Split a monolithic module into two focused modules",
            task=(
                "Split monolith.py into two files: "
                "math_utils.py (containing `add` and `multiply`) and "
                "string_utils.py (containing `upper_case` and `reverse_string`). "
                "Keep the functions identical."
            ),
            setup_files={
                "monolith.py": (
                    "def add(a, b): return a + b\n"
                    "def multiply(a, b): return a * b\n"
                    "def upper_case(s): return s.upper()\n"
                    "def reverse_string(s): return s[::-1]\n"
                ),
            },
            expected_files={
                "math_utils.py": "def add",
                "string_utils.py": "def upper_case",
            },
            test_command=(
                "python -c \""
                "from math_utils import add, multiply; "
                "from string_utils import upper_case, reverse_string; "
                "assert add(2, 3) == 5 and multiply(2, 4) == 8 and upper_case('hi') == 'HI' and reverse_string('abc') == 'cba'\""
            ),
            difficulty="medium",
            category="multi_file",
            tags=["refactor", "split"],
        ),
        Scenario(
            name="add_shared_constants",
            description="Extract magic numbers into a shared constants module",
            task=(
                "Extract the magic number `3.14159` from geometry.py and "
                "the magic number `9.81` from physics.py into a new file "
                "constants.py. Update both files to import from constants.py."
            ),
            setup_files={
                "geometry.py": "def circle_area(r): return 3.14159 * r * r\n",
                "physics.py": "def weight(mass): return mass * 9.81\n",
            },
            expected_files={
                "constants.py": "3.14159",
                "geometry.py": "from constants import",
                "physics.py": "from constants import",
            },
            test_command=(
                "python -c \""
                "import constants, geometry, physics; "
                "assert abs(geometry.circle_area(1) - 3.14159) < 1e-4 and abs(physics.weight(2) - 19.62) < 1e-4\""
            ),
            difficulty="medium",
            category="multi_file",
            tags=["refactor", "constants"],
        ),

        # ── Bug introduction / fix ───────────────────────────────────────────
        Scenario(
            name="fix_null_pointer",
            description="Fix a None-reference bug",
            task=(
                "Fix the bug in profile.py: `get_display_name` crashes with "
                "AttributeError when `user` is None. It should return 'Anonymous' instead."
            ),
            setup_files={
                "profile.py": (
                    "def get_display_name(user):\n"
                    "    return user['name'].title()\n"
                ),
            },
            expected_files={"profile.py": "Anonymous"},
            test_command=(
                "python -c \""
                "from profile import get_display_name; "
                "assert get_display_name(None) == 'Anonymous'\""
            ),
            difficulty="medium",
            category="bug_fix",
            tags=["null_check"],
        ),
        Scenario(
            name="fix_resource_leak",
            description="Fix a file-handle resource leak",
            task=(
                "Fix the resource leak in logger.py: `append_log` opens the file "
                "but never closes it. Use a context manager to ensure the file "
                "is always closed."
            ),
            setup_files={
                "logger.py": (
                    "def append_log(path, message):\n"
                    "    f = open(path, 'a')\n"
                    "    f.write(message + '\\n')\n"
                ),
            },
            expected_files={"logger.py": "with open"},
            test_command=(
                "python -c \""
                "src = open('logger.py').read(); assert 'with ' in src; "
                "import logger; logger.append_log('out.log', 'hello'); "
                "assert 'hello' in open('out.log').read()\""
            ),
            difficulty="easy",
            category="bug_fix",
            tags=["resource_leak"],
        ),
        Scenario(
            name="fix_mutable_default_arg",
            description="Fix the mutable default argument anti-pattern",
            task=(
                "Fix the mutable default argument bug in accumulator.py: "
                "`collect` uses `items=[]` as a default argument, which causes "
                "values to persist across calls. Fix it using the `None` sentinel pattern."
            ),
            setup_files={
                "accumulator.py": (
                    "def collect(value, items=[]):\n"
                    "    items.append(value)\n"
                    "    return items\n"
                ),
            },
            expected_files={"accumulator.py": "if items is None"},
            test_command=(
                "python -c \""
                "from accumulator import collect; "
                "a = collect(1); b = collect(2); "
                "assert a != b, 'mutable default not fixed'\""
            ),
            difficulty="medium",
            category="bug_fix",
            tags=["mutable_default"],
        ),

        # ── Context boundary (requires reading multiple files) ───────────────
        Scenario(
            name="trace_call_chain",
            description="Follow a 5-file call chain to find the root cause",
            task=(
                "The function `run_pipeline` in pipeline.py is raising a KeyError. "
                "Trace through the call chain (pipeline.py → stage.py → transform.py → "
                "validator.py → schema.py) and fix the root cause."
            ),
            setup_files={
                "schema.py": "REQUIRED_FIELDS = ['name', 'value']\n",
                "validator.py": (
                    "from schema import REQUIRED_FIELDS\n\n"
                    "def validate(record):\n"
                    "    for f in REQUIRED_FIELDS:\n"
                    "        _ = record[f]  # KeyError if missing\n"
                    "    return True\n"
                ),
                "transform.py": (
                    "from validator import validate\n\n"
                    "def transform(record):\n"
                    "    validate(record)\n"
                    "    return {k: str(v) for k, v in record.items()}\n"
                ),
                "stage.py": (
                    "from transform import transform\n\n"
                    "def process_stage(records):\n"
                    "    return [transform(r) for r in records]\n"
                ),
                "pipeline.py": (
                    "from stage import process_stage\n\n"
                    "def run_pipeline(data):\n"
                    "    return process_stage(data)\n"
                ),
            },
            expected_files={"validator.py": "get("},
            test_command=(
                "python -c \""
                "from pipeline import run_pipeline; "
                "assert run_pipeline([{'name': 'a'}]) == [{'name': 'a'}]\""
            ),
            difficulty="hard",
            category="context_boundary",
            tags=["multi_file_read", "call_chain"],
        ),
        Scenario(
            name="dependency_graph_refactor",
            description="Refactor code that spans 5 interdependent files",
            task=(
                "Add a `timeout` parameter (default=30) to `fetch_data` in fetcher.py. "
                "Propagate it through: fetcher.py → session.py → connection.py → "
                "retry.py → config.py (where the default should be defined as "
                "`DEFAULT_TIMEOUT = 30`)."
            ),
            setup_files={
                "config.py": "# Connection configuration\nMAX_RETRIES = 3\n",
                "retry.py": "def with_retries(fn, retries):\n    return fn()\n",
                "connection.py": "def connect(host): return {'host': host}\n",
                "session.py": "from connection import connect\ndef open_session(host): return connect(host)\n",
                "fetcher.py": "from session import open_session\ndef fetch_data(url): return open_session(url)\n",
            },
            expected_files={"config.py": "DEFAULT_TIMEOUT"},
            test_command=(
                "python -c \""
                "import inspect, config, fetcher; "
                "assert config.DEFAULT_TIMEOUT == 30 and inspect.signature(fetcher.fetch_data).parameters['timeout'].default == 30\""
            ),
            difficulty="hard",
            category="context_boundary",
            tags=["parameter_propagation"],
        ),

        # ── Plan adherence ───────────────────────────────────────────────────
        Scenario(
            name="multi_step_crud",
            description="Implement a multi-step CRUD module in order",
            task=(
                "Create a simple in-memory CRUD store in store.py. "
                "Follow this exact order: "
                "1. Define a `Store` class with an empty `_data` dict. "
                "2. Add a `create(key, value)` method. "
                "3. Add a `read(key)` method that returns None if missing. "
                "4. Add an `update(key, value)` method. "
                "5. Add a `delete(key)` method. "
                "6. Write a test file test_store.py that tests all four operations."
            ),
            expected_files={
                "store.py": "class Store",
                "test_store.py": "def test_",
            },
            test_command=(
                "python -c \""
                "from store import Store; s = Store(); "
                "s.create('k', 1); assert s.read('k') == 1; "
                "s.update('k', 2); assert s.read('k') == 2; "
                "s.delete('k'); assert s.read('k') is None\""
            ),
            difficulty="medium",
            category="plan_adherence",
            tags=["crud", "multi_step"],
        ),

        # ── Tool failure recovery ────────────────────────────────────────────
        Scenario(
            name="recover_from_syntax_error",
            description="Agent self-corrects after writing syntactically invalid Python",
            task=(
                "Create a file sorter.py with a function `sort_descending(items)` "
                "that returns the list sorted in descending order. "
                "Make sure the file is valid Python."
            ),
            expected_files={"sorter.py": "def sort_descending"},
            test_command=(
                "python -c \""
                "import ast; ast.parse(open('sorter.py').read()); "
                "from sorter import sort_descending; "
                "assert sort_descending([3,1,2]) == [3,2,1]\""
            ),
            difficulty="easy",
            category="tool_failure_recovery",
            tags=["syntax_check", "self_correction"],
        ),
        Scenario(
            name="recover_from_import_error",
            description="Agent fixes a broken import after creating it",
            task=(
                "Create main.py that imports and uses `greet` from greeter.py. "
                "Also create greeter.py with the `greet(name)` function that "
                "returns f'Hello, {name}!'. Ensure both files work together."
            ),
            expected_files={
                "greeter.py": "def greet",
                "main.py": "from greeter import greet",
            },
            test_command="python -c \"import main\"",
            difficulty="easy",
            category="tool_failure_recovery",
            tags=["import_fix"],
        ),

        # ── Task disambiguation ──────────────────────────────────────────────
        Scenario(
            name="ambiguous_format_request",
            description="Interpret an ambiguous formatting request sensibly",
            task=(
                "Format the data in records.py. "
                "The file contains a list of dicts; make sure they are consistently "
                "formatted with one dict per line and keys in alphabetical order."
            ),
            setup_files={
                "records.py": (
                    "records = [\n"
                    "    {'z': 3, 'a': 1, 'name': 'Alice'},\n"
                    "    {'name': 'Bob', 'a': 2, 'z': 4},\n"
                    "]\n"
                ),
            },
            expected_files={"records.py": "3}"},
            test_command=(
                "python -c \""
                "import records; "
                "assert len(records.records) == 2 and all(list(r.keys()) == sorted(r.keys()) for r in records.records)\""
            ),
            difficulty="medium",
            category="disambiguation",
            tags=["formatting"],
        ),

        # ── Delegation / subagent ────────────────────────────────────────────
        Scenario(
            name="delegate_test_generation",
            description="Delegate test writing to a subagent",
            task=(
                "We have a module math_ops.py. Delegate to a subagent the task of "
                "writing comprehensive pytest tests for it in test_math_ops.py. "
                "The tests should cover `add`, `subtract`, `multiply`, and `divide` "
                "(divide should raise ZeroDivisionError for divisor=0)."
            ),
            setup_files={
                "math_ops.py": (
                    "def add(a, b): return a + b\n"
                    "def subtract(a, b): return a - b\n"
                    "def multiply(a, b): return a * b\n"
                    "def divide(a, b):\n"
                    "    if b == 0:\n"
                    "        raise ZeroDivisionError('Cannot divide by zero')\n"
                    "    return a / b\n"
                ),
            },
            expected_files={"test_math_ops.py": "def test_"},
            test_command="python -m pytest -q test_math_ops.py",
            difficulty="medium",
            category="delegation",
            tags=["subagent", "test_generation"],
        ),
        Scenario(
            name="delegate_documentation",
            description="Delegate README generation to a subagent",
            task=(
                "Delegate to a subagent the task of writing a README.md for this project. "
                "The README should include: project title, description, installation, "
                "and a usage example. Base it on the code in app.py."
            ),
            setup_files={
                "app.py": (
                    "\"\"\"Simple web scraper utility.\"\"\"\n\n"
                    "def scrape(url, selector):\n"
                    "    \"\"\"Scrape *selector* elements from *url*.\"\"\"\n"
                    "    pass\n"
                ),
            },
            expected_files={"README.md": "# "},
            test_command=(
                "python -c \""
                "txt = open('README.md').read().lower(); "
                "assert '#' in txt and 'install' in txt and ('usage' in txt or 'example' in txt)\""
            ),
            difficulty="easy",
            category="delegation",
            tags=["subagent", "documentation"],
        ),
        Scenario(
            name="circular_import_fix",
            description="Resolve a circular import between two modules",
            task=(
                "Fix the circular import between mod_a.py and mod_b.py while "
                "keeping both `get_a()` and `get_b()` working."
            ),
            setup_files={
                "mod_a.py": "from mod_b import VALUE_B\nVALUE_A = 1\ndef get_a(): return VALUE_A + VALUE_B\n",
                "mod_b.py": "from mod_a import VALUE_A\nVALUE_B = 2\ndef get_b(): return VALUE_A + VALUE_B\n",
            },
            expected_files={"mod_a.py": "def get_a", "mod_b.py": "def get_b"},
            test_command=(
                "python -c \""
                "from mod_a import get_a; from mod_b import get_b; "
                "assert get_a() == 3 and get_b() == 3\""
            ),
            difficulty="medium",
            category="bug_fix",
            tags=["circular_import"],
        ),
    ]


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
