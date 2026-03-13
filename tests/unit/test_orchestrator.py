from src.core.orchestration.orchestrator import Orchestrator, example_registry


def test_tool_registry_and_preflight():
    reg = example_registry()
    orch = Orchestrator(None, tool_registry=reg)
    tc = {"name": "echo", "arguments": {"text": "hello"}}
    res = orch.preflight_check(tc)
    assert res["ok"]


def test_execute_tool_echo():
    reg = example_registry()
    orch = Orchestrator(None, tool_registry=reg)
    tc = {"name": "echo", "arguments": {"text": "hello"}}
    res = orch.execute_tool(tc)
    assert res["ok"]
    assert res["result"]["echo"] == "hello"
