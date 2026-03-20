from src.core.orchestration.orchestrator import Orchestrator, ToolRegistry


def test_execute_tool_normalizes_result(tmp_path):
    # Pass a tool registry to avoid provider initialization during Orchestrator.__init__
    reg = ToolRegistry()
    orch = Orchestrator(working_dir=str(tmp_path), tool_registry=reg)
    # create a dummy read file
    p = tmp_path / "a.txt"
    p.write_text("hello\n")

    # Attempt edit without read -> should be blocked by read-before-edit rule
    edit_patch = "--- a.txt\n+++ a.txt\n@@ -1 +1 @@\n-hello\n+world\n"
    res0 = orch.execute_tool({"name": "edit_file", "arguments": {"path": "a.txt", "patch": edit_patch}})
    assert res0.get("ok") is False
    assert "read" in res0.get("error").lower()

    # call read_file via execute_tool
    # we need to register read_file tool in the provided registry
    from src.tools import file_tools
    reg.register("read_file", file_tools.read_file)
    reg.register("edit_file", file_tools.edit_file, side_effects=["write"])

    res = orch.execute_tool({"name": "read_file", "arguments": {"path": "a.txt"}})
    assert res.get("ok") is True
    result = res.get("result")
    assert isinstance(result, dict)
    # normalized result should contain 'status' or 'ok'
    assert ("status" in result) or (result.get("ok") in [True, False])

    # Now attempt edit again -> should be allowed (since read was performed)
    res2 = orch.execute_tool({"name": "edit_file", "arguments": {"path": "a.txt", "patch": edit_patch}})
    # depending on environment patch application may succeed; ensure it's a normalized response
    assert res2.get("ok") is True
    assert isinstance(res2.get("result"), dict)
