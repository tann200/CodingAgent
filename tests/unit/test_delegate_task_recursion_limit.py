import types


def test_delegate_task_depth_limit():
    """delegate_task should refuse to spawn when delegation depth is at max."""
    from src.tools import subagent_tools as st

    prev = st._DELEGATION_DEPTH_VAR.get()
    try:
        # Set depth to MAX and ensure delegate_task returns the expected error
        st._DELEGATION_DEPTH_VAR.set(st._MAX_DELEGATION_DEPTH)
        res = st.delegate_task("analyst", "do something")
        assert isinstance(res, str)
        assert "Maximum delegation depth" in res
    finally:
        # Restore previous depth
        st._DELEGATION_DEPTH_VAR.set(prev)


def test_delegate_task_strips_delegate_from_allowed(monkeypatch, tmp_path):
    """When allowed_tools explicitly includes delegate_task, it is stripped for child agent."""
    from src.tools import subagent_tools as st

    observed = {}

    class FakeGraph:
        async def ainvoke(self, initial_state, config):
            # Capture the orchestrator object passed into the graph invocation
            observed["orchestrator"] = config.get("configurable", {}).get(
                "orchestrator"
            )
            # Return a minimal final state
            return {
                "history": [{"role": "assistant", "content": "done"}],
                "last_result": {},
            }

    class FakeGraphFactory:
        def get_graph(self, role):
            return FakeGraph()

    class FakeBrain:
        def compile_system_prompt(self, canonical_role):
            return "system prompt"

    # Monkeypatch heavy dependencies to keep delegate_task execution light
    monkeypatch.setattr(st, "GraphFactory", FakeGraphFactory())
    monkeypatch.setattr(st, "get_agent_brain_manager", lambda: FakeBrain())

    # Call delegate_task with allowed_tools that includes 'delegate_task'
    res = st.delegate_task(
        "analyst",
        "perform isolated analysis",
        working_dir=str(tmp_path),
        allowed_tools=["delegate_task", "read_file"],
    )

    # Ensure the fake graph saw an orchestrator and its allowed_tools has delegate_task removed
    orch = observed.get("orchestrator")
    assert orch is not None, "Subagent orchestrator was not passed to the graph"
    # _allowed_tools on SubagentOrchestrator should be a set (or None)
    allowed = getattr(orch, "_allowed_tools", None)
    assert allowed is not None, "_allowed_tools was not set on SubagentOrchestrator"
    assert "delegate_task" not in allowed
