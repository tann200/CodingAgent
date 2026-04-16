from src.core.orchestration.orchestrator_bootstrap import (
    register_config_reload_handlers,
)


def test_register_config_reload_handlers_idempotent(monkeypatch):
    # Ensure no leftover registration state on the function
    if hasattr(register_config_reload_handlers, "_registered"):
        try:
            delattr(register_config_reload_handlers, "_registered")
        except Exception:
            pass

    # Fake reloader that records added callbacks
    class FakeReloader:
        def __init__(self):
            self.callbacks = []

        def add_callback(self, cb):
            self.callbacks.append(cb)

    fake = FakeReloader()

    def fake_get_config_reloader(initial_load=False):
        return fake

    monkeypatch.setattr(
        "src.core.config_hot_reload.get_config_reloader",
        fake_get_config_reloader,
        raising=True,
    )

    # Minimal orchestrator-like object
    class FakeOrch:
        def __init__(self):
            self.event_bus = type("B", (), {"publish": lambda *a, **k: None})()
            self.msg_mgr = type("M", (), {"messages": []})()
            self.working_dir = None
            self.lifecycle_manager = type(
                "L", (), {"on_shutdown": lambda *a, **k: None}
            )()

    fo = FakeOrch()

    # Call register twice — should only register a single callback with the reloader
    register_config_reload_handlers(fo)
    register_config_reload_handlers(fo)

    assert len(fake.callbacks) == 1
