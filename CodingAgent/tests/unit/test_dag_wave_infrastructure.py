"""P4-T4: DAG / wave execution infrastructure — decision test.

Decision (P4-T4): KEEP the infrastructure in sequential-wave mode.

Rationale:
- dag_parser.py, execution_waves, plan_dag, and current_wave are actively used
  across execution_node.py, planning_node.py, replan_node.py, and execution_helpers.py.
- Retiring would require changes across 6+ files with significant regression risk.
- Full parallel activation (asyncio.gather per wave) is deferred to a future release.
- Current behaviour: waves are computed and stored; steps execute sequentially
  within each wave. The infrastructure is ready for parallel dispatch when needed.

These tests verify the infrastructure is intact and the decision is documented.
"""
from pathlib import Path


def test_dag_parser_importable():
    """dag_parser.py must remain importable and export expected symbols."""
    from src.core.orchestration.dag_parser import _convert_flat_to_dag, PlanDAG

    assert callable(_convert_flat_to_dag)
    assert PlanDAG is not None


def test_convert_flat_to_dag_produces_waves():
    """_convert_flat_to_dag converts a flat step list into a PlanDAG with waves."""
    from src.core.orchestration.dag_parser import _convert_flat_to_dag

    steps = [
        {"description": "Step 1"},
        {"description": "Step 2"},
        {"description": "Step 3"},
    ]
    dag = _convert_flat_to_dag(steps)
    waves = dag.topological_sort_waves()
    assert waves is not None
    assert isinstance(waves, list)
    # all steps must be covered
    flat = [item for wave in waves for item in wave]
    assert len(flat) == len(steps)


def test_state_has_wave_fields():
    """AgentState spec must retain plan_dag, execution_waves, current_wave."""
    from src.core.orchestration.graph.state import _AgentStateSpec

    annotations = _AgentStateSpec.__annotations__
    assert "plan_dag" in annotations, "plan_dag field missing from state"
    assert "execution_waves" in annotations, "execution_waves field missing from state"
    assert "current_wave" in annotations, "current_wave field missing from state"


def test_current_wave_in_int_or_none_fields():
    """current_wave must be normalised as int-or-None in state validation."""
    from src.core.orchestration.graph.state import _INT_OR_NONE_FIELDS

    assert "current_wave" in _INT_OR_NONE_FIELDS


def test_planning_result_sets_execution_waves():
    """_build_resolved_plan_result must include execution_waves and current_wave."""
    from src.core.orchestration.graph.nodes.planning_result import _build_resolved_plan_result

    dummy_plan = [{"description": "s1"}, {"description": "s2"}]
    dummy_waves = [["0"], ["1"]]
    result = _build_resolved_plan_result(
        current_plan=dummy_plan,
        current_step=0,
        plan_attempts=1,
        relevant_files=[],
        key_symbols=[],
        affected_files=[],
        execution_waves=dummy_waves,
    )
    assert "execution_waves" in result
    assert result["execution_waves"] == dummy_waves
    assert "current_wave" in result
    assert result["current_wave"] == 0
