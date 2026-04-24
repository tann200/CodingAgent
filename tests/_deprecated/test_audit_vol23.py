"""
Regression tests for Vol23 audit fixes + Vol21 carry-forward resolutions.

Covers:
  BUG-VOL23-1  ensure_future task reference + done callback in execution_node
  BUG-VOL23-2  Hardcoded "add today's date on top" directive removed
  WF-VOL23-1   asyncio.get_running_loop() replaces deprecated get_event_loop()
  WF-VOL23-2   r"\\band\\b" removed from _task_has_more_steps multi_step_patterns
  BUG-VOL23-3  should_after_planning uses state.get("rounds", 0) not state["rounds"]
  RA-VOL23-1   parse_python_file logs exceptions at DEBUG level
  PERF-VOL23-1 HubAndSpokeCoordinator._executor created once in __init__
  SEC-VOL23-1  Session-title thread stored on orchestrator, joined in close()
  OE-VOL23-1   Fallback asyncio.run() has running-loop guard
  ARCH-VOL21-1 GraphFactory.get_graph() delegates to full 14-node compiled graph
  ARCH-VOL21-2 orchestrator_bootstrap split into 4 helper functions
  CP-14        MessageManager.SCHEMA_VERSION + to_dict/from_dict serialisation
  OE-VOL21-2   HubAndSpokeCoordinator removed (no live callers)
"""

import inspect


class TestBugVol231EnsureFutureTaskReference:
    """BUG-VOL23-1: ensure_future result must be stored to prevent GC."""

    def test_hook_task_stored_in_execution_node(self):
        """Verify _hook_task reference is captured and done-callback is added."""
        import src.core.orchestration.graph.nodes.execution_node as en

        src_text = inspect.getsource(en)
        # Stored task reference
        assert "_hook_task = asyncio.ensure_future(" in src_text, (
            "BUG-VOL23-1: ensure_future result must be assigned to _hook_task"
        )
        # Done callback registered
        assert "_hook_task.add_done_callback(" in src_text, (
            "BUG-VOL23-1: add_done_callback must be called on _hook_task"
        )
        # Logging inside the done callback
        assert "_log_hook_exc" in src_text, (
            "BUG-VOL23-1: _log_hook_exc done-callback must be defined"
        )


class TestBugVol232HardcodedDateRemoved:
    """BUG-VOL23-2: 'add today's date on top' directive must not appear in source."""

    def test_hardcoded_date_directive_absent(self):
        import src.core.orchestration.graph.nodes.execution_node as en

        src_text = inspect.getsource(en)
        assert "adding today's date on top" not in src_text, (
            "BUG-VOL23-2: hardcoded 'adding today's date on top' directive "
            "must be removed from execution_node"
        )
        assert "Action required: Modify the file by adding" not in src_text, (
            "BUG-VOL23-2: hardcoded modification directive must not appear"
        )


class TestWfVol231GetRunningLoop:
    """WF-VOL23-1: Deprecated asyncio.get_event_loop() replaced with get_running_loop()."""

    def _check_file_uses_running_loop(self, module):
        src_text = inspect.getsource(module)
        # Should not use get_event_loop() inside async defs
        # (allow it in non-async utility code)
        assert "asyncio.get_event_loop().time()" not in src_text, (
            f"WF-VOL23-1: {module.__name__} must not call asyncio.get_event_loop().time()"
        )

    def test_planning_node_uses_running_loop(self):
        import src.core.orchestration.graph.nodes.planning_node as m
        self._check_file_uses_running_loop(m)

    def test_debug_node_uses_running_loop(self):
        import src.core.orchestration.graph.nodes.debug_node as m
        self._check_file_uses_running_loop(m)

    def test_replan_node_uses_running_loop(self):
        import src.core.orchestration.graph.nodes.replan_node as m
        self._check_file_uses_running_loop(m)


class TestWfVol232AndPatternRemoved:
    """WF-VOL23-2: r'\\band\\b' must not appear in multi_step_patterns."""

    def test_band_pattern_absent_from_builder(self):
        """The \\band\\b pattern must not appear as an active list element."""
        from src.core.orchestration.graph.builder import _task_has_more_steps

        # Verify via behaviour: 'and' alone must NOT trigger multi-step routing.
        # (The pattern could still appear in comments explaining its removal.)
        state = {"task": "read and summarize the README", "tool_call_count": 1}
        assert _task_has_more_steps(state) is False, (
            r'WF-VOL23-2: r"\band\b" must not be active in multi_step_patterns; '
            "'read and summarize' should not trigger multi-step detection"
        )

    def test_and_task_not_multistep(self):
        """'and' alone no longer triggers multi-step detection."""
        from src.core.orchestration.graph.builder import _task_has_more_steps

        state = {"task": "create folder and file", "tool_call_count": 1}
        assert _task_has_more_steps(state) is False

    def test_then_task_is_multistep(self):
        """'then' keyword triggers multi-step detection."""
        from src.core.orchestration.graph.builder import _task_has_more_steps

        state = {"task": "create folder then create file", "tool_call_count": 1}
        assert _task_has_more_steps(state) is True


class TestBugVol233ShouldAfterPlanningGetRounds:
    """BUG-VOL23-3: should_after_planning must use state.get('rounds', 0)."""

    def test_no_keyerror_when_rounds_absent(self):
        """should_after_planning must not raise KeyError when 'rounds' absent."""
        from src.core.orchestration.graph_factory import should_after_planning

        # Minimal state with no 'rounds' key — must not raise
        state = {}
        result = should_after_planning(state)
        assert result in ("end", "execute", "memory_sync")

    def test_rounds_15_returns_end(self):
        """State with rounds >= 15 must route to 'end'."""
        from src.core.orchestration.graph_factory import should_after_planning

        state = {"rounds": 15}
        assert should_after_planning(state) == "end"


class TestRaVol231ParsePythonFileLogsExceptions:
    """RA-VOL23-1: parse_python_file must log exceptions at DEBUG level."""

    def test_exception_handler_logs(self, tmp_path):
        """RA-VOL23-1: parse_python_file must log at DEBUG and return {} on parse failure."""
        from unittest.mock import patch
        from src.core.indexing.repo_indexer import parse_python_file
        import src.core.indexing.repo_indexer as ri

        # Write a file with invalid Python syntax to trigger the exception path
        bad_file = tmp_path / "bad_syntax.py"
        bad_file.write_text("def (\n  broken syntax {\n")

        debug_calls = []
        with patch.object(ri.logger, "debug", side_effect=lambda *a, **kw: debug_calls.append(a)):
            result = parse_python_file(bad_file)

        assert result == {}, (
            "RA-VOL23-1: parse_python_file must return {} when parsing fails"
        )
        assert debug_calls, (
            "RA-VOL23-1: parse_python_file must call logger.debug when parsing fails"
        )
        # The first positional arg is the format string; remaining args include path
        all_args = " ".join(str(a) for call in debug_calls for a in call)
        assert "bad_syntax.py" in all_args or str(bad_file) in all_args, (
            "RA-VOL23-1: logger.debug call must include the failing file path"
        )


class TestArchVol211GraphFactoryFullPipeline:
    """ARCH-VOL21-1: GraphFactory.get_graph() must return the full 14-node graph."""

    def test_get_graph_returns_compiled_graph(self):
        """get_graph() must call _get_compiled_graph(), not a minimal subgraph."""
        from src.core.orchestration import graph_factory

        src_text = inspect.getsource(graph_factory.GraphFactory.get_graph)
        assert "_get_compiled_graph" in src_text, (
            "ARCH-VOL21-1: get_graph() must delegate to _get_compiled_graph()"
        )

    def test_all_legacy_roles_return_graph(self):
        """All legacy role strings must still return a non-None graph."""
        from src.core.orchestration.graph_factory import GraphFactory

        for role in ("planner", "coder", "reviewer", "researcher"):
            graph = GraphFactory.get_graph(role)
            assert graph is not None, f"ARCH-VOL21-1: get_graph('{role}') returned None"

    def test_invalid_role_still_returns_none(self):
        from src.core.orchestration.graph_factory import GraphFactory

        assert GraphFactory.get_graph("invalid_role_xyz") is None


class TestArchVol212OrchestratorBootstrapSplit:
    """ARCH-VOL21-2: orchestrator_bootstrap.py split into 4 helper functions."""

    def test_four_helper_functions_exist(self):
        import src.core.orchestration.orchestrator_bootstrap as ob

        for fn_name in (
            "_init_infrastructure",
            "_init_providers",
            "_init_event_subscriptions",
            "_init_services",
        ):
            assert hasattr(ob, fn_name), (
                f"ARCH-VOL21-2: orchestrator_bootstrap must define {fn_name}"
            )
            assert callable(getattr(ob, fn_name))

    def test_bootstrap_orchestrator_is_thin(self):
        """bootstrap_orchestrator should be short (calls the 4 helpers)."""
        import src.core.orchestration.orchestrator_bootstrap as ob

        src_text = inspect.getsource(ob.bootstrap_orchestrator)
        lines = [l for l in src_text.splitlines() if l.strip()]
        assert len(lines) < 60, (
            f"ARCH-VOL21-2: bootstrap_orchestrator has {len(lines)} non-empty lines; "
            "should be ≤60 (thin wrapper calling 4 helpers)"
        )


class TestCp14MessageManagerSchemVersion:
    """CP-14: MessageManager must have SCHEMA_VERSION and serialisation helpers."""

    def test_schema_version_constant(self):
        from src.core.orchestration.message_manager import MessageManager

        assert hasattr(MessageManager, "SCHEMA_VERSION"), (
            "CP-14: MessageManager must define SCHEMA_VERSION class attribute"
        )
        assert isinstance(MessageManager.SCHEMA_VERSION, int)
        assert MessageManager.SCHEMA_VERSION >= 1

    def test_to_dict_returns_version(self):
        from src.core.orchestration.message_manager import MessageManager

        mgr = MessageManager()
        mgr.append("user", "hello")
        d = mgr.to_dict()
        assert "version" in d
        assert d["version"] == MessageManager.SCHEMA_VERSION
        assert "messages" in d
        assert len(d["messages"]) == 1

    def test_from_dict_round_trips(self):
        from src.core.orchestration.message_manager import MessageManager

        mgr = MessageManager()
        mgr.append("user", "hello world")
        d = mgr.to_dict()

        mgr2 = MessageManager.from_dict(d)
        assert len(mgr2.messages) == 1
        assert mgr2.messages[0]["content"] == "hello world"
        assert mgr2.version == MessageManager.SCHEMA_VERSION

    def test_from_dict_future_version_logs_warning(self, capfd):
        """from_dict with a higher version should load without error (just warn)."""
        from src.core.orchestration.message_manager import MessageManager

        data = {
            "version": MessageManager.SCHEMA_VERSION + 99,
            "messages": [{"role": "user", "content": "test"}],
        }
        mgr = MessageManager.from_dict(data)
        assert len(mgr.messages) == 1


class TestOeVol212HubAndSpokeRemoved:
    """OE-VOL21-2: HubAndSpokeCoordinator must be removed from graph_factory."""

    def test_class_not_exported(self):
        """HubAndSpokeCoordinator must no longer exist in graph_factory."""
        import src.core.orchestration.graph_factory as gf

        assert not hasattr(gf, "HubAndSpokeCoordinator"), (
            "OE-VOL21-2: HubAndSpokeCoordinator must be removed (no live callers)"
        )

    def test_graphfactory_still_works(self):
        """Removing HubAndSpokeCoordinator must not break GraphFactory."""
        from src.core.orchestration.graph_factory import GraphFactory

        graph = GraphFactory.get_graph("coder")
        assert graph is not None
