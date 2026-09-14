"""Public API for the CodingAgent evaluation framework (audit PHASE-3 items 3.2/3.3, PHASE-4 item 4.5)."""

from src.core.evaluation.compare import (
    compare_models,
    run_models,
    save_comparison,
)
from src.core.evaluation.regression import (
    compare_baseline,
    is_regression,
    load_baseline,
    save_baseline,
)
from src.core.evaluation.scenario_evaluator import (
    Scenario,
    ScenarioEvaluator,
    ScenarioResult,
    get_default_scenarios,
    pass_at_k,
    run_benchmark,
    run_pass_at_k,
)
from src.core.evaluation.swebench import (
    SWEBenchInstance,
    SWEBenchRunner,
    load_instances,
)

__all__ = [
    "Scenario",
    "ScenarioEvaluator",
    "ScenarioResult",
    "get_default_scenarios",
    "pass_at_k",
    "run_benchmark",
    "run_pass_at_k",
    "compare_baseline",
    "is_regression",
    "load_baseline",
    "save_baseline",
    "SWEBenchInstance",
    "SWEBenchRunner",
    "load_instances",
    "compare_models",
    "run_models",
    "save_comparison",
]