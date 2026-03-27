"""
Unit tests for dag_parser.py - Phase A: Dependency DAGs
"""

from src.core.orchestration.dag_parser import (
    PlanDAG,
    StepNode,
    _parse_dag_content,
    _convert_flat_to_dag,
)


class TestStepNode:
    def test_step_node_creation(self):
        step = StepNode(
            step_id="step_0", description="Test step", files=["test.py"], depends_on=[]
        )
        assert step.step_id == "step_0"
        assert step.description == "Test step"
        assert step.files == ["test.py"]
        assert step.depends_on == []
        assert step.status == "pending"


class TestPlanDAG:
    def test_empty_dag(self):
        dag = PlanDAG([])
        assert len(dag.steps) == 0
        assert dag.validate() is True

    def test_single_step_dag(self):
        step = StepNode("step_0", "Test", [], [])
        dag = PlanDAG([step])
        assert len(dag.steps) == 1
        assert dag.validate() is True

    def test_dag_with_dependencies(self):
        steps = [
            StepNode("step_0", "First", [], []),
            StepNode("step_1", "Second", [], ["step_0"]),
            StepNode("step_2", "Third", [], ["step_1"]),
        ]
        dag = PlanDAG(steps)
        assert dag.validate() is True

    def test_cycle_detection(self):
        steps = [
            StepNode("step_0", "First", [], ["step_1"]),
            StepNode("step_1", "Second", [], ["step_0"]),
        ]
        dag = PlanDAG(steps)
        assert dag.validate() is False

    def test_topological_sort_waves(self):
        steps = [
            StepNode("step_0", "First", [], []),
            StepNode("step_1", "Second", [], []),
            StepNode("step_2", "Third", [], ["step_0", "step_1"]),
        ]
        dag = PlanDAG(steps)
        waves = dag.topological_sort_waves()

        assert len(waves) == 2
        assert set(waves[0]) == {"step_0", "step_1"}
        assert waves[1] == ["step_2"]

    def test_get_ready_steps(self):
        steps = [
            StepNode("step_0", "First", [], []),
            StepNode("step_1", "Second", [], ["step_0"]),
        ]
        dag = PlanDAG(steps)

        ready = dag.get_ready_steps(set())
        assert set(ready) == {"step_0"}

        ready = dag.get_ready_steps({"step_0"})
        assert ready == ["step_1"]

    def test_to_todo_format(self):
        steps = [
            StepNode("step_0", "First", ["file1.py"], []),
            StepNode("step_1", "Second", ["file2.py"], ["step_0"]),
        ]
        dag = PlanDAG(steps)

        todo_format = dag.to_todo_format()

        assert len(todo_format) == 2
        assert todo_format[0]["id"] == 0
        assert todo_format[0]["description"] == "First"
        assert todo_format[0]["depends_on"] == []
        assert todo_format[1]["id"] == 1
        assert todo_format[1]["depends_on"] == [0]

    def test_to_todo_markdown(self):
        steps = [
            StepNode("step_0", "First", [], []),
            StepNode("step_1", "Second", [], ["step_0"]),
        ]
        dag = PlanDAG(steps)

        markdown = dag.to_todo_markdown()

        assert "- [ ] **Step 1:** First" in markdown
        assert "- [ ] **Step 2:** Second (depends on: Step 1)" in markdown


class TestParseDAGContent:
    def test_parse_valid_dag_json(self):
        content = """
        {
          "steps": [
            {"step_id": "step_0", "description": "First", "files": [], "depends_on": []},
            {"step_id": "step_1", "description": "Second", "files": ["a.py"], "depends_on": ["step_0"]}
          ]
        }
        """
        dag = _parse_dag_content(content)

        assert dag is not None
        assert len(dag.steps) == 2
        assert dag.steps["step_0"].description == "First"
        assert dag.steps["step_1"].depends_on == ["step_0"]

    def test_parse_invalid_content_returns_none(self):
        content = "This is not valid JSON"
        dag = _parse_dag_content(content)
        assert dag is None


class TestConvertFlatToDAG:
    def test_convert_flat_list(self):
        flat_plan = [
            {"description": "Step 1"},
            {"description": "Step 2"},
        ]
        dag = _convert_flat_to_dag(flat_plan)

        assert len(dag.steps) == 2
        assert dag.steps["step_0"].description == "Step 1"
        assert dag.steps["step_1"].description == "Step 2"
        assert dag.validate() is True
