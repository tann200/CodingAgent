import pytest
from pathlib import Path
from src.tools._tool import ToolDefinition
from src.core.orchestration.orchestrator import Orchestrator
from src.core.inference.adapters.mock_adapter import MockAdapter


def test_validate_args_rejects_workdir_and_working_dir_undeclared():
    def dummy(path: str):
        pass

    defn = ToolDefinition(name="dummy", fn=dummy, description="test tool")
    errs = defn.validate_args({"path": "a.py", "workdir": "/etc"})
    assert len(errs) == 1
    assert "unexpected undeclared field 'workdir'" in errs[0]

    errs2 = defn.validate_args({"path": "a.py", "working_dir": "/etc"})
    assert len(errs2) == 1
    assert "unexpected undeclared field 'working_dir'" in errs2[0]


def test_execute_tool_confinement_all_tools(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    ws.mkdir()
    secret_dir = tmp_path / "secret_dir"
    secret_dir.mkdir()
    (secret_dir / "secret.txt").write_text("SUPER_SECRET_DATA")

    # Patch heavy background model checks
    monkeypatch.setattr(
        "src.core.orchestration.orchestrator._ensure_provider_manager_initialized_sync",
        lambda: None,
    )
    monkeypatch.setattr(
        "src.core.orchestration.orchestrator.Orchestrator._background_model_check",
        lambda self: None,
    )
    monkeypatch.setattr(
        "src.core.inference.llm_manager.ProviderManager.initialize",
        lambda self: None,
    )

    adapter = MockAdapter(responses=["ok"])
    orch = Orchestrator(adapter=adapter, working_dir=str(ws))

    # Test read_file with malicious workdir
    res = orch.execute_tool({"name": "read_file", "arguments": {"path": "secret.txt", "workdir": str(secret_dir)}})
    assert res.get("status") == "error" or not res.get("ok", True)
    assert "outside" in str(res).lower() or "argument_validation_failed" in str(res)

    # Test write_file with malicious workdir
    res = orch.execute_tool(
        {"name": "write_file", "arguments": {"path": "pwned.txt", "content": "bad", "workdir": str(secret_dir)}}
    )
    assert res.get("status") == "error" or not res.get("ok", True)
    assert not (secret_dir / "pwned.txt").exists()

    # Test delete_file with malicious workdir
    res = orch.execute_tool({"name": "delete_file", "arguments": {"path": "secret.txt", "workdir": str(secret_dir)}})
    assert res.get("status") == "error" or not res.get("ok", True)
    assert (secret_dir / "secret.txt").exists()

    # Test grep with malicious workdir
    res = orch.execute_tool({"name": "grep", "arguments": {"pattern": "SUPER_SECRET", "workdir": str(secret_dir)}})
    assert res.get("status") == "error" or not res.get("ok", True)

    # Test glob with malicious workdir
    res = orch.execute_tool({"name": "glob", "arguments": {"pattern": "*", "workdir": str(secret_dir)}})
    assert res.get("status") == "error" or not res.get("ok", True)

    # Test list_files with malicious workdir
    res = orch.execute_tool({"name": "list_files", "arguments": {"path": ".", "workdir": str(secret_dir)}})
    assert res.get("status") == "error" or not res.get("ok", True)

    # Test batched_file_read with malicious workdir
    res = orch.execute_tool({"name": "batched_file_read", "arguments": {"paths": ["secret.txt"], "workdir": str(secret_dir)}})
    assert res.get("status") == "error" or not res.get("ok", True)

    # Test lsp_diagnostics with malicious working_dir
    res = orch.execute_tool({"name": "lsp_diagnostics", "arguments": {"path": "secret.txt", "working_dir": str(secret_dir)}})
    assert res.get("status") == "error" or not res.get("ok", True)

    # Test delegate_task with malicious working_dir
    res = orch.execute_tool(
        {"name": "delegate_task", "arguments": {"role": "analyst", "subtask_description": "inspect", "working_dir": str(secret_dir)}}
    )
    assert res.get("status") == "error" or not res.get("ok", True) or "outside" in str(res).lower()

    # Test memory_search with malicious workdir
    res = orch.execute_tool({"name": "memory_search", "arguments": {"query": "test", "workdir": str(secret_dir)}})
    assert res.get("status") == "error" or not res.get("ok", True)
