import json
from src.core.orchestration.orchestrator import Orchestrator
from tests.integration.mocks.deterministic_adapter import DeterministicAdapter

# Modules that import call_model directly at module load time — all must be patched.
_CALL_MODEL_TARGETS = [
    "src.core.orchestration.graph.nodes.execution_node.call_model",
    "src.core.orchestration.graph.nodes.planning_node.call_model",
    "src.core.orchestration.graph.nodes.perception_node.call_model",
    "src.core.orchestration.graph.nodes.debug_node.call_model",
    "src.core.orchestration.graph.nodes.replan_node.call_model",
    "src.core.inference.llm_manager.call_model",
]


def test_loop_prevention(tmp_path, monkeypatch):
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

    async def mock_call_model(messages, model=None, provider=None, *largs, **kwargs):
        return adapter.generate(messages, model=model, provider=provider, **kwargs)

    # Patch call_model in every node module that imported it at load time
    for target in _CALL_MODEL_TARGETS:
        try:
            monkeypatch.setattr(target, mock_call_model)
        except AttributeError:
            pass  # module not yet imported — skip

    orchestrator = Orchestrator(adapter=adapter, working_dir=str(tmp_path))

    def bash_mock(**kwargs):
        return "Command executed"

    orchestrator.tool_registry.register("bash", bash_mock, [], "Run bash")

    import time

    trace_path = tmp_path / ".agent-context" / "execution_trace.json"
    trace = []
    max_rounds = 12
    scenario_len = len(scenarios["loop_scenario"])
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
