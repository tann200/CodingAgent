import json
from src.core.orchestration.orchestrator import Orchestrator
from tests.integration.mocks.deterministic_adapter import DeterministicAdapter


def test_loop_prevention(tmp_path):
    # Setup scenario where the model repeats the exact same tool call multiple times
    # Each graph run makes 2 LLM calls (perception + planning), so we need 7 steps
    # to get 3 executions (3rd execution will be blocked by loop prevention)
    repeated_tool_call = "```yaml\nname: bash\narguments:\n  command: ls -la\n```"

    scenarios = {
        "loop_scenario": [
            repeated_tool_call,  # graph run 1, perception -> tool 1
            repeated_tool_call,  # graph run 1, planning -> tool 2
            repeated_tool_call,  # graph run 2, perception -> tool 3 (should be blocked)
            repeated_tool_call,  # will not be reached
            repeated_tool_call,  # will not be reached
            "I will stop looping now.",  # final message
        ]
    }

    adapter = DeterministicAdapter(scenarios=scenarios)
    adapter.set_scenario("loop_scenario")

    # Mock LLM calls to use the deterministic adapter
    import src.core.llm_manager

    original_call_model = src.core.llm_manager.call_model

    async def mock_call_model(messages, model=None, provider=None, *largs, **kwargs):
        return adapter.generate(messages, model=model, provider=provider, **kwargs)

    src.core.llm_manager.call_model = mock_call_model

    try:
        # Create orchestrator pointing to tmp_path
        orchestrator = Orchestrator(adapter=adapter, working_dir=str(tmp_path))

        # Give it a simple mock tool
        def bash_mock(**kwargs):
            return "Command executed"

        orchestrator.tool_registry.register("bash", bash_mock, [], "Run bash")

        # Run the agent, allow multiple rounds to consume adapter
        import time

        trace_path = tmp_path / ".agent-context" / "execution_trace.json"
        trace = []
        max_rounds = 12
        scenario_len = (
            len(scenarios["loop_scenario"]) if isinstance(scenarios, dict) else 4
        )
        for _ in range(max_rounds):
            orchestrator.run_agent_once(
                None, [{"role": "user", "content": "Start"}], {}
            )
            time.sleep(0.05)
            if trace_path.exists():
                try:
                    trace = json.loads(trace_path.read_text())
                except Exception:
                    trace = []
            if len(trace) >= 3:
                break
            if getattr(adapter, "step_index", 0) >= scenario_len:
                break

        # Check trace file exists and has 2 tool executions, not 3 (because 3rd was blocked)
        assert trace_path.exists(), "execution_trace.json not found"
        assert len(trace) == 2, f"Expected 2 tool executions, got {len(trace)}"

        # Check that the orchestrator injected the loop detected message into the messages
        messages = orchestrator.msg_mgr.all()
        loop_messages = [
            m
            for m in messages
            if m["role"] == "system" and "[LOOP DETECTED]" in m["content"]
        ]
        assert len(loop_messages) >= 1, (
            "Loop detection message was not injected into the conversation"
        )

    finally:
        # Restore original
        src.core.llm_manager.call_model = original_call_model
