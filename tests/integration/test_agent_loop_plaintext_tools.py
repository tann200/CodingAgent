import pytest
import json
from pathlib import Path
from src.core.orchestration.orchestrator import Orchestrator
from tests.integration.mocks.deterministic_adapter import DeterministicAdapter

SCENARIOS = {
    "provider_probe": [
        "<tool>\nname: search_code\nargs: {\"query\": \"ProviderManager\"}\n</tool>",
        "<tool>\nname: read_file\nargs: {\"path\": \"src/core/llm_manager.py\"}\n</tool>",
        "I have found and read the ProviderManager."
    ],
    "fix_syntax": [
        "<tool>\nname: read_file\nargs: {\"path\": \"src/dummy.py\"}\n</tool>",
        "<tool>\nname: edit_file\nargs: {\"path\": \"src/dummy.py\", \"patch\": \"@@ -1 +1 @@\\n- def foo(): pass\\n+ def foo(): return 1\\n\"}\n</tool>",
        "<tool>\nname: run_tests\nargs: {}\n</tool>",
        "I have fixed the syntax and run the tests."
    ],
    "bash_then_act": [
        "<tool>\nname: bash\nargs: {\"command\": \"ls -la\"}\n</tool>",
        "<tool>\nname: read_file\nargs: {\"path\": \"src/dummy.py\"}\n</tool>",
        "<tool>\nname: edit_file\nargs: {\"path\": \"src/dummy.py\", \"patch\": \"@@ -1 +1 @@\\n- pass\\n+ return True\\n\"}\n</tool>",
        "I have acted on the bash output."
    ]
}

@pytest.fixture
def mock_llm_manager():
    import src.core.llm_manager
    original_call_model = src.core.llm_manager.call_model
    yield
    src.core.llm_manager.call_model = original_call_model

@pytest.mark.parametrize("scenario_name, expected_sequence", [
    ("provider_probe", ["search_code", "read_file"]),
    ("fix_syntax", ["read_file", "edit_file", "run_tests"]),
    ("bash_then_act", ["bash", "read_file", "edit_file"])
])
def test_agent_loop_plaintext_tools(tmp_path, mock_llm_manager, scenario_name, expected_sequence):
    adapter = DeterministicAdapter(scenarios=SCENARIOS)
    adapter.set_scenario(scenario_name)

    import src.core.llm_manager
    async def mock_call_model(messages, model=None, provider=None, *largs, **kwargs):
        return adapter.generate(messages, model=model, provider=provider, **kwargs)

    src.core.llm_manager.call_model = mock_call_model

    orchestrator = Orchestrator(adapter=adapter, working_dir=str(tmp_path))
    
    dummy_file = tmp_path / "src" / "dummy.py"
    dummy_file.parent.mkdir(parents=True, exist_ok=True)
    dummy_file.write_text("def foo(): pass\n")

    def run_tests_mock(**kwargs):
        return "Tests passed"
        
    def bash_mock(**kwargs):
        return "Command executed"
        
    def search_code_mock(**kwargs):
        return "Found code"
        
    def read_file_mock(**kwargs):
        return "File content"
        
    def edit_file_mock(**kwargs):
        return "File edited"
        
    orchestrator.tool_registry.register("run_tests", run_tests_mock, [], "Run test suite")
    orchestrator.tool_registry.register("read_file", read_file_mock, [], "Read file")
    orchestrator.tool_registry.register("edit_file", edit_file_mock, ["write"], "Edit file")
    orchestrator.tool_registry.register("search_code", search_code_mock, [], "Search code")
    orchestrator.tool_registry.register("bash", bash_mock, [], "Run bash")

    messages = [{"role": "user", "content": f"Execute scenario {scenario_name}"}]
    
    orchestrator.run_agent_once(None, messages, {})

    trace_path = tmp_path / ".agent-context" / "execution_trace.json"
    assert trace_path.exists()

    trace = json.loads(trace_path.read_text())
    executed_tools = [step["tool"] for step in trace]

    assert executed_tools == expected_sequence, f"Scenario {scenario_name} failed. Expected {expected_sequence}, got {executed_tools}"
