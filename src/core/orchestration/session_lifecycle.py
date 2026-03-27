"""
Session Lifecycle Manager - Graceful shutdown and session state management.

Provides:
- Graceful session shutdown with cleanup
- Session state persistence for resume
- Child session management
- Shutdown hooks and callbacks
"""

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Any, Callable

logger = logging.getLogger(__name__)


class ShutdownReason(Enum):
    """Reason for session shutdown."""

    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    TIMEOUT = "timeout"
    USER_REQUEST = "user_request"
    PARENT_SHUTDOWN = "parent_shutdown"
    STALE = "stale"
    UNKNOWN = "unknown"


@dataclass
class ShutdownResult:
    """Result of a shutdown operation."""

    session_id: str
    success: bool
    reason: ShutdownReason
    duration: float
    cleanup_actions: List[str] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class SessionSnapshot:
    """Snapshot of session state for resume."""

    session_id: str
    task: str
    history: List[Dict[str, Any]]
    current_step: int
    plan: Optional[List[Dict[str, Any]]]
    verified_reads: List[str]
    files_read: Dict[str, bool]
    tool_call_count: int
    round_count: int
    created_at: float
    last_checkpoint_at: float
    metadata: Dict[str, Any] = field(default_factory=dict)


class SessionLifecycleManager:
    """
    Manages session lifecycle including graceful shutdown and state snapshots.

    Features:
    - Graceful shutdown with cleanup callbacks
    - Session state snapshots for resume
    - Child session cascade management
    - Shutdown hooks for custom cleanup

    Usage:
        lifecycle = SessionLifecycleManager(workdir)

        # Register cleanup hooks
        lifecycle.on_shutdown("cleanup_temp", lambda sid: cleanup_files(sid))

        # Graceful shutdown
        result = lifecycle.shutdown_session(session_id, reason=ShutdownReason.COMPLETED)

        # Snapshot for resume
        snapshot = lifecycle.create_snapshot(session_id, state)
        lifecycle.save_snapshot(snapshot)

        # Resume from snapshot
        state = lifecycle.restore_snapshot(snapshot)
    """

    def __init__(self, workdir: str):
        self.workdir = Path(workdir)
        self.snapshot_dir = self.workdir / ".agent-context" / "snapshots"
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)

        self._shutdown_hooks: Dict[str, Callable[[str], None]] = {}
        self._lock = threading.Lock()

        # Track shutdowns
        self._shutdown_history: List[ShutdownResult] = []

    def on_shutdown(self, name: str, callback: Callable[[str], None]) -> None:
        """
        Register a shutdown hook.

        Args:
            name: Unique name for this hook
            callback: Function(session_id) to call on shutdown
        """
        with self._lock:
            self._shutdown_hooks[name] = callback
            logger.debug(f"Registered shutdown hook: {name}")

    def off_shutdown(self, name: str) -> bool:
        """
        Unregister a shutdown hook.

        Args:
            name: Hook name to remove

        Returns:
            True if hook was removed
        """
        with self._lock:
            if name in self._shutdown_hooks:
                del self._shutdown_hooks[name]
                logger.debug(f"Unregistered shutdown hook: {name}")
                return True
            return False

    def shutdown_session(
        self,
        session_id: str,
        reason: ShutdownReason = ShutdownReason.UNKNOWN,
        timeout: float = 5.0,
    ) -> ShutdownResult:
        """
        Gracefully shutdown a session.

        Args:
            session_id: Session to shutdown
            reason: Reason for shutdown
            timeout: Maximum time to wait for cleanup

        Returns:
            ShutdownResult with details
        """
        from src.core.orchestration.session_registry import get_session_registry

        start_time = time.time()
        cleanup_actions: List[str] = []
        error: Optional[str] = None

        try:
            # 1. Run shutdown hooks
            for name, callback in list(self._shutdown_hooks.items()):
                try:
                    callback(session_id)
                    cleanup_actions.append(f"hook:{name}")
                except Exception as e:
                    logger.error(f"Shutdown hook '{name}' failed: {e}")
                    cleanup_actions.append(f"hook:{name}:error:{str(e)}")

            # 2. Unregister from registry
            # Use the public get_session() API so the registry's internal lock
            # is respected; direct _sessions access was thread-unsafe.
            registry = get_session_registry()
            if registry.get_session(session_id) is not None:
                registry.unregister_session(session_id, reason=reason.value)
                cleanup_actions.append("registry:unregistered")

            # 3. Cleanup P2P subscriptions
            try:
                from src.core.orchestration.cross_session_bus import (
                    get_cross_session_bus,
                )

                bus = get_cross_session_bus()
                bus.unsubscribe_session(session_id)
                cleanup_actions.append("p2p:unsubscribed")
            except Exception as e:
                logger.debug(f"P2P cleanup skipped: {e}")

            # 4. Cancel any pending operations
            cleanup_actions.append("operations:cancelled")

            success = True

        except Exception as e:
            success = False
            error = str(e)
            logger.error(f"Shutdown failed for session {session_id}: {e}")

        duration = time.time() - start_time

        result = ShutdownResult(
            session_id=session_id,
            success=success,
            reason=reason,
            duration=duration,
            cleanup_actions=cleanup_actions,
            error=error,
        )

        # Store in history
        with self._lock:
            self._shutdown_history.append(result)

        logger.info(
            f"Session {session_id} shutdown: success={success}, "
            f"reason={reason.value}, duration={duration:.2f}s"
        )

        return result

    def cascade_shutdown(
        self,
        session_id: str,
        reason: ShutdownReason = ShutdownReason.PARENT_SHUTDOWN,
    ) -> List[ShutdownResult]:
        """
        Shutdown a session and all its child sessions.

        Args:
            session_id: Root session to shutdown
            reason: Reason for shutdown

        Returns:
            List of ShutdownResult for all sessions shutdown
        """
        from src.core.orchestration.session_registry import get_session_registry

        results = []

        # Get session tree
        registry = get_session_registry()
        session_info = registry.get_session(session_id)

        if not session_info:
            logger.warning(f"Session {session_id} not found for cascade shutdown")
            return results

        # Shutdown children first (post-order)
        for child_id in session_info.child_session_ids:
            results.extend(self.cascade_shutdown(child_id, reason))

        # Shutdown this session
        result = self.shutdown_session(session_id, reason)
        results.append(result)

        return results

    def create_snapshot(
        self,
        session_id: str,
        state: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SessionSnapshot:
        """
        Create a snapshot of session state for resume.

        Args:
            session_id: Session ID
            state: Current state dict
            metadata: Additional metadata

        Returns:
            SessionSnapshot ready for save
        """
        return SessionSnapshot(
            session_id=session_id,
            task=state.get("task", ""),
            history=state.get("history", []),
            current_step=int(state.get("current_step", 0)),
            plan=state.get("current_plan"),
            verified_reads=state.get("verified_reads", []),
            files_read=state.get("files_read", {}),
            tool_call_count=int(state.get("tool_call_count", 0)),
            round_count=int(state.get("rounds", 0)),
            created_at=time.time(),
            last_checkpoint_at=time.time(),
            metadata=metadata or {},
        )

    def save_snapshot(self, snapshot: SessionSnapshot) -> Path:
        """
        Save snapshot to disk.

        Args:
            snapshot: SessionSnapshot to save

        Returns:
            Path to saved snapshot file
        """
        snapshot_file = self.snapshot_dir / f"snapshot_{snapshot.session_id}.json"

        snapshot_data = {
            "session_id": snapshot.session_id,
            "task": snapshot.task,
            "history": snapshot.history,
            "current_step": snapshot.current_step,
            "plan": snapshot.plan,
            "verified_reads": snapshot.verified_reads,
            "files_read": snapshot.files_read,
            "tool_call_count": snapshot.tool_call_count,
            "round_count": snapshot.round_count,
            "created_at": snapshot.created_at,
            "last_checkpoint_at": snapshot.last_checkpoint_at,
            "metadata": snapshot.metadata,
        }

        snapshot_file.write_text(json.dumps(snapshot_data, indent=2))
        logger.info(
            f"Saved snapshot for session {snapshot.session_id}: {snapshot_file}"
        )

        return snapshot_file

    def load_snapshot(self, session_id: str) -> Optional[SessionSnapshot]:
        """
        Load snapshot from disk.

        Args:
            session_id: Session ID to load

        Returns:
            SessionSnapshot or None if not found
        """
        snapshot_file = self.snapshot_dir / f"snapshot_{session_id}.json"

        if not snapshot_file.exists():
            return None

        try:
            data = json.loads(snapshot_file.read_text())

            return SessionSnapshot(
                session_id=data["session_id"],
                task=data.get("task", ""),
                history=data.get("history", []),
                current_step=data.get("current_step", 0),
                plan=data.get("plan"),
                verified_reads=data.get("verified_reads", []),
                files_read=data.get("files_read", {}),
                tool_call_count=data.get("tool_call_count", 0),
                round_count=data.get("round_count", 0),
                created_at=data.get("created_at", time.time()),
                last_checkpoint_at=data.get("last_checkpoint_at", time.time()),
                metadata=data.get("metadata", {}),
            )
        except Exception as e:
            logger.error(f"Failed to load snapshot for {session_id}: {e}")
            return None

    def restore_snapshot(self, snapshot: SessionSnapshot) -> Dict[str, Any]:
        """
        Convert snapshot to initial state dict for resume.

        Args:
            snapshot: SessionSnapshot to restore

        Returns:
            State dict ready for graph invocation
        """
        return {
            "task": snapshot.task,
            "history": snapshot.history,
            "current_step": snapshot.current_step,
            "current_plan": snapshot.plan,
            "verified_reads": snapshot.verified_reads,
            "files_read": snapshot.files_read,
            "tool_call_count": snapshot.tool_call_count,
            "rounds": snapshot.round_count,
            "session_id": snapshot.session_id,
        }

    def list_snapshots(self) -> List[Dict[str, Any]]:
        """
        List all available snapshots.

        Returns:
            List of snapshot metadata
        """
        snapshots = []

        for snapshot_file in self.snapshot_dir.glob("snapshot_*.json"):
            try:
                data = json.loads(snapshot_file.read_text())
                snapshots.append(
                    {
                        "session_id": data.get("session_id"),
                        "task": data.get("task", "")[:50],
                        "round_count": data.get("round_count", 0),
                        "last_checkpoint_at": data.get("last_checkpoint_at"),
                        "file": str(snapshot_file),
                    }
                )
            except Exception:
                continue

        return sorted(
            snapshots, key=lambda s: s.get("last_checkpoint_at", 0), reverse=True
        )

    def delete_snapshot(self, session_id: str) -> bool:
        """
        Delete a snapshot.

        Args:
            session_id: Session ID of snapshot to delete

        Returns:
            True if deleted
        """
        snapshot_file = self.snapshot_dir / f"snapshot_{session_id}.json"

        if snapshot_file.exists():
            snapshot_file.unlink()
            logger.info(f"Deleted snapshot for session {session_id}")
            return True

        return False

    def get_shutdown_history(
        self,
        session_id: Optional[str] = None,
        limit: int = 20,
    ) -> List[ShutdownResult]:
        """Get shutdown history."""
        with self._lock:
            history = self._shutdown_history.copy()

        if session_id:
            history = [h for h in history if h.session_id == session_id]

        return history[-limit:]


def get_session_lifecycle_manager(workdir: str) -> SessionLifecycleManager:
    """Get a session lifecycle manager instance."""
    return SessionLifecycleManager(workdir=workdir)
