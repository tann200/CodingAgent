"""tool_execution_service.py — D-10: Extracted tool execution service.

Owns the pre-execution checks and hook invocations that were previously
inlined in ``Orchestrator.execute_tool()``.  ``Orchestrator`` holds one
instance as ``self.tool_execution_service`` and calls through it for
permission checking and hook dispatch.

Pre-execution checks (in order):
0. Idempotency / duplicate-call guard
1. Tool name normalisation (aliases)
2. ``user_approved`` strip
3. PermissionLevel gate (DANGER / PROMPT tools)
4. ``--permission-mode`` mode enforcement
5. Explore-mode guard (analyst-only tools)
6. Plan-mode write gate
7. Pre-tool hook

Post-execution:
1. Post-tool hook (async)

Usage::

    service = ToolExecutionService(
        registry=orch.tool_registry,
        event_bus=orch.event_bus,
        hook_runner=orch.hook_runner,
    )
    verdict = service.pre_execute(name, args, orchestrator=orch)
    if verdict.blocked:
        return verdict.result
    result = dispatch_tool(...)
    await service.post_execute_async(name, args, result)
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from src.core.orchestration.tool_constants import PERM_ORDER as _PERM_ORDER

logger = logging.getLogger(__name__)

# TS-4: Transient error keywords that warrant an automatic retry.
_TRANSIENT_ERROR_KEYWORDS = (
    "timeout",
    "connection",
    "temporarily unavailable",
    "resource busy",
    "locked",
    "try again",
)


async def execute_with_retry(
    tool_fn: Callable[..., Dict[str, Any]],
    args: Dict[str, Any],
    *,
    max_attempts: int = 3,
    base_delay: float = 0.5,
) -> Dict[str, Any]:
    """TS-4: Execute *tool_fn* with exponential backoff on transient errors.

    Retries up to *max_attempts* times when the result dict contains a transient
    error keyword in its ``error`` field.  Delays are ``base_delay * 2**attempt``
    seconds (0.5 s, 1 s, 2 s by default).  Always returns the last result even if
    all retries were exhausted.

    Parameters
    ----------
    tool_fn:
        Synchronous callable that accepts ``**args`` and returns a dict.
    args:
        Arguments forwarded to *tool_fn*.
    max_attempts:
        Maximum number of attempts (default 3).
    base_delay:
        Base sleep time in seconds; doubled per retry (default 0.5 s).
    """
    result: Dict[str, Any] = {}
    for attempt in range(max_attempts):
        try:
            result = tool_fn(**args)
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
        err_str = (
            (result.get("error") or "").lower() if isinstance(result, dict) else ""
        )
        is_transient = any(kw in err_str for kw in _TRANSIENT_ERROR_KEYWORDS)
        if not is_transient:
            return result
        if attempt < max_attempts - 1:
            delay = base_delay * (2**attempt)
            logger.debug(
                "execute_with_retry: transient error on attempt %d/%d, "
                "retrying in %.1fs — error=%r",
                attempt + 1,
                max_attempts,
                delay,
                err_str[:120],
            )
            await asyncio.sleep(delay)
    return result


@dataclass
class ExecutionVerdict:
    """Result of the pre-execution check."""

    blocked: bool
    """True when the tool call should be rejected before dispatch."""

    result: Optional[Dict[str, Any]] = None
    """The rejection dict to return when ``blocked=True``."""

    idempotent_skip: bool = False
    """True when the call was skipped due to idempotency (already done)."""


class ToolExecutionService:
    """Handles permission checks and hook dispatch for tool calls.

    Parameters
    ----------
    registry:
        ``ToolRegistry`` instance used to look up tool metadata.
    event_bus:
        ``EventBus`` used to publish ``tool.permission_required`` events.
    hook_runner:
        ``ToolHookRunner`` (or any object with ``run_pre`` / ``async_run_post``
        methods) — optional; hooks are skipped when ``None``.
    """

    def __init__(
        self,
        registry: Any,
        event_bus: Optional[Any] = None,
        hook_runner: Optional[Any] = None,
    ) -> None:
        self._registry = registry
        self._event_bus = event_bus
        self._hook_runner = hook_runner
        # idempotency: set of (name, frozenset(args.items())) processed in this turn
        self._seen_calls: set = set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def pre_execute(
        self,
        name: str,
        args: Dict[str, Any],
        orchestrator: Optional[Any] = None,
    ) -> ExecutionVerdict:
        """Run all pre-execution checks.

        Returns an ``ExecutionVerdict``.  When ``verdict.blocked`` is True
        the caller must return ``verdict.result`` without dispatching the tool.
        """
        # 0. Idempotency guard — skip exact duplicate calls within a single turn.
        _call_key = (name, frozenset((k, str(v)) for k, v in args.items()))
        if _call_key in self._seen_calls:
            logger.debug(
                "ToolExecutionService: duplicate call skipped: %s %s", name, args
            )
            return ExecutionVerdict(
                blocked=True,
                result={"ok": False, "error": "Duplicate tool call skipped."},
            )
        # NOTE: _seen_calls.add is deferred to after all checks pass so that a
        # denied call does not permanently block retries (e.g. after the user
        # grants approval on a second attempt).

        # 1. Alias normalisation
        try:
            from src.tools.tools_config import TOOL_ALIASES

            name = TOOL_ALIASES.get(name, name)
        except Exception:
            pass

        # 2. Strip LLM-injected user_approved (prevents WorkspaceGuard bypass).
        # SEC-4: Strip from the original dict so any code path that re-reads the
        # caller's args after pre_execute() returns also sees the cleaned version.
        args.pop("user_approved", None)
        args = dict(args)  # defensive copy for local mutation only

        # 3. PermissionLevel gate
        verdict = await self._check_permission_gate(name, args)
        if verdict.blocked:
            return verdict

        # 4. --permission-mode enforcement
        verdict = self._check_permission_mode(name)
        if verdict.blocked:
            return verdict

        # 5. Explore-mode guard
        verdict = self._check_explore_mode(name, orchestrator)
        if verdict.blocked:
            return verdict

        # 6. Plan-mode write gate
        verdict = self._check_plan_mode(name, orchestrator)
        if verdict.blocked:
            return verdict

        # 7. Pre-tool hook — exit 2 from a shell hook denies the call (CP-7)
        hook_verdict = self._run_pre_hook(name, args)
        if hook_verdict.blocked:
            deny_msg = (
                hook_verdict.result.get("error", f"Pre-tool hook denied '{name}'")
                if hook_verdict.result
                else f"Pre-tool hook denied '{name}'"
            )
            return ExecutionVerdict(
                blocked=True,
                result={"ok": False, "error": deny_msg},
            )

        # All checks passed — register this call as seen so true duplicates are
        # caught on subsequent invocations within the same turn.
        self._seen_calls.add(_call_key)

        return ExecutionVerdict(blocked=False)

    async def execute(
        self, name: str, args: Dict[str, Any], *, working_dir: Optional[str] = None
    ) -> Any:
        """Dispatch the tool call to the underlying registry/executor.

        This helper is provided so tests can treat ToolExecutionService as a
        ToolExecutorProtocol implementation (it proxies to a registry that may
        expose either a synchronous ``execute_tool`` or an async ``execute``).
        """
        # Prefer an async execute if present
        if hasattr(self._registry, "execute"):
            maybe = getattr(self._registry, "execute")
            if asyncio.iscoroutinefunction(maybe):
                return await maybe(name, args, working_dir=working_dir)
            # else fall through to call synchronously in thread

        # Synchronous execution: run in thread pool to avoid blocking the caller
        def _call() -> Any:
            if hasattr(self._registry, "execute_tool"):
                return getattr(self._registry, "execute_tool")(name, args)
            if hasattr(self._registry, "execute"):
                return getattr(self._registry, "execute")(name, args)
            raise RuntimeError("No executable method found on registry")

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _call)

    async def post_execute_async(
        self,
        name: str,
        args: Dict[str, Any],
        result: Dict[str, Any],
    ) -> None:
        """Run post-execution hook asynchronously.

        When the hook runner returns a denied result (exit 2), the tool
        result dict is mutated in-place: ``ok=False`` and ``is_error=True``
        are set so downstream code treats the output as an error (CP-7).
        Never raises.
        """
        if self._hook_runner is None:
            return
        try:
            hook_result = None
            if hasattr(self._hook_runner, "async_run_post"):
                hook_result = await self._hook_runner.async_run_post(name, args, result)
            elif hasattr(self._hook_runner, "run_post"):
                hook_result = self._hook_runner.run_post(name, args, result)
            # CP-7: if post-hook denied, mark result as an error
            if hook_result is not None and getattr(hook_result, "denied", False):
                result["ok"] = False
                result["is_error"] = True
                messages = getattr(hook_result, "messages", [])
                if messages:
                    result["error"] = messages[0]
                logger.info(
                    "ToolExecutionService.post_execute_async: post-hook denied '%s'",
                    name,
                )
        except Exception as exc:
            logger.debug("ToolExecutionService.post_execute_async: error: %s", exc)

    def reset_idempotency(self) -> None:
        """Clear the seen-calls set at the start of each agent turn."""
        self._seen_calls.clear()

    # ------------------------------------------------------------------
    # Internal checks (each returns an ExecutionVerdict)
    # ------------------------------------------------------------------

    async def _check_permission_gate(
        self, name: str, args: Dict[str, Any]
    ) -> ExecutionVerdict:
        """Gate DANGER / PROMPT tools behind user approval."""
        try:
            from src.tools.tools_config import (
                get_tool_permission,
                is_autonomous,
                PermissionLevel,
            )
            from src.core.orchestration.approval_gate import (
                register_tool_gate,
                is_tool_denied,
                discard_tool_denied,
            )

            _perm = get_tool_permission(name)
            needs_gate = _perm in (PermissionLevel.DANGER, PermissionLevel.PROMPT)
            if not needs_gate:
                return ExecutionVerdict(blocked=False)

            if is_autonomous():
                return ExecutionVerdict(blocked=False)

            _t4_id = uuid.uuid4().hex[:8]
            _t4_ev = register_tool_gate(_t4_id)
            try:
                if self._event_bus is not None and hasattr(self._event_bus, "publish"):
                    self._event_bus.publish(
                        "tool.permission_required",
                        {"tool": name, "args": args, "tool_id": _t4_id},
                    )
            except Exception:
                pass
            granted = await _t4_ev.wait_async(timeout=120.0)
            if not granted or is_tool_denied(_t4_id):
                try:
                    discard_tool_denied(_t4_id)
                except Exception:
                    pass
                return ExecutionVerdict(
                    blocked=True,
                    result={
                        "ok": False,
                        "error": f"Tool '{name}' was denied by the user.",
                    },
                )
        except Exception as exc:
            logger.debug("ToolExecutionService._check_permission_gate: %s", exc)
        return ExecutionVerdict(blocked=False)

    @staticmethod
    def _check_permission_mode(name: str) -> ExecutionVerdict:
        """Enforce --permission-mode restriction."""
        try:
            from src.tools.tools_config import (
                get_active_permission_mode,
                get_tool_permission,
            )

            _active_mode = get_active_permission_mode()
            if _active_mode is None:
                return ExecutionVerdict(blocked=False)

            _tool_perm = get_tool_permission(name)
            _active_rank = _PERM_ORDER.get(getattr(_active_mode, "value", ""), 99)
            _tool_rank = _PERM_ORDER.get(getattr(_tool_perm, "value", ""), 99)
            if _tool_rank > _active_rank:
                return ExecutionVerdict(
                    blocked=True,
                    result={
                        "ok": False,
                        "error": (
                            f"Tool '{name}' requires '{getattr(_tool_perm, 'value', '')}' "
                            f"permission but active mode is '{getattr(_active_mode, 'value', '')}'."
                        ),
                    },
                )
        except Exception as _e:
            logger.warning("Permission mode check failed for tool '%s' (fail-open): %s", name, _e)
        return ExecutionVerdict(blocked=False)

    @staticmethod
    def _check_explore_mode(name: str, orchestrator: Optional[Any]) -> ExecutionVerdict:
        """Block non-read tools when explore_mode is active."""
        if not orchestrator or not getattr(orchestrator, "explore_mode", False):
            return ExecutionVerdict(blocked=False)
        try:
            from src.core.orchestration.role_config import is_tool_allowed_for_role

            if not is_tool_allowed_for_role(name, "analyst"):
                return ExecutionVerdict(
                    blocked=True,
                    result={
                        "ok": False,
                        "error": (
                            f"Explore mode is active: tool '{name}' is not permitted. "
                            "Only read-only exploration tools are allowed."
                        ),
                    },
                )
        except Exception as _e:
            logger.warning("Explore mode check failed for tool '%s' (fail-open): %s", name, _e)
        return ExecutionVerdict(blocked=False)

    @staticmethod
    def _check_plan_mode(name: str, orchestrator: Optional[Any]) -> ExecutionVerdict:
        """Block write tools when plan mode is enabled and not approved."""
        if not orchestrator:
            return ExecutionVerdict(blocked=False)
        try:
            from src.core.orchestration.plan_mode import PlanMode

            _pm = getattr(orchestrator, "plan_mode", None)
            if (
                _pm
                and getattr(_pm, "enabled", False)
                and name in PlanMode.BLOCKED_TOOLS
            ):
                _approved = getattr(orchestrator, "_plan_mode_approved", None)
                if _approved is not True:
                    return ExecutionVerdict(
                        blocked=True,
                        result={
                            "ok": False,
                            "error": (
                                f"Tool '{name}' is blocked: the current plan has not been "
                                "approved yet. Await user approval before making file changes."
                            ),
                        },
                    )
        except Exception as _e:
            logger.warning("Plan mode check failed for tool '%s' (fail-open): %s", name, _e)
        return ExecutionVerdict(blocked=False)

    def _run_pre_hook(self, name: str, args: Dict[str, Any]) -> ExecutionVerdict:
        """Run the pre-tool hook.

        Returns an ``ExecutionVerdict``.  When the hook runner returns a
        denied result (exit 2), ``verdict.blocked=True`` so the caller can
        reject the tool call.  All other errors are logged and treated as
        allow (non-fatal).
        """
        if self._hook_runner is None:
            return ExecutionVerdict(blocked=False)
        try:
            hook_result = self._hook_runner.run_pre(name, args)
            # CP-7: hook_runner may return a HookResult (shell_hooks) OR be
            # the legacy ToolHookRunner that returns None.  Handle both.
            if hook_result is not None and getattr(hook_result, "denied", False):
                messages = getattr(hook_result, "messages", [])
                deny_msg = messages[0] if messages else f"Pre-tool hook denied '{name}'"
                return ExecutionVerdict(
                    blocked=True,
                    result={"ok": False, "error": deny_msg},
                )
        except Exception as exc:
            logger.debug("ToolExecutionService._run_pre_hook: %s", exc)
        return ExecutionVerdict(blocked=False)
