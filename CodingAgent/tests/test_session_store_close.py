import threading

from src.core.memory.jsonl_session_store import JsonlSessionStore


def test_close_clears_locks_and_is_idempotent(tmp_path):
    workdir = tmp_path / "proj"
    store = JsonlSessionStore(str(workdir))

    # Create a session file by adding a message
    store.add_message("s1", "user", "hello")

    # Ensure the per-session lock exists
    assert "s1" in store._locks

    # Create a read operation in another thread to exercise locking
    def worker():
        store.get_messages("s1")

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    # Close should clear internal locks and be safe to call multiple times
    store.close()

    assert store._locks == {}

    # second close must not raise
    store.close()
