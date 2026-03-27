"""
Agent session manager for peer-to-peer communication.
Manages active agent sessions and their subscriptions.

Implements ACP/MCP compliant state hydration:
- session.request_state: TUI requests current session state on mount
- session.hydrated: AgentSessionManager responds with full state
"""

import logging
import threading
import uuid
from typing import Dict, Set, Callable, Any, List, Optional
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class AgentRole(Enum):
    ORCHESTRATOR = "orchestrator"
    SCOUT = "scout"
    RESEARCHER = "researcher"
    CODER = "coder"
    REVIEWER = "reviewer"
    TESTER = "tester"


@dataclass
class AgentSession:
    session_id: str
    role: AgentRole
    agent_id: str
    task: str
    status: str = "running"
    callbacks: Dict[str, Callable] = field(default_factory=dict)


class SessionState:
    """Represents the complete agent session state for hydration."""

    def __init__(self):
        self.session_id: str = ""
        self.task: str = ""
        self.message_history: List[Dict[str, str]] = []
        self.current_plan: List[Dict[str, Any]] = []
        self.current_step: int = 0
        self.provider: str = ""
        self.model: str = ""
        self.token_budget: Dict[str, Any] = {}
        self.active_agents: Dict[str, AgentSession] = {}
        self.files_modified: List[str] = []
        self.files_read: List[str] = []
        self.pending_p2p: List[Dict] = []

    def to_dict(self) -> Dict[str, Any]:
        """Convert to ACP-compliant session state dict."""
        return {
            "sessionId": self.session_id,
            "task": self.task,
            "messageHistory": self.message_history,
            "currentPlan": {
                "steps": self.current_plan,
                "currentStep": self.current_step,
            },
            "provider": {
                "name": self.provider,
                "model": self.model,
            },
            "tokenBudget": self.token_budget,
            "activeAgents": [
                {
                    "agentId": s.agent_id,
                    "role": s.role.value,
                    "task": s.task,
                    "status": s.status,
                }
                for s in self.active_agents.values()
            ],
            "workspace": {
                "filesModified": self.files_modified,
                "filesRead": self.files_read,
            },
            "pendingP2P": self.pending_p2p,
        }


class AgentSessionManager:
    """
    Manages active agent sessions and their subscriptions.

    CRITICAL: P2P messages are buffered, NOT applied directly to state.
    State can only be mutated via LangGraph node return values.

    GAP 1: State Hydration - Supports session.request_state / session.hydrated handshake.
    """

    _instance = None
    _instance_lock = threading.Lock()

    def __init__(self):
        self._sessions: Dict[str, AgentSession] = {}
        self._role_subscriptions: Dict[AgentRole, Set[str]] = {
            role: set() for role in AgentRole
        }
        self._pending_p2p: List[Dict] = []
        self._p2p_lock = threading.Lock()
        self._current_session_state: Optional[SessionState] = None
        # Thread-safety: _current_session_state is read/written from both the orchestrator
        # main thread (update_session_state) and the EventBus delivery thread
        # (_handle_state_request → get_session_state). Guard all accesses with this lock.
        self._state_lock = threading.Lock()
        self._setup_hydration_handler()

    def _setup_hydration_handler(self):
        """Subscribe to session.request_state events for state hydration."""
        try:
            from src.core.orchestration.event_bus import get_event_bus

            eb = get_event_bus()
            eb.subscribe("session.request_state", self._handle_state_request)
        except Exception:
            pass

    def _handle_state_request(self, payload: Dict[str, Any]) -> None:
        """Handle session.state request - publish hydrated state."""
        try:
            from src.core.orchestration.event_bus import get_event_bus

            eb = get_event_bus()
            hydrated_state = self.get_session_state()
            eb.publish("session.hydrated", hydrated_state.to_dict())
            logger.info("AgentSessionManager: published session.hydrated")
        except Exception as e:
            logger.error(f"AgentSessionManager: failed to publish hydrated state: {e}")

    def update_session_state(
        self,
        session_id: Optional[str] = None,
        task: Optional[str] = None,
        message_history: Optional[List[Dict[str, str]]] = None,
        current_plan: Optional[List[Dict[str, Any]]] = None,
        current_step: Optional[int] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        token_budget: Optional[Dict[str, Any]] = None,
        files_modified: Optional[List[str]] = None,
        files_read: Optional[List[str]] = None,
    ) -> None:
        """Update the current session state (for hydration)."""
        with self._state_lock:
            if self._current_session_state is None:
                self._current_session_state = SessionState()

            state = self._current_session_state
            if session_id is not None:
                state.session_id = session_id
            if task is not None:
                state.task = task
            if message_history is not None:
                state.message_history = message_history
            if current_plan is not None:
                state.current_plan = current_plan
            if current_step is not None:
                state.current_step = current_step
            if provider is not None:
                state.provider = provider
            if model is not None:
                state.model = model
            if token_budget is not None:
                state.token_budget = token_budget
            if files_modified is not None:
                state.files_modified = files_modified
            if files_read is not None:
                state.files_read = files_read

            # Sync pending P2P messages
            state.pending_p2p = self.flush_pending_p2p()

    def get_session_state(self) -> SessionState:
        """Get current session state for hydration."""
        with self._state_lock:
            if self._current_session_state is None:
                self._current_session_state = SessionState()
                self._current_session_state.session_id = "default"

            # Always sync latest pending P2P and active agents
            self._current_session_state.pending_p2p = self.flush_pending_p2p()
            self._current_session_state.active_agents = {
                k: v for k, v in self._sessions.items() if v.status == "running"
            }
            return self._current_session_state

    @classmethod
    def get_instance(cls) -> "AgentSessionManager":
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def buffer_p2p_message(self, source: str, payload: Dict[str, Any]):
        """
        Buffer incoming P2P message for later processing.

        This is thread-safe and does NOT mutate LangGraph state.
        """
        with self._p2p_lock:
            self._pending_p2p.append(
                {
                    "source": source,
                    "payload": payload,
                    "timestamp": __import__("time").time(),
                }
            )

    def flush_pending_p2p(self) -> List[Dict]:
        """Flush and return all pending P2P messages."""
        with self._p2p_lock:
            messages = self._pending_p2p.copy()
            self._pending_p2p.clear()
        return messages

    def register_agent(self, role: AgentRole, task: str) -> AgentSession:
        """Register a new agent session."""
        session_id = str(uuid.uuid4())[:8]
        agent_id = f"{role.value}_{session_id}"

        session = AgentSession(
            session_id=session_id,
            role=role,
            agent_id=agent_id,
            task=task,
            status="running",
            callbacks={},
        )
        self._sessions[agent_id] = session
        self._role_subscriptions[role].add(agent_id)

        return session

    def subscribe_to_role(
        self, agent_id: str, role: AgentRole, callback: Callable[[Any], None]
    ):
        """Subscribe agent to messages from a specific role."""
        from src.core.orchestration.event_bus import get_event_bus

        eb = get_event_bus()
        topic = f"agent.{role.value}.broadcast"

        def wrapped(msg):
            if msg.agent_id != agent_id:
                callback(msg)

        eb.subscribe(topic, wrapped)
        self._sessions[agent_id].callbacks[topic] = wrapped

    def publish_to_role(self, sender_id: str, role: AgentRole, payload: Any):
        """Broadcast message to all agents of a specific role."""
        from src.core.orchestration.event_bus import get_event_bus

        eb = get_event_bus()
        topic = f"agent.{role.value}.broadcast"

        eb.publish(topic, {"from": sender_id, "role": role.value, "payload": payload})

    def publish_to_agent(self, sender_id: str, target_id: str, payload: Any):
        """Send direct message to specific agent."""
        from src.core.orchestration.event_bus import get_event_bus

        eb = get_event_bus()
        eb.publish_to_agent(target_id, {"from": sender_id, "payload": payload})

    def get_active_agents(self) -> Dict[str, AgentSession]:
        """Get all active agent sessions."""
        return {k: v for k, v in self._sessions.items() if v.status == "running"}

    def unregister_agent(self, agent_id: str):
        """Unregister an agent session."""
        if agent_id in self._sessions:
            session = self._sessions[agent_id]
            self._role_subscriptions[session.role].discard(agent_id)
            del self._sessions[agent_id]


def get_agent_session_manager() -> AgentSessionManager:
    """Get the global agent session manager instance."""
    return AgentSessionManager.get_instance()
