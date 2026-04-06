"""
FilePickerOverlay — floating file picker shown when the user types '@' in the prompt.

Shows workspace files matching the text typed after '@'. The parent app drives
updates via update_picker(matches, selected_index).

No src.core imports — purely UI.
"""

from __future__ import annotations

from textual.widgets import Static


class FilePickerOverlay(Static):
    """Floating file picker shown above the prompt input when the user types '@'.

    Rendered as a Rich markup string with the currently-selected item highlighted.
    The parent app drives updates via update_picker(matches, selected_index).
    Hidden (display: none) until the app calls update_picker() with results.
    """

    DEFAULT_CSS = """
    FilePickerOverlay {
        background: #2d2d2d;
        border: tall #444488;
        padding: 0 1;
        height: auto;
        max-height: 10;
        display: none;
    }
    """

    def update_picker(self, matches: list[str], selected: int) -> None:
        """Re-render the picker with the current match list and selection.

        Hides the widget automatically when *matches* is empty.
        """
        if not matches:
            self.display = False
            return
        lines: list[str] = []
        for i, path in enumerate(matches[:8]):
            if i == selected:
                lines.append(f"[bold white on #003f6f] {path} [/bold white on #003f6f]")
            else:
                lines.append(f"[cyan] {path}[/cyan]")
        self.update("\n".join(lines))
        self.display = True
