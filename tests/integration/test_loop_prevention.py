import json
from src.core.orchestration.orchestrator import Orchestrator
from tests.integration.mocks.deterministic_adapter import DeterministicAdapter

def test_loop_prevention(tmp_path):
    # Setup scenario where the model repeats the exact same tool call 4 times
    repeated_tool_call = "<tool>\nname: bash\nargs: {\"command\": \"ls -la\"}\n</tool>"
    
    scenarios = {
        "loop_scenario": [
            repeated_tool_call,
            repeated_tool_call,
            repeated_tool_call,
            repeated_tool_call,  # This 4th one should trigger the loop detection
            "I will stop looping now."
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
        scenario_len = len(scenarios["loop_scenario"]) if isinstance(scenarios, dict) else 4
        for _ in range(max_rounds):
            orchestrator.run_agent_once(None, [{"role": "user", "content": "Start"}], {})
            time.sleep(0.05)
            if trace_path.exists():
                try:
                    trace = json.loads(trace_path.read_text())
                except Exception:
                    trace = []
            if len(trace) >= 3:
                break
            if getattr(adapter, 'step_index', 0) >= scenario_len:
                break

        # Check trace file exists and has 3 tool executions, not 4 (because 4th was blocked)
        assert trace_path.exists(), "execution_trace.json not found"
        assert len(trace) == 3, f"Expected 3 tool executions, got {len(trace)}"
        
        # Check that the orchestrator injected the loop detected message into the messages
        messages = orchestrator.msg_mgr.all()
        loop_messages = [m for m in messages if m["role"] == "system" and "[LOOP DETECTED]" in m["content"]]
        assert len(loop_messages) >= 1, "Loop detection message was not injected into the conversation"
        
    finally:
        # Restore original
        src.core.llm_manager.call_model = original_call_model
