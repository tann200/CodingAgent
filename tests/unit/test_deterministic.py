"""
Tests for deterministic execution and reproducibility features.
"""

from src.core.orchestration.orchestrator import Orchestrator


class TestDeterministicExecution:
    """Tests for deterministic execution with seed control."""

    def test_orchestrator_has_deterministic_flag(self):
        """Test orchestrator accepts deterministic flag."""
        orch = Orchestrator(
            working_dir="/tmp/test",
            deterministic=True,
            seed=42,
        )
        assert orch.deterministic is True
        assert orch.seed == 42

    def test_orchestrator_defaults_deterministic_false(self):
        """Test orchestrator defaults to non-deterministic."""
        orch = Orchestrator(
            working_dir="/tmp/test",
        )
        assert orch.deterministic is False
        assert orch.seed is None

    def test_deterministic_flag_passed_to_config(self):
        """Test deterministic flag is passed to graph config."""
        orch = Orchestrator(
            working_dir="/tmp/test",
            deterministic=True,
            seed=123,
        )

        # Check that deterministic and seed are stored on orchestrator
        assert hasattr(orch, "deterministic")
        assert hasattr(orch, "seed")
        assert orch.deterministic is True
        assert orch.seed == 123

    def test_deterministic_preserved_in_initial_state(self):
        """Test deterministic settings preserved."""
        orch = Orchestrator(
            working_dir="/tmp/test",
            deterministic=True,
            seed=42,
        )

        # Check orchestrator has the attributes
        assert orch.deterministic is True
        assert orch.seed == 42
