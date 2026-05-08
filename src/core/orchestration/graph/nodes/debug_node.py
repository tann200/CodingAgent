from langchain_core.runnables import RunnableConfig
import asyncio
import logging
import threading
from typing import Dict, Any

from src.core.orchestration.graph.state import StateLike
from src.core.context.context_builder import ContextBuilder
from src.core.inference.llm_manager import call_model
from src.core.orchestration.tool_parser import parse_tool_block
from src.core.orchestration.graph.nodes.node_utils import _resolve_orchestrator
from src.core.utils.strings import valid_str as _valid_str

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


async def debug_node(state: StateLike, config: RunnableConfig) -> Dict[str, Any]:
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

    # Persist error to session store (lightweight caller instrumentation)
    try:
        if orchestrator and hasattr(orchestrator, "session_store"):
            _sid = getattr(orchestrator, "_current_task_id", None)
            _thread_name = getattr(threading.current_thread(), "name", "unknown")
            logger.debug(
                "session_store: write (session=%r, thread=%s, site=%s)",
                _sid,
                _thread_name,
                "debug_node:add_error",
            )
            orchestrator.session_store.add_error(
                session_id=_sid,
                error_type=error_type,
                error_message=error_summary[:500],
                context={"attempt": current_attempt + 1},
            )
            # Learning loop: record as a cross-session mistake so future tasks
            # can avoid the same pattern.  Summary is kept short (≤120 chars)
            # so FTS tokenisation is effective.
            if hasattr(orchestrator.session_store, "add_mistake"):
                _mistake_summary = f"{error_type}: {error_summary[:100]}"
                orchestrator.session_store.add_mistake(
                    session_id=_sid or "unknown",
                    summary=_mistake_summary,
                    context=error_summary[:400],
                    tool=None,  # debug_node doesn't know the specific tool
                )
    except Exception:
        pass  # never block execution

    fix_prompt = f"""You are a debugging assistant. Attempt {next_attempt}/{max_attempts}.

Task: {task}
Error type: {error_type}
Error details: {error_summary}

Guidance: {TYPE_GUIDANCE[error_type]}

Generate a JSON function call to fix the issue. Use edit_file, write_file, or bash as appropriate."""

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
        from src.core.orchestration.provider_capabilities import resolve_provider_capabilities as _resolve_pc
        provider_capabilities = _resolve_pc(orchestrator)

        messages = builder.build_prompt(
            role_name="debugger",
            active_skills=[],
            task_description=fix_prompt,
            tools=tools_list,
            conversation=state.get("history", []),
            max_tokens=4000,
            provider_capabilities=provider_capabilities,
            model_tier=state.get("model_tier"),  # S1-B/S1-C
            model_name=provider_capabilities.get("model") or "",
        )

        provider = None
        model = None

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
        # Only consult ProviderManager if an adapter instance is present. When
        # there is no adapter (e.g., in some headless/test contexts) we must not
        # infer a model from a global provider manager — prefer explicit None.
        if (provider is None or model is None) and getattr(
            orchestrator, "adapter", None
        ) is not None:
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
            await asyncio.wait([llm_task], timeout=0.2)
        try:
            resp = await llm_task
        except asyncio.CancelledError:
            raise  # propagate — node itself was cancelled; do not swallow

        content = ""
        if isinstance(resp, dict):
            _choices = resp.get("choices")
            if _choices and len(_choices) > 0:
                ch = (
                    _choices[0].get("message")
                    if isinstance(_choices[0], dict)
                    else None
                )
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
