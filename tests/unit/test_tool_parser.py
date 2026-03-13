import pytest
from src.core.orchestration.tool_parser import parse_tool_block

def test_parse_tool_block_json_args():
    text = """
Some text before.
<tool>
name: edit_file
args: {"path": "src/foo.py", "patch": "@@ -1 +1 @@\\n- old\\n+ new\\n"}
</tool>
Some text after.
"""
    res = parse_tool_block(text)
    assert res is not None
    assert res['name'] == 'edit_file'
    assert res['arguments']['path'] == 'src/foo.py'
    assert '@@ -1 +1 @@' in res['arguments']['patch']

def test_parse_tool_block_yaml_like():
    text = """
<tool>
name: bash
command: ls -la
</tool>
"""
    res = parse_tool_block(text)
    assert res is not None
    assert res['name'] == 'bash'
    assert res['arguments']['command'] == 'ls -la'

def test_parse_tool_block_no_tool():
    res = parse_tool_block("No tool here.")
    assert res is None
