"""Durable graph checkpointing (audit PHASE-3 item 3.4).

Two complementary mechanisms, both keyed on the orchestrator's task id
(the thread id):

1. ``JsonlCheckpointSaver`` — a ``langgraph.checkpoint.base.BaseCheckpointSaver``
   that persists every committed superstep to a per-thread JSONL file under
   ``agent_context_path(workdir)/checkpoints``.  This gives node-level crash
   forensics and langgraph time-travel state history for a task.

2. Round-boundary state snapshots — the inference loop writes a JSON-safe
   copy of the graph state after each completed round.  On a process crash
   the snapshot survives; re-invoking the same thread (e.g. ``--continue``
   with the original task id) automatically rehydrates ``initial_state``
   from the snapshot so completed rounds are not redone.  This is the
   mechanism that actually restores work after a crash, because langgraph
   itself cannot re-run pending nodes from a plain on-disk checkpoint.

All persistence is best-effort (graceful degradation): a failing save or
load never breaks graph execution.
"""

from __future__ import annotations

import base64
import json
import pickle
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence

from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    CheckpointTuple,
    get_checkpoint_id,
    get_checkpoint_metadata,
)

from src.core.logger import logger as guilogger

_LOCK = threading.RLock()

_checkpointer_instance: Optional["JsonlCheckpointSaver"] = None
_checkpointer_lock = threading.Lock()

# Channels that carry live per-run (or unpicklable) objects.  They are not
# persisted in snapshots; rehydrated initial state rebuilds them fresh.
LIVE_CHANNELS = frozenset(
    {
        "cancel_event",
        "_file_lock_manager",
        "_agent_session_manager",
        "_context_controller",
        "_write_queue",
        "_pending_injections_source",
    }
)

_MAX_RECORDS_PER_THREAD = 500

# thread_id -> resolved checkpoint dir, so lookups can still recover the
# storage location when no orchestrator object is on hand.
_DIR_REGISTRY: Dict[str, str] = {}


class PickleSerde:
    """Minimal (type, blob) serializer round-tripping arbitrary objects."""

    @staticmethod
    def dumps_typed(obj: Any) -> tuple:
        return "pickle", pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)

    @staticmethod
    def loads_typed(data: tuple) -> Any:
        return pickle.loads(data[1])


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"))


def _checkpointing_enabled() -> bool:
    """Return whether durable checkpointing is active for this process.

    An explicit ``CODINGAGENT_GRAPH_CHECKPOINTING`` env value wins over the
    ``graph_checkpointing`` config key.  The default is OFF under pytest so
    test runs never write into the module's session store or a repository's
    ``.codingAgent`` tree.
    """
    try:
        import os

        env = os.environ.get("CODINGAGENT_GRAPH_CHECKPOINTING", "")
        if env == "0":
            return False
        if env == "1":
            return True
    except Exception:
        pass
    try:
        from src.core.config_loader import get as _cfg_get

        v = _cfg_get("graph_checkpointing")
        if v is not None:
            return bool(v)
    except Exception:
        pass
    return "pytest" not in sys.modules


def _thread_dir(thread: str) -> Optional[Path]:
    """Resolve the per-thread checkpoint directory from the registry."""
    d = _DIR_REGISTRY.get(thread)
    return Path(d) if d else None


def _checkpoints_dir_for(orch: Any) -> Optional[Path]:
    """Resolve the agent-context checkpoint dir for an orchestrator."""
    try:
        if not _checkpointing_enabled():
            return None
        from src.tools.tools_config import agent_context_path

        wd = getattr(orch, "working_dir", None)
        if not wd:
            return None
        d = agent_context_path(Path(str(wd))) / "checkpoints"
        d.mkdir(parents=True, exist_ok=True)
        return d
    except Exception:
        return None


def default_dir_resolver(config: Any) -> Optional[Path]:
    """Resolve the checkpoint directory for *config* (or ``None`` to skip)."""
    try:
        cfg = (config or {}).get("configurable") or {}
    except Exception:
        return None
    if not _checkpointing_enabled():
        return None
    thread = cfg.get("thread_id")
    orch = cfg.get("orchestrator")
    if orch is not None:
        d = _checkpoints_dir_for(orch)
        if d is not None:
            if thread:
                _DIR_REGISTRY[str(thread)] = str(d)
            return d
    if thread:
        return _thread_dir(str(thread))
    return None


def thread_key(orch: Any) -> str:
    """Stable checkpoint thread id for an orchestrator (= its task id)."""
    return str(getattr(orch, "_current_task_id", None) or "default")


def build_thread_config(orch: Any, checkpoint_id: Optional[str] = None) -> Dict[str, Any]:
    """Run config for the graph, keyed on the orchestrator's thread."""
    configurable: Dict[str, Any] = {
        "thread_id": thread_key(orch),
        "orchestrator": orch,
    }
    if checkpoint_id:
        configurable["checkpoint_id"] = checkpoint_id
    return {"configurable": configurable}


def default_checkpointer() -> Optional["JsonlCheckpointSaver"]:
    """Shared saver used by the tier graphs (compiled once per process)."""
    global _checkpointer_instance
    if _checkpointer_instance is None:
        with _checkpointer_lock:
            if _checkpointer_instance is None:
                _checkpointer_instance = JsonlCheckpointSaver()
    return _checkpointer_instance


# ---------------------------------------------------------------------------
# Round-boundary state snapshots (the crash-recovery mechanism)
# ---------------------------------------------------------------------------


def _sanitize_value(v: Any, _depth: int = 0) -> Any:
    """Reduce *v* to JSON-encodable form; raise for unsupported types."""
    if _depth > 10:
        raise ValueError("depth exceeded")
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, Path):
        return str(v)
    if isinstance(v, dict):
        return {
            str(k): _sanitize_value(x, _depth + 1)
            for k, x in list(v.items())[:2000]
        }
    if isinstance(v, (list, tuple, set)):
        return [_sanitize_value(x, _depth + 1) for x in list(v)[:2000]]
    raise ValueError(type(v).__name__)


def _snapshot_path(orch: Any) -> Optional[Path]:
    d = _checkpoints_dir_for(orch)
    if d is None:
        return None
    thread = thread_key(orch)
    _DIR_REGISTRY[thread] = str(d)
    return d / f"checkpoint_{thread}.state.jsonl"


def save_thread_state(orch: Any, state: Dict[str, Any]) -> None:
    """Persist a JSON-safe copy of the graph state for *orch*'s thread."""
    try:
        path = _snapshot_path(orch)
        if path is None:
            return
        payload: Dict[str, Any] = {}
        for k, v in (state or {}).items():
            if k in LIVE_CHANNELS:
                continue
            try:
                sv = _sanitize_value(v)
                json.dumps(sv, ensure_ascii=False)
                payload[k] = sv
            except Exception:
                continue
        line = json.dumps(
            {
                "kind": "state",
                "ts": time.time(),
                "state": payload,
            },
            ensure_ascii=False,
        )
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".jsonl.tmp")
            tmp.write_text(line + "\n", encoding="utf-8")
            Path(tmp).replace(path)
    except Exception as _snap_err:
        guilogger.debug("checkpoint: state snapshot failed (non-fatal): %s", _snap_err)


def load_thread_state(orch: Any) -> Optional[Dict[str, Any]]:
    """Return the most recent state snapshot for *orch*'s thread, if any."""
    try:
        path = _snapshot_path(orch)
        if path is None or not path.exists():
            return None
        with _LOCK:
            data = json.loads(path.read_text(encoding="utf-8"))
        state = data.get("state")
        return state if isinstance(state, dict) else None
    except Exception:
        return None


def has_resumable_checkpoint(
    graph: Any,
    orch: Any,
    cancel_event: Any = None,
) -> bool:
    """True when a durable state snapshot exists for *orch*'s thread.

    ``graph`` is accepted for call-site symmetry (a compiled graph always
    carries the checkpointer); only the snapshot store is consulted.
    """
    if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
        return False
    return load_thread_state(orch) is not None


def rehydrate_initial_state(
    initial_state: Dict[str, Any],
    prior: Dict[str, Any],
) -> Dict[str, Any]:
    """Merge a persisted snapshot over a freshly built initial state."""
    merged = dict(initial_state)
    for k, v in (prior or {}).items():
        if k in LIVE_CHANNELS:
            continue
        if v is not None:
            merged[k] = v
    return merged


def purge_thread_checkpoint(orch: Any) -> None:
    """Delete all persisted state for *orch*'s thread (task finished)."""
    try:
        thread = thread_key(orch)
        d = _DIR_REGISTRY.pop(thread, None)
        if d is None:
            path = _snapshot_path(orch)
            d = str(path.parent) if path is not None else None
        if not d:
            return
        base = Path(d)
        for name in (
            f"checkpoint_{thread}.jsonl",
            f"checkpoint_{thread}.writes.jsonl",
            f"checkpoint_{thread}.state.jsonl",
        ):
            try:
                (base / name).unlink(missing_ok=True)
            except Exception:
                pass
    except Exception:
        pass


class JsonlCheckpointSaver(BaseCheckpointSaver):
    """Crash-durable langgraph saver: each thread lands in its own JSONL file."""

    def __init__(
        self,
        *,
        dir_resolver: Optional[Callable[[Any], Optional[Path]]] = None,
    ) -> None:
        super().__init__(serde=PickleSerde())
        self._dir_resolver: Callable[[Any], Optional[Path]] = (
            dir_resolver or default_dir_resolver
        )

    # -- storage plumbing -------------------------------------------------

    def _thread_files(self, config: Any) -> Optional[tuple]:
        if not isinstance(config, dict):
            return None
        cfg = config.get("configurable") or {}
        thread = cfg.get("thread_id")
        if not thread:
            return None
        d = self._dir_resolver(config)
        if d is None:
            return None
        try:
            d = Path(d)
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            return None
        _DIR_REGISTRY[str(thread)] = str(d)
        return d / f"checkpoint_{thread}.jsonl", d / f"checkpoint_{thread}.writes.jsonl"

    def _read_records(self, path: Optional[Path], kind: Optional[str] = None) -> List[dict]:
        if path is None or not path.exists():
            return []
        out: List[dict] = []
        with _LOCK:
            try:
                raw = path.read_text(encoding="utf-8").splitlines()
            except Exception:
                return []
            for line in raw:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if kind is None or rec.get("kind") == kind:
                    out.append(rec)
        return out

    def _write_record(self, path: Optional[Path], record: dict) -> None:
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            return
        with _LOCK:
            try:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
            except Exception:
                return
            recs = self._read_records(path)
            if len(recs) > _MAX_RECORDS_PER_THREAD:
                keep = recs[-_MAX_RECORDS_PER_THREAD:]
                try:
                    tmp = path.with_suffix(".jsonl.tmp")
                    tmp.write_text(
                        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep),
                        encoding="utf-8",
                    )
                    Path(tmp).replace(path)
                except Exception:
                    pass

    # -- BaseCheckpointSaver contract --------------------------------------

    def put(
        self,
        config: Any,
        checkpoint: Any,
        metadata: Any,
        new_versions: Any,
    ) -> Any:
        try:
            c = dict(checkpoint)
            thread = config["configurable"]["thread_id"]
            ns = config["configurable"].get("checkpoint_ns", "")
            values = c.pop("channel_values", None) or {}

            values_payload: Dict[str, Any] = {}
            for k, v in values.items():
                if k in LIVE_CHANNELS:
                    continue
                try:
                    values_payload[k] = _b64(pickle.dumps(v))
                except Exception:
                    guilogger.debug("checkpoint: dropping unpicklable channel %r", k)

            core = _b64(pickle.dumps(c))
            md = _b64(json.dumps(get_checkpoint_metadata(config, metadata)).encode("utf-8"))
            files = self._thread_files(config)
            if files is not None:
                self._write_record(
                    files[0],
                    {
                        "kind": "checkpoint",
                        "thread": str(thread),
                        "ns": ns,
                        "id": c.get("id"),
                        "parent": config["configurable"].get("checkpoint_id"),
                        "checkpoint": core,
                        "values": values_payload,
                        "metadata": md,
                        "ts": time.time(),
                    },
                )
        except Exception as _put_err:
            guilogger.debug("checkpoint: put failed (non-fatal): %s", _put_err)
        return {
            "configurable": {
                "thread_id": config["configurable"]["thread_id"],
                "checkpoint_ns": config["configurable"].get("checkpoint_ns", ""),
                "checkpoint_id": checkpoint.get("id"),
            }
        }

    def put_writes(
        self,
        config: Any,
        writes: Sequence[tuple],
        task_id: str,
        task_path: str = "",
    ) -> None:
        try:
            ckpt_id = config["configurable"]["checkpoint_id"]
            files = self._thread_files(config)
            if files is None:
                return
            existing = {
                (r.get("task_id"), r.get("channel"))
                for r in self._read_records(files[1], kind="writes")
                if r.get("id") == ckpt_id
            }
            for chan, val in writes:
                key = (task_id, chan)
                if key in existing:
                    continue
                existing.add(key)
                try:
                    value = _b64(pickle.dumps(val))
                except Exception:
                    continue
                self._write_record(
                    files[1],
                    {
                        "kind": "writes",
                        "id": ckpt_id,
                        "task_id": task_id,
                        "task_path": task_path,
                        "channel": chan,
                        "value": value,
                        "ts": time.time(),
                    },
                )
        except Exception as _w_err:
            guilogger.debug("checkpoint: put_writes failed (non-fatal): %s", _w_err)

    def _load_once(self, files: tuple, ckpt_id: Optional[str], ns: str) -> Optional[dict]:
        recs = [
            r
            for r in self._read_records(files[0], kind="checkpoint")
            if (ckpt_id is None or r.get("id") == ckpt_id) and r.get("ns") == ns
        ]
        if not recs:
            return None
        rec = recs[-1]
        try:
            core = pickle.loads(_unb64(rec["checkpoint"]))
            values: Dict[str, Any] = {}
            for k, blob in (rec.get("values") or {}).items():
                try:
                    values[k] = pickle.loads(_unb64(blob))
                except Exception:
                    pass
            core["channel_values"] = values
            try:
                metadata = json.loads(_unb64(rec["metadata"]))
            except Exception:
                metadata = {}
        except Exception as _ld_err:
            guilogger.debug("checkpoint: load corrupted record: %s", _ld_err)
            return None
        return {"record": rec, "core": core, "metadata": metadata}

    def get_tuple(self, config: Any) -> Optional[CheckpointTuple]:
        try:
            files = self._thread_files(config)
            if files is None:
                return None
            cfg = config.get("configurable") or {}
            ckpt_id = get_checkpoint_id(config)
            ns = cfg.get("checkpoint_ns", "")
            loaded = self._load_once(files, ckpt_id, ns)
            if loaded is None:
                return None
            rec = loaded["record"]
            writes: List[tuple] = []
            for r in self._read_records(files[1], kind="writes"):
                if r.get("id") != rec.get("id"):
                    continue
                try:
                    writes.append(
                        (r.get("task_id"), r.get("channel"), pickle.loads(_unb64(r["value"])))
                    )
                except Exception:
                    continue
            return CheckpointTuple(
                config={
                    "configurable": {
                        "thread_id": cfg.get("thread_id"),
                        "checkpoint_ns": ns,
                        "checkpoint_id": rec.get("id"),
                    }
                },
                checkpoint=loaded["core"],
                metadata=loaded["metadata"],
                parent_config=(
                    {
                        "configurable": {
                            "thread_id": cfg.get("thread_id"),
                            "checkpoint_ns": ns,
                            "checkpoint_id": rec.get("parent"),
                        }
                    }
                    if rec.get("parent")
                    else None
                ),
                pending_writes=writes,
            )
        except Exception as _gt_err:
            guilogger.debug("checkpoint: get_tuple failed (non-fatal): %s", _gt_err)
            return None

    def list(
        self,
        config: Any,
        *,
        filter: Optional[dict] = None,  # noqa: A002
        before: Optional[Any] = None,
        limit: Optional[int] = None,
    ) -> Iterator[CheckpointTuple]:
        try:
            files = self._thread_files(config)
            if files is None:
                return
            cfg = config.get("configurable") or {}
            ns = cfg.get("checkpoint_ns", "")
            before_id = get_checkpoint_id(before) if before else None
            yielded = 0
            for rec in reversed(self._read_records(files[0], kind="checkpoint")):
                if rec.get("ns") != ns:
                    continue
                if before_id is not None:
                    if rec.get("id") != before_id:
                        continue
                    before_id = None
                    continue
                loaded = self._load_once(files, rec.get("id"), ns)
                if loaded is None:
                    continue
                yield CheckpointTuple(
                    config={
                        "configurable": {
                            "thread_id": cfg.get("thread_id"),
                            "checkpoint_ns": ns,
                            "checkpoint_id": rec.get("id"),
                        }
                    },
                    checkpoint=loaded["core"],
                    metadata=loaded["metadata"],
                    parent_config=(
                        {
                            "configurable": {
                                "thread_id": cfg.get("thread_id"),
                                "checkpoint_ns": ns,
                                "checkpoint_id": rec.get("parent"),
                            }
                        }
                        if rec.get("parent")
                        else None
                    ),
                    pending_writes=[],
                )
                yielded += 1
                if limit is not None and yielded >= limit:
                    return
        except Exception as _lst_err:
            guilogger.debug("checkpoint: list failed (non-fatal): %s", _lst_err)
            return

    def delete_thread(self, thread_id: str) -> None:
        tid = str(thread_id)
        files = self._thread_files({"configurable": {"thread_id": tid}})
        if files is None:
            return
        for p in files:
            try:
                Path(p).unlink(missing_ok=True)
            except Exception:
                pass
        try:
            _DIR_REGISTRY.pop(tid, None)
        except Exception:
            pass

    async def aget_tuple(self, config: Any) -> Optional[CheckpointTuple]:
        return self.get_tuple(config)

    async def aput(self, config: Any, checkpoint: Any, metadata: Any, new_versions: Any) -> Any:
        return self.put(config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: Any,
        writes: Sequence[tuple],
        task_id: str,
        task_path: str = "",
    ) -> None:
        self.put_writes(config, writes, task_id, task_path)

    async def alist(
        self,
        config: Any,
        *,
        filter: Optional[dict] = None,  # noqa: A002
        before: Optional[Any] = None,
        limit: Optional[int] = None,
    ) -> Any:
        for item in self.list(config, filter=filter, before=before, limit=limit):
            yield item

    async def adelete_thread(self, thread_id: str) -> None:
        self.delete_thread(thread_id)
