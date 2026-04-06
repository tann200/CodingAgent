"""
_truncate.py — Centralised tool-output truncation service (TOOLS-01).

Implements the pattern from OpenCode's ``tool/truncate.ts``:

- Hard limits: 2 000 lines OR 100 KB (whichever is reached first).
- Adaptive hint: if the agent context includes ``delegate_task`` in its
  allowed tools, the hint suggests delegation to the explore agent.
  Otherwise it suggests using Grep / Read with an offset.
- Returns the original text unchanged when it is within limits.

Usage
-----
    from src.tools._truncate import Truncate

    result = Truncate.output(text)
    result = Truncate.output(text, agent_context=active_agent_definition)

The ``agent_context`` argument accepts an ``AgentDefinition`` instance (or any
object with an ``allowed_tools`` attribute that is either None or a set of
strings).  It is typed as ``object`` here to avoid a hard circular-import
dependency on ``agent_types.py``.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

#: Maximum number of lines before truncation.
MAX_LINES: int = 2_000

#: Maximum number of bytes before truncation (100 KB).
MAX_BYTES: int = 100 * 1024

# ---------------------------------------------------------------------------
# Hint messages
# ---------------------------------------------------------------------------

_HINT_DELEGATE = (
    "\n\n[Output truncated — too large to display in full. "
    "Consider delegating to the explore agent using delegate_task for "
    "in-depth analysis of large outputs.]"
)

_HINT_SEARCH = (
    "\n\n[Output truncated — too large to display in full. "
    "Use Grep with a specific pattern or Read with an offset/limit to "
    "inspect the remaining content.]"
)


# ---------------------------------------------------------------------------
# Truncate
# ---------------------------------------------------------------------------


class Truncate:
    """Static utility class for truncating tool output."""

    @staticmethod
    def output(text: str, agent_context: Optional[object] = None) -> str:
        """Truncate *text* if it exceeds the line or byte limit.

        Parameters
        ----------
        text:
            The raw tool output string.
        agent_context:
            Optional ``AgentDefinition``-compatible object.  When the agent's
            ``allowed_tools`` includes ``"delegate_task"`` (or is None, meaning
            the agent has full access), the truncation hint recommends delegation.
            Otherwise it recommends Grep / Read.

        Returns
        -------
        str
            The original *text* if within limits, or a truncated version with
            an appended hint.
        """
        if not text:
            return text

        # --- Fast path: check byte limit first (cheaper than line split) ---
        encoded = text.encode("utf-8", errors="replace")
        byte_overflow = len(encoded) > MAX_BYTES

        lines = text.splitlines(keepends=True)
        line_overflow = len(lines) > MAX_LINES

        if not byte_overflow and not line_overflow:
            return text

        hint = Truncate._choose_hint(agent_context)

        if line_overflow:
            truncated = "".join(lines[:MAX_LINES])
            logger.debug(
                "_truncate: output truncated to %d lines (was %d lines)",
                MAX_LINES,
                len(lines),
            )
            return truncated + hint

        # Byte overflow (but within line limit): cut at MAX_BYTES boundary.
        # Re-encode after slicing to avoid splitting a multi-byte character.
        safe_bytes = encoded[:MAX_BYTES]
        truncated = safe_bytes.decode("utf-8", errors="replace")
        logger.debug(
            "_truncate: output truncated to %d bytes (was %d bytes)",
            MAX_BYTES,
            len(encoded),
        )
        return truncated + hint

    @staticmethod
    def _choose_hint(agent_context: Optional[object]) -> str:
        """Return the appropriate truncation hint for *agent_context*."""
        if agent_context is None:
            # No agent info → assume full access → delegation hint.
            return _HINT_DELEGATE

        allowed = getattr(agent_context, "allowed_tools", None)
        if allowed is None:
            # allowed_tools=None means unrestricted → delegation hint.
            return _HINT_DELEGATE

        # allowed_tools is a set — check for delegate_task.
        try:
            if "delegate_task" in allowed:
                return _HINT_DELEGATE
        except TypeError:
            pass

        return _HINT_SEARCH

    @staticmethod
    def truncate_dict_values(
        data: object, agent_context: Optional[object] = None
    ) -> object:
        """Recursively truncate all string values in a dict/list structure.

        Useful for truncating structured tool results (e.g. JSON output from
        bash or grep) where individual fields may be excessively long.
        """
        if isinstance(data, str):
            return Truncate.output(data, agent_context)
        if isinstance(data, dict):
            return {k: Truncate.truncate_dict_values(v, agent_context) for k, v in data.items()}
        if isinstance(data, list):
            return [Truncate.truncate_dict_values(item, agent_context) for item in data]
        return data
