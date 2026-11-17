"""Pre/post tool call hooks.

Users can create ``.codingAgent/hooks.json`` (or the global hooks file returned by
``src.core.paths.get_hooks_path()`` for global defaults) to configure shell
commands that run before and after tool
calls.

Configuration format::

    {
        "pre_tool": [
            {"match": "*",         "cmd": "echo pre $TOOL_NAME"},
            {"match": "bash",      "cmd": "/usr/local/bin/bash-guard.sh"}
        ],
        "post_tool": [
            {"match": "write_file", "cmd": "lint-on-write.sh $TOOL_NAME"}
        ]
    }

``match`` is a glob pattern matched against the tool name (``fnmatch``).
``cmd`` is executed in a subprocess with the following environment variables:

    TOOL_NAME      — the tool being called
    TOOL_ARGS_JSON — JSON-encoded arguments dict
    TOOL_RESULT_JSON — (post-tool only) JSON-encoded result dict

Pre-tool hooks that exit with a **non-zero** exit code *deny* the tool call.
The hook's ``stderr`` (first 500 chars) is returned as the denial reason.

Post-tool hooks run fire-and-forget with a 5-second timeout; their exit code
is ignored.

Hook execution is entirely optional.  If no hook config exists the system adds
zero overhead.  All errors are silently swallowed so a broken hook never
prevents a tool from running (except via intentional non-zero exit).
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core.paths import get_hooks_path

logger = logging.getLogger(__name__)

_PRE_HOOK_TIMEOUT = 10  # seconds; longer because guard scripts may need I/O
_POST_HOOK_TIMEOUT = 5
_STDERR_CAP = 500  # chars from hook stderr reported back to caller

_GLOBAL_HOOKS_PATH = get_hooks_path()


@dataclass
class HookResult:
    allowed: bool = True
    reason: str = ""


@dataclass
class _HookConfig:
    pre_tool: List[Dict[str, str]] = field(default_factory=list)
    post_tool: List[Dict[str, str]] = field(default_factory=list)


def _load_hook_config(
    working_dir: Optional[Path],
    *,
    trusted: Optional[bool] = None,
) -> _HookConfig:
    """Merge global hooks.json with workspace .codingAgent/hooks.json.

    Workspace entries and stub scripts are ONLY loaded when the workspace is
    trusted (``trusted=True`` or ``is_trusted()``). Untrusted repositories must
    never execute repo-controlled hook commands automatically.
    """
    entries: Dict[str, List] = {"pre_tool": [], "post_tool": []}
    if trusted is None:
        try:
            from src.core.orchestration.deferred_init import is_trusted

            ws_trusted = is_trusted()
        except Exception:
            ws_trusted = False
    else:
        ws_trusted = bool(trusted)

    paths: List[Optional[Path]] = [_GLOBAL_HOOKS_PATH]
    if ws_trusted:
        paths.append(_workspace_hooks_path(working_dir))

    for path in paths:
        if path is None or not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for key in ("pre_tool", "post_tool"):
                for entry in data.get(key, []):
                    if isinstance(entry, dict) and "cmd" in entry:
                        entries[key].append(entry)
        except Exception as exc:
            logger.debug("tool_hooks: failed to load %s: %s", path, exc)

    # Auto-detect .codingAgent/hooks/pre_tool.sh / post_tool.sh stubs ONLY in trusted workspaces.
    if ws_trusted and working_dir is not None:
        # Resolve the configured agent-context directory using the central
        # helper when available; fall back to the legacy location if needed.
        try:
            from src.tools.tools_config import agent_context_path

            stub_dir = agent_context_path(working_dir) / "hooks"
        except Exception:
            # Fallback to legacy logic: configured ctx name env var or configured
            # default. Use .agent-context as a safe legacy fallback when tools_config
            # is not importable.
            import os as _os

            ctx_name = _os.getenv("CODINGAGENT_CONTEXT_DIR") or ".codingAgent"
            stub_dir = working_dir / ctx_name / "hooks"
        _sh_map = (
            ("pre_tool", stub_dir / "pre_tool.sh"),
            ("post_tool", stub_dir / "post_tool.sh"),
        )
        for key, stub_path in _sh_map:
            if not entries[key] and stub_path.is_file():
                entries[key].append({"match": "*", "cmd": str(stub_path)})
                logger.debug("tool_hooks: auto-wired stub %s", stub_path)

    return _HookConfig(pre_tool=entries["pre_tool"], post_tool=entries["post_tool"])


def _workspace_hooks_path(working_dir: Optional[Path]) -> Optional[Path]:
    if working_dir is None:
        return None
    try:
        from src.tools.tools_config import get_context_dir_name

        ctx = get_context_dir_name()
    except Exception:
        import os as _os

        ctx = _os.getenv("CODINGAGENT_CONTEXT_DIR") or ".codingAgent"

    candidate = Path(working_dir) / ctx / "hooks.json"
    if candidate.exists():
        return candidate
    return candidate


def _matches(pattern: str, tool_name: str) -> bool:
    return fnmatch.fnmatch(tool_name, pattern) or pattern == "*"


_SECRET_ENV_KEY_TOKENS = (
    "API_KEY",
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "PASSWD",
    "PRIVATE_KEY",
    "CREDENTIAL",
    "AUTH",
)


def _scrub_secret_env(env: Dict[str, str]) -> Dict[str, str]:
    """Strip likely secret/credential environment variables before spawning hooks."""
    cleaned: Dict[str, str] = {}
    for k, v in env.items():
        upper_k = k.upper()
        if any(tok in upper_k for tok in _SECRET_ENV_KEY_TOKENS):
            continue
        cleaned[k] = v
    return cleaned


def _build_env(tool_name: str, args: Dict[str, Any], result: Any = None) -> dict:
    # Copy the current process environment for subprocess execution while
    # scrubbing delegation depth keys and secret/credential env vars.
    env = _scrub_secret_env(os.environ.copy())
    env.pop("CODINGAGENT_DELEGATION_DEPTH", None)
    env.pop("AGENT_DELEGATION_DEPTH", None)
    env["TOOL_NAME"] = tool_name

    try:
        from src.core.security.redaction import redact_data

        safe_args = redact_data(args)
        safe_result = redact_data(result) if result is not None else None
    except Exception:
        safe_args = {}
        safe_result = {} if result is not None else None

    try:
        env["TOOL_ARGS_JSON"] = json.dumps(safe_args)
    except Exception:
        env["TOOL_ARGS_JSON"] = "{}"
    if result is not None:
        try:
            env["TOOL_RESULT_JSON"] = json.dumps(safe_result)
        except Exception:
            env["TOOL_RESULT_JSON"] = "{}"
    return env


class ToolHookRunner:
    """Runs configured pre/post tool hooks for a given working directory.

    Instances are cheap to create — they load (and cache) hook config lazily.
    """

    def __init__(
        self,
        working_dir: Optional[Path] = None,
        trusted: Optional[bool] = None,
    ) -> None:
        self._working_dir = working_dir
        self._trusted = trusted
        self._config: Optional[_HookConfig] = None

    def _get_config(self) -> _HookConfig:
        if self._config is None:
            self._config = _load_hook_config(self._working_dir, trusted=self._trusted)
        return self._config

    def reload(self) -> None:
        """Force a re-read of the hook config files."""
        self._config = None

    def run_pre(self, tool_name: str, args: Dict[str, Any]) -> HookResult:
        """Run all matching pre-tool hooks.

        Returns ``HookResult(allowed=False, reason=...)`` if any hook exits
        non-zero; returns ``HookResult(allowed=True)`` otherwise.
        """
        cfg = self._get_config()
        if not cfg.pre_tool:
            return HookResult()

        env = _build_env(tool_name, args)
        for entry in cfg.pre_tool:
            pattern = entry.get("match", "*")
            cmd = entry.get("cmd", "")
            if not cmd or not _matches(pattern, tool_name):
                continue
            try:
                import os as _os

                _shell_path = "/bin/sh" if _os.name != "nt" else "cmd"
                proc = subprocess.run(
                    [_shell_path, "-c", cmd],
                    capture_output=True,
                    text=True,
                    timeout=_PRE_HOOK_TIMEOUT,
                    env=env,
                    cwd=str(self._working_dir) if self._working_dir else None,
                )
                if proc.returncode != 0:
                    reason = (proc.stderr or proc.stdout or "hook denied call")[
                        :_STDERR_CAP
                    ]
                    logger.info(
                        "tool_hooks: pre-hook denied %r (exit %d): %s",
                        tool_name,
                        proc.returncode,
                        reason,
                    )
                    return HookResult(allowed=False, reason=reason.strip())
            except subprocess.TimeoutExpired:
                logger.warning(
                    "tool_hooks: pre-hook timed out for %r, allowing", tool_name
                )
            except Exception as exc:
                logger.debug("tool_hooks: pre-hook error for %r: %s", tool_name, exc)

        return HookResult()

    def run_post(
        self, tool_name: str, args: Dict[str, Any], result: Any = None
    ) -> None:
        """Run all matching post-tool hooks (fire and forget, errors ignored)."""
        cfg = self._get_config()
        if not cfg.post_tool:
            return

        env = _build_env(tool_name, args, result)
        for entry in cfg.post_tool:
            pattern = entry.get("match", "*")
            cmd = entry.get("cmd", "")
            if not cmd or not _matches(pattern, tool_name):
                continue
            try:
                import os as _os

                _shell_path = "/bin/sh" if _os.name != "nt" else "cmd"
                subprocess.run(
                    [_shell_path, "-c", cmd],
                    capture_output=True,
                    text=True,
                    timeout=_POST_HOOK_TIMEOUT,
                    env=env,
                    cwd=str(self._working_dir) if self._working_dir else None,
                )
            except Exception:
                pass

    async def async_run_post(
        self, tool_name: str, args: Dict[str, Any], result: Any = None
    ) -> None:
        """TASK-13: Async variant of run_post using asyncio.create_subprocess_exec.

        Fire-and-forget: errors are silently swallowed so a broken post-hook
        never prevents the caller from proceeding.  Each hook command runs in
        its own shell process with a _POST_HOOK_TIMEOUT second deadline.
        """
        cfg = self._get_config()
        if not cfg.post_tool:
            return

        env = _build_env(tool_name, args, result)
        cwd = str(self._working_dir) if self._working_dir else None
        for entry in cfg.post_tool:
            pattern = entry.get("match", "*")
            cmd = entry.get("cmd", "")
            if not cmd or not _matches(pattern, tool_name):
                continue
            # Use cross-platform shell detection (sh on Unix, cmd on Windows)
            import os

            shell = "/bin/sh" if os.name != "nt" else "cmd"
            try:
                proc = await asyncio.create_subprocess_exec(
                    shell,
                    "-c",
                    cmd,
                    env=env,
                    cwd=cwd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                try:
                    await asyncio.wait_for(proc.wait(), timeout=_POST_HOOK_TIMEOUT)
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            except Exception:
                pass
