import logging
import os
import threading
from typing import Any, Dict

from langgraph.graph import StateGraph, END
from src.core.orchestration.graph.state import AgentState
from src.core.orchestration.graph.state_schemas import wrap_node
from src.core.orchestration.graph.nodes.initialize_node import initialize_node
from src.core.orchestration.graph.nodes.frontier_loop_node import agent_loop_node
from src.core.orchestration.graph.nodes.approval_node import approval_node
from src.core.orchestration.graph.nodes.verification_node import verification_node
from src.core.orchestration.graph.nodes.compact_node import compact_node
from src.core.orchestration.graph.nodes.finalize_node import finalize_node
from src.core.orchestration.graph import canonical_routing as _canonical_routing

logger = logging.getLogger(__name__)

_STATE_SCHEMAS_STRICT = os.environ.get(
    "CODINGAGENT_STATE_SCHEMAS_STRICT", "1"
).lower() in ("1", "true", "yes")


def _validated(name: str, fn):
    """Wrap *fn* with state-schema validation at graph boundary."""
    return wrap_node(name, fn, strict=_STATE_SCHEMAS_STRICT)


_COMPILED_GRAPH = None
_COMPILED_GRAPH_LOCK = threading.Lock()
_GRAPH_CACHE: Dict[str, Any] = {}
_GRAPH_CACHE_LOCK = threading.Lock()


def _graph_checkpointer():
    """Durable checkpoint saver for automatic crash recovery (audit 3.4)."""
    from src.core.orchestration.graph.checkpoint_saver import default_checkpointer

    return default_checkpointer()


def _compile_canonical_graph():
    """Assemble the unified 6-node LangGraph cognitive pipeline.

    Topology:
        START -> initialize -> agent_loop
        agent_loop -> [agent_loop | approval | verify | compact | finalize]
        approval -> [agent_loop | finalize]
        verify -> [agent_loop | finalize]
        compact -> [agent_loop | finalize]
        finalize -> END
    """
    workflow = StateGraph(AgentState)

    workflow.add_node("initialize", _validated("initialize", initialize_node))
    workflow.add_node("agent_loop", _validated("agent_loop", agent_loop_node))
    workflow.add_node("approval", _validated("approval", approval_node))
    workflow.add_node("verify", _validated("verify", verification_node))
    workflow.add_node("compact", _validated("compact", compact_node))
    workflow.add_node("finalize", _validated("finalize", finalize_node))

    workflow.set_entry_point("initialize")
    workflow.add_edge("initialize", "agent_loop")

    workflow.add_conditional_edges(
        "agent_loop",
        _canonical_routing.route_after_agent_loop,
        {
            "agent_loop": "agent_loop",
            "approval": "approval",
            "verify": "verify",
            "compact": "compact",
            "finalize": "finalize",
        },
    )

    workflow.add_conditional_edges(
        "approval",
        _canonical_routing.route_after_approval,
        {"agent_loop": "agent_loop", "finalize": "finalize"},
    )

    workflow.add_conditional_edges(
        "verify",
        _canonical_routing.route_after_verify,
        {"agent_loop": "agent_loop", "finalize": "finalize"},
    )

    workflow.add_conditional_edges(
        "compact",
        _canonical_routing.route_after_compact,
        {"agent_loop": "agent_loop", "finalize": "finalize"},
    )

    workflow.add_edge("finalize", END)

    return workflow.compile(checkpointer=_graph_checkpointer())


def compile_canonical_graph():
    """Public constructor for the unified 6-node cognitive pipeline."""
    return _compile_canonical_graph()


def compile_agent_graph():
    """Assemble the 6-node canonical LangGraph cognitive pipeline."""
    return compile_canonical_graph()


def _get_compiled_graph():
    """Return the cached compiled agent graph, compiling it on first call."""
    global _COMPILED_GRAPH
    if _COMPILED_GRAPH is None:
        with _COMPILED_GRAPH_LOCK:
            if _COMPILED_GRAPH is None:
                _COMPILED_GRAPH = compile_canonical_graph()
    return _COMPILED_GRAPH


def _reset_compiled_graph() -> None:
    """Reset the cached graph (for tests that need a fresh compile)."""
    global _COMPILED_GRAPH, _GRAPH_CACHE
    _COMPILED_GRAPH = None
    with _GRAPH_CACHE_LOCK:
        _GRAPH_CACHE.clear()


def build_tier_graph(tier: str = "canonical"):
    """Return a compiled LangGraph graph for the given tier (canonical 6-node graph)."""
    cached = _GRAPH_CACHE.get("canonical")
    if cached is not None:
        return cached

    with _GRAPH_CACHE_LOCK:
        cached = _GRAPH_CACHE.get("canonical")
        if cached is not None:
            return cached
        logger.info("build_tier_graph: compiling canonical unified 6-node graph for tier '%s'", tier)
        compiled = _compile_canonical_graph()
        _GRAPH_CACHE["canonical"] = compiled
        return compiled


def get_compiled_graph_for_orchestrator(
    orchestrator: Any = None,
    *,
    model: str | None = None,
    adapter: Any = None,
):
    """Return the compiled graph selected for the current orchestrator/model tier."""
    return build_tier_graph("canonical")
