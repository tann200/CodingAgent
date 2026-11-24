from src.core.memory.session_store import SessionStore


def test_close_is_idempotent(tmp_path):
    workdir = tmp_path / "proj"
    store = SessionStore(str(workdir))

    store.add_message("s1", "user", "hello")
    assert len(store.get_messages("s1")) == 1

    # Closing twice must not raise
    store.close()
    store.close()
