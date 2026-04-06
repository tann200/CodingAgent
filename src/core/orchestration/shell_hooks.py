"""Shell hook runner — CP-7.

Python port of claw-code's ``hooks.rs``.  Executes user-configured shell
commands before and after each tool call, with the same exit-code semantics:

- Exit 0  → allow (tool proceeds / result kept as-is)
- Exit 2  → deny  (pre-hook: block the tool call;
                    post-hook: mark result ``is_error=True``)
- Any other non-zero → warn (log a warning, allow tool to continue)

Hooks are configured in ``{workdir}/.agent/settings.json`` under the key
``"hooks"``::

    {
        "hooks": {
            "PreToolUse":  ["./scripts/pre_hook.sh"],
            "PostToolUse": ["./scripts/post_hook.sh"]
        }
    }

Each command receives a JSON payload on **stdin** and the following env-vars:

    HOOK_EVENT          "PreToolUse" | "PostToolUse"
    HOOK_TOOL_NAME      name of the tool being called
    HOOK_TOOL_INPUT     JSON string of the tool arguments
    HOOK_TOOL_IS_ERROR  "1" | "0"
    HOOK_TOOL_OUTPUT    tool output (PostToolUse only)

The hook command is executed via ``sh -c`` (POSIX) or ``cmd /C`` (Windows).
The hook's stdout is captured as a message and published on the event bus.

Design:
- ``ShellHookRunner`` is drop-in compatible with the existing
  ``ToolExecutionService`` hook interface (``run_pre`` / ``async_run_post``).
- Settings are loaded lazily on first use and cached for the lifetime of the
  runner; call ``reload()`` to invalidate the cache.
- A missing or malformed settings file is treated as "no hooks configured"
  — never raises.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_SETTINGS_FILE = Path(".agent") / "settings.json"

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class HookResult:
    """Outcome of running a single batch of hook commands."""

    denied: bool
    """True when a hook exited with code 2 (deny)."""

    messages: list[str] = field(default_factory=list)
    """Captured stdout lines from all hook commands."""

    @classmethod
    def allow(cls, messages: list[str] | None = None) -> "HookResult":
        return cls(denied=False, messages=messages or [])

    @classmethod
    def deny(cls, messages: list[str] | None = None) -> "HookResult":
        return cls(denied=True, messages=messages or [])


# ---------------------------------------------------------------------------
# Settings loader
# ---------------------------------------------------------------------------


def _load_hooks_config(workdir: str | Path | None = None) -> dict[str, list[str]]:
    """Load the hooks section from ``.agent/settings.json``.

    Returns a dict with keys ``"PreToolUse"`` and ``"PostToolUse"`` (each a
    possibly-empty list of command strings).  Never raises — returns empty
    lists on any error.
    """
    empty: dict[str, list[str]] = {"PreToolUse": [], "PostToolUse": []}
    try:
        base = Path(workdir) if workdir else Path.cwd()
        settings_path = base / _SETTINGS_FILE
        if not settings_path.exists():
            return empty
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        hooks = data.get("hooks", {})
        if not isinstance(hooks, dict):
            return empty
        return {
            "PreToolUse": [str(c) for c in hooks.get("PreToolUse", [])],
            "PostToolUse": [str(c) for c in hooks.get("PostToolUse", [])],
        }
    except Exception as exc:
        logger.debug("shell_hooks: failed to load settings: %s", exc)
        return {"PreToolUse": [], "PostToolUse": []}


# ---------------------------------------------------------------------------
# Command execution
# ---------------------------------------------------------------------------


def _build_payload(
    event: str,
    tool_name: str,
    tool_input: dict[str, Any],
    tool_output: Optional[str],
    is_error: bool,
) -> str:
    """Build the JSON payload sent to hook commands on stdin."""
    return json.dumps(
        {
            "hook_event_name": event,
            "tool_name": tool_name,
            "tool_input": tool_input,
            "tool_input_json": json.dumps(tool_input),
            "tool_output": tool_output,
            "tool_result_is_error": is_error,
        }
    )


def _shell_argv(command: str) -> list[str]:
    """Return the argv to run *command* in a shell, platform-aware."""
    if platform.system() == "Windows":
        return ["cmd", "/C", command]
    return ["sh", "-c", command]


def _run_one_command(
    command: str,
    event: str,
    tool_name: str,
    tool_input: dict[str, Any],
    tool_output: Optional[str],
    is_error: bool,
    payload: str,
) -> tuple[str, str | None]:
    """Run a single hook shell command synchronously.

    Returns ``(outcome, message)`` where *outcome* is ``"allow"``,
    ``"deny"``, or ``"warn"``; *message* is captured stdout / warning text
    or None.
    """
    env = dict(os.environ)
    env["HOOK_EVENT"] = event
    env["HOOK_TOOL_NAME"] = tool_name
    env["HOOK_TOOL_INPUT"] = json.dumps(tool_input)
    env["HOOK_TOOL_IS_ERROR"] = "1" if is_error else "0"
    if tool_output is not None:
        env["HOOK_TOOL_OUTPUT"] = tool_output

    try:
        proc = subprocess.run(
            _shell_argv(command),
            input=payload.encode(),
            capture_output=True,
            timeout=30,
            env=env,
        )
    except Exception as exc:
        warn_msg = f"{event} hook `{command}` failed to start for `{tool_name}`: {exc}"
        logger.warning(warn_msg)
        return "warn", warn_msg

    stdout = proc.stdout.decode(errors="replace").strip()
    stderr = proc.stderr.decode(errors="replace").strip()
    message: str | None = stdout if stdout else None
    code = proc.returncode

    if code == 0:
        return "allow", message
    if code == 2:
        if message is None:
            message = f"{event} hook denied tool `{tool_name}`"
        return "deny", message

    # Any other non-zero exit → warn
    warn_parts = [
        f"Hook `{command}` exited with status {code}; allowing tool execution to continue"
    ]
    if message:
        warn_parts.append(message)
    elif stderr:
        warn_parts.append(stderr)
    return "warn", ": ".join(warn_parts)


def _run_commands(
    event: str,
    commands: list[str],
    tool_name: str,
    tool_input: dict[str, Any],
    tool_output: Optional[str],
    is_error: bool,
) -> HookResult:
    """Run a list of shell hook commands for *event* and aggregate outcomes."""
    if not commands:
        return HookResult.allow()

    payload = _build_payload(event, tool_name, tool_input, tool_output, is_error)
    messages: list[str] = []

    for cmd in commands:
        outcome, msg = _run_one_command(
            cmd, event, tool_name, tool_input, tool_output, is_error, payload
        )
        if msg:
            messages.append(msg)
        if outcome == "deny":
            return HookResult.deny(messages)
        # "allow" and "warn" both continue to the next command

    return HookResult.allow(messages)


# ---------------------------------------------------------------------------
# ShellHookRunner
# ---------------------------------------------------------------------------


class ShellHookRunner:
    """Loads shell hooks from settings and runs them around tool calls.

    Compatible with ``ToolExecutionService`` hook interface:
    - ``run_pre(tool_name, args)`` — synchronous, returns ``HookResult``
    - ``run_post(tool_name, args, result)`` — synchronous, returns ``HookResult``
    - ``async_run_post(tool_name, args, result)`` — async wrapper

    Parameters
    ----------
    workdir:
        Directory that contains the ``.agent/settings.json`` file.
        Defaults to ``Path.cwd()``.
    event_bus:
        Optional event bus to publish ``hook.message`` events when hooks
        produce stdout output.
    """

    def __init__(
        self,
        workdir: str | Path | None = None,
        event_bus: Any = None,
    ) -> None:
        self._workdir = Path(workdir) if workdir else None
        self._event_bus = event_bus
        self._config: dict[str, list[str]] | None = None  # lazy-loaded

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _get_config(self) -> dict[str, list[str]]:
        if self._config is None:
            self._config = _load_hooks_config(self._workdir)
        return self._config

    def reload(self) -> None:
        """Invalidate the cached settings so they are re-read on next use."""
        self._config = None

    # ------------------------------------------------------------------
    # Public hook API (ToolExecutionService interface)
    # ------------------------------------------------------------------

    def run_pre(self, tool_name: str, args: dict[str, Any]) -> HookResult:
        """Run ``PreToolUse`` hooks synchronously.

        Returns a ``HookResult``; ``denied=True`` means the tool should be
        blocked.
        """
        commands = self._get_config().get("PreToolUse", [])
        result = _run_commands("PreToolUse", commands, tool_name, args, None, False)
        self._publish_messages(result.messages, tool_name, "PreToolUse")
        return result

    def run_post(
        self,
        tool_name: str,
        args: dict[str, Any],
        result: dict[str, Any],
    ) -> HookResult:
        """Run ``PostToolUse`` hooks synchronously.

        Returns a ``HookResult``; ``denied=True`` means the tool result should
        be marked ``is_error=True``.
        """
        commands = self._get_config().get("PostToolUse", [])
        tool_output = _extract_tool_output(result)
        is_error = not result.get("ok", True)
        hook_result = _run_commands(
            "PostToolUse", commands, tool_name, args, tool_output, is_error
        )
        self._publish_messages(hook_result.messages, tool_name, "PostToolUse")
        return hook_result

    async def async_run_post(
        self,
        tool_name: str,
        args: dict[str, Any],
        result: dict[str, Any],
    ) -> HookResult:
        """Async wrapper around ``run_post`` — runs in executor to avoid blocking."""
        # LOW-5 fix: get_event_loop() is deprecated in Python 3.10+ when there is
        # no running loop; get_running_loop() is correct inside an async function.
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.run_post, tool_name, args, result)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _publish_messages(
        self, messages: list[str], tool_name: str, event: str
    ) -> None:
        """Publish hook stdout messages to the event bus if available."""
        if not messages or self._event_bus is None:
            return
        try:
            for msg in messages:
                self._event_bus.publish(
                    "hook.message",
                    {"tool_name": tool_name, "event": event, "message": msg},
                )
        except Exception as exc:
            logger.debug("ShellHookRunner: event_bus publish failed: %s", exc)


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _extract_tool_output(result: dict[str, Any]) -> str:
    """Best-effort extraction of a string representation of a tool result."""
    for key in ("output", "result", "content", "error"):
        val = result.get(key)
        if val is not None:
            return str(val)[:4096]
    return json.dumps(result)[:4096]
