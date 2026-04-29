"""
Memory tools for the coding agent.

Provides:
  - memory_search: searches VectorStore, TASK_STATE.md, compaction checkpoints,
    and execution traces for relevant context.
    - memory_save: persists a note to the memory file returned by
      `src.core.paths.get_memory_path()` so it is available in future
      sessions (GAP-NEW-4 / GAP-FRONTIER-4).
    - Tier-aware memory limits: different limits for Lite/Standard/Full agents.
"""

import logging
import os
import tempfile
import time
from typing import Any, Dict, Optional

from src.core.paths import get_memory_path
from src.core.memory.security import scan_memory_content
from src.core.memory.file_lock import locked_file
from src.tools._tool import tool, PermissionKind

logger = logging.getLogger(__name__)


# Memory tiers and limits - tier-aware memory limiting (P1)
_MEMORY_TIER_LIMITS = {
    "lite": {
        "max_entries": 50,
        "max_bytes": 10_000,  # ~10 KB
        "max_chars": 2200,  # P0: character bounds (matching hermes-agent)
    },
    "standard": {
        "max_entries": 200,
        "max_bytes": 50_000,  # ~50 KB
        "max_chars": 2200,
    },
    "full": {
        "max_entries": 500,
        "max_bytes": 200_000,  # ~200 KB
        "max_chars": 2200,
    },
}

# USER memory separate bounds (per hermes-agent dual-store pattern)
_USER_TIER_LIMITS = {
    "lite": {"max_chars": 1375},
    "standard": {"max_chars": 1375},
    "full": {"max_chars": 1375},
}

# Default to standard tier if unable to determine
_DEFAULT_TIER = "standard"

# Memory file and limits
_MEMORY_FILE = get_memory_path()


def _get_agent_tier_from_state(agent_state: Optional[Dict[str, Any]] = None) -> str:
    """
    Determine agent tier from agent state or context.

    Args:
        agent_state: Optional agent state dictionary

    Returns:
        Agent tier: 'lite', 'standard', or 'full'
    """
    if not agent_state:
        return _DEFAULT_TIER

    # Try to get tier from various possible locations in state
    tier_sources = [
        agent_state.get("agent_tier"),
        agent_state.get("tier"),
        agent_state.get("model_tier"),
        agent_state.get("current_tier"),
    ]

    for tier in tier_sources:
        if tier and isinstance(tier, str):
            tier_lower = tier.lower()
            if tier_lower in _MEMORY_TIER_LIMITS:
                return tier_lower
            # Handle full model names that indicate tier
            if (
                any(size in tier_lower for size in ["7b", "8b", "14b"])
                and "lite" not in tier_lower
            ):
                # Small models tend to be lite tier
                if "lite" in tier_lower or any(
                    indicator in tier_lower for indicator in ["1b", "3b"]
                ):
                    return "lite"
                elif any(size in tier_lower for size in ["70b", "65b"]):
                    return "full"
                else:
                    return "standard"  # Default for medium models

    # Fallback: try to infer from model name if present
    model_name = agent_state.get("model") or agent_state.get("current_model") or ""
    if model_name:
        model_lower = model_name.lower()
        if any(indicator in model_lower for indicator in ["1b", "3b"]):
            return "lite"
        elif any(indicator in model_lower for indicator in ["70b", "65b"]):
            return "full"
        elif any(indicator in model_lower for indicator in ["7b", "8b", "14b"]):
            return "standard"

    return _DEFAULT_TIER


def _get_tier_limits(tier: Optional[str] = None) -> Dict[str, int]:
    """
    Get memory limits for a specific tier.

    Args:
        tier: Agent tier ('lite', 'standard', 'full') or None for default

    Returns:
        Dictionary with 'max_entries', 'max_bytes', and 'max_chars' limits
    """
    if tier and tier in _MEMORY_TIER_LIMITS:
        return _MEMORY_TIER_LIMITS[tier]
    return _MEMORY_TIER_LIMITS[_DEFAULT_TIER]


def _get_user_tier_limits(tier: Optional[str] = None) -> int:
    """Get user memory character limits for a specific tier."""
    if tier and tier in _USER_TIER_LIMITS:
        return _USER_TIER_LIMITS[tier]["max_chars"]
    return _USER_TIER_LIMITS[_DEFAULT_TIER]["max_chars"]


# Memory file and limits
_MEMORY_FILE = get_memory_path()


@tool(tags=["coding", "planning", "review"], permission_kind=PermissionKind.MEMORY)
def memory_save(
    content: str,
    category: Optional[str] = None,
    agent_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Persist a note to the memory file.

    Args:
        content: Text content to save (will be trimmed)
        category: Optional category for the memory entry
        agent_state: Optional agent state for tier-aware limits

    Returns:
        Status dictionary with operation results
    """
    content = content.strip()
    if not content:
        return {"status": "error", "error": "content cannot be empty"}

    tier = _get_agent_tier_from_state(agent_state)
    limits = _get_tier_limits(tier)
    max_chars = limits.get("max_chars", 2200)

    if len(content) > max_chars:
        return {
            "status": "error",
            "error": f"content too long ({len(content)} chars, max {max_chars}). Be concise.",
        }

    # Security check: scan for injection/exfiltration patterns
    scan_error = scan_memory_content(content)
    if scan_error:
        return {"status": "error", "error": scan_error}

    # Get tier-aware limits
    tier = _get_agent_tier_from_state(agent_state)
    limits = _get_tier_limits(tier)
    max_entries = limits["max_entries"]
    max_bytes = limits["max_bytes"]

    cat = (category or "note").strip().lower()
    ts = time.strftime("%Y-%m-%d %H:%M")
    entry = f"- [{cat}] {ts}: {content}\n"

    try:
        _MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)

        # TASK-REL-2: serialise the entire read-modify-write under an exclusive
        # OS-level file lock so concurrent memory_save calls (e.g. from two
        # async tasks or two processes) do not interleave their writes.
        with locked_file(_MEMORY_FILE, mode="a+") as fh:
            # Read existing content (seek to start after opening with "a+")
            fh.seek(0)
            try:
                existing = fh.read()
            except Exception:
                existing = ""

            # Trim oldest entries if file would exceed tier-specific size limit
            new_content = existing + entry
            if len(new_content.encode("utf-8")) > max_bytes:
                lines = new_content.splitlines(keepends=True)
                while lines and len("".join(lines).encode("utf-8")) > max_bytes:
                    lines.pop(0)
                new_content = "".join(lines)

            # Also enforce entry count limit
            current_entries = new_content.count("\n- [")
            if current_entries > max_entries:
                # Remove oldest entries to meet entry limit
                lines = new_content.splitlines(keepends=True)
                # Count entries from the end (newest) to keep the most recent ones
                entry_count = 0
                keep_lines = []
                for line in reversed(lines):
                    if line.strip().startswith("- ["):
                        entry_count += 1
                        if entry_count > max_entries:
                            continue  # Skip this old entry
                    keep_lines.append(line)
                # Reverse back to original order
                new_content = "".join(reversed(keep_lines))

            # Write atomically: write to a sibling temp file, then os.replace.
            # We still hold the OS lock during the replace so no other process
            # can read a partial state.
            fd, tmp_path = tempfile.mkstemp(dir=str(_MEMORY_FILE.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as tmp_fh:
                tmp_fh.write(new_content)
            os.replace(tmp_path, str(_MEMORY_FILE))

        return {
            "status": "ok",
            "saved": entry.strip(),
            "memory_file": str(_MEMORY_FILE),
            "total_entries": new_content.count("\n- ["),
            "tier": tier,
            "limits_applied": limits,
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
