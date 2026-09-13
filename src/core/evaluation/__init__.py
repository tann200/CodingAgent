"""Public API for the CodingAgent evaluation framework (audit PHASE-3 item 3.2)."""

from src.core.evaluation.scenario_evaluator import (
    Scenario,
    ScenarioEvaluator,
    ScenarioResult,
    get_default_scenarios,
    pass_at_k,
    run_benchmark,
    run_pass_at_k,
)

__all__ = [
    "Scenario",
    "ScenarioEvaluator",
    "ScenarioResult",
    "get_default_scenarios",
    "pass_at_k",
    "run_benchmark",
    "run_pass_at_k",
]