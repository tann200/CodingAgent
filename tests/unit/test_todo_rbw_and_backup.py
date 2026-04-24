import json


from src.tools.todo_tools import manage_todo, _todo_json_path, _todo_path


def test_notify_rbw_updates_orchestrator_and_invalidate(tmp_path, monkeypatch):
    workdir = tmp_path

    class MockOrch:
        def __init__(self):
            self._session_read_files = set()

    mock_orch = MockOrch()

    from src.tools.subagent_tools import _PARENT_ORCHESTRATOR_VAR

    token = _PARENT_ORCHESTRATOR_VAR.set(mock_orch)
    try:
        calls = []
        from src.core.context.context_builder import ContextBuilder

        # Replace invalidate_path with a classmethod that records calls
        monkeypatch.setattr(
            ContextBuilder,
            "invalidate_path",
            classmethod(lambda cls, path: calls.append(path)),
        )

        res = manage_todo(
            "create", str(workdir), steps=["a", "b"], depends_on=[[], [0]]
        )
        assert res["status"] == "ok"

        md_path = str(_todo_path(str(workdir)).resolve())
        json_path = str(_todo_json_path(str(workdir)).resolve())

        assert md_path in mock_orch._session_read_files
        assert json_path in mock_orch._session_read_files

        # ContextBuilder.invalidate_path should have been called for TODO.md
        assert md_path in calls
    finally:
        _PARENT_ORCHESTRATOR_VAR.reset(token)


def test_backup_and_restore_on_replace_failure(tmp_path, monkeypatch):
    workdir = tmp_path

    # Create initial todo so that backups exist on the next save
    res = manage_todo("create", str(workdir), steps=["a", "b"], depends_on=[[], [0]])
    assert res["status"] == "ok"

    json_path = _todo_json_path(str(workdir))
    assert json_path.exists()
    original = json.loads(json_path.read_text(encoding="utf-8"))

    import pathlib

    orig_replace = pathlib.Path.replace

    def failing_replace(self, target):
        # Simulate failure only when moving a tmp file into todo.json
        if ".tmp." in self.name and str(target).endswith("todo.json"):
            raise OSError("simulated replace failure")
        return orig_replace(self, target)

    monkeypatch.setattr(pathlib.Path, "replace", failing_replace)

    try:
        res2 = manage_todo(
            "create", str(workdir), steps=["x", "y"], depends_on=[[], [0]]
        )
        assert res2["status"] == "error"

        # Ensure original todo.json content was restored
        assert json.loads(json_path.read_text(encoding="utf-8")) == original

        # No leftover backups
        backups = list(json_path.parent.glob("todo.json.bak.*"))
        assert len(backups) == 0
    finally:
        # Restore original replace implementation
        monkeypatch.setattr(pathlib.Path, "replace", orig_replace)
