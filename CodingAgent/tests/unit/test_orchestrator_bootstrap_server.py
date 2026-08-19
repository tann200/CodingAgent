import sys
from types import SimpleNamespace
from pathlib import Path



def test_orchestrator_registers_event_bus_with_server(monkeypatch):
    # Prepare a fake src.server.app module with register_event_bus
    from types import ModuleType

    mod_app = ModuleType("src.server.app")

    def _reg(bus):
        setattr(mod_app, "_registered", bus)

    mod_app.register_event_bus = _reg
    mod_app.ServerEventBusAdapter = lambda bus: "adapter"
    mod_app.run_server = lambda host, port: None

    # Create a parent package module for src.server
    mod_server = ModuleType("src.server")
    mod_server.app = mod_app

    # Save any existing modules and inject our fake module so imports inside
    # orchestrator_bootstrap pick it up.
    orig_mod_app = sys.modules.pop("src.server.app", None)
    orig_mod_server = sys.modules.pop("src.server", None)
    sys.modules["src.server"] = mod_server
    sys.modules["src.server.app"] = mod_app

    # Ensure the environment variable enables the HTTP server path
    monkeypatch.setenv("CODING_AGENT_HTTP_SERVER", "true")

    # Create a minimal orch object
    from src.core.orchestration.event_bus import EventBus

    orch = SimpleNamespace()
    orch.event_bus = EventBus()
    orch.working_dir = Path(".")
    orch.tool_registry = SimpleNamespace(tools={})
    # Provide minimal no-op hooks expected by _init_services
    orch._publish_active_config = lambda: None
    orch._background_model_check = lambda: None

    # Patch threading.Thread.start to no-op so no background server thread runs
    import threading

    orig_start = threading.Thread.start
    threading.Thread.start = lambda self: None

    try:
        # Call the _init_services helper which contains the server startup block
        from src.core.orchestration import orchestrator_bootstrap as ob

        # The function is safe to call; many internal ops are try/except guarded.
        ob._init_services(orch)

        # Assert that the orchestrator attempted to initialise the server in
        # one of the expected ways: either register_event_bus() was called on
        # the server module, the module received .event_bus assignment, or the
        # orchestrator created a background server thread. Any of these
        # indicates the integration point was exercised.
        registered = getattr(mod_app, "_registered", None)
        fallback_assigned = getattr(mod_app, "event_bus", None)
        thread_set = getattr(orch, "_http_server_thread", None) is not None
        assert (
            registered is orch.event_bus
            or fallback_assigned is orch.event_bus
            or thread_set
        )
    finally:
        threading.Thread.start = orig_start
        # Restore any original modules we replaced
        try:
            del sys.modules["src.server.app"]
        except Exception:
            pass
        try:
            del sys.modules["src.server"]
        except Exception:
            pass
        if orig_mod_app is not None:
            sys.modules["src.server.app"] = orig_mod_app
        if orig_mod_server is not None:
            sys.modules["src.server"] = orig_mod_server
