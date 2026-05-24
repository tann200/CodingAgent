"""Custom TUI widgets."""

import difflib
from rich.syntax import Syntax
from textual.widgets import Static


class DiffViewer(Static):
    """Widget to display diffs or new file content."""

    async def show_diff(self, old_text: str, new_text: str, language: str = "python") -> None:
        """Show the diff between old and new text, or new text if old is empty."""
        if not old_text:
            syntax = Syntax(new_text, lexer=language, theme="monokai", line_numbers=True)
        else:
            diff_lines = list(difflib.unified_diff(
                old_text.splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile="old",
                tofile="new",
                lineterm=""
            ))
            diff_text = "".join(diff_lines)
            syntax = Syntax(diff_text, lexer="diff", theme="monokai")

        self.update(syntax)