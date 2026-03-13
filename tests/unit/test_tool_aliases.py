from src.core.orchestration.orchestrator import example_registry
from pathlib import Path


def test_tool_aliases_read_write(tmp_path):
    reg = example_registry()
    assert "read_file" in reg.tools
    assert "fs.read" in reg.tools
    assert "write_file" in reg.tools
    assert "fs.write" in reg.tools

    # write a file using canonical write_file
    write_fn = reg.get("write_file").get("fn")
    write_res = write_fn(str(tmp_path / "foo.txt"), "hello-alias", workdir=tmp_path)
    # verify file exists
    p = tmp_path / "foo.txt"
    assert p.exists()
    # read via canonical read
    read_fn = reg.get("read_file").get("fn")
    r1 = read_fn(str(p), workdir=tmp_path)
    # read via alias fs.read
    alias_read_fn = reg.get("fs.read").get("fn")
    r2 = alias_read_fn(str(p), workdir=tmp_path)
    assert r1.get("content") == r2.get("content")
    assert "hello-alias" in r1.get("content")

    # overwrite using alias fs.write on a new file path
    write_alias = reg.get("fs.write").get("fn")
    write_alias(str(tmp_path / "bar.txt"), "bar-content", workdir=tmp_path)
    assert (tmp_path / "bar.txt").read_text(encoding="utf-8") == "bar-content"

