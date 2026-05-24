"""
SideBySideDiff — two-column diff viewer with Accept / Reject buttons.
InlineDiff     — single-column unified diff viewer with +/- markers.

Parses a unified diff string and renders it in a Horizontal layout:
  left panel  = old content  (removed lines highlighted red)
  right panel = new content  (added lines highlighted green)

Context lines appear on both sides.  Removed/added blocks are paired so rows
stay aligned within each hunk.  Emits SideBySideDiff.Accepted or .Rejected
then removes itself.
"""

from __future__ import annotations

from textual.widget import Widget
from textual.widgets import Static, Button
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual import on


def _parse_sidebyside(diff: str) -> tuple[list[str], list[str]]:
    """
    Convert a unified diff into parallel (left, right) line lists.

    Strategy per hunk:
      - Collect consecutive - / + blocks together.
      - Pad the shorter side with empty-ish placeholders so rows align.
      - Context lines (space-prefixed or bare) land on both sides.
    """
    left: list[str] = []
    right: list[str] = []

    lines = diff.splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i]

        # Skip file headers
        if ln.startswith("---") or ln.startswith("+++"):
            i += 1
            continue

        if ln.startswith("@@"):
            left.append(f"[bold #00b4d8]{ln}[/]")
            right.append(f"[bold #00b4d8]{ln}[/]")
            i += 1
            continue

        # Gather a contiguous block of removed/added lines
        if ln[:1] in ("-", "+"):
            removed: list[str] = []
            added: list[str] = []
            while i < len(lines) and lines[i][:1] in ("-", "+"):
                if lines[i].startswith("-"):
                    removed.append(lines[i][1:])
                else:
                    added.append(lines[i][1:])
                i += 1
            # Pad to equal length so panels stay aligned.
            # Use None as a sentinel for padding rows (vs. a genuinely empty
            # added/removed line which is an empty string "").
            pad = max(len(removed), len(added))
            removed_padded: list = removed + [None] * (pad - len(removed))
            added_padded: list = added + [None] * (pad - len(added))
            for r, a in zip(removed_padded, added_padded):
                left.append(f"[on #3d0000]{r} [/]" if r is not None else "[dim]  [/]")
                right.append(f"[on #003d00]{a} [/]" if a is not None else "[dim]  [/]")
            continue

        # Context line
        ctx = ln[1:] if ln.startswith(" ") else ln
        left.append(ctx)
        right.append(ctx)
        i += 1

    return left, right


class SideBySideDiff(Widget):
    """
    Side-by-side diff viewer with per-file Accept / Reject buttons.

    Posts SideBySideDiff.Accepted or SideBySideDiff.Rejected to the app,
    then removes itself from the DOM.
    """

    class Accepted(Message):
        """User accepted the diff — write should proceed."""

        def __init__(self, path: str) -> None:
            self.path = path
            super().__init__()

    class Rejected(Message):
        """User rejected the diff — write should be skipped."""

        def __init__(self, path: str) -> None:
            self.path = path
            super().__init__()

    def __init__(self, path: str, diff: str, is_new_file: bool = False) -> None:
        super().__init__()
        self._path = path
        self._diff = diff
        self._is_new_file = is_new_file

    def compose(self):
        action = "NEW FILE" if self._is_new_file else "DIFF"
        yield Static(
            f"[bold]┌─ {action}:[/] {self._path}",
            classes="sbs_header",
            markup=True,
        )

        left_lines, right_lines = _parse_sidebyside(self._diff)

        # Cap at 50 lines per panel to avoid huge widgets
        cap = 50
        if len(left_lines) > cap:
            left_lines = left_lines[:cap] + [
                f"[dim]… {len(left_lines) - cap} more lines[/]"
            ]
            right_lines = right_lines[:cap] + [""]

        with Horizontal(classes="sbs_panels"):
            with Vertical(classes="sbs_col sbs_col_old"):
                yield Static("OLD", classes="sbs_col_label")
                yield Static(
                    "\n".join(left_lines) if left_lines else "[dim](empty)[/]",
                    classes="sbs_content",
                    markup=True,
                )
            with Vertical(classes="sbs_col"):
                yield Static("NEW", classes="sbs_col_label")
                yield Static(
                    "\n".join(right_lines) if right_lines else "[dim](empty)[/]",
                    classes="sbs_content",
                    markup=True,
                )

        with Horizontal(classes="sbs_actions"):
            yield Static("Apply changes?  ", classes="sbs_prompt")
            yield Button("Accept", variant="success", id="btn_sbs_accept")
            yield Button("Reject", variant="error", id="btn_sbs_reject")

    @on(Button.Pressed, "#btn_sbs_accept")
    def _on_accept(self, event: Button.Pressed) -> None:
        event.stop()
        self.post_message(self.Accepted(self._path))
        self.remove()

    @on(Button.Pressed, "#btn_sbs_reject")
    def _on_reject(self, event: Button.Pressed) -> None:
        event.stop()
        self.post_message(self.Rejected(self._path))
        self.remove()


def _render_inline(diff: str) -> list[str]:
    """Convert a unified diff into a single-column list of markup lines."""
    lines: list[str] = []
    for ln in diff.splitlines():
        if ln.startswith("---") or ln.startswith("+++"):
            continue
        if ln.startswith("@@"):
            lines.append(f"[bold #00b4d8]{ln}[/]")
        elif ln.startswith("-"):
            lines.append(f"[on #3d0000]- {ln[1:]}[/]")
        elif ln.startswith("+"):
            lines.append(f"[on #003d00]+ {ln[1:]}[/]")
        else:
            lines.append(ln[1:] if ln.startswith(" ") else ln)
    return lines


class InlineDiff(Widget):
    """
    Single-column inline diff viewer with Accept / Reject buttons.

    Posts the same SideBySideDiff.Accepted / .Rejected messages so the
    handler in app.py works for both styles without change.
    """

    # Re-use SideBySideDiff message types so handlers are shared.
    Accepted = SideBySideDiff.Accepted
    Rejected = SideBySideDiff.Rejected

    def __init__(self, path: str, diff: str, is_new_file: bool = False) -> None:
        super().__init__()
        self._path = path
        self._diff = diff
        self._is_new_file = is_new_file

    def compose(self):
        action = "NEW FILE" if self._is_new_file else "DIFF"
        yield Static(
            f"[bold]┌─ {action}:[/] {self._path}",
            classes="sbs_header",
            markup=True,
        )

        inline_lines = _render_inline(self._diff)
        cap = 100
        if len(inline_lines) > cap:
            inline_lines = inline_lines[:cap] + [
                f"[dim]… {len(inline_lines) - cap} more lines[/]"
            ]

        yield Static(
            "\n".join(inline_lines) if inline_lines else "[dim](empty)[/]",
            classes="sbs_content",
            markup=True,
        )

        with Horizontal(classes="sbs_actions"):
            yield Static("Apply changes?  ", classes="sbs_prompt")
            yield Button("Accept", variant="success", id="btn_inline_accept")
            yield Button("Reject", variant="error", id="btn_inline_reject")

    @on(Button.Pressed, "#btn_inline_accept")
    def _on_accept(self, event: Button.Pressed) -> None:
        event.stop()
        self.post_message(self.Accepted(self._path))
        self.remove()

    @on(Button.Pressed, "#btn_inline_reject")
    def _on_reject(self, event: Button.Pressed) -> None:
        event.stop()
        self.post_message(self.Rejected(self._path))
        self.remove()
