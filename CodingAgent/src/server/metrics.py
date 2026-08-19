"""In-process Prometheus-format metrics for the HTTP/SSE server.

Extracted from app.py (Phase G — thin facade cleanup).
All counters are thread-safe via a shared lock.
"""

from __future__ import annotations

import threading
from typing import Dict, Tuple

from src.server.event_delivery import record_dropped_event

_METRICS_LOCK = threading.Lock()
_CORRECTIVE_PROMPT_COUNTERS: Dict[Tuple[str, str], int] = {}
_DROPPED_EVENT_COUNTERS: Dict[str, int] = {}
_CLIENT_DROPPED_EVENT_COUNTERS: Dict[Tuple[str, str], int] = {}
_ADMIN_AUTH_COUNTERS: Dict[str, int] = {"attempts": 0, "successes": 0, "failures": 0}


def inc_corrective_prompt_counter(reason: str | None, model_tier: str | None) -> None:
    key = (reason or "unknown", model_tier or "unknown")
    with _METRICS_LOCK:
        _CORRECTIVE_PROMPT_COUNTERS[key] = _CORRECTIVE_PROMPT_COUNTERS.get(key, 0) + 1


def format_metrics_text() -> str:
    lines = [
        "# HELP codingagent_corrective_prompts_total Total corrective prompts issued",
        "# TYPE codingagent_corrective_prompts_total counter",
    ]
    total = 0
    with _METRICS_LOCK:
        items = list(_CORRECTIVE_PROMPT_COUNTERS.items())
    for (reason, tier), val in items:
        total += val
        reason_s = str(reason).replace('"', '\\"')
        tier_s = str(tier).replace('"', '\\"')
        lines.append(
            f'codingagent_corrective_prompts_total{{reason="{reason_s}",model_tier="{tier_s}"}} {val}'
        )
    lines.append(f"codingagent_corrective_prompts_total {total}")

    lines.append("")
    lines.append(
        "# HELP codingagent_sse_events_dropped_total Total SSE events dropped per event type"
    )
    lines.append("# TYPE codingagent_sse_events_dropped_total counter")
    with _METRICS_LOCK:
        dropped_items = list(_DROPPED_EVENT_COUNTERS.items())
    dropped_total = 0
    for ename, val in dropped_items:
        dropped_total += val
        ename_s = str(ename).replace('"', '\\"')
        lines.append(f'codingagent_sse_events_dropped_total{{event="{ename_s}"}} {val}')
    lines.append(f"codingagent_sse_events_dropped_total {dropped_total}")

    lines.append("")
    lines.append(
        "# HELP codingagent_sse_events_dropped_per_client_total Total SSE events dropped per client session and event"
    )
    lines.append("# TYPE codingagent_sse_events_dropped_per_client_total counter")
    with _METRICS_LOCK:
        client_items = list(_CLIENT_DROPPED_EVENT_COUNTERS.items())
    for (sid, ename), val in client_items:
        sid_s = str(sid).replace('"', '\\"')
        ename_s = str(ename).replace('"', '\\"')
        lines.append(
            f'codingagent_sse_events_dropped_per_client_total{{session_id="{sid_s}",event="{ename_s}"}} {val}'
        )

    lines.append("")
    lines.append(
        "# HELP codingagent_admin_auth_total Admin auth attempts/successes/failures"
    )
    lines.append("# TYPE codingagent_admin_auth_total counter")
    with _METRICS_LOCK:
        a_items = list(_ADMIN_AUTH_COUNTERS.items())
    for k, v in a_items:
        k_s = str(k).replace('"', '\\"')
        lines.append(f'codingagent_admin_auth_total{{type="{k_s}"}} {v}')
    total_auth = sum(v for _, v in a_items)
    lines.append(f"codingagent_admin_auth_total {total_auth}")
    return "\n".join(lines) + "\n"


def inc_event_dropped_counter(event_name: str) -> None:
    with _METRICS_LOCK:
        _DROPPED_EVENT_COUNTERS[event_name] = (
            _DROPPED_EVENT_COUNTERS.get(event_name, 0) + 1
        )


def inc_client_event_dropped_counter(event_name: str, session_id: str) -> None:
    key = (str(session_id or "unknown"), str(event_name or "unknown"))
    with _METRICS_LOCK:
        _CLIENT_DROPPED_EVENT_COUNTERS[key] = (
            _CLIENT_DROPPED_EVENT_COUNTERS.get(key, 0) + 1
        )


def record_dropped_session_event(event_name: str, session_id: str) -> None:
    record_dropped_event(
        event_name,
        session_id,
        inc_event_dropped_counter=inc_event_dropped_counter,
        inc_client_event_dropped_counter=inc_client_event_dropped_counter,
    )


def inc_admin_auth_counter(key: str) -> None:
    if key not in ("attempts", "successes", "failures"):
        return
    with _METRICS_LOCK:
        _ADMIN_AUTH_COUNTERS[key] = _ADMIN_AUTH_COUNTERS.get(key, 0) + 1
