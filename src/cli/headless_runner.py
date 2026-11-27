"""Headless execution and session-resume helpers for ``codingagent``."""

from __future__ import annotations

import os
import sys
from typing import Any, Callable, Optional


def load_last_user_task(workdir: Optional[str], *, dbg_fn: Optional[Callable[[str], None]] = None) -> str:
    """Return the most recent user task for --continue.

    Checks the persisted ``last_plan.json`` ``task`` field first (written by
    planning_node during real agent runs), then falls back to scanning the
    newest saved sessions for a user message.
    """
    import json as _json

    _wd = workdir or os.getcwd()
    # 1. Persisted last-plan task record (planning_node writes this per run).
    try:
        from src.core.orchestration.graph.nodes.planning_helpers import (
            get_last_plan_path,
        )

        _plan_path = get_last_plan_path(workdir=_wd)
        if _plan_path.exists():
            _data = _json.loads(_plan_path.read_text(encoding="utf-8"))
            _task = str((_data or {}).get("task") or "").strip()
            if _task:
                return _task
    except Exception as _p_err:
        (dbg_fn or (lambda _m: None))(f"[src.main] --continue: last_plan read failed: {_p_err}")

    # 2. Fallback: newest sessions with a real user message.
    try:
        from src.core.orchestration.session_store import list_sessions, load_session

        for s in list_sessions(limit=10):
            loaded = load_session(s.session_id)
            if loaded is None:
                continue
            for msg in reversed(loaded.messages):
                if not isinstance(msg, dict):
                    continue
                if str(msg.get("role", "")).lower() in ("user", "human"):
                    return flatten_content(msg.get("content", ""))
    except Exception as _le:
        (dbg_fn or (lambda _m: None))(f"[src.main] --continue: failed to load last task: {_le}")
    return ""


def flatten_content(content: Any) -> str:
    """Flatten a message content field (str or list of parts) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for p in content:
            if isinstance(p, dict):
                parts.append(p.get("text") or p.get("content") or "")
            elif isinstance(p, str):
                parts.append(p)
        return "\n".join(parts)
    return str(content)


def run_headless(
    task: str,
    output_format: str,
    workdir: Optional[str],
    dry_run: bool = False,
    resume_session: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    trusted: Optional[bool] = None,
    plugin_dir: Optional[str] = None,
    private: bool = False,
    sys_mod: Any = None,
    dbg_fn: Optional[Callable[[str], None]] = None,
) -> int:
    """Run a single task without the TUI and print the result.

    Parameters
    ----------
    task:
        The user task string to pass to the agent.
    output_format:
        One of ``pretty``, ``json``, or ``raw``.
    workdir:
        Optional working directory override.
    dry_run:
        When ``True`` the orchestrator is started in dry-run mode: write and
        destructive tool calls are intercepted and logged as previews instead
        of being executed.
    resume_session:
        Optional session_id or path to session file to resume from.
    provider:
        Optional provider override applied via a live ``model.routing`` switch
        (mirrors the TUI ``/provider`` command).
    model:
        Optional model override applied via a live ``model.routing`` switch
        (mirrors the TUI ``/model`` command).
    """
    import json as _json

    _sys = sys_mod if sys_mod is not None else sys
    _messages = None
    _stored_session_id: Optional[str] = None
    if resume_session:
        try:
            from pathlib import Path as _Path
            from src.core.orchestration.session_store import load_session, StoredSession

            # Check if it's a file path
            _session_path = _Path(resume_session)
            if _session_path.exists() and _session_path.suffix == ".json":
                _data = _json.loads(_session_path.read_text(encoding="utf-8"))
                _stored_session_id = str(_data.get("session_id") or "")
                _stored: Optional[StoredSession] = StoredSession(
                    version=int(_data.get("version", 1)),
                    session_id=str(_data.get("session_id") or ""),
                    task_name=str(_data.get("task_name", "")),
                    working_dir=str(_data.get("working_dir", "")),
                    messages=list(_data.get("messages", [])),
                    message_count=int(_data.get("message_count", 0)),
                    turn_count=int(_data.get("turn_count", 0)),
                    input_tokens=int(_data.get("input_tokens", 0)),
                    output_tokens=int(_data.get("output_tokens", 0)),
                    created_at=str(_data.get("created_at", "")),
                )
                _messages = _stored.messages  # type: ignore[union-attr]
                print(
                    f"[SESSION] Resumed session {_stored.session_id}", file=_sys.stderr  # type: ignore[union-attr]
                )
            else:
                # Try loading by session_id
                _stored = load_session(resume_session)
                if _stored:
                    _messages = _stored.messages
                    _stored_session_id = _stored.session_id
                    print(
                        f"[SESSION] Resumed session {_stored.session_id}",
                        file=_sys.stderr,
                    )
                else:
                    print(
                        f"[SESSION] Session '{resume_session}' not found",
                        file=_sys.stderr,
                    )
        except Exception as _sess_err:
            print(f"[SESSION] Failed to load session: {_sess_err}", file=_sys.stderr)

    try:
        from src.core.orchestration.orchestrator import Orchestrator
        from src.cli.headless_stream import (
            StreamJsonEmitter,
            classify_headless_status,
            exit_code_for_status,
        )

        _orch_kwargs: dict[str, Any] = {
            "working_dir": workdir,
            "dry_run": dry_run,
            "trusted": trusted,
            "plugin_dir": plugin_dir,
        }
        if private:
            _orch_kwargs["private"] = True
        orch = Orchestrator(**_orch_kwargs)

        # CP-3.4: give this invocation a stable checkpoint thread id.  Resume
        # paths reuse the *original* session id so crash-recovery resumes the
        # same thread; fresh tasks mint one so unrelated runs never collide.
        try:
            _existing_tid = (
                orch.get_current_task_id() if hasattr(orch, "get_current_task_id") else None
            )
            if not _existing_tid:
                if _stored_session_id:
                    orch._current_task_id = _stored_session_id
                else:
                    import uuid as _uuid

                    orch._current_task_id = str(_uuid.uuid4())[:8]
        except Exception:
            pass

        # Phase 3.7: apply --provider/--model overrides via a live model.routing
        # switch (same mechanism the TUI uses for /provider + /model). Publish
        # after Orchestrator construction so ProviderManager is wired to the bus.
        if provider or model:
            try:
                from src.core.messaging.event_types import ModelRouting

                orch.event_bus.publish_typed(
                    ModelRouting(
                        provider=provider or "",
                        selected=model or "",
                        available_models=[],
                    )
                )
                (dbg_fn or (lambda _m: None))(
                    f"[src.main] model.routing applied: "
                    f"provider={provider or ''} model={model or ''}"
                )
            except Exception as _mr_err:
                (dbg_fn or (lambda _m: None))(f"[src.main] model.routing apply failed: {_mr_err}")

        # TASK-36: Normalize resumed conversation history when switching provider/model
        if _messages and (provider or model):
            try:
                from src.core.inference.history_normalizer import (
                    normalize_history_for_model_switch,
                )

                _messages = normalize_history_for_model_switch(_messages)
            except Exception as _hn_err:
                (dbg_fn or (lambda _m: None))(f"[src.main] history normalization failed: {_hn_err}")

        # P3-1: Headless streaming — subscribe to response.stream_chunk events
        # before run_agent_once() so tokens are printed incrementally.
        _streaming_active = output_format == "stream"
        _stream_json_active = output_format == "stream-json"
        _stream_had_output = False
        _ndjson_emitter: Optional[StreamJsonEmitter] = None

        if _stream_json_active:
            _ndjson_emitter = StreamJsonEmitter(_sys.stdout)
            _ndjson_emitter.attach(orch.event_bus)

        if _streaming_active:
            def _on_stream_chunk(payload: dict) -> None:
                nonlocal _stream_had_output
                chunk = payload.get("chunk", "") if isinstance(payload, dict) else str(payload)
                is_reasoning = payload.get("is_reasoning", False) if isinstance(payload, dict) else False
                if chunk and not is_reasoning:
                    _sys.stdout.write(chunk)
                    _sys.stdout.flush()
                    _stream_had_output = True

            orch.event_bus.subscribe("response.stream_chunk", _on_stream_chunk)

        try:
            result = orch.run_agent_once(
                system_prompt_name="operational",
                messages=_messages if _messages else [{"role": "user", "content": task}],
                tools={},
            )
        finally:
            if _ndjson_emitter is not None:
                _ndjson_emitter.detach()

        _headless_status = classify_headless_status(result)
        _usage_summary = {
            "input_tokens": int((result.get("usage") or {}).get("input_tokens", 0) if isinstance(result.get("usage"), dict) else (getattr(orch, "_usage_buffer", {}) or {}).get("input_tokens", 0)),
            "output_tokens": int((result.get("usage") or {}).get("output_tokens", 0) if isinstance(result.get("usage"), dict) else (getattr(orch, "_usage_buffer", {}) or {}).get("output_tokens", 0)),
        }

        if dry_run and output_format not in ("json", "stream-json"):
            dry_calls = result.get("dry_run_intercepted", [])
            if dry_calls:
                print("[DRY RUN] The following write/destructive tool calls were")
                print("[DRY RUN] intercepted and NOT executed:\n")
                for entry in dry_calls:
                    print(
                        f"  {entry.get('would_call', entry.get('tool', '?'))}"
                        f"({_json.dumps(entry.get('args', {}), ensure_ascii=False)})"
                    )
                print()

        if _stream_json_active and _ndjson_emitter is not None:
            _ndjson_emitter.emit_final_result(
                status=_headless_status,
                result_text=str(result.get("assistant_message", "") or ""),
                usage=_usage_summary,
                error=str(result.get("error") or "") or None,
            )
            return exit_code_for_status(_headless_status)
        if output_format == "json":
            out_payload = dict(result)
            out_payload.setdefault("type", "result")
            out_payload.setdefault("status", _headless_status)
            out_payload.setdefault("result", result.get("assistant_message", ""))
            out_payload.setdefault("usage", _usage_summary)
            print(_json.dumps(out_payload, ensure_ascii=False, default=str))
            return exit_code_for_status(_headless_status)
        elif output_format == "raw":
            print(result.get("assistant_message", ""))
        elif output_format == "stream":
            # Chunks already printed incrementally; ensure trailing newline if
            # any output was written and the last chunk didn't end with one.
            if _stream_had_output:
                _sys.stdout.write("\n")
                _sys.stdout.flush()
            else:
                # Fallback: no chunks arrived (non-streaming provider), print full message
                print(result.get("assistant_message", ""))
        else:
            # pretty / text
            msg = result.get("assistant_message", "")
            summary = result.get("work_summary", "")
            print(msg)
            if summary:
                print("\n---")
                print(summary)
        return exit_code_for_status(_headless_status)
    except Exception as exc:
        (dbg_fn or (lambda _m: None))(f"[src.main] headless run failed: {exc}")
        if output_format == "stream-json":
            from src.cli.headless_stream import StreamJsonEmitter

            StreamJsonEmitter(_sys.stdout).emit_final_result(
                status="failed",
                result_text="",
                usage={},
                error=str(exc),
            )
        elif output_format == "json":
            import json as _json

            print(_json.dumps({"type": "result", "status": "failed", "result": "", "usage": {}, "error": str(exc)}))
        else:
            print(f"Error: {exc}", file=_sys.stderr)
        return 1

