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
import hashlib
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


def _trusted_hooks_store_path() -> Path:
    override = os.environ.get("CODINGAGENT_TRUSTED_HOOKS_PATH", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".codingagent" / "trusted_hooks.json"


def compute_hook_sha256(path: Path) -> str:
    """Return the hex SHA-256 digest of a project hook config or script file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_trusted_hook_hashes() -> Dict[str, str]:
    store = _trusted_hooks_store_path()
    if not store.is_file():
        return {}
    try:
        data = json.loads(store.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except Exception:
        pass
    return {}


def approve_project_hook(path: Path) -> str:
    """Record the current SHA-256 digest of *path* in the user's trusted-hooks store."""
    resolved = path.resolve()
    digest = compute_hook_sha256(resolved)
    store = _trusted_hooks_store_path()
    hashes = _load_trusted_hook_hashes()
    hashes[str(resolved)] = digest
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps(hashes, indent=2, sort_keys=True), encoding="utf-8")
    return digest


def is_project_hook_hash_trusted(path: Path) -> bool:
    """Return True iff *path*'s current on-disk SHA-256 matches an explicitly approved hash."""
    if os.environ.get("CODINGAGENT_AUTO_TRUST_HOOKS", "").strip().lower() in ("1", "true", "yes"):
        return True
    try:
        resolved = path.resolve()
        if not resolved.is_file():
            return False
        hashes = _load_trusted_hook_hashes()
        expected = hashes.get(str(resolved))
        if not expected:
            return False
        return compute_hook_sha256(resolved) == expected
    except Exception:
        return False


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
    require_hash_trust: bool = False,
) -> _HookConfig:
    """Merge global hooks.json with workspace .codingAgent/hooks.json.

    Workspace entries and stub scripts are ONLY loaded when the workspace is
    trusted (``trusted=True`` or ``is_trusted()``). When *require_hash_trust*
    is True, each project hook file must also match its approved SHA-256 hash
    in ``~/.codingagent/trusted_hooks.json`` (fail-closed on first encounter or
    tampering).
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

    paths: List[tuple[Optional[Path], bool]] = [(_GLOBAL_HOOKS_PATH, False)]
    if ws_trusted:
        paths.append((_workspace_hooks_path(working_dir), True))

    for path, is_project in paths:
        if path is None or not path.is_file():
            continue
        if is_project and require_hash_trust and not is_project_hook_hash_trusted(path):
            logger.warning(
                "tool_hooks: skipping unapproved or modified project hook config %s "
                "(approve SHA-256 digest before execution)",
                path,
            )
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
        try:
            from src.tools.tools_config import agent_context_path

            stub_dir = agent_context_path(working_dir) / "hooks"
        except Exception:
            import os as _os

            ctx_name = _os.getenv("CODINGAGENT_CONTEXT_DIR") or ".codingAgent"
            stub_dir = working_dir / ctx_name / "hooks"
        _sh_map = (
            ("pre_tool", stub_dir / "pre_tool.sh"),
            ("post_tool", stub_dir / "post_tool.sh"),
        )
        for key, stub_path in _sh_map:
            if not entries[key] and stub_path.is_file():
                if require_hash_trust and not is_project_hook_hash_trusted(stub_path):
                    logger.warning(
                        "tool_hooks: skipping unapproved or modified project hook script %s",
                        stub_path,
                    )
                    continue
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
        require_hash_trust: bool = False,
    ) -> None:
        self._working_dir = working_dir
        self._trusted = trusted
        self._require_hash_trust = require_hash_trust
        self._config: Optional[_HookConfig] = None

    def _get_config(self) -> _HookConfig:
        if self._config is None:
            self._config = _load_hook_config(
                self._working_dir,
                trusted=self._trusted,
                require_hash_trust=self._require_hash_trust,
            )
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
                    "tool_hooks: pre-hook timed out for %r — failing closed", tool_name
                )
                return HookResult(
                    allowed=False,
                    reason=f"Pre-tool hook timed out after {_PRE_HOOK_TIMEOUT}s (fail-closed)",
                )
            except Exception as exc:
                logger.warning("tool_hooks: pre-hook error for %r — failing closed: %s", tool_name, exc)
                return HookResult(
                    allowed=False,
                    reason=f"Pre-tool hook execution error (fail-closed): {exc}",
                )

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
