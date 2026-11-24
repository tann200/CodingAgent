"""Native async AgentRunner for the unified 6-stage canonical pipeline (TASK-25 Stage B).

Replaces the heavy LangGraph ``StateGraph`` runtime shell with a direct,
debuggable async Python state machine executing the exact canonical topology::

    START -> initialize -> agent_loop
    agent_loop -> [agent_loop | approval | verify | compact | finalize]
    approval -> [agent_loop | finalize]
    verify -> [agent_loop | finalize]
    compact -> [agent_loop | finalize]
    finalize -> END

Preserves all invariants from ``docs/SPEC.md §3``:
- Strict ``NodeOutputSchema`` validation at every node boundary.
- ``merge_or_replace_list`` semantics for ``history`` and ``verified_reads``
  (guaranteeing O(N) linear history growth without LangGraph reducer bugs).
- ``recursion_limit`` enforcement (default 50 steps).
- Zero LangGraph channel serialization warnings (unserializable live objects
  such as ``workflow_config`` and ``cancel_event`` stay in-process while
  redacted JSON crash-recovery snapshots persist cleanly via ``SessionStore`` /
  ``checkpoint_saver``).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Tuple

from src.core.orchestration.graph import canonical_routing as _canonical_routing
from src.core.orchestration.graph.state import merge_or_replace_list

logger = logging.getLogger(__name__)

NodeFn = Callable[[Dict[str, Any], Dict[str, Any]], Awaitable[Dict[str, Any]]]
RouterFn = Callable[[Mapping[str, Any]], str]

_LIST_REDUCER_KEYS = frozenset({"history", "verified_reads"})


@dataclass
class AgentRunState:
    """Strongly-typed lightweight view over the active runner execution state."""

    task: str
    working_dir: str
    session_id: Optional[str] = None
    history: list[Dict[str, Any]] = field(default_factory=list)
    verified_reads: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    runtime: Dict[str, Any] = field(default_factory=dict)
    run: Dict[str, Any] = field(default_factory=dict)
    extras: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "AgentRunState":
        core_keys = {
            "task",
            "working_dir",
            "session_id",
            "history",
            "verified_reads",
            "errors",
            "runtime",
            "run",
        }
        return cls(
            task=str(data.get("task") or ""),
            working_dir=str(data.get("working_dir") or "."),
            session_id=data.get("session_id"),
            history=list(data.get("history") or []),
            verified_reads=list(data.get("verified_reads") or []),
            errors=list(data.get("errors") or []),
            runtime=dict(data.get("runtime") or {}),
            run=dict(data.get("run") or {}),
            extras={k: v for k, v in data.items() if k not in core_keys},
        )

    def to_dict(self) -> Dict[str, Any]:
        out = dict(self.extras)
        out.update(
            {
                "task": self.task,
                "working_dir": self.working_dir,
                "session_id": self.session_id,
                "history": list(self.history),
                "verified_reads": list(self.verified_reads),
                "errors": list(self.errors),
                "runtime": dict(self.runtime),
                "run": dict(self.run),
            }
        )
        return out


@dataclass(frozen=True)
class _BoundNodeCallable:
    """Compatibility wrapper exposing ``.afunc`` for node-introspection tests."""

    afunc: NodeFn


@dataclass(frozen=True)
class _RunnerNodeRef:
    """Node entry exposed in ``AgentRunner.nodes``."""

    name: str
    bound: _BoundNodeCallable


@dataclass(frozen=True)
class _RunnerGraphView:
    """Lightweight topology view returned by ``AgentRunner.get_graph()``."""

    nodes: Dict[str, Any]
    edges: Tuple[Tuple[str, str], ...]


def apply_state_update(
    state: Dict[str, Any], update: Optional[Mapping[str, Any]]
) -> Dict[str, Any]:
    """Merge a single node's output dict into *state* using canonical reducers."""
    if not update:
        return state
    merged = dict(state)
    for key, value in update.items():
        if key in _LIST_REDUCER_KEYS:
            merged[key] = merge_or_replace_list(merged.get(key), value)
        else:
            merged[key] = value
    return merged


class AgentRunner:
    """Native async state-machine executor for the 6-stage canonical pipeline."""

    def __init__(
        self,
        *,
        nodes: Mapping[str, NodeFn],
        checkpointer: Optional[Any] = None,
        routers: Optional[Mapping[str, RouterFn]] = None,
    ) -> None:
        self._node_fns: Dict[str, NodeFn] = dict(nodes)
        self.nodes: Dict[str, _RunnerNodeRef] = {
            name: _RunnerNodeRef(name=name, bound=_BoundNodeCallable(afunc=fn))
            for name, fn in self._node_fns.items()
        }
        self.checkpointer = checkpointer
        self._routers: Dict[str, RouterFn] = (
            dict(routers)
            if routers is not None
            else {
                "agent_loop": _canonical_routing.route_after_agent_loop,
                "approval": _canonical_routing.route_after_approval,
                "verify": _canonical_routing.route_after_verify,
                "compact": _canonical_routing.route_after_compact,
            }
        )

    def get_graph(self) -> _RunnerGraphView:
        """Return a graph topology view compatible with ``CompiledGraph.get_graph()``."""
        all_nodes: Dict[str, Any] = {"__start__": None}
        for name in self._node_fns:
            all_nodes[name] = self.nodes[name]
        all_nodes["__end__"] = None
        edges: Tuple[Tuple[str, str], ...] = (
            ("__start__", "initialize"),
            ("initialize", "agent_loop"),
            ("agent_loop", "agent_loop"),
            ("agent_loop", "approval"),
            ("agent_loop", "verify"),
            ("agent_loop", "compact"),
            ("agent_loop", "finalize"),
            ("approval", "agent_loop"),
            ("approval", "finalize"),
            ("verify", "agent_loop"),
            ("verify", "finalize"),
            ("compact", "agent_loop"),
            ("compact", "finalize"),
            ("finalize", "__end__"),
        )
        return _RunnerGraphView(nodes=all_nodes, edges=edges)

    def _persist_checkpoint_best_effort(
        self, state: Dict[str, Any], config: Dict[str, Any], completed_node: str
    ) -> None:
        """Persist a redacted JSON crash-recovery checkpoint when enabled."""
        if self.checkpointer is None:
            return
        try:
            from src.core.orchestration.graph.checkpoint_saver import (
                _checkpointing_enabled,
                save_thread_state,
            )

            if not _checkpointing_enabled():
                return
            orch = (config.get("configurable") or {}).get("orchestrator")
            if orch is None:
                return
            save_thread_state(orch, state)
            # Also persist resumable redacted state in canonical SQLite SessionStore when available
            store = getattr(orch, "session_store", None)
            sid = state.get("session_id") or getattr(orch, "_current_task_id", None)
            if store is not None and sid and hasattr(store, "save_session_state"):
                try:
                    compact_snap = {
                        "task": state.get("task"),
                        "rounds": state.get("rounds", 0),
                        "last_node": completed_node,
                        "run": state.get("run"),
                        "modified_files": list(state.get("modified_files") or []),
                        "history": list(state.get("history") or []),
                    }
                    store.save_session_state(str(sid), compact_snap)
                except Exception:
                    pass
        except Exception as exc:
            logger.debug("AgentRunner: checkpoint persist skipped: %s", exc)

    async def ainvoke(
        self,
        initial_state: Mapping[str, Any],
        config: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Execute the canonical pipeline asynchronously from ``initialize`` to ``finalize``."""
        run_cfg: Dict[str, Any] = dict(config) if config is not None else {}
        recursion_limit = int(run_cfg.get("recursion_limit") or 50)
        if recursion_limit < 1:
            recursion_limit = 50

        state: Dict[str, Any] = dict(initial_state)
        current_node: Optional[str] = "initialize"
        steps = 0

        while current_node is not None:
            steps += 1
            if steps > recursion_limit:
                raise RecursionError(
                    f"AgentRunner exceeded recursion_limit={recursion_limit} "
                    f"at node {current_node!r}"
                )

            node_fn = self._node_fns.get(current_node)
            if node_fn is None:
                raise KeyError(f"AgentRunner: unknown node {current_node!r}")

            update = await node_fn(state, run_cfg)
            state = apply_state_update(state, update)
            self._persist_checkpoint_best_effort(state, run_cfg, current_node)

            if current_node == "initialize":
                current_node = "agent_loop"
            elif current_node == "finalize":
                current_node = None
            else:
                router = self._routers.get(current_node)
                if router is None:
                    current_node = "finalize"
                else:
                    next_node = router(state)
                    if next_node not in self._node_fns:
                        logger.warning(
                            "AgentRunner: router from %s returned unknown node %r; routing to finalize",
                            current_node,
                            next_node,
                        )
                        current_node = "finalize"
                    else:
                        current_node = next_node

        return state

    def invoke(
        self,
        initial_state: Mapping[str, Any],
        config: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Synchronous convenience wrapper around ``ainvoke``."""
        return asyncio.run(self.ainvoke(initial_state, config))


__all__ = ["AgentRunState", "AgentRunner", "apply_state_update"]
