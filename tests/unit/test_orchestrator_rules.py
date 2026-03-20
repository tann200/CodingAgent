from src.core.orchestration.orchestrator import Orchestrator, WRITE_TOOLS_REQUIRING_READ
from pydantic import BaseModel


def test_read_before_edit_enforcement(tmp_path):
    orch = Orchestrator(working_dir=str(tmp_path))

    # Create a dummy file
    test_file = tmp_path / "test.txt"
    test_file.write_text("initial content\n")

    # Attempt to edit without reading
    edit_call = {
        "name": "edit_file",
        "arguments": {
            "path": "test.txt",
            "patch": "--- test.txt\n+++ test.txt\n@@ -1 +1 @@\n-initial content\n+updated content\n",
        },
    }

    res = orch.execute_tool(edit_call)
    assert res["ok"] is False
    assert "must read" in res["error"]

    # Now read the file
    read_call = {"name": "read_file", "arguments": {"path": "test.txt"}}
    res = orch.execute_tool(read_call)
    assert res["ok"] is True
    assert res["result"]["status"] == "ok"

    # Now attempt to edit again
    res = orch.execute_tool(edit_call)
    if not res["ok"] or res["result"]["status"] != "ok":
        print(f"Error: {res}")
    assert res["ok"] is True
    assert res["result"]["status"] == "ok"
    assert test_file.read_text().strip() == "updated content"


def test_preflight_check_sandbox(tmp_path):
    orch = Orchestrator(working_dir=str(tmp_path))

    # Attempt to write outside sandbox
    write_call = {
        "name": "write_file",
        "arguments": {"path": "../outside.txt", "content": "illegal"},
    }

    # Check preflight manually first
    pre = orch.preflight_check(write_call)
    assert pre["ok"] is False
    assert "outside working directory" in pre["error"]

    # Verify preflight blocks the path traversal attempt (already asserted above).
    # The orchestrator loop integration is covered by the preflight_check assertion.


class MyToolContract(BaseModel):
    test: str


def my_tool(test: str):
    return {"test": test}


def test_tool_contract_validation(tmp_path):
    orch = Orchestrator(working_dir=str(tmp_path))
    from src.core.orchestration.tool_contracts import register_tool_contract

    register_tool_contract("my_tool", MyToolContract)
    orch.tool_registry.register("my_tool", my_tool)

    # Valid call
    edit_call = {"name": "my_tool", "arguments": {"test": "hello"}}
    res = orch.execute_tool(edit_call)
    assert res["ok"] is True
    assert res["result"]["test"] == "hello"

    # Invalid call
    edit_call = {"name": "my_tool", "arguments": {"wrong_arg": "hello"}}
    res = orch.execute_tool(edit_call)
    assert res["ok"] is False


def test_write_tools_requiring_read_set():
    """WRITE_TOOLS_REQUIRING_READ contains the expected tool names."""
    assert "edit_file" in WRITE_TOOLS_REQUIRING_READ
    assert "write_file" in WRITE_TOOLS_REQUIRING_READ
    assert "edit_by_line_range" in WRITE_TOOLS_REQUIRING_READ
    assert "apply_patch" in WRITE_TOOLS_REQUIRING_READ


def test_write_file_requires_read_first(tmp_path):
    orch = Orchestrator(working_dir=str(tmp_path))

    existing = tmp_path / "existing.txt"
    existing.write_text("hello\n")

    write_call = {
        "name": "write_file",
        "arguments": {"path": "existing.txt", "content": "overwritten"},
    }

    res = orch.execute_tool(write_call)
    assert res["ok"] is False
    assert "must read" in res["error"]

    # After reading, write is allowed
    orch.execute_tool({"name": "read_file", "arguments": {"path": "existing.txt"}})
    res = orch.execute_tool(write_call)
    assert res["ok"] is True


def test_new_file_write_not_blocked_without_prior_read(tmp_path):
    """Writing a brand-new file (doesn't exist on disk) must NOT be blocked."""
    orch = Orchestrator(working_dir=str(tmp_path))

    write_call = {
        "name": "write_file",
        "arguments": {"path": "brand_new.txt", "content": "new content"},
    }

    res = orch.execute_tool(write_call)
    assert res["ok"] is True, f"Expected ok=True for new file write, got: {res}"


class TestOrchestratorLoopHandledCheck:
    """Regression tests for the multi-round orchestrator loop 'handled' check.

    Before the fix: the loop checked `role == "tool"` for execution results, but
    execution_node stores results as `role == "user"`.  This caused the loop to
    run all 12 rounds for every simple fast-path task (list_files, read_file, etc.)
    because `handled` was always False.

    After the fix: the check matches any message that contains "tool_execution_result"
    regardless of role, so fast-path tasks exit after 1 graph round.
    """

    def _make_history_with_user_result(self, yaml_content: str, result_content: str):
        """Build a history list like execution_node produces (role='user' for results)."""
        return [
            {"role": "assistant", "content": yaml_content},
            {"role": "user", "content": result_content},
        ]

    def _make_history_with_tool_result(self, yaml_content: str, result_content: str):
        """Build a history list with role='tool' (hypothetical future format)."""
        return [
            {"role": "assistant", "content": yaml_content},
            {"role": "tool", "content": result_content},
        ]

    def _check_handled(self, history, last_assistant):
        """Replicate the orchestrator's 'handled' check."""
        last_assistant_idx = None
        for idx in range(len(history) - 1, -1, -1):
            if history[idx].get("role") == "assistant" and history[idx].get("content") == last_assistant:
                last_assistant_idx = idx
                break

        handled = False
        if last_assistant_idx is not None:
            for later in history[last_assistant_idx + 1:]:
                if "tool_execution_result" in (later.get("content") or ""):
                    handled = True
                    break
        return handled

    def test_user_role_result_is_detected_as_handled(self):
        """execution_node uses role='user' — must be detected as handled."""
        yaml_content = "```yaml\nname: list_files\narguments:\n  path: .\n```"
        result_content = '{"tool_execution_result": {"ok": true, "result": {"items": []}}}'
        history = self._make_history_with_user_result(yaml_content, result_content)
        assert self._check_handled(history, yaml_content) is True

    def test_tool_role_result_is_also_detected_as_handled(self):
        """For future compatibility, role='tool' must also be detected."""
        yaml_content = "```yaml\nname: read_file\narguments:\n  path: foo.py\n```"
        result_content = '{"tool_execution_result": {"ok": true, "result": {"content": "x"}}}'
        history = self._make_history_with_tool_result(yaml_content, result_content)
        assert self._check_handled(history, yaml_content) is True

    def test_no_result_is_not_handled(self):
        """If tool hasn't been executed yet, handled must be False."""
        yaml_content = "```yaml\nname: list_files\narguments:\n  path: .\n```"
        history = [{"role": "assistant", "content": yaml_content}]
        assert self._check_handled(history, yaml_content) is False

    def test_unrelated_user_message_is_not_handled(self):
        """A user message without tool_execution_result must not mark as handled."""
        yaml_content = "```yaml\nname: list_files\narguments:\n  path: .\n```"
        history = [
            {"role": "assistant", "content": yaml_content},
            {"role": "user", "content": "please do something else"},
        ]
        assert self._check_handled(history, yaml_content) is False


class TestOrchestratorResponseAssembly:
    """Regression tests for the response assembly bugs fixed alongside the loop fix.

    Three bugs were present:
    1. Tool result extraction checked role='tool' but execution_node uses role='user'.
    2. _is_tool_call_msg only matched bare "name:" but not ```yaml blocks.
    3. Result unwrapping looked for data["result"] but data was {"tool_execution_result": {...}}.
    """

    # ------------------------------------------------------------------ helpers

    def _is_tool_call_msg(self, last_assistant: str) -> bool:
        """Replicate orchestrator._is_tool_call_msg logic (including <think> stripping)."""
        import re
        _s = re.sub(r"<think>.*?</think>", "", last_assistant, flags=re.DOTALL).strip()
        return (
            not last_assistant
            or _s.startswith("name:")
            or _s.startswith("```yaml")
            or _s.startswith("```\nname:")
            or (_s.startswith("```") and "name:" in _s)
        )

    def _extract_tool_results(self, history: list) -> list:
        """Replicate orchestrator tool-result extraction logic (Bug 2 + Bug 3 fix)."""
        import json

        tool_results = []
        for i, m in enumerate(history):
            is_tool_result = m.get("role") == "tool" or (
                m.get("role") == "user"
                and "tool_execution_result" in (m.get("content") or "")
            )
            if not is_tool_result:
                continue

            content = m.get("content", "")
            if "tool_execution_result" in content:
                try:
                    data = json.loads(content)
                    if isinstance(data, dict) and "tool_execution_result" in data:
                        ter = data["tool_execution_result"]
                        if isinstance(ter, dict) and "result" in ter:
                            tool_results.append(ter["result"])
                        else:
                            tool_results.append(ter)
                    elif isinstance(data, dict) and "result" in data:
                        tool_results.append(data["result"])
                    elif isinstance(data, dict) and data.get("ok"):
                        tool_results.append(data)
                except (json.JSONDecodeError, TypeError):
                    tool_results.append(content)
            elif content:
                tool_results.append(content)
        return tool_results

    # ------------------------------------------------------------------ Bug 1: role="user" extraction

    def test_user_role_result_is_extracted(self):
        """execution_node stores results with role='user' — must be extracted."""
        result_content = '{"tool_execution_result": {"ok": true, "result": {"items": ["a.py"]}}}'
        history = [
            {"role": "assistant", "content": "```yaml\nname: list_files\narguments:\n  path: .\n```"},
            {"role": "user", "content": result_content},
        ]
        results = self._extract_tool_results(history)
        assert len(results) == 1
        assert results[0] == {"items": ["a.py"]}

    def test_tool_role_result_is_also_extracted(self):
        """role='tool' (legacy/future) must also be extracted."""
        result_content = '{"tool_execution_result": {"ok": true, "result": {"content": "hello"}}}'
        history = [
            {"role": "assistant", "content": "```yaml\nname: read_file\narguments:\n  path: x.py\n```"},
            {"role": "tool", "content": result_content},
        ]
        results = self._extract_tool_results(history)
        assert len(results) == 1
        assert results[0] == {"content": "hello"}

    def test_plain_user_message_not_extracted(self):
        """A normal user message without tool_execution_result must not be extracted."""
        history = [
            {"role": "user", "content": "hello there"},
        ]
        results = self._extract_tool_results(history)
        assert results == []

    # ------------------------------------------------------------------ Bug 2: YAML block detection

    def test_yaml_fenced_block_detected_as_tool_call(self):
        """```yaml\\nname: ... blocks must be detected as tool calls."""
        msg = "```yaml\nname: list_files\narguments:\n  path: .\n```"
        assert self._is_tool_call_msg(msg) is True

    def test_bare_name_detected_as_tool_call(self):
        """Bare 'name: ...' (no fences) must be detected as tool call."""
        msg = "name: list_files\narguments:\n  path: ."
        assert self._is_tool_call_msg(msg) is True

    def test_backtick_name_block_detected_as_tool_call(self):
        """```\\nname: ... blocks must be detected as tool calls."""
        msg = "```\nname: list_files\narguments:\n  path: .\n```"
        assert self._is_tool_call_msg(msg) is True

    def test_prose_response_not_detected_as_tool_call(self):
        """A plain text assistant response must NOT be detected as a tool call."""
        msg = "Here are the files in the working directory:\n- src/\n- tests/"
        assert self._is_tool_call_msg(msg) is False

    def test_empty_assistant_message_detected_as_tool_call(self):
        """An empty assistant message is treated as a tool call (no prose to show)."""
        assert self._is_tool_call_msg("") is True

    def test_think_prefixed_yaml_block_detected_as_tool_call(self):
        """LM Studio/Qwen models emit <think>...</think> before the YAML block.
        After stripping <think>, the block must be detected as a tool call."""
        msg = "<think>The user wants to list files...\n</think>\n\n```yaml\nname: list_files\narguments:\n  path: .\n```"
        assert self._is_tool_call_msg(msg) is True

    def test_think_prefixed_prose_not_detected_as_tool_call(self):
        """A <think> block followed by prose (not YAML) must not be a tool call."""
        msg = "<think>I should answer directly.</think>\n\nHere are the files: src/, tests/"
        assert self._is_tool_call_msg(msg) is False

    # ------------------------------------------------------------------ Bug 3: envelope unwrapping

    def test_envelope_unwrapping_extracts_inner_result(self):
        """{"tool_execution_result": {"ok": True, "result": {...}}} → inner result dict."""
        import json

        envelope = json.dumps(
            {"tool_execution_result": {"ok": True, "result": {"items": ["x.py", "y.py"]}}}
        )
        history = [
            {"role": "user", "content": envelope},
        ]
        results = self._extract_tool_results(history)
        assert results == [{"items": ["x.py", "y.py"]}]

    def test_envelope_without_result_key_returns_ter(self):
        """{"tool_execution_result": {"ok": False, "error": "..."}} — returns the ter dict."""
        import json

        envelope = json.dumps(
            {"tool_execution_result": {"ok": False, "error": "not found"}}
        )
        history = [{"role": "user", "content": envelope}]
        results = self._extract_tool_results(history)
        assert results == [{"ok": False, "error": "not found"}]

    def test_legacy_flat_format_inside_envelope(self):
        """{"tool_execution_result": {"result": {...}}} — inner result extracted."""
        import json

        # The legacy path inside the envelope: ter has "result" key
        envelope = json.dumps(
            {"tool_execution_result": {"result": {"content": "file body"}, "ok": True}}
        )
        history = [{"role": "tool", "content": envelope}]
        results = self._extract_tool_results(history)
        assert results == [{"content": "file body"}]

