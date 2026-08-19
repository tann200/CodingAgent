"""Shared test helpers, notably ``publish_typed_as_dict`` for test bus stubs."""


def publish_typed_as_dict(self, event):
    """Reusable ``publish_typed`` for test bus stubs: converts a typed event to
    a ``(name, dict)`` tuple and emits it via ``self.publish(name, dict)``.

    Strips auto-generated ``correlation_id`` and ``timestamp`` fields so the
    resulting dict matches the legacy string-event assertion format.

    Usage in a test file::

        class _EventBus:
            publish = ...  # your existing publish(self, name, payload)
            publish_typed = publish_typed_as_dict

    This avoids copying the same conversion boilerplate across dozens of tests.
    """
    from src.core.orchestration.event_bus import _get_event_name_for_class

    name = _get_event_name_for_class(type(event)) or type(event).__name__
    d = event.to_dict()
    d.pop("correlation_id", None)
    d.pop("timestamp", None)
    self.publish(name, d)
