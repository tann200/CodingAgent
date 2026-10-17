"""Unit tests for tool schema strictness and additionalProperties enforcement (Audit Section 7 / Appendix)."""

from src.tools._tool import ToolDefinition


def test_schema_includes_additional_properties_false():
    def dummy_tool(path: str, count: int = 1):
        """A test tool."""
        pass

    defn = ToolDefinition(name="dummy_tool", fn=dummy_tool, description="test")
    schema = defn.to_openai_schema()
    parameters = schema["function"]["parameters"]

    assert parameters["type"] == "object"
    assert parameters["additionalProperties"] is False
    assert "path" in parameters["properties"]
    assert "count" in parameters["properties"]


def test_schema_allows_additional_properties_when_kwargs_present():
    def kwargs_tool(path: str, **kwargs):
        """A tool accepting arbitrary kwargs."""
        pass

    defn = ToolDefinition(name="kwargs_tool", fn=kwargs_tool, description="test")
    schema = defn.to_openai_schema()
    parameters = schema["function"]["parameters"]

    assert parameters["additionalProperties"] is True


def test_validate_args_rejects_undeclared_fields():
    def strict_tool(path: str, count: int = 1):
        """Strict tool."""
        pass

    defn = ToolDefinition(name="strict_tool", fn=strict_tool, description="test")
    errors = defn.validate_args({"path": "foo.py", "count": 2, "hallucinated_param": "bad"})
    assert len(errors) == 1
    assert "unexpected undeclared field 'hallucinated_param'" in errors[0]


def test_validate_args_rejects_workdir_and_working_dir():
    def tool_with_internal(path: str):
        """Tool."""
        pass

    defn = ToolDefinition(name="tool_with_internal", fn=tool_with_internal, description="test")
    # workdir and working_dir must be rejected as model arguments to prevent workspace escape (SPEC-01)
    errors = defn.validate_args({"path": "foo.py", "workdir": "/some/dir"})
    assert len(errors) == 1
    assert "unexpected undeclared field 'workdir'" in errors[0]

    errors_wd = defn.validate_args({"path": "foo.py", "working_dir": "/some/dir"})
    assert len(errors_wd) == 1
    assert "unexpected undeclared field 'working_dir'" in errors_wd[0]


def test_validate_args_accepts_extra_fields_when_kwargs_declared():
    def flex_tool(path: str, **kwargs):
        """Flex tool."""
        pass

    defn = ToolDefinition(name="flex_tool", fn=flex_tool, description="test")
    errors = defn.validate_args({"path": "foo.py", "extra_param": 123})
    assert errors == []
