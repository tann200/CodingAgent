"""Canonical 6-stage pipeline builder backed by native async AgentRunner (TASK-25 Stage B).

Replaces the LangGraph ``StateGraph`` compilation shell with ``AgentRunner``
while preserving the exact public contract (``compile_canonical_graph``,
``compile_agent_graph``, ``build_tier_graph``, ``_get_compiled_graph``,
``get_compiled_graph_for_orchestrator``) and strict ``NodeOutputSchema``
boundary validation.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict

from src.core.orchestration.graph.nodes.approval_node import approval_node
from src.core.orchestration.graph.nodes.compact_node import compact_node
from src.core.orchestration.graph.nodes.finalize_node import finalize_node
from src.core.orchestration.graph.nodes.frontier_loop_node import agent_loop_node
from src.core.orchestration.graph.nodes.initialize_node import initialize_node
from src.core.orchestration.graph.nodes.verification_node import verification_node
from src.core.orchestration.graph.state_schemas import wrap_node
from src.core.orchestration.runner import AgentRunner

logger = logging.getLogger(__name__)

_STATE_SCHEMAS_STRICT = os.environ.get(
    "CODINGAGENT_STATE_SCHEMAS_STRICT", "1"
).lower() in ("1", "true", "yes")


def _validated(name: str, fn: Any) -> Any:
    """Wrap *fn* with state-schema validation at the stage boundary."""
    return wrap_node(name, fn, strict=_STATE_SCHEMAS_STRICT)


_COMPILED_GRAPH: AgentRunner | None = None
_COMPILED_GRAPH_LOCK = threading.Lock()
_GRAPH_CACHE: Dict[str, AgentRunner] = {}
_GRAPH_CACHE_LOCK = threading.Lock()


def _graph_checkpointer() -> Any:
    """Durable checkpoint saver for automatic crash recovery (audit 3.4)."""
    from src.core.orchestration.graph.checkpoint_saver import default_checkpointer

    return default_checkpointer()


def _compile_canonical_graph() -> AgentRunner:
    """Assemble the unified 6-stage native async ``AgentRunner`` pipeline.

    Topology:
        START -> initialize -> agent_loop
        agent_loop -> [agent_loop | approval | verify | compact | finalize]
        approval -> [agent_loop | finalize]
        verify -> [agent_loop | finalize]
        compact -> [agent_loop | finalize]
        finalize -> END
    """
    return AgentRunner(
        nodes={
            "initialize": _validated("initialize", initialize_node),
            "agent_loop": _validated("agent_loop", agent_loop_node),
            "approval": _validated("approval", approval_node),
            "verify": _validated("verify", verification_node),
            "compact": _validated("compact", compact_node),
            "finalize": _validated("finalize", finalize_node),
        },
        checkpointer=_graph_checkpointer(),
    )


def compile_canonical_graph() -> AgentRunner:
    """Public constructor for the unified 6-stage cognitive pipeline."""
    return _compile_canonical_graph()


def compile_agent_graph() -> AgentRunner:
    """Assemble the 6-stage canonical cognitive pipeline."""
    return compile_canonical_graph()


def _get_compiled_graph() -> AgentRunner:
    """Return the cached compiled agent runner, constructing it on first call."""
    global _COMPILED_GRAPH
    if _COMPILED_GRAPH is None:
        with _COMPILED_GRAPH_LOCK:
            if _COMPILED_GRAPH is None:
                _COMPILED_GRAPH = compile_canonical_graph()
    return _COMPILED_GRAPH


def _reset_compiled_graph() -> None:
    """Reset the cached runner (for tests that need a fresh compile)."""
    global _COMPILED_GRAPH, _GRAPH_CACHE
    _COMPILED_GRAPH = None
    with _GRAPH_CACHE_LOCK:
        _GRAPH_CACHE.clear()


def build_tier_graph(tier: str = "canonical") -> AgentRunner:
    """Return a compiled ``AgentRunner`` for the given tier (canonical 6-stage pipeline)."""
    cached = _GRAPH_CACHE.get("canonical")
    if cached is not None:
        return cached

    with _GRAPH_CACHE_LOCK:
        cached = _GRAPH_CACHE.get("canonical")
        if cached is not None:
            return cached
        logger.info(
            "build_tier_graph: building native AgentRunner 6-stage pipeline for tier '%s'",
            tier,
        )
        compiled = _compile_canonical_graph()
        _GRAPH_CACHE["canonical"] = compiled
        return compiled


def get_compiled_graph_for_orchestrator(
    orchestrator: Any = None,
    *,
    model: str | None = None,
    adapter: Any = None,
) -> AgentRunner:
    """Return the compiled pipeline selected for the current orchestrator/model tier."""
    return build_tier_graph("canonical")
