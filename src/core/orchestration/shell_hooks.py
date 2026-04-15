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
import fnmatch
import json
import logging
import os
import platform
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence, Union

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


def _normalise_hook_entry(entry: Any) -> dict[str, str] | None:
    """Normalise a raw hook config entry to ``{"matcher": str, "command": str}``.

    Accepts:
    - Plain string — command applies to all tools (matcher ``"*"``).
    - Dict with ``"command"`` key — optional ``"matcher"`` key (defaults to ``"*"``).

    Returns None for malformed entries so callers can skip them.
    """
    if isinstance(entry, str):
        cmd = entry.strip()
        return {"matcher": "*", "command": cmd} if cmd else None
    if isinstance(entry, dict):
        cmd = str(entry.get("command", "")).strip()
        if not cmd:
            logger.debug(
                "shell_hooks: hook entry missing 'command'; skipping: %r", entry
            )
            return None
        matcher = str(entry.get("matcher", "*")).strip() or "*"
        return {"matcher": matcher, "command": cmd}
    logger.debug("shell_hooks: unrecognised hook entry type %r; skipping", type(entry))
    return None


# TASK-8c: config now stores normalised dicts; key "matcher" filters by tool name.
def _load_hooks_config(
    workdir: str | Path | None = None,
) -> dict[str, list[Union[str, dict[str, str]]]]:
    """Load the hooks section from ``.agent/settings.json``.

    Returns a dict with keys ``"PreToolUse"`` and ``"PostToolUse"``.  Each
    value is a list of normalised hook entries (dicts with ``"matcher"`` and
    ``"command"`` keys).  Never raises — returns empty lists on any error.

    Hook entries support an optional ``"matcher"`` field (fnmatch pattern that
    must match the tool name for the hook to run).  Plain string entries are
    treated as ``"matcher": "*"`` (run for every tool):

    .. code-block:: json

        {
          "hooks": {
            "PreToolUse": [
              "./scripts/pre_all.sh",
              {"matcher": "bash", "command": "./scripts/pre_bash_only.sh"}
            ]
          }
        }
    """
    empty: dict[str, list[Union[str, dict[str, str]]]] = {
        "PreToolUse": [],
        "PostToolUse": [],
    }
    try:
        base = Path(workdir) if workdir else Path.cwd()
        settings_path = base / _SETTINGS_FILE
        if not settings_path.exists():
            return empty
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        hooks = data.get("hooks", {})
        if not isinstance(hooks, dict):
            return empty
        result: dict[str, list[Union[str, dict[str, str]]]] = {
            "PreToolUse": [],
            "PostToolUse": [],
        }
        for event_key in ("PreToolUse", "PostToolUse"):
            for raw in hooks.get(event_key, []):
                normalised = _normalise_hook_entry(raw)
                if normalised is not None:
                    result[event_key].append(normalised)
        return result
    except Exception as exc:
        logger.debug("shell_hooks: failed to load settings: %s", exc)
        return {"PreToolUse": [], "PostToolUse": []}


def _matching_commands(
    entries: Sequence[Optional[Union[str, dict[str, str]]]], tool_name: str
) -> list[str]:
    """Return the commands from *entries* whose matcher pattern matches *tool_name*.

    Accept both a legacy list[str] (each string is a command that applies to all
    tools) and the newer list[dict] form where each dict contains "matcher" and
    "command" keys. This keeps the loader and tests interoperable.
    """
    commands: list[str] = []
    lname = tool_name.lower()
    for e in entries:
        # Tests (and some legacy configs) may include None entries — skip them.
        if e is None:
            continue
        if isinstance(e, str):
            cmd = e.strip()
            if cmd:
                commands.append(cmd)
        elif isinstance(e, dict):
            matcher = str(e.get("matcher", "*")).lower()
            cmd = e.get("command")
            if not isinstance(cmd, str):
                continue
            if fnmatch.fnmatch(lname, matcher):
                commands.append(cmd)
    return commands


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
        # _config stores the normalised hook entries. Historically callers
        # sometimes passed plain list[str] values; accept both list[str]
        # and list[dict[str,str]] forms to remain backward-compatible.
        self._config: dict[str, list[Union[str, dict[str, str]]]] | None = (
            None  # lazy-loaded
        )

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _get_config(self) -> dict[str, list[Union[str, dict[str, str]]]]:
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
        blocked.  Only hooks whose ``matcher`` pattern matches *tool_name* are
        executed (TASK-8c).
        """
        entries = self._get_config().get("PreToolUse", [])
        commands = _matching_commands(entries, tool_name)
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
        be marked ``is_error=True``.  Only hooks whose ``matcher`` pattern
        matches *tool_name* are executed (TASK-8c).
        """
        entries = self._get_config().get("PostToolUse", [])
        commands = _matching_commands(entries, tool_name)
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
        try:
            # Prefer the run_with_correlation helper so ContextVars (eg. correlation
            # id) are propagated into the worker thread where hooks run.
            from src.core.orchestration.event_bus import run_with_correlation

            from typing import cast as _cast

            return _cast(
                HookResult,
                await run_with_correlation(
                    loop, None, self.run_post, tool_name, args, result
                ),
            )
        except Exception:
            # Fallback: copy the current context and run the call inside it so
            # ContextVars (eg. correlation id) propagate into the hook thread.
            try:
                import contextvars as _contextvars

                _ctx = _contextvars.copy_context()

                from typing import cast as _cast

                def _call_run_post() -> HookResult:
                    # ctx.run is untyped; cast the result to HookResult to satisfy
                    # static type checkers. At runtime this simply returns the value.
                    return _cast(
                        HookResult, _ctx.run(self.run_post, tool_name, args, result)
                    )

                return await loop.run_in_executor(None, _call_run_post)
            except Exception:
                # Last-resort: direct run_in_executor without context copy
                return await loop.run_in_executor(
                    None, self.run_post, tool_name, args, result
                )

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
