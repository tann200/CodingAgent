import pytest
import json
from pathlib import Path
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
        
        # Run agent
        orchestrator.run_agent_once(None, [{"role": "user", "content": "Start"}], {})
        
        # Check trace file exists and has 3 tool executions, not 4 (because 4th was blocked)
        trace_path = tmp_path / ".agent-context" / "execution_trace.json"
        assert trace_path.exists()
        
        trace = json.loads(trace_path.read_text())
        assert len(trace) == 3, f"Expected 3 tool executions, got {len(trace)}"
        
        # Check that the orchestrator injected the loop detected message into the messages
        messages = orchestrator.msg_mgr.all()
        loop_messages = [m for m in messages if m["role"] == "system" and "[LOOP DETECTED]" in m["content"]]
        assert len(loop_messages) >= 1, "Loop detection message was not injected into the conversation"
        
    finally:
        # Restore original
        src.core.llm_manager.call_model = original_call_model
