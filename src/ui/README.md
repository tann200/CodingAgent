TUI module for CodingAgent

Plans:
- Provide a lightweight, testable controller layer that integrates with core services
  (EventBus, Orchestrator, ProviderManager).
- Keep presentation (Textual app, widgets) separated from controllers so unit tests
  can exercise logic without a display.

Structure:
- src/ui/app.py - top-level application shim
- src/ui/views/* - view controllers (main_view, provider_panel)
- src/ui/components/* - small components like log panel
- src/ui/styles/*.tcss - Textual CSS files (kept separate)

How to run tests:
- The TUI package is non-GUI for now; instantiate `CodingAgentApp` in tests to
  exercise wiring without launching a GUI.

