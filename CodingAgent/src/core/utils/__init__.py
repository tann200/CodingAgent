"""Shared utility helpers for CodingAgent.

Public API:
    strip_markup(text)  — remove Rich / Textual bracket-style markup tags
    HistoryWrapper      — minimal Textual history adapter (checkpoint + list-like)
"""

from __future__ import annotations

import re
from typing import Iterator, List, Optional


# ---------------------------------------------------------------------------
# Markup stripping
# ---------------------------------------------------------------------------


def strip_markup(text: str) -> str:
    """Remove Rich / Textual bracket-style markup tags from *text*.

    Strips tags of the form ``[bold]``, ``[/bold]``, ``[dim]``, ``[color]``,
    ``[on #aabbcc]``, etc.  Does not raise on non-string input — coerces to
    ``str`` first.

    Examples::

        >>> strip_markup("[bold]Hello[/bold] [dim]there[/dim]")
        'Hello there'
    """
    if not isinstance(text, str):
        text = str(text)
    return re.sub(r"\[/?[a-zA-Z0-9_\-#=;\s]*\]", "", text)


# ---------------------------------------------------------------------------
# HistoryWrapper — minimal Textual history adapter
# ---------------------------------------------------------------------------


class HistoryWrapper:
    """Small adapter that provides the minimal history API Textual expects.

    Textual's ``TextArea`` assumes a history-like object with a ``checkpoint()``
    method and list-like behaviour.  Previously a plain list was assigned which
    caused ``AttributeError`` when Textual called ``history.checkpoint()``.

    This wrapper delegates to an internal Python list and implements the
    commonly-used methods/operators (``append``, ``clear``, ``__len__``,
    ``__getitem__``, iteration) plus a no-op ``checkpoint()``.

    Absorbed from ``src/ui/textual_app_impl._HistoryWrapper`` (LEGACY-04).
    """

    def __init__(self, items: Optional[List[str]] = None) -> None:
        self._list: List[str] = list(items) if items else []

    # Textual calls this; keep as a harmless no-op.
    def checkpoint(self) -> None:
        return None

    def append(self, item: str) -> None:
        self._list.append(item)

    def extend(self, items: List[str]) -> None:
        self._list.extend(items)

    def clear(self) -> None:
        self._list.clear()

    def to_list(self) -> List[str]:
        return list(self._list)

    # Sequence protocol
    def __len__(self) -> int:
        return len(self._list)

    def __getitem__(self, index: int) -> str:  # type: ignore[override]
        return self._list[index]

    def __iter__(self) -> Iterator[str]:
        return iter(self._list)

    def __repr__(self) -> str:
        return f"HistoryWrapper({self._list!r})"


__all__ = ["strip_markup", "HistoryWrapper"]
