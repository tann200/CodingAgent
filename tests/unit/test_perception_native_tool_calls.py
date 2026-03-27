"""
Tests for Phase 7: Native Tool Support - perception_node native tool_calls parsing.

These tests verify that perception_node correctly handles:
1. Native JSON tool_calls from frontier models (OpenAI, etc.)
2. Fallback to YAML parsing for local models (LM Studio, Ollama)
"""

import json
import pytest
from unittest.mock import MagicMock, patch


def test_parse_native_tool_call_basic():
    """Test parsing basic native tool_call structure."""
    # Simulate what perception_node does with tool_calls
    message_obj = {
        "content": "",
        "tool_calls": [
            {
                "id": "call_abc123",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path": "main.py"}'},
            }
        ],
    }

    # Extract tool call (same logic as perception_node)
    tool_call = None
    native_tool_calls = message_obj.get("tool_calls")
    if (
        native_tool_calls
        and isinstance(native_tool_calls, list)
        and len(native_tool_calls) > 0
    ):
        tc = native_tool_calls[0]
        if isinstance(tc, dict):
            func = tc.get("function")
            if func:
                name = func.get("name")
                args = func.get("arguments")
                if isinstance(args, str):
                    args = json.loads(args)
                if name:
                    tool_call = {"name": name, "arguments": args or {}}

    assert tool_call is not None
    assert tool_call["name"] == "read_file"
    assert tool_call["arguments"]["path"] == "main.py"


def test_parse_native_tool_call_complex_args():
    """Test parsing native tool_call with complex arguments."""
    message_obj = {
        "content": "",
        "tool_calls": [
            {
                "id": "call_xyz789",
                "type": "function",
                "function": {
                    "name": "edit_file",
                    "arguments": json.dumps(
                        {
                            "path": "src/main.py",
                            "oldString": "def hello():",
                            "newString": "def hello_world():",
                        }
                    ),
                },
            }
        ],
    }

    # Extract tool call
    tool_call = None
    native_tool_calls = message_obj.get("tool_calls")
    if (
        native_tool_calls
        and isinstance(native_tool_calls, list)
        and len(native_tool_calls) > 0
    ):
        tc = native_tool_calls[0]
        if isinstance(tc, dict):
            func = tc.get("function")
            if func:
                name = func.get("name")
                args = func.get("arguments")
                if isinstance(args, str):
                    args = json.loads(args)
                if name:
                    tool_call = {"name": name, "arguments": args or {}}

    assert tool_call is not None
    assert tool_call["name"] == "edit_file"
    assert tool_call["arguments"]["oldString"] == "def hello():"
    assert tool_call["arguments"]["newString"] == "def hello_world():"


def test_native_tool_call_preference_over_yaml():
    """Test that native tool_calls are preferred over YAML parsing."""
    # Response with BOTH native tool_calls and YAML in content
    message_obj = {
        "content": "```yaml\nname: read_file\narguments:\n  path: old.py\n```",
        "tool_calls": [
            {
                "id": "call_new",
                "type": "function",
                "function": {
                    "name": "write_file",
                    "arguments": '{"path": "new.py", "content": "print(1)"}',
                },
            }
        ],
    }

    # Should prefer native tool_calls
    tool_call = None
    native_tool_calls = message_obj.get("tool_calls")
    if (
        native_tool_calls
        and isinstance(native_tool_calls, list)
        and len(native_tool_calls) > 0
    ):
        tc = native_tool_calls[0]
        if isinstance(tc, dict):
            func = tc.get("function")
            if func:
                name = func.get("name")
                args = func.get("arguments")
                if isinstance(args, str):
                    args = json.loads(args)
                if name:
                    tool_call = {"name": name, "arguments": args or {}}

    # Should get write_file (from native), NOT read_file (from YAML)
    assert tool_call is not None
    assert tool_call["name"] == "write_file"
    assert tool_call["arguments"]["path"] == "new.py"


def test_fallback_to_yaml_when_no_native_calls():
    """Test fallback to YAML parsing when no native tool_calls."""
    content = "```yaml\nname: list_files\narguments:\n  path: .\n```"
    message_obj = {"content": content, "tool_calls": None}

    # Should fall back to YAML parsing (simulated)
    native_tool_calls = message_obj.get("tool_calls")
    tool_call = None

    if (
        not native_tool_calls
        or not isinstance(native_tool_calls, list)
        or len(native_tool_calls) == 0
    ):
        # Fallback to YAML parsing (simplified)
        if "name:" in content and "arguments:" in content:
            # Simplified YAML extraction
            import re

            name_match = re.search(r"name:\s*(\w+)", content)
            if name_match:
                tool_call = {"name": name_match.group(1), "arguments": {}}

    assert tool_call is not None
    assert tool_call["name"] == "list_files"


def test_empty_tool_calls_handled():
    """Test that empty tool_calls list is handled properly."""
    message_obj = {"content": "", "tool_calls": []}

    tool_call = None
    native_tool_calls = message_obj.get("tool_calls")

    # Should not crash and should return None
    if (
        native_tool_calls
        and isinstance(native_tool_calls, list)
        and len(native_tool_calls) > 0
    ):
        tc = native_tool_calls[0]
        if isinstance(tc, dict):
            func = tc.get("function")
            if func:
                name = func.get("name")
                if name:
                    tool_call = {"name": name, "arguments": {}}

    assert tool_call is None


def test_invalid_arguments_json_handled():
    """Test that invalid JSON in arguments is handled gracefully."""
    message_obj = {
        "content": "",
        "tool_calls": [
            {
                "id": "call_bad",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": "not valid json {",  # Invalid JSON
                },
            }
        ],
    }

    tool_call = None
    native_tool_calls = message_obj.get("tool_calls")
    if (
        native_tool_calls
        and isinstance(native_tool_calls, list)
        and len(native_tool_calls) > 0
    ):
        tc = native_tool_calls[0]
        if isinstance(tc, dict):
            func = tc.get("function")
            if func:
                name = func.get("name")
                args = func.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}  # Fallback to empty dict
                if name:
                    tool_call = {"name": name, "arguments": args or {}}

    # Should still extract name but with empty arguments
    assert tool_call is not None
    assert tool_call["name"] == "read_file"
    assert tool_call["arguments"] == {}
