from src.core.orchestration.tool_parser import parse_tool_block


def test_parse_yaml_block_code():
    """Test YAML code block format"""
    text = """```yaml
name: read_file
arguments:
  path: src/main.py
```"""
    res = parse_tool_block(text)
    assert res is not None
    assert res["name"] == "read_file"
    assert res["arguments"]["path"] == "src/main.py"


def test_parse_yaml_compact_format():
    """Test compact YAML format (tool name as key)"""
    text = """```yaml
edit_file:
  path: src/foo.py
  content: hello world
```"""
    res = parse_tool_block(text)
    assert res is not None
    assert res["name"] == "edit_file"
    assert res["arguments"]["path"] == "src/foo.py"
    assert res["arguments"]["content"] == "hello world"


def test_parse_inline_yaml():
    """Test inline YAML format (not in code block)"""
    text = """
name: bash
arguments:
  command: ls -la
"""
    res = parse_tool_block(text)
    assert res is not None
    assert res["name"] == "bash"
    assert res["arguments"]["command"] == "ls -la"


def test_parse_tool_block_no_tool():
    """Test that non-tool text returns None"""
    res = parse_tool_block("No tool here.")
    assert res is None


def test_parse_json_args_inline():
    """Test JSON arguments inline in YAML"""
    text = """```yaml
name: edit_file
arguments: {"path": "src/main.py", "content": "hello world"}
```"""
    res = parse_tool_block(text)
    assert res is not None
    assert res["name"] == "edit_file"
    assert res["arguments"]["path"] == "src/main.py"
    assert res["arguments"]["content"] == "hello world"
