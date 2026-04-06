"""tests/unit/test_orchestrator_tool_registry.py

Consolidated tests for Orchestrator tool registry behaviour.
Replaces two single-test files:
  - test_tool_aliases.py   (fs.read / fs.write aliases)
  - test_tool_contracts.py (execute_tool result normalisation + read-before-write)
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Tool aliases (fs.read, fs.write)
# ---------------------------------------------------------------------------

def test_tool_aliases_read_write(tmp_path):
    from src.core.orchestration.orchestrator import example_registry

    reg = example_registry()
    assert "read_file" in reg.tools
    assert "fs.read" in reg.tools
    assert "write_file" in reg.tools
    assert "fs.write" in reg.tools

    write_fn = reg.get("write_file").get("fn")  # type: ignore[union-attr]
    write_fn(str(tmp_path / "foo.txt"), "hello-alias", workdir=tmp_path)  # type: ignore[misc]
    p = tmp_path / "foo.txt"
    assert p.exists()

    read_fn = reg.get("read_file").get("fn")  # type: ignore[union-attr]
    alias_read_fn = reg.get("fs.read").get("fn")  # type: ignore[union-attr]
    r1 = read_fn(str(p), workdir=tmp_path)  # type: ignore[misc]
    r2 = alias_read_fn(str(p), workdir=tmp_path)  # type: ignore[misc]
    assert r1.get("content") == r2.get("content")
    assert "hello-alias" in r1.get("content")

    write_alias = reg.get("fs.write").get("fn")  # type: ignore[union-attr]
    write_alias(str(tmp_path / "bar.txt"), "bar-content", workdir=tmp_path)  # type: ignore[misc]
    assert (tmp_path / "bar.txt").read_text(encoding="utf-8") == "bar-content"


# ---------------------------------------------------------------------------
# execute_tool — result normalisation + read-before-write guard
# ---------------------------------------------------------------------------

def test_execute_tool_normalizes_result(tmp_path):
    from src.core.orchestration.orchestrator import Orchestrator, ToolRegistry
    from src.tools import file_tools

    reg = ToolRegistry()
    orch = Orchestrator(working_dir=str(tmp_path), tool_registry=reg)

    p = tmp_path / "a.txt"
    p.write_text("hello\n")

    # edit before read → blocked by read-before-write guard
    edit_patch = "--- a.txt\n+++ a.txt\n@@ -1 +1 @@\n-hello\n+world\n"
    res0 = orch.execute_tool(
        {"name": "edit_file", "arguments": {"path": "a.txt", "patch": edit_patch}}
    )
    assert res0.get("ok") is False
    assert "read" in (res0.get("error") or "").lower()

    # register tools and read first
    reg.register("read_file", file_tools.read_file)
    reg.register("edit_file", file_tools.edit_file, side_effects=["write"])

    res = orch.execute_tool({"name": "read_file", "arguments": {"path": "a.txt"}})
    assert res.get("ok") is True
    assert isinstance(res.get("result"), dict)

    # now edit is allowed
    res2 = orch.execute_tool(
        {"name": "edit_file", "arguments": {"path": "a.txt", "patch": edit_patch}}
    )
    assert res2.get("ok") is True
    assert isinstance(res2.get("result"), dict)
