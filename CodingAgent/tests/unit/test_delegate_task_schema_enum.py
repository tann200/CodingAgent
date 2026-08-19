

def test_delegate_task_role_enum_exists():
    """ToolDefinition.to_openai_schema should inject a role enum for delegate_task."""
    from src.tools.subagent_tools import delegate_task
    from src.tools._tool import TOOL_ATTR

    defn = getattr(delegate_task, TOOL_ATTR)
    schema = defn.to_openai_schema()
    params = schema["function"]["parameters"]
    assert "role" in params["properties"]
    role_prop = params["properties"]["role"]
    assert "enum" in role_prop and isinstance(role_prop["enum"], list)
    # Expect at least canonical role names to be present
    for expected in ("analyst", "operational", "strategic", "reviewer", "debugger"):
        assert expected in role_prop["enum"]
