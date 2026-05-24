import logging
from collections import deque
from pathlib import Path
from typing import Callable, Optional


# Cross-platform log directory — prefer core.paths.get_data_dir() when available
def _get_log_dir() -> Path:
    # Prefer the TUI core-paths loader which handles the src shadowing.
    # Import locally so importing this module does not perform package-relative
    # imports at module-import time (conftest aliases may register this module
    # under a different name before packages exist).  The setup_logging() code
    # below calls this lazily.
    from ._core_paths_loader import get_log_dir as _get_log_dir_helper

    return _get_log_dir_helper()


# NOTE: do not call _get_log_dir() at import time. LOG_DIR/LOG_FILE are set
# lazily by setup_logging() to avoid import-time side effects when tests
# alias tui modules as src.ui.* in conftest.
LOG_DIR: Optional[Path] = None
LOG_FILE: Optional[Path] = None

MAX_BUFFER_LINES = 500


class InMemoryHandler(logging.Handler):
    def __init__(self, maxlen: int = MAX_BUFFER_LINES):
        super().__init__()
        self.buffer: deque[str] = deque(maxlen=maxlen)
        self._callbacks: list[Callable[[str], None]] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self.buffer.append(msg)
            for cb in list(self._callbacks):
                try:
                    cb(msg)
                except Exception:
                    pass
        except Exception:
            self.handleError(record)

    def register_callback(self, cb: Callable[[str], None]) -> None:
        self._callbacks.append(cb)

    def unregister_callback(self, cb: Callable[[str], None]) -> None:
        try:
            self._callbacks.remove(cb)
        except ValueError:
            pass

    def get_lines(self) -> list[str]:
        return list(self.buffer)


_memory_handler: Optional[InMemoryHandler] = None
_logger: Optional[logging.Logger] = None


def _level_style(levelname: str) -> str:
    return {
        "DEBUG": "console_debug",
        "INFO": "console_info",
        "WARNING": "console_warning",
        "ERROR": "console_error",
        "CRITICAL": "console_error",
    }.get(levelname, "console_line")


def setup_logging(level: int = logging.DEBUG) -> logging.Logger:
    global _memory_handler, _logger
    global LOG_DIR, LOG_FILE, _memory_handler, _logger

    if _logger is not None:
        return _logger

    _logger = logging.getLogger("agent_tui")
    _logger.setLevel(level)
    _logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Ensure the log directory exists; compute lazily.
    try:
        log_dir = _get_log_dir()
    except Exception:
        # Fall back to legacy TUI location if the loader cannot be used.
        log_dir = Path.home() / ".agent_tui"
    log_dir.mkdir(parents=True, exist_ok=True)
    LOG_DIR = log_dir
    LOG_FILE = LOG_DIR / "agent.log"

    file_handler = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    _logger.addHandler(file_handler)

    _memory_handler = InMemoryHandler(maxlen=MAX_BUFFER_LINES)
    _memory_handler.setLevel(logging.DEBUG)
    _memory_handler.setFormatter(fmt)
    _logger.addHandler(_memory_handler)

    return _logger


def get_logger(name: str = "") -> logging.Logger:
    parent = setup_logging()
    if name:
        return parent.getChild(name)
    return parent


def get_memory_handler() -> InMemoryHandler:
    setup_logging()
    # LOW-14 fix: assert is stripped by python -O; use an explicit RuntimeError
    # so the caller always gets a meaningful message even in optimised mode.
    if _memory_handler is None:
        raise RuntimeError(
            "get_memory_handler() called before setup_logging() completed"
        )
    return _memory_handler
