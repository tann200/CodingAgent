import logging
from typing import Any, Optional

from src.core.orchestration.graph.state import StateLike
from src.core.orchestration.role_config import (
    normalize_role,
    CANONICAL_ROLES,
    ROLE_ALIASES,
)


logger = logging.getLogger(__name__)


def should_after_planning(state: StateLike) -> str:
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

class GraphFactory:
    GRAPH_TYPES = {
        "planner": "planning",
        "coder": "execution",
        "reviewer": "verification",
        "researcher": "search",
    }

    @staticmethod
    def create_planner_graph() -> Any:
        return GraphFactory.get_graph("planner")

    @staticmethod
    def create_coder_graph() -> Any:
        return GraphFactory.get_graph("coder")

    @staticmethod
    def create_reviewer_graph() -> Any:
        return GraphFactory.get_graph("reviewer")

    @staticmethod
    def create_researcher_graph() -> Any:
        return GraphFactory.get_graph("researcher")

    @staticmethod
    def get_graph(
        role: str,
        orchestrator: Any = None,
        model: Optional[str] = None,
    ) -> Optional[Any]:
        # Compatibility facade only: role-based callers still enter through
        # GraphFactory, but all valid roles now resolve to the single canonical
        # tier-aware graph selector.
        r = role.strip().lower() if role else ""
        legacy_roles = {"planner", "coder", "reviewer", "researcher"}
        is_legacy = r in legacy_roles
        is_known = r in CANONICAL_ROLES or r in ROLE_ALIASES
        if not is_legacy and not is_known:
            return None

        canonical = normalize_role(role)
        if not is_legacy and canonical not in CANONICAL_ROLES:
            return None

        try:
            from src.core.orchestration.graph.builder import (
                get_compiled_graph_for_orchestrator,
            )

            return get_compiled_graph_for_orchestrator(
                orchestrator=orchestrator,
                model=model,
            )
        except Exception as exc:
            logger.warning(
                "GraphFactory.get_graph: canonical graph resolution failed for role=%s: %s",
                role,
                exc,
            )
            return None

    @staticmethod
    def get_default_graph() -> Any:
        return GraphFactory.get_graph("coder")
