from typing import Dict, Any, Optional
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
    if state["rounds"] >= 15:
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
        graph_creators = {
            "planner": GraphFactory.create_planner_graph,
            "coder": GraphFactory.create_coder_graph,
            "reviewer": GraphFactory.create_reviewer_graph,
            "researcher": GraphFactory.create_researcher_graph,
        }
        # Accept legacy names or canonical role names
        if role in graph_creators:
            creator = graph_creators.get(role)
            if creator:
                return creator()

        # Only normalize if role is a known alias or canonical name
        r = role.strip().lower() if role else ""
        if r not in CANONICAL_ROLES and r not in ROLE_ALIASES:
            return None

        # Normalize role to canonical, then map canonical -> legacy key
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
        if creator:
            return creator()
        return None

    @staticmethod
    def get_default_graph() -> Any:
        return GraphFactory.create_coder_graph()


class HubAndSpokeCoordinator:
    def __init__(self, event_bus: Any = None):
        self.event_bus = event_bus
        self.agents: Dict[str, Any] = {}
        self.task_queue: list = []
        self.results: Dict[str, Any] = {}

    def register_agent(
        self, agent_id: str, role: str, config: Optional[Dict[str, Any]] = None
    ) -> bool:
        graph = GraphFactory.get_graph(role)
        if not graph:
            return False
        self.agents[agent_id] = {
            "role": role,
            "graph": graph,
            "config": config or {},
            "status": "idle",
        }
        if self.event_bus and hasattr(self.event_bus, "_agent_ids"):
            self.event_bus._agent_ids.add(agent_id)
        return True

    def dispatch_task(
        self, task: str, agent_id: str, context: Optional[Dict[str, Any]] = None
    ) -> None:
        if agent_id not in self.agents:
            return
        self.task_queue.append(
            {
                "task": task,
                "agent_id": agent_id,
                "context": context or {},
            }
        )

    def run_next(self, orchestrator: Any) -> Optional[Dict[str, Any]]:
        if not self.task_queue:
            return None
        item = self.task_queue.pop(0)
        agent_id = item["agent_id"]
        agent = self.agents.get(agent_id)
        if not agent:
            return None
        agent["status"] = "running"
        try:
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(
                    agent["graph"].ainvoke,
                    {
                        "task": item["task"],
                        "history": [],
                        "verified_reads": [],
                        "rounds": 0,
                        "working_dir": orchestrator.working_dir,
                        "system_prompt": orchestrator.msg_mgr.get_system_prompt(),
                    },
                    {"configurable": {"orchestrator": orchestrator}},
                )
                result = future.result()
            agent["status"] = "completed"
            self.results[agent_id] = result
            return result
        except Exception as e:
            agent["status"] = "failed"
            return {"error": str(e)}

    def get_agent_status(self, agent_id: str) -> Optional[str]:
        agent = self.agents.get(agent_id)
        return agent.get("status") if agent else None

    def list_agents(self) -> Dict[str, Dict[str, str]]:
        return {
            aid: {"role": a["role"], "status": a["status"]}
            for aid, a in self.agents.items()
        }

    def broadcast_message(self, message: Any, priority: int = 1) -> None:
        if self.event_bus:
            self.event_bus.broadcast_to_agents(message, priority)
