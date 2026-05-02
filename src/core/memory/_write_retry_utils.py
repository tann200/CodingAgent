"""Utility functions for retrying file writes with exponential backoff."""

import json
import os
import shutil
import tempfile
import time
import traceback
from typing import Any, Callable, Optional

import logging

logger = logging.getLogger(__name__)


def write_with_retry(
    write_func: Callable[[], Any],
    max_attempts: int = 5,
    base_delay: float = 0.1,
    max_delay: float = 2.0,
    context_msg: str = "",
) -> Any:
    """
    Execute a write function with exponential backoff retry logic.

    Args:
        write_func: Function that performs the write operation (should return result)
        max_attempts: Maximum number of retry attempts
        base_delay: Base delay in seconds for exponential backoff
        max_delay: Maximum delay in seconds
        context_msg: Additional context for error messages

    Returns:
        Result from write_func

    Raises:
        Exception: Last exception if all attempts fail
    """
    last_exception = None

    for attempt in range(max_attempts):
        try:
            return write_func()
        except Exception as e:
            last_exception = e
            if attempt == max_attempts - 1:  # Last attempt
                logger.error(
                    "%sWrite operation failed after %d attempts: %s",
                    f"{context_msg} " if context_msg else "",
                    max_attempts,
                    str(e),
                )
                logger.debug("Full traceback:\n%s", traceback.format_exc())
                raise

            # Calculate delay with exponential backoff and jitter
            delay = min(base_delay * (2**attempt), max_delay)
            # Add jitter to prevent thundering herd
            delay *= 0.5 + (time.time() % 1.0) * 0.5

            logger.warning(
                "%sWrite attempt %d/%d failed: %s. Retrying in %.2fs...",
                f"{context_msg} " if context_msg else "",
                attempt + 1,
                max_attempts,
                str(e),
                delay,
            )
            time.sleep(delay)

    # This should never be reached due to the raise in the loop, but just in case
    raise last_exception


def atomic_write_json(
    filepath: str | os.PathLike, data: Any, logger_obj: Optional[logging.Logger] = None
) -> bool:
    """
    Write JSON data to file atomically using tempfile.mkstemp and os.replace.

    Args:
        filepath: Target file path
        data: Data to serialize as JSON
        logger_obj: Optional logger for debug messages

    Returns:
        True if successful, False if atomic_write_json is not available
    """
    log = logger_obj or logger

    try:
        # Try to use the central atomic write utility if available
        try:
            from src.core.io_utils import atomic_write_json as central_atomic_write

            ok = central_atomic_write(filepath, data, logger=log)
            if ok:
                return True
            log.warning(
                "atomic_write_json returned False for %s; falling back",
                filepath,
            )
        except Exception:
            log.debug(
                "atomic_write_json unavailable or failed for %s; falling back\n%s",
                filepath,
                traceback.format_exc(),
            )

        # Fallback to manual atomic write
        filepath = str(filepath)
        directory = os.path.dirname(filepath)
        if directory:
            os.makedirs(directory, exist_ok=True)

        fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
            except Exception:
                try:
                    os.close(fd)
                except Exception:
                    pass
                raise

            try:
                os.replace(tmp_path, filepath)
            except Exception:
                try:
                    shutil.move(tmp_path, filepath)
                except Exception:
                    log.debug(
                        "mkstemp fallback failed for %s; final fallback to write_text\n%s",
                        filepath,
                        traceback.format_exc(),
                    )
                    try:
                        with open(filepath, "w", encoding="utf-8") as f:
                            json.dump(data, f, indent=2)
                    except Exception:
                        log.exception(
                            "failed to write JSON to %s",
                            filepath,
                        )
                        raise
            return True
        except Exception as e:
            log.error("failed to write JSON to %s: %s", filepath, e)
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except Exception:
                pass
            raise

    except Exception:
        return False
