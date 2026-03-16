from src.core.orchestration.orchestrator import (
    Orchestrator,
    example_registry,
    _generate_work_summary,
)


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


def test_generate_work_summary():
    state = {
        "task": "Test task",
        "rounds": 3,
        "current_plan": [
            {"description": "Step 1", "completed": True},
            {"description": "Step 2", "completed": True},
            {"description": "Step 3", "completed": False},
        ],
        "current_step": 2,
        "verified_reads": ["file1.py", "file2.py"],
    }
    history = [
        {"role": "tool", "tool": "read_file", "content": "..."},
        {"role": "tool", "tool": "read_file", "content": "..."},
        {"role": "tool", "tool": "write_file", "content": "..."},
    ]
    result = _generate_work_summary(state, history)
    assert "Test task" in result
    assert "3" in result
    assert "read_file" in result
    assert "write_file" in result
    assert "Steps completed: 2/3" in result
    assert "file1.py" not in result
    assert "2" in result


def test_generate_work_summary_empty():
    result = _generate_work_summary(None, [])
    assert result == ""


def test_generate_work_summary_no_plan():
    state = {"task": "Simple task", "rounds": 1}
    history = [{"role": "tool", "tool": "echo", "content": "..."}]
    result = _generate_work_summary(state, history)
    assert "Simple task" in result
    assert "1" in result
    assert "echo" in result
