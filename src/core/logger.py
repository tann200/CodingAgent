"""
Thread-safe logging for the Textual TUI with audit logging support.
"""

import json
import traceback
from datetime import datetime
from enum import Enum
from pathlib import Path
from queue import Empty, Queue, Full
from threading import Lock, Thread, Event, local as threading_local
from typing import Any, List, Optional, Dict
import logging
from src.core.orchestration.event_bus import get_event_bus


class AuditEventType(Enum):
    """Types of security-sensitive events to audit."""

    COMMAND_EXECUTION = "COMMAND_EXECUTION"
    FILE_ACCESS = "FILE_ACCESS"
    FILE_WRITE = "FILE_WRITE"
    FILE_DELETE = "FILE_DELETE"
    PATH_TRAVERSAL_BLOCKED = "PATH_TRAVERSAL_BLOCKED"
    SECURITY_VIOLATION = "SECURITY_VIOLATION"
    AUTHENTICATION = "AUTHENTICATION"
    CONFIGURATION_CHANGE = "CONFIGURATION_CHANGE"
    SYSTEM_ERROR = "SYSTEM_ERROR"


class AuditLevel(Enum):
    """Audit event severity levels."""

    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


# Module-level event bus registration slot (optional)
_event_bus = None


def set_event_bus(bus) -> None:
    """Register an event bus object that exposes a `publish(event_name, payload)` method.

    The logger will publish `log.entry` events for real-time TUI consumption.
    """
    global _event_bus
    _event_bus = bus


class GUILogger:
    _instance: Optional["GUILogger"] = None
    _lock: Lock = Lock()
    _queue: Queue[Any]
    _logs: List[Dict[str, Any]]
    _max_history: int
    _queue_maxsize: int = 5000

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        # Initialize instance attributes only once
        if not hasattr(self, "_initialized"):
            # bounded queue to avoid unbounded memory growth
            self._queue = Queue(maxsize=self._queue_maxsize)
            self._logs = []
            self._max_history = 1000
            self._initialized = True

    def _format(self, message: Any) -> str:
        """Convert arbitrary objects into a safe string for UI display.

        Prefer JSON for dict-like objects, otherwise fall back to repr(). This
        prevents non-string objects (e.g. SystemMessage, dicts, lists) from
        reaching Textual label rendering and causing MarkupError.
        """
        if isinstance(message, str):
            return message
        try:
            # Try JSON serialization for dict-like structures
            return json.dumps(message, default=lambda o: str(o), ensure_ascii=False)
        except Exception:
            try:
                return repr(message)
            except Exception:
                return str(message)

    def log(self, message: str, level: str = "INFO") -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        safe_msg = self._format(message)
        entry = {"timestamp": timestamp, "level": level.upper(), "message": safe_msg}

        # Print to stdout for console visibility (important messages)
        try:
            print(f"[{timestamp}] [{level.upper()}] {safe_msg}")
        except OSError:
            # Some test runners (Textual headless on Windows) replace stdout with invalid handles.  # noqa: E501
            # Fall back to sys.stderr.write which is more stable.
            import sys

            try:
                sys.stderr.write(f"[{timestamp}] [{level.upper()}] {safe_msg}\n")
            except Exception:
                # Last resort: ignore printing failures to avoid crashing tests
                pass

        # Route through standard logging system for TUI ConsolePanel
        try:
            import logging

            standard_logger = logging.getLogger("agent_tui")
            standard_level = {
                "DEBUG": logging.DEBUG,
                "INFO": logging.INFO,
                "WARNING": logging.WARNING,
                "ERROR": logging.ERROR,
                "CRITICAL": logging.CRITICAL,
            }.get(level.upper(), logging.INFO)
            standard_logger.log(standard_level, safe_msg)
        except Exception:
            pass  # Don't let logging failures break the application

        # Lightweight dedupe: if the last logged entry (if any) has the same level and message and same timestamp, skip  # noqa: E501
        with self._lock:
            if self._logs:
                last = self._logs[-1]
                if (
                    last.get("level") == entry["level"]
                    and last.get("message") == entry["message"]
                    and last.get("timestamp") == entry["timestamp"]
                ):
                    return
            # Append to persistent history
            self._logs.append(entry)
            if len(self._logs) > self._max_history:
                self._logs = self._logs[-self._max_history :]
        # Always put into the transient queue for real-time consumers
        # Use put_nowait to avoid blocking when queue is full (prevents hang on recursive logging)
        try:
            self._queue.put_nowait(entry)
        except Full:
            pass  # Drop oldest-equivalent: queue full means consumer is behind, discard silently
        # Publish to EventBus for UI consumers if available
        try:
            bus = get_event_bus()
            try:
                bus.publish('log.new', entry)
            except Exception:
                # non-fatal: don't let UI failures break logging
                pass
        except Exception:
            pass

    def get_logs(self, clear: bool = False) -> List[Dict[str, Any]]:
        """
        Retrieve log entries.
        - If clear==True: return only the newly queued entries (those not yet drained).
          Do NOT wipe the persistent history; only drain the transient queue.
        - If clear==False: return the persistent history followed by any newly queued
          entries (without modifying internal history).
        """
        queued: List[Dict[str, Any]] = []
        while True:
            try:
                entry: dict[str, Any] = self._queue.get_nowait()
                queued.append(entry)
            except Empty:
                break

        if clear:
            return queued
        with self._lock:
            history_copy = list(self._logs)
        return history_copy + queued

    def clear(self) -> None:
        self._queue = Queue(maxsize=self._queue_maxsize)
        with self._lock:
            self._logs = []

    def info(self, message: Any) -> None:
        self.log(message, "INFO")

    def error(self, message: Any) -> None:
        self.log(message, "ERROR")

    def warning(self, message: Any) -> None:
        self.log(message, "WARNING")

    def debug(self, message: Any) -> None:
        self.log(message, "DEBUG")

    def exception(self, message: Any) -> None:
        """Log an exception with traceback at ERROR level.

        This mirrors the common logging.exception API so callers can use
        guilogger.exception("msg") inside except blocks.
        """
        tb = traceback.format_exc()
        full_msg = (
            f"{message}\n{tb}" if tb and tb.strip() != "NoneType: None" else message
        )
        self.log(full_msg, "ERROR")


# --- Audit Logging (background writer with rotation) ---

_audit_log_path: Optional[Path] = None
_audit_queue: Optional[Queue] = None
_audit_thread: Optional[Thread] = None
_audit_stop: Optional[Event] = None
_audit_max_bytes: int = 5 * 1024 * 1024  # 5 MB


def _audit_worker(path: Path, q: Queue, stop_event: Event, max_bytes: int):
    """Background worker that writes audit entries from queue to the file, with simple rotation."""
    try:
        while not stop_event.is_set():
            try:
                entry = q.get(timeout=0.5)
            except Exception:
                # timeout or empty
                continue
            try:
                # ensure file exists
                path.parent.mkdir(parents=True, exist_ok=True)
                s = json.dumps(entry, default=lambda o: str(o)) + "\n"
                with open(path, "a", encoding="utf-8") as f:
                    f.write(s)
                # rotation
                try:
                    size = path.stat().st_size
                    if size > max_bytes:
                        # rotate: move to .1 (overwrite)
                        rot = Path(str(path) + ".1")
                        try:
                            if rot.exists():
                                rot.unlink()
                        except Exception:
                            pass
                        try:
                            path.rename(rot)
                        except Exception:
                            pass
                except Exception:
                    pass
            except Exception:
                # swallow write errors; don't crash worker
                pass
    finally:
        # drain remaining items quickly
        while True:
            try:
                entry = q.get_nowait()
                try:
                    with open(path, "a", encoding="utf-8") as f:
                        f.write(json.dumps(entry, default=lambda o: str(o)) + "\n")
                except Exception:
                    pass
            except Empty:
                break


def _start_audit_worker_if_needed():
    global _audit_queue, _audit_thread, _audit_stop, _audit_log_path
    if _audit_log_path is None:
        return
    if _audit_queue is None:
        _audit_queue = Queue()
    if _audit_stop is None:
        _audit_stop = Event()
    if _audit_thread is None or not _audit_thread.is_alive():
        t = Thread(target=_audit_worker, args=(_audit_log_path, _audit_queue, _audit_stop, _audit_max_bytes), daemon=True)
        _audit_thread = t
        t.start()


def set_audit_log_path(path: Optional[Path]) -> None:
    """Set the path for audit log file and start background writer."""
    global _audit_log_path, _audit_queue, _audit_thread, _audit_stop
    if path is None:
        # stop worker if running
        if _audit_stop is not None:
            _audit_stop.set()
        _audit_log_path = None
        return
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        _audit_log_path = p
        _start_audit_worker_if_needed()
    except Exception:
        _audit_log_path = None


def audit_log(
    event_type: AuditEventType,
    level: AuditLevel,
    message: str,
    details: Optional[dict[str, Any]] = None,
    user: str = "system",
) -> bool:
    """Log a security audit event by enqueueing it for the background writer.

    Returns True if the event was queued or written, False if audit logging not configured.
    """
    global _audit_queue, _audit_log_path
    if _audit_log_path is None or _audit_queue is None:
        return False
    try:
        timestamp = datetime.now().isoformat()
        entry: dict[str, Any] = {
            "timestamp": timestamp,
            "event_type": event_type.value,
            "level": level.value,
            "message": message,
            "user": user,
            "details": details or {},
        }
        try:
            _audit_queue.put_nowait(entry)
            return True
        except Full:
            # queue full, drop audit to avoid blocking
            return False
    except Exception:
        return False


def audit_command_execution(
    command: str, allowed: bool, reason: str, user: str = "system"
) -> None:
    audit_log(
        event_type=AuditEventType.COMMAND_EXECUTION,
        level=AuditLevel.INFO if allowed else AuditLevel.WARNING,
        message=f"Command {'allowed' if allowed else 'blocked'}: {command[:50]}...",
        details={"command": command, "reason": reason},
        user=user,
    )


def audit_file_access(
    file_path: str, operation: str, allowed: bool, user: str = "system"
) -> None:
    event_type = {
        "read": AuditEventType.FILE_ACCESS,
        "write": AuditEventType.FILE_WRITE,
        "delete": AuditEventType.FILE_DELETE,
    }.get(operation.lower(), AuditEventType.FILE_ACCESS)

    audit_log(
        event_type=event_type,
        level=AuditLevel.INFO if allowed else AuditLevel.WARNING,
        message=f"File {operation}: {file_path}",
        details={"file_path": file_path, "operation": operation, "allowed": allowed},
        user=user,
    )


def audit_security_violation(
    violation_type: str, details: str, user: str = "system"
) -> None:
    audit_log(
        event_type=AuditEventType.SECURITY_VIOLATION,
        level=AuditLevel.ERROR,
        message=f"Security violation: {violation_type}",
        details={"violation_type": violation_type, "details": details},
        user=user,
    )


logger = GUILogger()


class _GUILoggingHandler(logging.Handler):
    # Thread-local re-entrancy guard to prevent recursive logging loops.
    # When GUILogger.log() calls standard_logger.log("agent_tui", ...), that record
    # propagates back through the root logger and re-enters this handler, filling
    # the bounded queue until put() blocks forever.
    _local = threading_local()

    def __init__(self):
        super().__init__()
        self.setLevel(logging.DEBUG)

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._local, 'active', False):
            return  # Break recursion: already inside emit on this thread
        self._local.active = True
        try:
            msg = self.format(record)
            level = record.levelname
            # forward to guilogger
            try:
                logger.log(msg, level)
            except Exception:
                # fallback to printing
                print(f"[logger_bridge] {level}: {msg}")
        except Exception:
            pass
        finally:
            self._local.active = False


_installed_handler = False


def install_stdlib_handler(level: int = logging.INFO) -> None:
    """Install a stdlib logging handler that forwards records to the GUILogger.

    Idempotent: calling multiple times will not add duplicate handlers.
    """
    global _installed_handler
    root = logging.getLogger()
    # Check if handler already installed (by attribute)
    for h in list(root.handlers):
        if isinstance(h, _GUILoggingHandler):
            return
    handler = _GUILoggingHandler()
    handler.setLevel(level)
    # optional formatter
    fmt = logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s')
    handler.setFormatter(fmt)
    root.addHandler(handler)


# expose install function at module level
__all__ = [
    "GUILogger",
    "logger",
    "set_audit_log_path",
    "audit_log",
    "audit_file_access",
    "install_stdlib_handler",
    "set_event_bus",
]

