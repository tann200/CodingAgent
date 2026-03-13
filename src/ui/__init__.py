"""TUI package for the CodingAgent project.

This package contains the top-level TUI application entrypoint and modular
view components. The implementation is intentionally minimal so unit tests can
import UI modules without opening a display. The real TUI will use Textual or
another framework; those imports are guarded behind try/except so tests run in
headless environments.
"""
__all__ = ["app", "views", "components"]

