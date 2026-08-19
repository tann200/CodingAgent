from textual.screen import ModalScreen
from textual.widgets import Static
from textual.containers import VerticalScroll


class ProbeResultsScreen(ModalScreen):
    """Modal screen that displays probe results in a scrollable view."""

    BINDINGS = [("escape", "dismiss", "Close")]

    def __init__(self, data: dict):
        super().__init__()
        self.data = data

    def compose(self):
        # Render a compact textual summary
        summary_lines = []
        provs = self.data.get("providers", []) if self.data else []
        for p in provs:
            name = p.get("name")
            status = p.get("status")
            details = p.get("details") or ""
            models = p.get("models") or []
            resolved = p.get("resolved")
            summary_lines.append(f"{name} - {status}")
            if details:
                # Truncate long details for UI
                d = details if len(details) < 500 else details[:497] + "..."
                summary_lines.append(f"  {d}")
            if models:
                summary_lines.append(f"  Models: {', '.join(models[:5])}")
            if resolved:
                summary_lines.append(f"  Resolved: {resolved}")
            summary_lines.append("")

        content = "\n".join(summary_lines) or "No provider information available."
        yield VerticalScroll(Static(content, markup=False))
