import pytest
from src.tools.registry import register_tool, unregister_tool, get_tool, list_tools, call_tool, clear_registry, get_tool_descriptions, _registry

def test_registry_lifecycle():
    clear_registry()
    assert len(list_tools()) == 0
    
    def my_tool(a: int, b: int) -> int:
        return a + b
        
    register_tool("add", my_tool, "Adds two numbers", side_effects=True)
    
    assert "add" in list_tools()
    tool = get_tool("add")
    assert tool is not None
    assert tool["description"] == "Adds two numbers"
    
    res = call_tool("add", a=1, b=2)
    assert res == 3
    
    desc = get_tool_descriptions()
    assert "add" in desc
    assert "Adds two numbers" in desc
    
    unregister_tool("add")
    assert "add" not in list_tools()
    
def test_call_tool_not_found():
    clear_registry()
    with pytest.raises(KeyError, match="Tool not found: nonexistent"):
        call_tool("nonexistent")
