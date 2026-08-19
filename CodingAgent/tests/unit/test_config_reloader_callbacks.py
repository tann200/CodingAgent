

def test_config_reloader_callbacks_invoked(tmp_path):
    """Ensure callbacks registered with ConfigReloader are invoked on load
    and on watcher-driven change notification.
    """
    from src.core.config_hot_reload import ConfigReloader

    reloader = ConfigReloader(initial_load=False)

    calls = []

    def cb(changed_paths):
        calls.append(changed_paths)

    reloader.add_callback(cb)

    # Explicit programmatic load should invoke callback with None
    reloader.load()
    assert calls and calls[-1] is None

    # Simulate watcher-driven change by invoking the internal _on_change
    # method which in normal operation would be called by the watcher loop.
    # Call _on_change directly with a sample set and expect the callback to
    # receive that set.
    sample = {"/fake/path/config.user.yaml"}
    # Use the public-facing method if present; otherwise call the private
    # helper used in the module.
    # The watcher uses ConfigWatcher._on_change; here we simulate by calling
    # the reloader's _invoke_callbacks (since _on_change belongs to the
    # ConfigWatcher). We'll call _invoke_callbacks to simulate the watcher
    # behavior.
    reloader._invoke_callbacks(sample)
    assert calls[-1] == sample
