"""
Tests for advanced memory features: TrajectoryLogger, DreamConsolidator,
RefactoringAgent, ReviewAgent, and SkillLearner.
"""

import pytest
import shutil
import json
from pathlib import Path

from src.core.memory.advanced_features import (
    TrajectoryLogger,
    DreamConsolidator,
    RefactoringAgent,
    ReviewAgent,
    SkillLearner,
)


class TestTrajectoryLogger:
    """Tests for TrajectoryLogger."""

    @pytest.fixture
    def temp_workdir(self, tmp_path):
        """Create a temporary workdir for testing."""
        workdir = tmp_path / "test_workdir"
        workdir.mkdir()
        yield str(workdir)
        shutil.rmtree(workdir, ignore_errors=True)

    def test_trajectory_logger_initialization(self, temp_workdir):
        """Test TrajectoryLogger initializes correctly."""
        logger = TrajectoryLogger(temp_workdir)
        assert logger.workdir == Path(temp_workdir)
        assert logger.trajectory_dir.exists()

    def test_log_run_creates_file(self, temp_workdir):
        """Test logging a run creates a trajectory file."""
        logger = TrajectoryLogger(temp_workdir)

        result = logger.log_run(
            task="Create a function",
            plan="1. Write function\n2. Test function",
            tool_sequence=[{"content": "write_file"}],
            patch="+ def foo(): pass",
            tests="passed",
            success=True,
            session_id="test_001",
        )

        assert Path(result).exists()

        # Verify content
        with open(result) as f:
            data = json.load(f)
            assert data["task"] == "Create a function"
            assert data["success"] is True
            assert data["session_id"] == "test_001"

    def test_get_recent_trajectories(self, temp_workdir):
        """Test retrieving recent trajectories."""
        logger = TrajectoryLogger(temp_workdir)

        # Log multiple runs
        logger.log_run("task1", "plan1", [], "patch1", "tests", True, "session_1")
        logger.log_run("task2", "plan2", [], "patch2", "tests", False, "session_2")
        logger.log_run("task3", "plan3", [], "patch3", "tests", True, "session_3")

        recent = logger.get_recent_trajectories(limit=2)
        assert len(recent) == 2

    def test_get_successful_trajectories(self, temp_workdir):
        """Test filtering successful trajectories."""
        logger = TrajectoryLogger(temp_workdir)

        logger.log_run("task1", "plan1", [], "patch1", "tests", True, "session_1")
        logger.log_run("task2", "plan2", [], "patch2", "tests", False, "session_2")
        logger.log_run("task3", "plan3", [], "patch3", "tests", True, "session_3")

        successful = logger.get_successful_trajectories()
        assert len(successful) == 2
        assert all(s["success"] for s in successful)

    def test_export_training_data(self, temp_workdir):
        """Test exporting training data."""
        logger = TrajectoryLogger(temp_workdir)

        logger.log_run("task1", "plan1", [], "patch1", "tests", True, "session_1")

        output_path = logger.export_training_data()
        assert Path(output_path).exists()

        with open(output_path) as f:
            data = json.load(f)
            assert len(data) == 1


class TestDreamConsolidator:
    """Tests for DreamConsolidator."""

    @pytest.fixture
    def temp_workdir(self, tmp_path):
        """Create a temporary workdir for testing."""
        workdir = tmp_path / "test_workdir"
        workdir.mkdir()
        yield str(workdir)
        shutil.rmtree(workdir, ignore_errors=True)

    def test_consolidator_initialization(self, temp_workdir):
        """Test DreamConsolidator initializes correctly."""
        consolidator = DreamConsolidator(temp_workdir)
        assert consolidator.workdir == Path(temp_workdir)
        assert consolidator.memory_dir.exists()

    def test_consolidate_memories_creates_file(self, temp_workdir):
        """Test consolidation creates a summary file."""
        consolidator = DreamConsolidator(temp_workdir)

        # Create TASK_STATE.md with some content
        task_state = Path(temp_workdir) / ".agent-context" / "TASK_STATE.md"
        task_state.parent.mkdir(parents=True, exist_ok=True)
        task_state.write_text(
            "Completed: def test(): pass\nTask: Create a test function"
        )

        result = consolidator.consolidate_memories()

        assert "timestamp" in result
        assert "patterns" in result
        assert "code_generation" in result["patterns"]

    def test_consolidate_detects_test_pattern(self, temp_workdir):
        """Test consolidation detects test patterns."""
        consolidator = DreamConsolidator(temp_workdir)

        # Create TASK_STATE.md with test content
        task_state = Path(temp_workdir) / ".agent-context" / "TASK_STATE.md"
        task_state.parent.mkdir(parents=True, exist_ok=True)
        task_state.write_text("Running pytest tests")

        result = consolidator.consolidate_memories()

        assert "test_driven" in result["patterns"]

    def test_get_consolidated_knowledge(self, temp_workdir):
        """Test retrieving consolidated knowledge."""
        consolidator = DreamConsolidator(temp_workdir)

        # Create some consolidation files
        consolidator.consolidate_memories()

        knowledge = consolidator.get_consolidated_knowledge()
        assert len(knowledge) >= 1


class TestRefactoringAgent:
    """Tests for RefactoringAgent."""

    @pytest.fixture
    def temp_workdir(self, tmp_path):
        """Create a temporary workdir for testing."""
        workdir = tmp_path / "test_workdir"
        workdir.mkdir()
        yield str(workdir)
        shutil.rmtree(workdir, ignore_errors=True)

    def test_refactoring_agent_initialization(self, temp_workdir):
        """Test RefactoringAgent initializes correctly."""
        agent = RefactoringAgent(temp_workdir)
        assert agent.workdir == Path(temp_workdir)

    def test_detect_long_function(self, temp_workdir):
        """Test detection of long functions."""
        agent = RefactoringAgent(temp_workdir)

        # Create a file with a long function
        test_file = Path(temp_workdir) / "long_function.py"
        test_file.write_text("""
def long_function():
    line1 = 1
    line2 = 2
    line3 = 3
    line4 = 4
    line5 = 5
    line6 = 6
    line7 = 7
    line8 = 8
    line9 = 9
    line10 = 10
    line11 = 11
    line12 = 12
    line13 = 13
    line14 = 14
    line15 = 15
    line16 = 16
    line17 = 17
    line18 = 18
    line19 = 19
    line20 = 20
    line21 = 21
    line22 = 22
    line23 = 23
    line24 = 24
    line25 = 25
    line26 = 26
    line27 = 27
    line28 = 28
    line29 = 29
    line30 = 30
    line31 = 31
    line32 = 32
    line33 = 33
    line34 = 34
    line35 = 35
    line36 = 36
    line37 = 37
    line38 = 38
    line39 = 39
    line40 = 40
    line41 = 41
    line42 = 42
    line43 = 43
    line44 = 44
    line45 = 45
    line46 = 46
    line47 = 47
    line48 = 48
    line49 = 49
    line50 = 50
    line51 = 51
    return line1 + line2
""")

        smells = agent.detect_code_smells("long_function.py")
        assert len(smells) > 0
        assert any(s["type"] == "long_function" for s in smells)

    def test_detect_too_many_parameters(self, temp_workdir):
        """Test detection of too many parameters."""
        agent = RefactoringAgent(temp_workdir)

        test_file = Path(temp_workdir) / "many_params.py"
        test_file.write_text("""
def func(a, b, c, d, e, f, g, h):
    return a + b + c + d + e + f + g + h
""")

        smells = agent.detect_code_smells("many_params.py")
        assert len(smells) > 0
        assert any(s["type"] == "too_many_parameters" for s in smells)

    def test_detect_large_class(self, temp_workdir):
        """Test detection of large classes."""
        agent = RefactoringAgent(temp_workdir)

        test_file = Path(temp_workdir) / "large_class.py"
        # Create a class with many methods
        class_code = "class Large:\n"
        for i in range(25):
            class_code += f"    def method_{i}(self): pass\n"

        test_file.write_text(class_code)

        smells = agent.detect_code_smells("large_class.py")
        assert len(smells) > 0
        assert any(s["type"] == "large_class" for s in smells)

    def test_suggest_refactoring(self, temp_workdir):
        """Test refactoring suggestions."""
        agent = RefactoringAgent(temp_workdir)

        test_file = Path(temp_workdir) / "smelly.py"
        test_file.write_text("""
def func(a, b, c, d, e, f, g, h):
    return a
""")

        suggestion = agent.suggest_refactoring("smelly.py")
        assert "smell_count" in suggestion
        assert "can_auto_fix" in suggestion


class TestReviewAgent:
    """Tests for ReviewAgent."""

    @pytest.fixture
    def temp_workdir(self, tmp_path):
        """Create a temporary workdir for testing."""
        workdir = tmp_path / "test_workdir"
        workdir.mkdir()
        yield str(workdir)
        shutil.rmtree(workdir, ignore_errors=True)

    def test_review_agent_initialization(self, temp_workdir):
        """Test ReviewAgent initializes correctly."""
        agent = ReviewAgent(temp_workdir)
        assert agent.workdir == Path(temp_workdir)

    def test_review_detects_todo(self, temp_workdir):
        """Test detection of TODO/FIXME in patches."""
        agent = ReviewAgent(temp_workdir)

        patch = """
def incomplete_function():
    # TODO: implement this
    pass
"""
        result = agent.review_patch(patch)

        assert len(result["issues"]) > 0
        assert any(i["type"] == "unresolved_task" for i in result["issues"])

    def test_review_detects_secrets(self, temp_workdir):
        """Test detection of hardcoded secrets."""
        agent = ReviewAgent(temp_workdir)

        patch = """
def authenticate():
    password = "hardcoded_secret"
    api_key = "sk-1234567890"
"""
        result = agent.review_patch(patch)

        assert len(result["issues"]) > 0
        assert any(i["type"] == "security" for i in result["issues"])

    def test_review_large_patch_warning(self, temp_workdir):
        """Test warning for large patches."""
        agent = ReviewAgent(temp_workdir)

        # Create a large patch
        patch = "\n".join([f"+ line {i}" for i in range(150)])

        result = agent.review_patch(patch)

        assert len(result["recommendations"]) > 0
        assert any(r["type"] == "large_patch" for r in result["recommendations"])

    def test_review_approved_when_clean(self, temp_workdir):
        """Test approval for clean patches."""
        agent = ReviewAgent(temp_workdir)

        patch = """
def hello():
    print("Hello, World!")
"""
        result = agent.review_patch(patch)

        assert result["overall"] == "approved"
        assert len(result["issues"]) == 0


class TestSkillLearner:
    """Tests for SkillLearner."""

    @pytest.fixture
    def temp_workdir(self, tmp_path):
        """Create a temporary workdir for testing."""
        workdir = tmp_path / "test_workdir"
        workdir.mkdir()

        # Create skill directory structure
        skill_dir = workdir / "agent-brain" / "skills"
        skill_dir.mkdir(parents=True, exist_ok=True)

        yield str(workdir)
        shutil.rmtree(workdir, ignore_errors=True)

    def test_skill_learner_initialization(self, temp_workdir):
        """Test SkillLearner initializes correctly."""
        learner = SkillLearner(temp_workdir)
        assert learner.workdir == Path(temp_workdir)
        assert learner.skill_dir.exists()

    def test_create_skill(self, temp_workdir):
        """Test creating a new skill."""
        learner = SkillLearner(temp_workdir)

        filepath = learner.create_skill(
            name="Test Skill",
            description="A test skill",
            patterns=["pattern1", "pattern2"],
            examples=[{"task": "task1", "solution": "solution1"}],
        )

        assert Path(filepath).exists()
        content = Path(filepath).read_text()
        assert "Test Skill" in content
        assert "pattern1" in content

    def test_list_skills(self, temp_workdir):
        """Test listing available skills."""
        learner = SkillLearner(temp_workdir)

        learner.create_skill("Skill One", "Description", [], [])
        learner.create_skill("Skill Two", "Description", [], [])

        skills = learner.list_skills()
        assert len(skills) == 2

    def test_get_skill(self, temp_workdir):
        """Test retrieving a skill."""
        learner = SkillLearner(temp_workdir)

        learner.create_skill("My Skill", "Description", [], [])

        content = learner.get_skill("My Skill")
        assert content is not None
        assert "My Skill" in content

    def test_get_nonexistent_skill(self, temp_workdir):
        """Test retrieving nonexistent skill returns None."""
        learner = SkillLearner(temp_workdir)

        result = learner.get_skill("Nonexistent")
        assert result is None


class TestMemoryUpdateNodeIntegration:
    """Integration tests for memory_update_node with advanced features."""

    @pytest.fixture
    def mock_state(self, tmp_path):
        """Create a mock agent state."""
        return {
            "task": "Create a test function",
            "history": [
                {"role": "user", "content": "Create a test function"},
                {"role": "assistant", "content": "I'll create that"},
                {
                    "role": "tool",
                    "content": "Created file test.py with def test(): pass",
                },
            ],
            "working_dir": str(tmp_path),
            "evaluation_result": "complete",
            "current_plan": [{"step": "create function", "completed": True}],
            "session_id": "test_session",
        }

    @pytest.fixture
    def mock_config(self):
        """Create a mock config."""
        return {"configurable": {"orchestrator": None}}

    @pytest.mark.asyncio
    async def test_memory_update_node_runs_without_error(
        self, mock_state, mock_config, tmp_path, monkeypatch
    ):
        """Test memory_update_node executes without errors."""

        # Mock the distill_context to avoid LLM calls (must be sync — distill_context is sync)
        def mock_distill(*args, **kwargs):
            # Create minimal TASK_STATE.md to satisfy consolidation
            agent_context = tmp_path / ".agent-context"
            agent_context.mkdir(parents=True, exist_ok=True)
            (agent_context / "TASK_STATE.md").write_text(
                "Task: test\nCompleted: step1\nNext: step2"
            )
            return {}

        monkeypatch.setattr(
            "src.core.orchestration.graph.nodes.memory_update_node.distill_context",
            mock_distill,
        )

        from src.core.orchestration.graph.nodes.memory_update_node import (
            memory_update_node,
        )

        result = await memory_update_node(mock_state, mock_config)
        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_memory_update_node_with_no_history(
        self, tmp_path, mock_config, monkeypatch
    ):
        """Test memory_update_node handles empty history."""

        # Mock the distill_context to avoid LLM calls (must be sync — distill_context is sync)
        def mock_distill(*args, **kwargs):
            return {}

        monkeypatch.setattr(
            "src.core.orchestration.graph.nodes.memory_update_node.distill_context",
            mock_distill,
        )

        from src.core.orchestration.graph.nodes.memory_update_node import (
            memory_update_node,
        )

        state = {
            "task": "",
            "history": [],
            "working_dir": str(tmp_path),
            "evaluation_result": "complete",
            "current_plan": [],
            "session_id": "test",
        }

        result = await memory_update_node(state, mock_config)
        assert isinstance(result, dict)

class TestSkillLearnerMemoryNodeWiring:
    """Tests that SkillLearner is correctly wired into memory_update_node."""

    @pytest.fixture
    def mock_config(self):
        return {"configurable": {"orchestrator": None}}

    @pytest.mark.asyncio
    async def test_skill_created_on_successful_multi_tool_task(
        self, tmp_path, mock_config, monkeypatch
    ):
        """A new skill file is created when task succeeds with ≥2 tool calls."""

        def mock_distill(*args, **kwargs):
            return {}

        monkeypatch.setattr(
            "src.core.orchestration.graph.nodes.memory_update_node.distill_context",
            mock_distill,
        )

        from src.core.orchestration.graph.nodes.memory_update_node import memory_update_node

        state = {
            "task": "implement authentication module",
            "history": [
                {"role": "tool", "content": "write_file called"},
                {"role": "tool", "content": "edit_file called"},
            ],
            "working_dir": str(tmp_path),
            "evaluation_result": "complete",
            "current_plan": [{"step": "s1", "completed": True}],
            "session_id": "s1",
        }

        await memory_update_node(state, mock_config)

        skill_dir = tmp_path / "agent-brain" / "skills"
        skills = list(skill_dir.glob("*.md")) if skill_dir.exists() else []
        assert len(skills) >= 1, "Expected at least one skill file to be created"

    @pytest.mark.asyncio
    async def test_skill_not_created_for_single_tool_task(
        self, tmp_path, mock_config, monkeypatch
    ):
        """No skill is created when task has fewer than 2 tool calls."""

        def mock_distill(*args, **kwargs):
            return {}

        monkeypatch.setattr(
            "src.core.orchestration.graph.nodes.memory_update_node.distill_context",
            mock_distill,
        )

        from src.core.orchestration.graph.nodes.memory_update_node import memory_update_node

        state = {
            "task": "single tool task",
            "history": [
                {"role": "tool", "content": "read_file called"},
            ],
            "working_dir": str(tmp_path),
            "evaluation_result": "complete",
            "current_plan": [],
            "session_id": "s2",
        }

        await memory_update_node(state, mock_config)

        skill_dir = tmp_path / "agent-brain" / "skills"
        skills = list(skill_dir.glob("*.md")) if skill_dir.exists() else []
        assert len(skills) == 0, "No skill should be created for single-tool task"

    @pytest.mark.asyncio
    async def test_duplicate_skill_not_created(
        self, tmp_path, mock_config, monkeypatch
    ):
        """Running the same task twice does not create duplicate skill files."""

        def mock_distill(*args, **kwargs):
            return {}

        monkeypatch.setattr(
            "src.core.orchestration.graph.nodes.memory_update_node.distill_context",
            mock_distill,
        )

        from src.core.orchestration.graph.nodes.memory_update_node import memory_update_node

        state = {
            "task": "implement duplicate task",
            "history": [
                {"role": "tool", "content": "write_file called"},
                {"role": "tool", "content": "edit_file called"},
            ],
            "working_dir": str(tmp_path),
            "evaluation_result": "complete",
            "current_plan": [{"step": "s1", "completed": True}],
            "session_id": "s3",
        }

        await memory_update_node(state, mock_config)
        await memory_update_node(state, mock_config)

        skill_dir = tmp_path / "agent-brain" / "skills"
        skills = list(skill_dir.glob("*.md")) if skill_dir.exists() else []
        assert len(skills) == 1, f"Expected exactly 1 skill file, got {len(skills)}"


class TestHelperFunctions:
    """Tests for helper functions in memory_update_node."""

    def test_extract_tool_sequence(self):
        """Test extracting tool sequence from history."""
        from src.core.orchestration.graph.nodes.memory_update_node import (
            _extract_tool_sequence,
        )

        history = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "I'll help"},
            {"role": "tool", "content": "File created successfully"},
            {"role": "tool", "content": "Another tool call"},
        ]

        tools = _extract_tool_sequence(history)
        assert len(tools) == 2

    def test_extract_tool_sequence_empty(self):
        """Test extracting from empty history."""
        from src.core.orchestration.graph.nodes.memory_update_node import (
            _extract_tool_sequence,
        )

        tools = _extract_tool_sequence([])
        assert tools == []

    def test_extract_patch_from_history(self):
        """Test extracting patch from history."""
        from src.core.orchestration.graph.nodes.memory_update_node import (
            _extract_patch_from_history,
        )

        history = [
            {"role": "user", "content": "Make changes"},
            {"role": "tool", "content": "Patch applied:\n+ new_line\n- old_line"},
        ]

        patch = _extract_patch_from_history(history)
        assert "Patch applied" in patch or "new_line" in patch

    def test_extract_modified_files(self):
        """Test extracting modified files from history."""
        from src.core.orchestration.graph.nodes.memory_update_node import (
            _extract_modified_files,
        )

        history = [
            {"role": "tool", "content": "Edited src/main.py and src/utils.py"},
        ]

        files = _extract_modified_files(history)
        assert "main.py" in str(files) or "utils.py" in str(files)
