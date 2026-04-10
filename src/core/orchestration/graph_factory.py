from typing import Any, Optional
from langgraph.graph import StateGraph, END
from langchain_core.runnables import RunnableConfig
from src.core.orchestration.graph.state import AgentState
from src.core.orchestration.graph.nodes.perception_node import perception_node
from src.core.orchestration.graph.nodes.execution_node import execution_node
from src.core.orchestration.graph.nodes.memory_update_node import memory_update_node
from src.core.orchestration.graph.nodes.planning_node import planning_node
from src.core.orchestration.graph.nodes.verification_node import verification_node
from src.core.orchestration.role_config import (
    normalize_role,
    CANONICAL_ROLES,
    ROLE_ALIASES,
)


def should_after_planning(state: AgentState) -> str:
    # BUG-VOL23-3: use .get() to avoid KeyError when state is a partial dict
    if state.get("rounds", 0) >= 15:
        return "end"
    if state.get("next_action"):
        return "execute"
    current_plan = state.get("current_plan")
    if current_plan and len(current_plan) > 0:
        return "execute"
    if state.get("last_result"):
        return "memory_sync"
    return "end"


def _create_wrapper(node_func):
    async def wrapper(state: AgentState, config: RunnableConfig):
        return await node_func(state, config)

    return wrapper


class GraphFactory:
    GRAPH_TYPES = {
        "planner": "planning",
        "coder": "execution",
        "reviewer": "verification",
        "researcher": "search",
    }

    @staticmethod
    def create_planner_graph() -> Any:
        workflow = StateGraph(AgentState)
        workflow.add_node("perception", _create_wrapper(perception_node))
        workflow.add_node("planning", _create_wrapper(planning_node))
        workflow.add_node("memory_sync", _create_wrapper(memory_update_node))
        workflow.set_entry_point("perception")
        workflow.add_edge("perception", "planning")
        workflow.add_conditional_edges(
            "planning",
            should_after_planning,
            {"execute": END, "memory_sync": "memory_sync", "end": END},
        )
        workflow.add_edge("memory_sync", END)
        return workflow.compile()

    @staticmethod
    def create_coder_graph() -> Any:
        workflow = StateGraph(AgentState)
        workflow.add_node("perception", _create_wrapper(perception_node))
        workflow.add_node("planning", _create_wrapper(planning_node))
        workflow.add_node("execution", _create_wrapper(execution_node))
        workflow.add_node("memory_sync", _create_wrapper(memory_update_node))
        workflow.set_entry_point("perception")
        workflow.add_edge("perception", "planning")
        workflow.add_conditional_edges(
            "planning",
            should_after_planning,
            {"execute": "execution", "memory_sync": "memory_sync", "end": END},
        )
        workflow.add_edge("execution", "memory_sync")
        workflow.add_edge("memory_sync", END)
        return workflow.compile()

    @staticmethod
    def create_reviewer_graph() -> Any:
        workflow = StateGraph(AgentState)
        workflow.add_node("perception", _create_wrapper(perception_node))
        workflow.add_node("verification", _create_wrapper(verification_node))
        workflow.add_node("execution", _create_wrapper(execution_node))
        workflow.add_node("memory_sync", _create_wrapper(memory_update_node))
        workflow.set_entry_point("perception")
        workflow.add_edge("perception", "verification")
        workflow.add_edge("verification", "execution")
        workflow.add_edge("execution", "memory_sync")
        workflow.add_edge("memory_sync", END)
        return workflow.compile()

    @staticmethod
    def create_researcher_graph() -> Any:
        workflow = StateGraph(AgentState)
        workflow.add_node("perception", _create_wrapper(perception_node))
        workflow.add_node("memory_sync", _create_wrapper(memory_update_node))
        workflow.set_entry_point("perception")
        workflow.add_edge("perception", "memory_sync")
        workflow.add_edge("memory_sync", END)
        return workflow.compile()

    @staticmethod
    def get_graph(role: str) -> Optional[Any]:
        # ARCH-VOL21-1: delegate to the full 14-node compiled graph instead of
        # building a minimal 3–4 node subgraph.  Subgraphs silently skipped
        # plan-validation, debug loops, evaluation, step-retry, and
        # wait-for-user gates.  The compiled graph is a singleton (thread-safe
        # double-check lock in builder.py) so this has no extra compilation cost.
        r = role.strip().lower() if role else ""
        # Validate the role is known before returning the full graph so callers
        # that pass an invalid role still get None (same behaviour as before).
        legacy_roles = {"planner", "coder", "reviewer", "researcher"}
        is_legacy = r in legacy_roles
        is_known = r in CANONICAL_ROLES or r in ROLE_ALIASES
        if not is_legacy and not is_known:
            return None
        try:
            from src.core.orchestration.graph.builder import _get_compiled_graph
            return _get_compiled_graph()
        except Exception:
            # Fallback to the role-specific subgraph so subagents can still
            # function if the full graph cannot be compiled.
            graph_creators = {
                "planner": GraphFactory.create_planner_graph,
                "coder": GraphFactory.create_coder_graph,
                "reviewer": GraphFactory.create_reviewer_graph,
                "researcher": GraphFactory.create_researcher_graph,
            }
            if r in graph_creators:
                return graph_creators[r]()
            canonical = normalize_role(role)
            canonical_to_legacy = {
                "strategic": "planner",
                "operational": "coder",
                "reviewer": "reviewer",
                "analyst": "researcher",
                "debugger": "coder",
            }
            legacy_key = canonical_to_legacy.get(canonical)
            creator = graph_creators.get(legacy_key) if legacy_key else None
            return creator() if creator else None

    @staticmethod
    def get_default_graph() -> Any:
        return GraphFactory.create_coder_graph()
