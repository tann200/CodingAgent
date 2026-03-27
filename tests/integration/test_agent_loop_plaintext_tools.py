import pytest
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

SCENARIOS = {
    "provider_probe": [
        "```yaml\nname: search_code\narguments:\n  query: ProviderManager\n```",
        "```yaml\nname: read_file\narguments:\n  path: src/core/inference/llm_manager.py\n```",
        "I have found and read the ProviderManager.",
    ],
    "fix_syntax": [
        "```yaml\nname: read_file\narguments:\n  path: src/dummy.py\n```",
        '```yaml\nname: edit_file\narguments:\n  path: src/dummy.py\n  patch: "@@ -1 +1 @@\\n- def foo(): pass\\n+ def foo(): return 1\\n"\n```',
        "```yaml\nname: run_tests\narguments:\n  \n```",
        "I have fixed the syntax and run the tests.",
    ],
    "bash_then_act": [
        "```yaml\nname: bash\narguments:\n  command: ls -la\n```",
        "```yaml\nname: read_file\narguments:\n  path: src/dummy.py\n```",
        '```yaml\nname: edit_file\narguments:\n  path: src/dummy.py\n  patch: "@@ -1 +1 @@\\n- pass\\n+ return True\\n"\n```',
        "I have acted on the bash output.",
    ],
}


@pytest.mark.parametrize(
    "scenario_name, expected_sequence",
    [
        ("provider_probe", ["search_code", "read_file"]),
        ("fix_syntax", ["read_file", "edit_file", "run_tests"]),
        ("bash_then_act", ["bash", "read_file", "edit_file"]),
    ],
)
def test_agent_loop_plaintext_tools(
    tmp_path, monkeypatch, scenario_name, expected_sequence
):
    adapter = DeterministicAdapter(scenarios=SCENARIOS)
    adapter.set_scenario(scenario_name)

    async def mock_call_model(messages, model=None, provider=None, *largs, **kwargs):
        return adapter.generate(messages, model=model, provider=provider, **kwargs)

    # Patch call_model in every node module that imported it at load time
    for target in _CALL_MODEL_TARGETS:
        try:
            monkeypatch.setattr(target, mock_call_model)
        except AttributeError:
            pass  # module not yet imported — skip

    orchestrator = Orchestrator(adapter=adapter, working_dir=str(tmp_path))

    dummy_file = tmp_path / "src" / "dummy.py"
    dummy_file.parent.mkdir(parents=True, exist_ok=True)
    dummy_file.write_text("def foo(): pass\n")

    def run_tests_mock(**kwargs):
        return {"status": "ok", "output": "Tests passed", "exit_code": 0}

    def bash_mock(**kwargs):
        return {
            "status": "ok",
            "stdout": "Command executed",
            "stderr": "",
            "exit_code": 0,
        }

    def search_code_mock(**kwargs):
        return {
            "status": "ok",
            "results": [{"file": "test.py", "line": 1, "content": "found code"}],
        }

    def read_file_mock(**kwargs):
        return {
            "status": "ok",
            "path": kwargs.get("path"),
            "content": "File content",
            "lines": 1,
        }

    def edit_file_mock(**kwargs):
        return {"status": "ok", "path": kwargs.get("path"), "edited": True}

    orchestrator.tool_registry.register(
        "run_tests", run_tests_mock, [], "Run test suite"
    )
    orchestrator.tool_registry.register("read_file", read_file_mock, [], "Read file")
    orchestrator.tool_registry.register(
        "edit_file", edit_file_mock, ["write"], "Edit file"
    )
    orchestrator.tool_registry.register(
        "search_code", search_code_mock, [], "Search code"
    )
    orchestrator.tool_registry.register("bash", bash_mock, [], "Run bash")

    messages = [{"role": "user", "content": f"Execute scenario {scenario_name}"}]

    # Run the orchestrator repeatedly until the deterministic adapter has produced all steps
    executed_tools = []
    trace_path = tmp_path / ".agent-context" / "execution_trace.json"
    import time

    max_rounds = 12
    scenario_len = len(SCENARIOS[scenario_name])
    for _ in range(max_rounds):
        orchestrator.run_agent_once(None, messages, {})
        time.sleep(0.05)
        if trace_path.exists():
            try:
                trace = json.loads(trace_path.read_text())
                executed_tools = [step.get("tool") for step in trace]
            except Exception:
                executed_tools = []
        # Stop if expected sequence achieved or adapter consumed
        if executed_tools == expected_sequence:
            break
        if getattr(adapter, "step_index", 0) >= scenario_len:
            break

    # Ensure trace exists and at least one tool executed
    assert trace_path.exists(), f"Trace file not found for scenario {scenario_name}"
    assert len(executed_tools) > 0, (
        f"No tools executed for scenario {scenario_name}; got {executed_tools}"
    )

    # Check executed_tools is a subsequence of expected_sequence (preserving order)
    it = iter(expected_sequence)
    for t in executed_tools:
        try:
            while next(it) != t:
                continue
        except StopIteration:
            assert False, (
                f"Executed tool {t} not in expected sequence {expected_sequence}"
            )
