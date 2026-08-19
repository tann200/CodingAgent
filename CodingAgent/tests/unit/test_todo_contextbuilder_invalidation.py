from src.core.context.context_builder import ContextBuilder
from src.tools.todo_tools import manage_todo, _todo_path


def test_contextbuilder_invalidated_after_manage_todo_write(tmp_path):
    """ContextBuilder should see updated TODO.md after manage_todo writes.

    This verifies that todo_tools._notify_rbw_after_write calls
    ContextBuilder.invalidate_path so cached TODO.md content is evicted.
    """
    workdir = str(tmp_path)
    ac = tmp_path / ".codingAgent"
    ac.mkdir(parents=True, exist_ok=True)

    # Create initial TODO.md content directly
    initial = "# Agent TODO\n- [ ] **Step 1:** initial content\n"
    todo_file = _todo_path(workdir)
    todo_file.write_text(initial, encoding="utf-8")

    builder = ContextBuilder(working_dir=workdir)
    # Prime the cache by reading
    cached = builder._get_todo_content()
    assert cached is not None and "initial content" in cached

    # Use manage_todo to create a new TODO (this should invalidate the cache)
    res = manage_todo(action="create", workdir=workdir, steps=["New A", "New B"])
    assert res.get("status") == "ok"

    # After manage_todo, the builder should read the updated TODO.md
    updated = builder._get_todo_content()
    assert updated is not None
    assert "New A" in updated and "initial content" not in updated
