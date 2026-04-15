import asyncio
import logging
from typing import Dict, Any

from src.core.orchestration.graph.state import StateLike
from src.core.context.context_builder import ContextBuilder
from src.core.inference.llm_manager import call_model
from src.core.orchestration.tool_parser import parse_tool_block
from src.core.orchestration.graph.nodes.node_utils import _resolve_orchestrator

logger = logging.getLogger(__name__)


def _classify_error(error_summary: str) -> str:
    """Classify error into a known category for more targeted prompts."""
    s = error_summary.lower()
    if "syntaxerror" in s or "indentationerror" in s or "invalid syntax" in s:
        return "syntax_error"
    if "importerror" in s or "modulenotfounderror" in s or "no module named" in s:
        return "import_error"
    if "assertionerror" in s or ("failed" in s and "test" in s):
        return "test_failure"
    if "e501" in s or "e302" in s or "flake8" in s or "pylint" in s or "ruff" in s:
        return "lint_error"
    if "typeerror" in s or "attributeerror" in s or "nameerror" in s:
        return "runtime_error"
    return "unknown_error"


TYPE_GUIDANCE = {
    "syntax_error": "Fix the syntax error. Check indentation and missing colons/parentheses.",
    "import_error": "Fix the import. Check module name spelling and that the module is installed.",
    "test_failure": "A test assertion failed. Read the failing test, understand what it expects, then fix the implementation.",
    "lint_error": "Fix the lint issue. Common issues: line too long (split it), missing blank lines, unused imports.",
    "runtime_error": "Fix the runtime error. Check attribute names, type mismatches, and None checks.",
    "unknown_error": "Analyze the error carefully and generate a targeted fix.",
}


async def debug_node(state: StateLike, config: Any) -> Dict[str, Any]:
    """
    Debug Node: Analyzes verification failures and attempts to fix issues.
    Uses the 'debugger' role from ContextBuilder (loaded from agent-brain).
    """
    logger.info("=== debug_node START ===")

    orchestrator = _resolve_orchestrator(state, config)
    if orchestrator is None:
        logger.error("debug_node: orchestrator is None")
        return {"next_action": None, "errors": ["orchestrator not found"]}

    current_attempt: int = int(state.get("debug_attempts") or 0)
    max_attempts: int = int(state.get("max_debug_attempts") or 3)
    total_debug_attempts: int = int(state.get("total_debug_attempts") or 0)
    last_error_type: str = state.get("last_debug_error_type") or ""
    last_result = state.get("last_result") or {}
    verification_result = state.get("verification_result") or {}
    # DOOM-LOOP FIX: Use original_task as the authoritative task string.
    # state["task"] may have been overwritten by planning_node with a step
    # description, which causes "Task: Task: Task:..." accumulation when
    # debug_node prefixes it with "Task: " again.
    task = str(state.get("original_task") or state.get("task") or "")
    # Defensive strip: remove any leftover "Task: " prefixes.
    while task.startswith("Task: ") or task.startswith("Task:\t"):
        task = task[6:]

    logger.info(f"debug_node: attempt {current_attempt + 1}/{max_attempts}")

    error_summary = ""
    if last_result.get("error"):
        error_summary = f"Tool error: {last_result.get('error')}"
    elif verification_result:
        v = verification_result.get("tests", {})
        if v.get("status") == "fail":
            error_summary = f"Test failure: {v.get('stdout', '')[:500]}"
        v = verification_result.get("linter", {})
        if v.get("status") == "fail":
            error_summary += f" Linter: {v.get('stdout', '')[:200]}"
        # HR-3 fix: also check JS/TS verification keys
        v = verification_result.get("js_tests", {})
        if v.get("status") == "fail":
            error_summary += f" JS tests: {v.get('stdout', '')[:300]}"
        v = verification_result.get("ts_check", {})
        if v.get("status") == "fail":
            error_summary += f" TypeScript check: {v.get('stdout', '')[:300]}"
        v = verification_result.get("eslint", {})
        if v.get("status") == "fail":
            error_summary += f" ESLint: {v.get('stdout', '')[:200]}"
        v = verification_result.get("syntax", {})
        if v.get("status") == "fail":
            error_summary += f" Syntax: {v.get('stdout', '')[:200]}"

    if current_attempt >= max_attempts:
        logger.warning("debug_node: max attempts reached, giving up")

        # Step C: Attempt automated rollback when debug retries are exhausted
        try:
            rollback_mgr = getattr(orchestrator, "rollback_manager", None)
            if rollback_mgr and rollback_mgr.current_snapshot:
                result = rollback_mgr.rollback()
                logger.info(f"Auto-rollback result: {result}")
                rollback_mgr.cleanup_old_snapshots(keep_last=5)
        except Exception as rb_err:
            logger.warning(f"Rollback failed: {rb_err}")

        return {
            "next_action": None,
            "errors": [
                f"Max debug attempts ({max_attempts}) reached — rollback attempted"
            ],
        }

    next_attempt = current_attempt + 1
    next_total = total_debug_attempts + 1
    # P2-A: global recovery cap (debug + replan combined)
    next_recovery = int(state.get("total_recovery_attempts") or 0) + 1

    # Classify the error for more targeted fixes
    error_type = _classify_error(error_summary)

    # P1-B fix: removed the per-error-type attempt counter reset that previously
    # fired when error_type changed between debug cycles.  That reset allowed the
    # debug loop to cycle through up to 6 error types × 3 attempts = 18 LLM calls
    # before the MAX_TOTAL_DEBUG hard cap (9) stopped it.  The error type is now
    # used only for routing (TYPE_GUIDANCE), not for resetting retry budgets.

    # Persist error to session store
    try:
        if orchestrator and hasattr(orchestrator, "session_store"):
            orchestrator.session_store.add_error(
                session_id=getattr(orchestrator, "_current_task_id", "unknown"),
                error_type=error_type,
                error_message=error_summary[:500],
                context={"attempt": current_attempt + 1},
            )
    except Exception:
        pass  # never block execution

    fix_prompt = f"""You are a debugging assistant. Attempt {next_attempt}/{max_attempts}.

Task: {task}
Error type: {error_type}
Error details: {error_summary}

Guidance: {TYPE_GUIDANCE[error_type]}

Generate a YAML tool call to fix the issue. Use edit_file, write_file, or bash as appropriate."""

    try:
        builder = ContextBuilder(working_dir=state.get("working_dir"))
        # OE-5: use role-filtered tool list for the debugger so the LLM only sees
        # tools relevant to debugging (bash, edit_file, read_file, run_tests …)
        if hasattr(orchestrator, "get_tools_for_role"):
            tools_list = orchestrator.get_tools_for_role("debugger")
        else:
            tools_list = [
                {"name": n, "description": m.get("description", "")}
                for n, m in orchestrator.tool_registry.tools.items()
            ]

        # Conservative provider/model resolution used across the codebase.
        # 1) orchestrator.get_provider_capabilities() (authoritative)
        # 2) ProviderManager.get_provider_capabilities(adapter)
        # 3) adapter attributes (provider, default_model, models)
        # Only accept concrete strings (no MagicMock placeholders). Guard imports
        # locally to avoid circular import issues in tests.
        provider_capabilities = {}
        try:
            try:
                from src.core.utils.strings import (
                    valid_str as _valid_str,
                    extract_str as _extract_str,
                )
            except Exception:

                def _valid_str(x: object) -> bool:
                    return (
                        isinstance(x, str)
                        and bool(x.strip())
                        and ("MagicMock" not in x)
                    )

                def _extract_str(candidate: object) -> str | None:
                    if candidate is None:
                        return None
                    if isinstance(candidate, dict):
                        for key in (
                            "provider_name",
                            "name",
                            "id",
                            "key",
                            "model",
                            "default_model",
                            "type",
                        ):
                            val = candidate.get(key)
                            if isinstance(val, str) and _valid_str(val):
                                return val.strip()
                        return None
                    if isinstance(candidate, str) and _valid_str(candidate):
                        return candidate.strip()
                    return None

            caps: dict = {}

            # 1) Orchestrator-level capabilities (authoritative)
            try:
                if (
                    orchestrator
                    and hasattr(orchestrator, "get_provider_capabilities")
                    and callable(getattr(orchestrator, "get_provider_capabilities"))
                ):
                    _rc = orchestrator.get_provider_capabilities()
                    if isinstance(_rc, dict) and _rc:
                        caps = dict(_rc)
            except Exception:
                caps = {}

            # 2) ProviderManager fallback
            if not caps:
                try:
                    from src.core.inference.llm_manager import (
                        get_provider_manager as _gpm,
                    )

                    _pm = _gpm()
                    adapter_for_pm = getattr(orchestrator, "_adapter", None)
                    _rc = _pm.get_provider_capabilities(adapter_for_pm)
                    if isinstance(_rc, dict) and _rc:
                        caps = dict(_rc)
                except Exception:
                    caps = caps or {}

            # 3) Adapter-only last resort (no network probes)
            if not caps:
                adapter_for_inspect = getattr(orchestrator, "adapter", None) or getattr(
                    orchestrator, "_adapter", None
                )
                if adapter_for_inspect:
                    try:
                        prov_attr = getattr(adapter_for_inspect, "provider", None)
                    except Exception:
                        prov_attr = None
                    provider_name = None
                    try:
                        provider_name = _extract_str(prov_attr)
                    except Exception:
                        provider_name = None
                    if not provider_name:
                        try:
                            provider_name = _extract_str(
                                getattr(adapter_for_inspect, "name", None)
                            )
                        except Exception:
                            provider_name = None

                    model = None
                    try:
                        model = _extract_str(
                            getattr(adapter_for_inspect, "default_model", None)
                        )
                    except Exception:
                        model = None
                    if not model:
                        try:
                            models_attr = getattr(adapter_for_inspect, "models", None)
                            if isinstance(models_attr, (list, tuple)):
                                for m in models_attr:
                                    mm = _extract_str(m)
                                    if mm:
                                        model = mm
                                        break
                            else:
                                model = _extract_str(models_attr)
                        except Exception:
                            model = None

                    supports_native_tools = False
                    try:
                        if isinstance(prov_attr, dict):
                            supports_native_tools = bool(
                                prov_attr.get("supports_native_tools", False)
                            )
                        else:
                            supports_native_tools = bool(
                                getattr(
                                    adapter_for_inspect, "supports_native_tools", False
                                )
                            )
                    except Exception:
                        supports_native_tools = False

                    provider_family = "default"
                    try:
                        from src.core.orchestration.provider_capabilities import (
                            _map_provider_family_impl as _map_pf,
                        )

                        provider_family = _map_pf(provider_name or "")
                    except Exception:
                        provider_family = "default"

                    caps = {
                        "supports_native_tools": bool(supports_native_tools),
                        "provider_family": provider_family,
                        "model": model,
                        "provider_name": provider_name or "",
                    }

            # Sanitize final caps
            try:
                _pname = _extract_str(
                    caps.get("provider_name")
                    or caps.get("provider")
                    or caps.get("name")
                )
            except Exception:
                _pname = None
            try:
                _model = _extract_str(caps.get("model") or caps.get("default_model"))
            except Exception:
                _model = None

            _pf = (
                caps.get("provider_family")
                if isinstance(caps.get("provider_family"), str)
                else None
            )
            _pf = _pf or "default"

            provider_capabilities = {
                "supports_native_tools": bool(caps.get("supports_native_tools", False)),
                "provider_family": _pf,
                "model": _model,
                "provider_name": _pname or "",
            }
        except Exception:
            provider_capabilities = {}

        messages = builder.build_prompt(
            role_name="debugger",
            active_skills=[],
            task_description=fix_prompt,
            tools=tools_list,
            conversation=state.get("history", []),
            max_tokens=4000,
            provider_capabilities=provider_capabilities,
            model_tier=state.get("model_tier"),  # S1-B/S1-C
        )

        provider = None
        model = None

        def _valid_str(x: object) -> bool:
            try:
                from src.core.utils.strings import valid_str as _vs

                return _vs(x)
            except Exception:
                return isinstance(x, str) and bool(x.strip()) and ("MagicMock" not in x)

        # Prefer orchestrator-level capabilities first
        try:
            if hasattr(orchestrator, "get_provider_capabilities") and callable(
                getattr(orchestrator, "get_provider_capabilities")
            ):
                caps = orchestrator.get_provider_capabilities()
                if isinstance(caps, dict):
                    p_raw = caps.get("provider_name") or caps.get("provider")
                    if _valid_str(p_raw):
                        provider = p_raw
                    m_raw = caps.get("model")
                    if _valid_str(m_raw):
                        model = m_raw
        except Exception:
            provider = None
            model = None

        # Secondary: ProviderManager with the adapter
        if provider is None or model is None:
            try:
                from src.core.inference.llm_manager import get_provider_manager

                pm = get_provider_manager()
                if pm:
                    try:
                        caps = pm.get_provider_capabilities(
                            getattr(orchestrator, "adapter", None)
                        )
                        if isinstance(caps, dict):
                            p_raw = caps.get("provider_name") or caps.get("provider")
                            if provider is None and _valid_str(p_raw):
                                provider = p_raw
                            m_raw = caps.get("model")
                            if model is None and _valid_str(m_raw):
                                model = m_raw
                    except Exception:
                        pass
            except Exception:
                pass

        # Final fallback: legacy adapter inspection
        if (provider is None or model is None) and getattr(
            orchestrator, "adapter", None
        ):
            try:
                ad = orchestrator.adapter
                if hasattr(ad, "provider") and isinstance(ad.provider, dict):
                    provider = provider or (
                        ad.provider.get("name") or ad.provider.get("type") or None
                    )
                if hasattr(ad, "models") and getattr(ad, "models"):
                    try:
                        models_attr = getattr(ad, "models")
                        if isinstance(models_attr, list) and models_attr:
                            model = model or models_attr[0]
                    except Exception:
                        pass
                else:
                    # Accept default_model only when it's a concrete non-empty string
                    try:
                        if hasattr(ad, "default_model"):
                            dm = getattr(ad, "default_model")
                            if isinstance(dm, str) and dm.strip():
                                model = model or dm
                    except Exception:
                        pass
            except Exception:
                pass

        cancel_event = state.get("cancel_event")
        if not cancel_event:
            cancel_event = getattr(orchestrator, "cancel_event", None)

        # WF-VOL21-2: Add elapsed-time deadline so the poll loop terminates even
        # when cancel_event is never set (headless / background execution).
        _debug_llm_timeout: int | None = 120
        try:
            from src.core.orchestration.project_settings import (
                get_active_settings as _gas_db,
            )

            _ps_db = _gas_db()
            if _ps_db is not None:
                _debug_llm_timeout = _ps_db.max_llm_wait_seconds or None
        except Exception:
            pass

        llm_task = asyncio.create_task(
            call_model(
                messages,
                provider=provider,
                model=model,
                stream=False,
                format_json=False,
            )
        )
        _debug_deadline = (
            asyncio.get_running_loop().time() + _debug_llm_timeout
            if _debug_llm_timeout
            else None
        )
        while not llm_task.done():
            if (
                cancel_event
                and hasattr(cancel_event, "is_set")
                and cancel_event.is_set()
            ):
                llm_task.cancel()
                logger.info("debug_node: Task canceled mid-generation")
                return {
                    "next_action": None,
                    "debug_attempts": next_attempt,
                    "total_debug_attempts": next_total,
                    "total_recovery_attempts": next_recovery,
                    "errors": ["canceled"],
                }
            if (
                _debug_deadline is not None
                and asyncio.get_running_loop().time() >= _debug_deadline
            ):
                llm_task.cancel()
                logger.warning(
                    f"debug_node: LLM call timed out after {_debug_llm_timeout}s"
                )
                return {
                    "next_action": None,
                    "debug_attempts": next_attempt,
                    "total_debug_attempts": next_total,
                    "total_recovery_attempts": next_recovery,
                    "errors": [f"llm_timeout:{_debug_llm_timeout}s"],
                }
            await asyncio.sleep(0.2)
        try:
            resp = await llm_task
        except asyncio.CancelledError:
            raise  # propagate — node itself was cancelled; do not swallow

        content = ""
        if isinstance(resp, dict):
            if resp.get("choices"):
                ch = resp["choices"][0].get("message")
                if isinstance(ch, dict):
                    content = ch.get("content") or ""

        tool_call = parse_tool_block(content)
        if tool_call:
            logger.info(f"debug_node: generated fix tool: {tool_call}")
            return {
                "next_action": tool_call,
                "debug_attempts": next_attempt,
                "total_debug_attempts": next_total,
                "total_recovery_attempts": next_recovery,
                "last_debug_error_type": error_type,
            }
        else:
            logger.warning("debug_node: no tool generated for fix")
            return {
                "next_action": None,
                "debug_attempts": next_attempt,
                "total_debug_attempts": next_total,
                "total_recovery_attempts": next_recovery,
                "last_debug_error_type": error_type,
                "errors": ["Debug node could not generate fix"],
            }

    except Exception as e:
        logger.error(f"debug_node: failed to generate fix: {e}")
        return {
            "next_action": None,
            "debug_attempts": next_attempt,
            "total_debug_attempts": next_total,
            "total_recovery_attempts": next_recovery,
            "last_debug_error_type": error_type,
            "errors": [str(e)],
        }
