#!/usr/bin/env python3
import os
import sys

ROOT = os.path.dirname(os.path.dirname(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import json
from src.core.orchestration.graph.nodes.node_utils import (
    _resolve_orchestrator,
    _notify_provider_limit,
)
from src.core.orchestration.graph.nodes.perception_node import perception_node
from src.core.orchestration.graph.nodes.analysis_node import analysis_node
from src.core.orchestration.graph.nodes.planning_node import planning_node
from src.core.orchestration.graph.nodes.execution_node import execution_node


class MockAdapter:
    def __init__(self, responses):
        self._iter = iter(responses)

    def chat(self, history, stream=False, format_json=False):
        try:
            return next(self._iter)
        except StopIteration:
            return None

    def extract_tool_calls(self, resp):
        if not resp:
            return []
        msg = resp.get("message") if isinstance(resp, dict) else None
        calls = []
        if (
            isinstance(resp, dict)
            and resp.get("response")
            and isinstance(resp.get("response"), dict)
            and resp["response"].get("tool_call")
        ):
            calls.append(resp["response"].get("tool_call"))
        if isinstance(msg, dict) and msg.get("tool_calls"):
            calls.extend(msg.get("tool_calls"))
        return calls


def test_node_utils():
    print("=== Testing node_utils ===")

    state = {"task": "test task", "orchestrator": None}
    config = {"configurable": {"orchestrator": None}}

    result = _resolve_orchestrator(state, config)
    assert result is None, "Expected None for empty config"
    print("node_utils._resolve_orchestrator: PASSED")

    _notify_provider_limit("connection timeout")
    print("node_utils._notify_provider_limit: PASSED (no error expected)")


def test_yaml_tool_parsing():
    print("\n=== Testing YAML tool parsing ===")
    from src.core.orchestration.tool_parser import parse_tool_block

    yaml_block = """```yaml
name: read_file
arguments:
  path: src/main.py
```"""
    result = parse_tool_block(yaml_block)
    assert result is not None, "Failed to parse YAML block"
    assert result["name"] == "read_file", f"Expected 'read_file', got {result['name']}"
    assert result["arguments"]["path"] == "src/main.py", (
        f"Wrong path: {result['arguments']}"
    )
    print("YAML code block parsing: PASSED")

    inline_yaml = """name: edit_file
arguments:
  path: src/test.py
  patch: "@@ -1,1 +1,2 @@"
"""
    result = parse_tool_block(inline_yaml)
    assert result is not None, "Failed to parse inline YAML"
    assert result["name"] == "edit_file"
    print("Inline YAML parsing: PASSED")

    compact_yaml = """```yaml
edit_file:
  path: src/app.py
  content: new content
```"""
    result = parse_tool_block(compact_yaml)
    assert result is not None, "Failed to parse compact YAML"
    print("Compact YAML parsing: PASSED")


def test_individual_node_imports():
    print("\n=== Testing individual node imports ===")

    assert perception_node is not None
    assert analysis_node is not None
    assert planning_node is not None
    assert execution_node is not None
    print("All node imports: PASSED")


def test_agent_brain_manager():
    print("\n=== Testing AgentBrainManager ===")
    from src.core.orchestration.agent_brain import get_agent_brain_manager

    brain = get_agent_brain_manager()
    assert brain is not None

    # Test roles
    roles = brain.get_all_roles()
    assert "operational" in roles, "Missing operational role"
    assert "strategic" in roles, "Missing strategic role"
    print(f"Roles: {list(roles.keys())}")

    # Test skills
    skills = brain.get_all_skills()
    assert "dry" in skills, "Missing dry skill"
    assert "context_hygiene" in skills, "Missing context_hygiene skill"
    print(f"Skills: {list(skills.keys())}")

    # Test identities
    soul = brain.get_identity("soul")
    laws = brain.get_identity("laws")
    assert soul, "Missing SOUL"
    assert laws, "Missing LAWS"
    print("Identities: OK")

    # Test compile_system_prompt
    prompt = brain.compile_system_prompt("operational")
    assert prompt, "Empty compiled prompt"
    assert "<system_role>" in prompt, "Missing system_role tag"
    assert "<operating_principles>" in prompt, "Missing operating_principles tag"
    assert "<core_laws>" in prompt, "Missing core_laws tag"
    print("compile_system_prompt: OK")

    print("AgentBrainManager: PASSED")


if __name__ == "__main__":
    test_node_utils()
    test_yaml_tool_parsing()
    test_individual_node_imports()
    test_agent_brain_manager()
    print("\n=== All tests passed ===")
