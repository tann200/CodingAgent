"""session_cost_tracker.py — D-10: Extracted session cost and usage tracking.

Owns the per-session usage buffer (tool call counts) and session cost
estimation logic previously embedded in ``Orchestrator``.

``Orchestrator`` creates one ``SessionCostTracker`` at init and delegates
all cost-related calls to it.  The tracker publishes a ``usage.turn_summary``
event after each LLM turn.

P4-4: Budget ceiling alert — when a non-``None`` ``budget_ceiling_usd`` is
supplied at construction, the tracker publishes a ``usage.budget_exceeded``
event (and logs a warning) the first time the cumulative session cost exceeds
the ceiling.  The ceiling is checked inside ``record_llm_usage`` under the
same lock that guards ``_session_cost_usd``.

Usage::

    tracker = SessionCostTracker(
        working_dir=Path("."),
        event_bus=bus,
        budget_ceiling_usd=1.00,   # alert when session cost > $1.00
    )
    tracker.record_tool_call("read_file")
    tracker.record_llm_usage(prompt_tokens=1000, completion_tokens=200, model="gpt-4o")
    tracker.flush(task_id="abc123")
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class SessionCostTracker:
    """Tracks tool-call counts and LLM usage cost for one agent session.

    Parameters
    ----------
    working_dir:
        Workspace root; usage.json is written to
        ``<working_dir>/.agent-context/usage.json``.
    event_bus:
        EventBus instance used to publish ``usage.turn_summary`` events.
        If ``None``, events are suppressed silently.
    budget_ceiling_usd:
        Optional maximum spend (in USD) for the session.  When the cumulative
        cost first exceeds this value, a ``usage.budget_exceeded`` event is
        published and a warning is logged.  Set to ``None`` (default) to
        disable budget alerting.
    """

    def __init__(
        self,
        working_dir: Path,
        event_bus: Optional[Any] = None,
        budget_ceiling_usd: Optional[float] = None,
    ) -> None:
        self._working_dir = working_dir
        self._event_bus = event_bus
        # tool name → call count since last flush
        self._usage_buffer: Dict[str, int] = {}
        # Lock protects _usage_buffer from concurrent EventBus / asyncio writes
        self._lock: threading.Lock = threading.Lock()
        # Cumulative session cost in USD
        self._session_cost_usd: float = 0.0
        # P4-4: budget ceiling
        self._budget_ceiling_usd: Optional[float] = (
            float(budget_ceiling_usd) if budget_ceiling_usd is not None else None
        )
        # True once the ceiling has been crossed — prevents repeated alerts
        self._budget_exceeded_notified: bool = False
        # CF-2: Subscribe to child delegation cost events so parent session_cost_usd
        # includes delegation spend.  Subscription is best-effort — if the bus is
        # None or the subscribe call fails, we silently skip.
        if event_bus is not None:
            try:
                event_bus.subscribe(
                    "usage.subagent_cost",
                    lambda payload: self.record_subagent_cost(
                        float(payload.get("cost_usd", 0.0)),
                        str(payload.get("role", "subagent")),
                    ),
                )
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def session_cost_usd(self) -> float:
        with self._lock:
            return self._session_cost_usd

    @property
    def tool_call_total(self) -> int:
        with self._lock:
            return sum(self._usage_buffer.values())

    def record_tool_call(self, tool_name: str, count: int = 1) -> None:
        """Increment the in-memory usage counter for *tool_name*."""
        with self._lock:
            self._usage_buffer[tool_name] = self._usage_buffer.get(tool_name, 0) + count

    def record_llm_usage(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        model: str = "",
        task_id: str = "",
        cache_creation_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> None:
        """Estimate and accumulate cost for one LLM turn.

        Cost estimation uses the pricing table from ``provider_context.py``
        when available; falls back to a minimal built-in table.

        ``cache_creation_tokens`` and ``cache_read_tokens`` correspond to
        Anthropic's ``cache_creation_input_tokens`` and
        ``cache_read_input_tokens`` returned in the usage block.  Cache writes
        are billed at the full input token rate; cache reads are billed at
        roughly 10 % of the input token rate.  Both are included in the cost
        estimate when pricing data is available.

        Publishes a ``usage.turn_summary`` event on the EventBus.
        """
        cost = self._estimate_cost(
            prompt_tokens,
            completion_tokens,
            model,
            cache_creation_tokens=cache_creation_tokens,
            cache_read_tokens=cache_read_tokens,
        )
        budget_exceeded = False
        with self._lock:
            self._session_cost_usd = round(self._session_cost_usd + cost, 8)
            current_cost = self._session_cost_usd
            # P4-4: Budget ceiling alert — publish once when the ceiling is first crossed.
            if (
                self._budget_ceiling_usd is not None
                and self._session_cost_usd > self._budget_ceiling_usd
                and not self._budget_exceeded_notified
            ):
                self._budget_exceeded_notified = True
                budget_exceeded = True
        if budget_exceeded:
            logger.warning(
                "SessionCostTracker: session cost $%.6f exceeded budget ceiling $%.6f",
                current_cost,
                self._budget_ceiling_usd,
            )
            self._publish_budget_exceeded()
        self._publish_turn_summary(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost,
            session_cost_usd=current_cost,
            model=model,
            task_id=task_id,
            cache_creation_tokens=cache_creation_tokens,
            cache_read_tokens=cache_read_tokens,
        )

    def flush(self, task_id: str = "") -> None:
        """Flush the in-memory usage buffer to ``usage.json`` on disk.

        Called once per task by ``Orchestrator.run_agent_once()`` after the
        graph finishes (replaces the old ``_flush_usage_buffer`` method).
        """
        with self._lock:
            if not self._usage_buffer:
                return
            buffer_snapshot = dict(self._usage_buffer)
            cost_snapshot = self._session_cost_usd
            # Clear the buffer now so a second flush() before reset() is idempotent
            self._usage_buffer = {}

        try:
            # Resolve canonical agent-context directory at write-time. This may
            # create directories and is appropriate for persistence.
            try:
                from src.tools.tools_config import agent_context_path

                usage_path = agent_context_path(self._working_dir) / "usage.json"
            except Exception:
                usage_path = self._working_dir / ".agent-context" / "usage.json"

            usage_path.parent.mkdir(parents=True, exist_ok=True)
            usage: dict = {}
            if usage_path.exists():
                try:
                    usage = json.loads(usage_path.read_text(encoding="utf-8"))
                except Exception:
                    usage = {}

            tool_stats = usage.get("tools", {})
            for name, count in buffer_snapshot.items():
                prev = tool_stats.get(name, {}).get("calls", 0)
                tool_stats[name] = {"calls": prev + count}
            usage["tools"] = tool_stats
            if task_id:
                usage["last_task_id"] = task_id
            # Persist cumulative session cost for cross-session budget tracking
            usage["session_cost_usd"] = round(cost_snapshot, 8)

            # Use centralized atomic writer when available, fall back to mkstemp
            try:
                from src.core.io_utils import atomic_write_json

                ok = atomic_write_json(usage_path, usage, logger=logger)
                if ok:
                    logger.debug(
                        "SessionCostTracker.flush: atomic write succeeded: %s",
                        usage_path,
                    )
                    return
                else:
                    logger.warning(
                        "SessionCostTracker.flush: atomic_write_json returned False, falling back"
                    )
            except Exception:
                logger.debug(
                    "SessionCostTracker.flush: atomic_write_json unavailable, using fallback",
                    exc_info=True,
                )
                fd = None
                tmp_path = None
                try:
                    usage_path.parent.mkdir(parents=True, exist_ok=True)
                    fd, tmp_path = tempfile.mkstemp(
                        dir=str(usage_path.parent), suffix=".tmp", prefix="usage_"
                    )
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        fd = None
                        json.dump(usage, fh, indent=2)
                        try:
                            fh.flush()
                            os.fsync(fh.fileno())
                        except Exception:
                            pass
                    try:
                        os.replace(tmp_path, usage_path)
                    except Exception:
                        try:
                            os.unlink(tmp_path)
                        except Exception:
                            pass
                        raise
                finally:
                    try:
                        if fd is not None:
                            os.close(fd)
                    except Exception:
                        pass
        except Exception as exc:
            logger.debug("SessionCostTracker.flush: error: %s", exc)

    def record_subagent_cost(self, cost_usd: float, role: str = "subagent") -> None:
        """CF-2: Accumulate child delegation cost into the parent session total.

        Called when a ``usage.subagent_cost`` event arrives from subagent_tools.
        The child cost is added to ``_session_cost_usd`` so the parent's final
        cost report includes delegation spend.  No ``usage.turn_summary`` is
        published; the TUI bridge already publishes a visual summary.
        """
        if cost_usd <= 0:
            return
        with self._lock:
            self._session_cost_usd = round(self._session_cost_usd + cost_usd, 8)
        logger.debug(
            "SessionCostTracker: accumulated subagent cost $%.6f (role=%s), "
            "session_total=$%.6f",
            cost_usd,
            role,
            self._session_cost_usd,
        )

    def reset(self) -> None:
        """Reset buffer and cumulative cost for a new task."""
        with self._lock:
            self._usage_buffer = {}
            self._session_cost_usd = 0.0
            self._budget_exceeded_notified = False

    def get_buffer(self) -> Dict[str, int]:
        """Return a copy of the current usage buffer."""
        with self._lock:
            return dict(self._usage_buffer)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _estimate_cost(
        prompt_tokens: int,
        completion_tokens: int,
        model: str,
        cache_creation_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> float:
        """Estimate cost for one LLM turn on *model*.

        Uses ``provider_context.estimate_cost_usd`` when available; falls back
        to a built-in minimal table.

        Anthropic cache billing:
        - ``cache_creation_tokens``: billed at the full input token rate
          (these tokens were written to the prompt cache).
        - ``cache_read_tokens``: billed at ~10 % of the input token rate
          (these tokens were served from the prompt cache).

        The provider_context path is preferred; cache tokens are factored in
        by the fallback path only, because provider_context may already handle
        them depending on the version installed.
        """
        try:
            from src.core.inference.provider_context import estimate_cost_usd  # type: ignore[import]

            return estimate_cost_usd(prompt_tokens, completion_tokens, model)
        except Exception:
            pass

        # Minimal built-in fallback table (USD per 1M tokens)
        _PRICING: Dict[str, tuple] = {
            "gpt-4o": (5.0, 15.0),
            "gpt-4o-mini": (0.15, 0.60),
            "claude-3-5-sonnet": (3.0, 15.0),
            "claude-3-opus": (15.0, 75.0),
        }
        model_lower = model.lower()
        input_price, output_price = 0.5, 1.5  # safe default
        for key, (ip, op) in _PRICING.items():
            if key in model_lower:
                input_price, output_price = ip, op
                break
        # Cache writes billed at input rate; reads at 10 % of input rate.
        cache_write_cost = cache_creation_tokens * input_price / 1_000_000
        cache_read_cost = cache_read_tokens * (input_price * 0.1) / 1_000_000
        return round(
            (prompt_tokens * input_price + completion_tokens * output_price) / 1_000_000
            + cache_write_cost
            + cache_read_cost,
            8,
        )

    def _publish_turn_summary(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float,
        session_cost_usd: float,
        model: str,
        task_id: str,
        cache_creation_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> None:
        if not self._event_bus:
            return
        try:
            self._event_bus.publish(
                "usage.turn_summary",
                {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                    "cache_creation_tokens": cache_creation_tokens,
                    "cache_read_tokens": cache_read_tokens,
                    "cost_usd": cost_usd,
                    "session_cost_usd": session_cost_usd,
                    "model": model,
                    "task_id": task_id,
                },
            )
        except Exception as exc:
            logger.debug("SessionCostTracker: publish error: %s", exc)

    def _publish_budget_exceeded(self) -> None:
        """Publish a ``usage.budget_exceeded`` event on the EventBus."""
        if not self._event_bus:
            return
        try:
            self._event_bus.publish(
                "usage.budget_exceeded",
                {
                    "session_cost_usd": self._session_cost_usd,
                    "budget_ceiling_usd": self._budget_ceiling_usd,
                },
            )
        except Exception as exc:
            logger.debug("SessionCostTracker: budget_exceeded publish error: %s", exc)
