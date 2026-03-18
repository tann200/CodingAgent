import sys
import json
import argparse
from pathlib import Path

from src.core.orchestration.orchestrator import Orchestrator
from tests.integration.mocks.deterministic_adapter import DeterministicAdapter

SCENARIOS = {
    "provider_probe": [
        '<tool>\nname: search_code\nargs: {"query": "ProviderManager"}\n</tool>',
        '<tool>\nname: read_file\nargs: {"path": "src/core/llm_manager.py"}\n</tool>',
        "I have found and read the ProviderManager.",
    ],
    "fix_syntax": [
        '<tool>\nname: read_file\nargs: {"path": "src/dummy.py"}\n</tool>',
        '<tool>\nname: edit_file\nargs: {"path": "src/dummy.py", "patch": "@@ -1 +1 @@\\n- def foo(): pass\\n+ def foo(): return 1\\n"}\n</tool>',
        "<tool>\nname: run_tests\nargs: {}\n</tool>",
        "I have fixed the syntax and run the tests.",
    ],
    "bash_then_act": [
        '<tool>\nname: bash\nargs: {"command": "ls -la"}\n</tool>',
        '<tool>\nname: read_file\nargs: {"path": "src/dummy.py"}\n</tool>',
        '<tool>\nname: edit_file\nargs: {"path": "src/dummy.py", "patch": "@@ -1 +1 @@\\n- pass\\n+ return True\\n"}\n</tool>',
        "I have acted on the bash output.",
    ],
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", type=str, required=True, choices=SCENARIOS.keys())
    parser.add_argument("--working-dir", type=str, required=True)
    args = parser.parse_args()

    adapter = DeterministicAdapter(scenarios=SCENARIOS)
    adapter.set_scenario(args.scenario)

    import src.core.inference.llm_manager

    original_call_model = src.core.inference.llm_manager.call_model

    async def mock_call_model(messages, model=None, provider=None, *largs, **kwargs):
        return adapter.generate(messages, model=model, provider=provider, **kwargs)

    src.core.inference.llm_manager.call_model = mock_call_model

    try:
        orchestrator = Orchestrator(adapter=adapter, working_dir=args.working_dir)

        dummy_file = Path(args.working_dir) / "src" / "dummy.py"
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

        orchestrator.tool_registry.register(
            "run_tests", run_tests_mock, [], "Run test suite"
        )
        orchestrator.tool_registry.register(
            "read_file", read_file_mock, [], "Read file"
        )
        orchestrator.tool_registry.register(
            "edit_file", edit_file_mock, ["write"], "Edit file"
        )
        orchestrator.tool_registry.register(
            "search_code", search_code_mock, [], "Search code"
        )
        orchestrator.tool_registry.register("bash", bash_mock, [], "Run bash")

        messages = [{"role": "user", "content": f"Execute scenario {args.scenario}"}]

        orchestrator.run_agent_once(None, messages, {})

        trace_path = Path(args.working_dir) / ".agent-context" / "execution_trace.json"
        if not trace_path.exists():
            print("Trace file not found")
            sys.exit(1)

        trace = json.loads(trace_path.read_text())
        executed_tools = [step["tool"] for step in trace]

        expected_sequence = []
        if args.scenario == "provider_probe":
            expected_sequence = ["search_code", "read_file"]
        elif args.scenario == "fix_syntax":
            expected_sequence = ["read_file", "edit_file", "run_tests"]
        elif args.scenario == "bash_then_act":
            expected_sequence = ["bash", "read_file", "edit_file"]

        if executed_tools != expected_sequence:
            print(f"FAILED: Expected {expected_sequence}, got {executed_tools}")
            sys.exit(1)

        print(f"SUCCESS: Scenario {args.scenario} completed. Sequence verified.")
        sys.exit(0)

    finally:
        src.core.inference.llm_manager.call_model = original_call_model


if __name__ == "__main__":
    main()
