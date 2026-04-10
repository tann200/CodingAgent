"""ET-3: Regression benchmark baseline tests.

Covers:
  ET3-1  baseline.json is valid JSON and has the expected top-level keys
  ET3-2  All bench_pipeline SCENARIO names have a threshold entry in baseline.json
  ET3-3  Threshold values are positive floats
  ET3-4  regression_multiplier is a positive float >= 1.0
  ET3-5  check_regression() helper correctly flags a slow result
  ET3-6  check_regression() does not flag a result within threshold
"""

from __future__ import annotations

import json
from pathlib import Path

BASELINE_PATH = Path(__file__).parents[2] / "benchmarks" / "baseline.json"
BENCH_PIPELINE = Path(__file__).parents[2] / "benchmarks" / "bench_pipeline.py"


def _load_baseline() -> dict:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# ET3-1  baseline.json structure
# ---------------------------------------------------------------------------


def test_et3_1_baseline_json_valid():
    """ET3-1: baseline.json parses as a valid JSON object with required keys."""
    baseline = _load_baseline()
    for key in ("version", "thresholds", "regression_multiplier"):
        assert key in baseline, f"Missing key {key!r} in baseline.json"
    assert isinstance(baseline["thresholds"], dict)


# ---------------------------------------------------------------------------
# ET3-2  All scenarios have a threshold
# ---------------------------------------------------------------------------


def test_et3_2_all_scenarios_covered():
    """ET3-2: Every SCENARIO name in bench_pipeline.py has a threshold in baseline.json."""
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("bench_pipeline", BENCH_PIPELINE)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load spec from {BENCH_PIPELINE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    scenario_names = {s["name"] for s in module.SCENARIOS}

    baseline = _load_baseline()
    thresholds = set(baseline["thresholds"].keys())

    missing = scenario_names - thresholds
    assert not missing, f"Scenarios missing from baseline.json thresholds: {missing}"


# ---------------------------------------------------------------------------
# ET3-3  Threshold values are positive floats
# ---------------------------------------------------------------------------


def test_et3_3_threshold_values_positive():
    """ET3-3: All threshold values in baseline.json are positive numbers."""
    baseline = _load_baseline()
    for name, val in baseline["thresholds"].items():
        assert isinstance(val, (int, float)), f"Threshold {name!r} is not numeric"
        assert val > 0, f"Threshold {name!r} = {val} is not positive"


# ---------------------------------------------------------------------------
# ET3-4  regression_multiplier is >= 1.0
# ---------------------------------------------------------------------------


def test_et3_4_regression_multiplier_valid():
    """ET3-4: regression_multiplier is a float >= 1.0."""
    baseline = _load_baseline()
    mult = baseline["regression_multiplier"]
    assert isinstance(mult, (int, float)), "regression_multiplier must be numeric"
    assert mult >= 1.0, f"regression_multiplier {mult} < 1.0 makes no sense"


# ---------------------------------------------------------------------------
# ET3-5 / ET3-6  check_regression() helper
# ---------------------------------------------------------------------------


def _check_regression(scenario_name: str, wall_time_s: float, baseline: dict) -> bool:
    """Return True if wall_time_s exceeds threshold × multiplier (regression)."""
    threshold = baseline["thresholds"].get(scenario_name)
    if threshold is None:
        return False  # unknown scenario — not a regression
    multiplier = baseline.get("regression_multiplier", 3.0)
    return wall_time_s > threshold * multiplier


def test_et3_5_flags_regression():
    """ET3-5: check_regression() returns True when time greatly exceeds threshold."""
    baseline = _load_baseline()
    # fast_path_write threshold = 10.0; multiplier = 3.0 → limit = 30.0
    # Pass 100 s — clearly a regression.
    assert _check_regression("fast_path_write", 100.0, baseline) is True


def test_et3_6_no_flag_within_threshold():
    """ET3-6: check_regression() returns False for a result within threshold."""
    baseline = _load_baseline()
    # fast_path_write threshold = 10.0; multiplier = 3.0 → limit = 30.0
    # Pass 5 s — well within threshold.
    assert _check_regression("fast_path_write", 5.0, baseline) is False
