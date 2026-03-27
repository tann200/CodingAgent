"""
Session Registry - Central registry for all active agent sessions.

Provides:
- Global session tracking across all orchestrators
- Session health monitoring
- Cross-session coordination
- Automatic cleanup of stale sessions
"""

import asyncio
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Set, Callable, Any

logger = logging.getLogger(__name__)


class SessionStatus(Enum):
    """Session lifecycle states."""

    PENDING = "pending"
    INITIALIZING = "initializing"
    RUNNING = "running"
    WAITING = "waiting"  # Blocked on user input, preview, etc.
    PAUSED = "paused"  # Suspended for delegation
    COMPLETING = "completing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SessionPriority(Enum):
    """Session priority levels for scheduling."""

    LOW = 0
    NORMAL = 1
    HIGH = 2
    CRITICAL = 3


@dataclass
class SessionInfo:
    """Metadata about an active session."""

    session_id: str
    task_id: str
    role: str
    status: SessionStatus
    priority: SessionPriority
    created_at: float
    last_active_at: float
    parent_session_id: Optional[str] = None
    child_session_ids: List[str] = field(default_factory=list)
    task_description: str = ""
    tool_call_count: int = 0
    token_usage: int = 0
    error_count: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


class SessionRegistry:
    """
    Central registry for all active agent sessions.

    Thread-safe singleton that provides:
    - Session registration/deregistration
    - Health monitoring and stale session cleanup
    - Cross-session queries and coordination
    - Event publishing for session lifecycle

    Usage:
        registry = SessionRegistry.get_instance()

        # Register a new session
        registry.register_session(session_id, role="operational", task="...")

        # Query active sessions
        active = registry.get_active_sessions()

        # Monitor session health
        registry.start_health_monitor()
    """

    _instance = None
    _instance_lock = threading.Lock()

    def __init__(self):
        self._sessions: Dict[str, SessionInfo] = {}
        self._task_to_session: Dict[str, str] = {}  # task_id -> session_id
        self._role_sessions: Dict[str, Set[str]] = {}  # role -> set of session_ids
        self._lock = threading.RLock()

        # Health monitoring
        self._health_monitor_running = False
        self._health_monitor_task: Optional[asyncio.Task] = None
        self._stale_threshold = 300.0  # 5 minutes without activity
        self._cleanup_interval = 60.0  # Run cleanup every 60 seconds

        # Event callbacks
        self._on_session_registered: List[Callable[[SessionInfo], None]] = []
        self._on_session_unregistered: List[Callable[[SessionInfo], None]] = []
        self._on_session_status_changed: List[
            Callable[[str, SessionStatus, SessionStatus], None]
        ] = []
        self._on_health_alert: List[
            Callable[[str, str], None]
        ] = []  # session_id, message

    @classmethod
    def get_instance(cls) -> "SessionRegistry":
        """Get the singleton instance."""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset the singleton (for testing)."""
        with cls._instance_lock:
            if cls._instance is not None:
                cls._instance.shutdown()
            cls._instance = None

    def register_session(
        self,
        session_id: str,
        role: str = "operational",
        task_description: str = "",
        priority: SessionPriority = SessionPriority.NORMAL,
        parent_session_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SessionInfo:
        """
        Register a new session.

        Args:
            session_id: Unique identifier for this session
            role: Agent role (operational, analyst, reviewer, etc.)
            task_description: Human-readable task description
            priority: Session priority level
            parent_session_id: If this is a subagent, the parent's session_id
            metadata: Additional metadata to store

        Returns:
            SessionInfo for the registered session
        """
        with self._lock:
            if session_id in self._sessions:
                logger.warning(f"Session {session_id} already registered")
                return self._sessions[session_id]

            task_id = str(uuid.uuid4())[:8]

            info = SessionInfo(
                session_id=session_id,
                task_id=task_id,
                role=role,
                status=SessionStatus.INITIALIZING,
                priority=priority,
                created_at=time.time(),
                last_active_at=time.time(),
                parent_session_id=parent_session_id,
                task_description=task_description,
                metadata=metadata or {},
            )

            self._sessions[session_id] = info
            self._task_to_session[task_id] = session_id

            # Track by role
            if role not in self._role_sessions:
                self._role_sessions[role] = set()
            self._role_sessions[role].add(session_id)

            # Update parent's child list
            if parent_session_id and parent_session_id in self._sessions:
                self._sessions[parent_session_id].child_session_ids.append(session_id)

            logger.info(
                f"Session registered: {session_id} (role={role}, "
                f"priority={priority.name}, parent={parent_session_id})"
            )

            # Notify callbacks
            self._notify_registered(info)

            return info

    def unregister_session(self, session_id: str, reason: str = "") -> bool:
        """
        Unregister a session and clean up relationships.

        Args:
            session_id: Session to unregister
            reason: Reason for unregistration

        Returns:
            True if session was unregistered, False if not found
        """
        with self._lock:
            if session_id not in self._sessions:
                logger.warning(f"Session {session_id} not found for unregistration")
                return False

            info = self._sessions[session_id]

            # Remove from role tracking
            if info.role in self._role_sessions:
                self._role_sessions[info.role].discard(session_id)

            # Remove task mapping
            self._task_to_session.pop(info.task_id, None)

            # Notify parent's child list
            if info.parent_session_id and info.parent_session_id in self._sessions:
                parent = self._sessions[info.parent_session_id]
                if session_id in parent.child_session_ids:
                    parent.child_session_ids.remove(session_id)

            # Cascade unregister children
            for child_id in list(info.child_session_ids):
                self.unregister_session(
                    child_id, f"Parent {session_id} unregistered: {reason}"
                )

            # Remove session
            del self._sessions[session_id]

            logger.info(
                f"Session unregistered: {session_id} (reason={reason}, "
                f"children_cancelled={len(info.child_session_ids)})"
            )

            # Notify callbacks
            self._notify_unregistered(info)

            return True

    def update_session_status(
        self,
        session_id: str,
        status: SessionStatus,
        error: Optional[str] = None,
    ) -> bool:
        """
        Update session status.

        Args:
            session_id: Session to update
            status: New status
            error: Error message if transitioning to FAILED

        Returns:
            True if updated, False if session not found
        """
        with self._lock:
            if session_id not in self._sessions:
                return False

            info = self._sessions[session_id]
            old_status = info.status
            info.status = status
            info.last_active_at = time.time()

            if error:
                info.error_count += 1
                info.metadata["last_error"] = error

            logger.debug(
                f"Session {session_id} status: {old_status.value} -> {status.value}"
            )

            self._notify_status_changed(session_id, old_status, status)

            return True

    def update_session_activity(
        self,
        session_id: str,
        tool_call: bool = False,
        token_usage: int = 0,
    ) -> bool:
        """
        Update session activity timestamp and stats.

        Args:
            session_id: Session to update
            tool_call: Whether this activity includes a tool call
            token_usage: Token usage delta

        Returns:
            True if updated, False if session not found
        """
        with self._lock:
            if session_id not in self._sessions:
                return False

            info = self._sessions[session_id]
            info.last_active_at = time.time()

            if tool_call:
                info.tool_call_count += 1

            if token_usage > 0:
                info.token_usage += token_usage

            return True

    def get_session(self, session_id: str) -> Optional[SessionInfo]:
        """Get session info by ID."""
        with self._lock:
            return self._sessions.get(session_id)

    def get_active_sessions(
        self,
        role: Optional[str] = None,
        status: Optional[SessionStatus] = None,
    ) -> List[SessionInfo]:
        """
        Get active sessions, optionally filtered.

        Args:
            role: Filter by role
            status: Filter by status (default: exclude COMPLETED, FAILED, CANCELLED)

        Returns:
            List of matching SessionInfo objects
        """
        with self._lock:
            if status:
                return [
                    s
                    for s in self._sessions.values()
                    if s.status == status and (role is None or s.role == role)
                ]

            # Default: exclude terminal states
            terminal = {
                SessionStatus.COMPLETED,
                SessionStatus.FAILED,
                SessionStatus.CANCELLED,
            }
            return [
                s
                for s in self._sessions.values()
                if s.status not in terminal and (role is None or s.role == role)
            ]

    def get_session_tree(self, session_id: str) -> Dict[str, Any]:
        """
        Get session and all descendants as a tree.

        Args:
            session_id: Root session ID

        Returns:
            Dict representing the session tree
        """
        with self._lock:

            def build_tree(sid: str) -> Dict[str, Any]:
                info = self._sessions.get(sid)
                if not info:
                    return {}
                return {
                    "session_id": sid,
                    "role": info.role,
                    "status": info.status.value,
                    "task": info.task_description[:50],
                    "children": [build_tree(cid) for cid in info.child_session_ids],
                }

            return build_tree(session_id)

    def get_stale_sessions(
        self, threshold: Optional[float] = None
    ) -> List[SessionInfo]:
        """
        Get sessions that haven't been active recently.

        Args:
            threshold: Stale threshold in seconds (default: self._stale_threshold)

        Returns:
            List of stale SessionInfo objects
        """
        threshold = threshold or self._stale_threshold
        now = time.time()

        with self._lock:
            return [
                s
                for s in self._sessions.values()
                if s.status in {SessionStatus.RUNNING, SessionStatus.WAITING}
                and (now - s.last_active_at) > threshold
            ]

    def get_statistics(self) -> Dict[str, Any]:
        """Get registry statistics."""
        with self._lock:
            by_status: Dict[str, int] = {}
            by_role: Dict[str, int] = {}
            total_tool_calls = 0
            total_token_usage = 0

            for info in self._sessions.values():
                status_key = info.status.value
                by_status[status_key] = by_status.get(status_key, 0) + 1
                by_role[info.role] = by_role.get(info.role, 0) + 1
                total_tool_calls += info.tool_call_count
                total_token_usage += info.token_usage

            return {
                "total_sessions": len(self._sessions),
                "by_status": by_status,
                "by_role": by_role,
                "total_tool_calls": total_tool_calls,
                "total_token_usage": total_token_usage,
                "stale_count": len(self.get_stale_sessions()),
            }

    # Event subscriptions
    def on_session_registered(self, callback: Callable[[SessionInfo], None]) -> None:
        """Register callback for session creation."""
        self._on_session_registered.append(callback)

    def on_session_unregistered(self, callback: Callable[[SessionInfo], None]) -> None:
        """Register callback for session deletion."""
        self._on_session_unregistered.append(callback)

    def on_status_changed(
        self, callback: Callable[[str, SessionStatus, SessionStatus], None]
    ) -> None:
        """Register callback for status changes."""
        self._on_session_status_changed.append(callback)

    def on_health_alert(self, callback: Callable[[str, str], None]) -> None:
        """Register callback for health alerts (stale sessions, errors, etc.)."""
        self._on_health_alert.append(callback)

    def _notify_registered(self, info: SessionInfo) -> None:
        for cb in self._on_session_registered:
            try:
                cb(info)
            except Exception as e:
                logger.error(f"Error in on_session_registered callback: {e}")

    def _notify_unregistered(self, info: SessionInfo) -> None:
        for cb in self._on_session_unregistered:
            try:
                cb(info)
            except Exception as e:
                logger.error(f"Error in on_session_unregistered callback: {e}")

    def _notify_status_changed(
        self, session_id: str, old: SessionStatus, new: SessionStatus
    ) -> None:
        for cb in self._on_session_status_changed:
            try:
                cb(session_id, old, new)
            except Exception as e:
                logger.error(f"Error in on_status_changed callback: {e}")

    def _notify_health_alert(self, session_id: str, message: str) -> None:
        for cb in self._on_health_alert:
            try:
                cb(session_id, message)
            except Exception as e:
                logger.error(f"Error in on_health_alert callback: {e}")

    # Health monitoring
    def start_health_monitor(self) -> None:
        """Start the background health monitoring task."""
        if self._health_monitor_running:
            logger.warning("Health monitor already running")
            return

        self._health_monitor_running = True
        logger.info("Starting session health monitor")

        async def _monitor_loop():
            while self._health_monitor_running:
                try:
                    await asyncio.sleep(self._cleanup_interval)
                    await self._check_session_health()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Health monitor error: {e}")

        try:
            loop = asyncio.get_running_loop()
            self._health_monitor_task = loop.create_task(_monitor_loop())
        except RuntimeError:
            # No running event loop — health monitor will not run in background
            pass

    def stop_health_monitor(self) -> None:
        """Stop the background health monitoring task."""
        self._health_monitor_running = False
        if self._health_monitor_task:
            self._health_monitor_task.cancel()
            self._health_monitor_task = None
        logger.info("Stopped session health monitor")

    async def _check_session_health(self) -> None:
        """Check all sessions for health issues."""
        stale = self.get_stale_sessions()

        for info in stale:
            message = (
                f"Session {info.session_id} stale for "
                f"{time.time() - info.last_active_at:.0f}s"
            )
            logger.warning(message)
            self._notify_health_alert(info.session_id, message)

            # Auto-warn after extended staleness
            if time.time() - info.last_active_at > self._stale_threshold * 2:
                self._notify_health_alert(
                    info.session_id,
                    f"CRITICAL: Session {info.session_id} extremely stale, may be hung",
                )

    def shutdown(self) -> None:
        """Shutdown the registry and stop all monitoring."""
        self.stop_health_monitor()

        with self._lock:
            # Unregister all sessions
            session_ids = list(self._sessions.keys())
            for sid in session_ids:
                self.unregister_session(sid, "Registry shutdown")

        logger.info("Session registry shutdown complete")


def get_session_registry() -> SessionRegistry:
    """Get the global session registry instance."""
    return SessionRegistry.get_instance()
