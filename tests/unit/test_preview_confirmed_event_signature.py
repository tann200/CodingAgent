from src.core.messaging.event_types import PreviewConfirmed, PreviewRejected


def test_preview_events_accept_path_and_preview_id():
    ev_c = PreviewConfirmed(preview_id="path/to/file.py", path="path/to/file.py")
    assert ev_c.preview_id == "path/to/file.py"
    assert ev_c.path == "path/to/file.py"

    ev_r = PreviewRejected(preview_id="path/to/file.py", path="path/to/file.py")
    assert ev_r.preview_id == "path/to/file.py"
    assert ev_r.path == "path/to/file.py"


def test_preview_events_default_values():
    ev_c = PreviewConfirmed()
    assert ev_c.preview_id == ""
    assert ev_c.path is None

    ev_r = PreviewRejected()
    assert ev_r.preview_id == ""
    assert ev_r.path is None
