from langchain_core.runnables import RunnableConfig
import asyncio
import json
import logging
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Mapping, Dict, Any, Optional

from src.core.orchestration.graph.state import StateLike
from src.core.context.context_builder import ContextBuilder
from src.core.inference.llm_manager import call_model
from src.core.orchestration.graph.nodes.node_utils import _resolve_orchestrator
from src.core.orchestration.graph.nodes.planning_fast_paths import (
    _build_existing_plan_result as _build_existing_plan_result_impl,
    _build_planning_early_response_result as _build_planning_early_response_result_impl,
    _build_planning_error_result as _build_planning_error_result_impl,
    _build_resumed_plan_result as _build_resumed_plan_result_impl,
    _build_simple_next_action_plan_result as _build_simple_next_action_plan_result_impl,
)
from src.core.orchestration.graph.nodes.planning_prompt import (
    _build_planning_task_description as _build_planning_task_description_impl,
)
from src.core.orchestration.graph.nodes.planning_result import (
    _build_resolved_plan_result as _build_resolved_plan_result_impl,
)
from src.core.orchestration.graph.nodes.planning_helpers import (
    append_plan_audit_log,
    get_last_plan_path,
    hydrate_repo_context_from_index,
    maybe_inject_repo_overview,
    load_last_plan,
    plan_is_resumable,
    resolve_planning_orchestrator,
    save_last_plan,
)
from src.core.orchestration.graph.nodes.node_utils import span_node as _span_node


logger = logging.getLogger(__name__)

_PLAN_RESUME_TTL_SECONDS = 1800  # 30 minutes


def _dag_waves(dag, label: str):
    """Return topological waves for *dag*, or None if validation fails (H-04)."""
    if not dag.validate():
        logger.warning("planning_node: %s DAG invalid; sequential execution", label)
        return None
    return dag.topological_sort_waves()


def _plan_is_resumable(
    data: Dict[str, Any],
    current_task: str,
    wd: str,
    resume_session: bool = False,
) -> bool:
    """Return True if a saved plan should be resumed rather than regenerated."""
    return plan_is_resumable(
        data=data,
        current_task=current_task,
        resume_ttl_seconds=_PLAN_RESUME_TTL_SECONDS,
        logger=logger,
        now_fn=datetime.now,
        datetime_fromisoformat_fn=datetime.fromisoformat,
        resume_session=resume_session,
    )


def _hydrate_repo_context_from_index(
    working_dir: str, task: str, relevant_files: list[str], key_symbols: list[str]
) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    """Best-effort repo-index fallback for planning context.

    planning_node already benefits from analysis_node output when available.
    This fallback only fills gaps when planning starts with weak repo context.
    """
    try:
        from src.core.indexing.repo_indexer import get_symbols_for_task as _gst
        return hydrate_repo_context_from_index(
            working_dir=working_dir,
            task=task,
            relevant_files=relevant_files,
            key_symbols=key_symbols,
            get_symbols_for_task_fn=_gst,
            logger=logger,
        )
    except Exception as exc:
        logger.debug(
            "planning_node: repo-context hydration failed (non-critical): %s", exc
        )
        return list(relevant_files), list(key_symbols), []


def _get_last_plan_path(workdir: str) -> Path:
    """Get the path to the last plan JSON file."""
    try:
        from src.tools.tools_config import agent_context_path
    except Exception:
        agent_context_path = None
    return get_last_plan_path(
        workdir=workdir,
        agent_context_path_fn=agent_context_path,
    )


def _load_last_plan(workdir: str) -> Dict[str, Any]:
    """Load the last plan from JSON file if it exists."""
    return load_last_plan(
        workdir=workdir,
        get_last_plan_path_fn=_get_last_plan_path,
        logger=logger,
    )


def _save_last_plan(workdir: str, plan: list, task: str, step: int = 0) -> None:
    """Save plan to JSON (cross-session persistence) and append to audit log."""
    _kw = dict(workdir=workdir, plan=plan, task=task, step=step,
               get_last_plan_path_fn=_get_last_plan_path, logger=logger, now_fn=datetime.now)
    save_last_plan(**_kw)
    append_plan_audit_log(**_kw)


def _build_planning_task_description(
    *,
    state: Mapping[str, Any],
    task: str,
    analysis_summary: str,
    relevant_files: list[str],
    key_symbols: list[str],
    repo_lookup_symbols: list[dict[str, Any]],
    plan_step_limit: int,
) -> str:
    """Compatibility wrapper around planning prompt/task-description assembly."""
    return _build_planning_task_description_impl(
        state=state,
        task=task,
        analysis_summary=analysis_summary,
        relevant_files=relevant_files,
        key_symbols=key_symbols,
        repo_lookup_symbols=repo_lookup_symbols,
        plan_step_limit=plan_step_limit,
    )


def _build_planning_error_result(
    *,
    current_plan: list,
    current_step: int,
    plan_attempts: int,
    errors: list[str],
) -> dict:
    """Compatibility wrapper around the planning error payload builder."""
    return _build_planning_error_result_impl(
        current_plan=current_plan,
        current_step=current_step,
        plan_attempts=plan_attempts,
        errors=errors,
    )


def _build_resumed_plan_result(
    *,
    loaded_plan: list,
    loaded_step: int,
    plan_attempts: int,
) -> dict:
    """Compatibility wrapper around the resumed-plan payload builder."""
    return _build_resumed_plan_result_impl(
        loaded_plan=loaded_plan,
        loaded_step=loaded_step,
        plan_attempts=plan_attempts,
    )


def _build_existing_plan_result(
    *,
    current_plan: list,
    current_step: int,
    step_description: str,
    plan_attempts: int,
) -> dict:
    """Compatibility wrapper around the existing-plan payload builder."""
    return _build_existing_plan_result_impl(
        current_plan=current_plan,
        current_step=current_step,
        step_description=step_description,
        plan_attempts=plan_attempts,
    )


def _build_simple_next_action_plan_result(
    *,
    current_plan: list,
    current_step: int,
    plan_attempts: int,
) -> dict:
    """Compatibility wrapper around the trivial one-step plan payload builder."""
    return _build_simple_next_action_plan_result_impl(
        current_plan=current_plan,
        current_step=current_step,
        plan_attempts=plan_attempts,
    )


def _build_planning_early_response_result(
    *,
    current_plan: list,
    current_step: int,
    plan_attempts: int,
    early_resp: dict,
) -> dict:
    """Compatibility wrapper around the planning early-response payload builder."""
    return _build_planning_early_response_result_impl(
        current_plan=current_plan,
        current_step=current_step,
        plan_attempts=plan_attempts,
        early_resp=early_resp,
    )


def _build_resolved_plan_result(
    *,
    current_plan: list,
    current_step: int,
    plan_attempts: int,
    relevant_files: list[str],
    key_symbols: list[str],
    affected_files: list,
    execution_waves: Any,
) -> dict:
    """Compatibility wrapper around resolved plan payload assembly."""
    return _build_resolved_plan_result_impl(
        current_plan=current_plan,
        current_step=current_step,
        plan_attempts=plan_attempts,
        relevant_files=relevant_files,
        key_symbols=key_symbols,
        affected_files=affected_files,
        execution_waves=execution_waves,
    )


async def planning_node(state: StateLike, config: RunnableConfig) -> Dict[str, Any]:
    """
    Planning Layer: Converts perception outputs into a structured plan.
    Uses the 'strategic' role from ContextBuilder (loaded from agent-brain).
    """
    with _span_node("planning", {"plan_attempts": state.get("plan_attempts", 0)}):
        return await _planning_node_impl(state, config)


async def _planning_node_impl(state: Mapping[str, Any], config: RunnableConfig) -> Dict[str, Any]:  # noqa: C901
    # P1-2: Increment inner planning-loop counter FIRST so all return paths carry it
    plan_attempts = int(state.get("plan_attempts") or 0) + 1

    # P2-9: Reset plan_mode_approved so each fresh plan requires re-approval.
    # Without this, a stale True from a prior approval skips the gate on subsequent plan cycles.

    orchestrator, orchestrator_error = resolve_planning_orchestrator(
        state=state,
        config=config,
        plan_attempts=plan_attempts,
        resolve_orchestrator_fn=_resolve_orchestrator,
        build_planning_error_result_fn=_build_planning_error_result,
        logger=logger,
    )
    if orchestrator_error is not None:
        return orchestrator_error

    # Treat state as a plain dict for flexible lookups
    s = dict(state)
    s["plan_attempts"] = plan_attempts

    # 4.4: Cross-session plan persistence - Load last plan if current is empty
    working_dir = str(s.get("working_dir") or ".")
    current_plan = s.get("current_plan")
    current_step = s.get("current_step", 0)
    task = str(s.get("task") or "")
    # Defensive strip: prevent "Task: Task: Task:..." cascading accumulation.
    # planning_node prefixes task with "Task: " in full_task; if state["task"] already
    # starts with that prefix (e.g. from a prior fallback plan or loop), the prompt
    # becomes "Task: Task: ..." which confuses the LLM.
    while task.startswith("Task: ") or task.startswith("Task:\t"):
        task = task[6:]

    if not isinstance(current_plan, list) or len(current_plan) == 0:
        # Try to load from last_plan.json
        last_plan_data = _load_last_plan(working_dir)
        if last_plan_data and last_plan_data.get("plan"):
            loaded_plan = last_plan_data["plan"]
            loaded_step = last_plan_data.get("current_step", 0)

            # P2-B fix: Replace 80% Jaccard word-overlap with TTL + exact match.
            # Jaccard was unreliable — "fix the login bug" and "test the login bug"
            # share >80% word overlap but need completely different plans.
            if (
                loaded_plan
                and task
                and _plan_is_resumable(
                    last_plan_data,
                    task,
                    working_dir,
                    resume_session=bool(state.get("resume_session")),
                )
            ):
                logger.info(
                    f"planning_node: resuming from saved plan with {len(loaded_plan)} steps"
                )
                return _build_resumed_plan_result(
                    loaded_plan=loaded_plan,
                    loaded_step=loaded_step,
                    plan_attempts=plan_attempts,
                )

    # If the perception already provided a next_action, try to build a simple plan

    # Minimal planner: if next_action exists, make a one-step plan; otherwise ask the LLM
    current_plan = s.get("current_plan")
    if not isinstance(current_plan, list):
        current_plan = []
    current_step = s.get("current_step")
    if not isinstance(current_step, int):
        current_step = 0
    task_decomposed = bool(s.get("task_decomposed", False))

    # If we already have a decomposed plan with steps, use it
    if task_decomposed and current_plan and current_step < len(current_plan):
        plan_len = len(current_plan)
        logger.info(f"Using decomposed plan: step {current_step + 1}/{plan_len}")
        step_desc = ""
        if current_step < len(current_plan):
            step_desc = str(current_plan[current_step].get("description", ""))
        # DOOM-LOOP FIX: Do NOT set "task": step_desc here.
        # Overwriting state["task"] with the step description caused cascading
        # "Task: Task: Task:..." prefix accumulation across debug/replan cycles.
        # The original task is preserved in state["task"] and state["original_task"].
        # The step description is communicated via state["step_description"] instead.
        return _build_existing_plan_result(
            current_plan=current_plan,
            current_step=current_step,
            step_description=step_desc,
            plan_attempts=plan_attempts,
        )

    next_action = s.get("next_action")
    if next_action:
        # Construct a trivial plan wrapping the existing action
        step = {
            "action": next_action,
            "description": "Execute the requested tool",
        }
        current_plan = [step]
        current_step = 0
        # 4.4: Persist simple plan for cross-session persistence
        _save_last_plan(working_dir, current_plan, task, current_step)
        return _build_simple_next_action_plan_result(
            current_plan=current_plan,
            current_step=current_step,
            plan_attempts=plan_attempts,
        )

    # Fallback: ask the model for a short plan (non-blocking best effort)
    try:
        builder = ContextBuilder(working_dir=state.get("working_dir"))
        history = s.get("history")
        if not isinstance(history, list):
            history = []

        # Build repo-aware context from analysis output
        analysis_summary = str(s.get("analysis_summary") or "No analysis available")
        relevant_files: list[str] = list(s.get("relevant_files") or [])  # type: ignore[arg-type]
        key_symbols: list[str] = list(s.get("key_symbols") or [])  # type: ignore[arg-type]
        repo_lookup_symbols: list[dict[str, Any]] = []

        relevant_files, key_symbols, repo_lookup_symbols = _hydrate_repo_context_from_index(
            str(s.get("working_dir") or ""),
            task,
            relevant_files,
            key_symbols,
        )

        # Repo-aware planning: inject directory tree when no analysis context present.
        analysis_summary = maybe_inject_repo_overview(
            str(s.get("working_dir") or ""), relevant_files, analysis_summary
        )

        # GAP-FRONTIER-6: Tier-dependent step limit — frontier models can plan more steps.
        _plan_step_limit = 8  # default (MEDIUM / unknown)
        try:
            from src.core.inference.model_tiers import (
                ModelTier,
                get_plan_step_limit as _gsl,
            )

            _mt = state.get("model_tier")
            _plan_step_limit = _gsl(ModelTier(_mt)) if _mt else 8
        except Exception:
            pass

        full_task = _build_planning_task_description(
            state=s,
            task=task,
            analysis_summary=analysis_summary,
            relevant_files=relevant_files,
            key_symbols=key_symbols,
            repo_lookup_symbols=repo_lookup_symbols,
            plan_step_limit=_plan_step_limit,
        )

        # Use strategic role from AgentBrainManager
        # Conservative provider/model resolution follows the canonical pattern:
        # 1) orchestrator.get_provider_capabilities() (authoritative)
        # 2) ProviderManager.get_provider_capabilities(adapter)
        # 3) adapter attributes (provider, default_model, models)
        # Only accept concrete strings (no MagicMock placeholders). Guard imports
        # locally to avoid circular import issues in tests.
        from src.core.orchestration.provider_capabilities import resolve_provider_capabilities as _resolve_pc
        provider_capabilities = _resolve_pc(orchestrator)

        messages = builder.build_prompt(
            role_name="strategic",
            active_skills=[],
            task_description=full_task,
            tools=[],
            conversation=history,
            max_tokens=3000,  # P5 fix: 1500 truncated complex multi-step plans
            provider_capabilities=provider_capabilities,
            model_tier=state.get("model_tier"),  # S1-B
            model_name=provider_capabilities.get("model") or "",
        )

        cancel_event = state.get("cancel_event")
        if not cancel_event:
            cancel_event = getattr(orchestrator, "cancel_event", None)

        # WF-5: Resolve per-LLM-call hard timeout from project settings.
        # Default: 120 s.  0 = disabled.
        _llm_timeout: int | None = 120
        try:
            from src.core.orchestration.project_settings import (
                get_active_settings as _gas_wf5,
            )

            _ps_wf5 = _gas_wf5()
            if _ps_wf5 is not None:
                _llm_timeout = _ps_wf5.max_llm_wait_seconds or None
        except Exception:
            pass

        # F14: call_model is always async; use create_task directly.
        # GAP 2: Hardcode temperature for planning (0.3 for slight creativity)
        # SES-W3 / SM-1: Model override priority:
        # 1. state["override_model"] — set by delegate_task for per-subagent binding
        # 2. planning_model from providers.json (get_model_for_role("strategic"))
        # 3. None — use active provider default
        _planning_model_override: Optional[str] = state.get("override_model") or None
        if not _planning_model_override:
            try:
                from src.core.config_loader import get_model_for_role as _gmfr

                _planning_model_override = _gmfr("strategic")
            except Exception:
                pass
        from src.core.inference.llm_helpers import call_model_with_timeout

        llm_kwargs = {"temperature": 0.3}
        try:
            # Pass the local call_model so tests that patch planning_node.call_model
            # continue to work. Model override is passed as the `model` parameter
            # to avoid duplicate-key kwargs.
            early_resp, resp = await call_model_with_timeout(
                messages,
                None,
                _planning_model_override,
                state,
                orchestrator,
                llm_kwargs,
                call_model_fn=call_model,
            )
        except asyncio.CancelledError:
            # Preserve cancellation propagation semantics for callers/tests.
            raise
        if early_resp is not None:
            # Propagate the early response shape into planning_node's return.
            return _build_planning_early_response_result(
                current_plan=current_plan,
                current_step=current_step,
                plan_attempts=plan_attempts,
                early_resp=early_resp,
            )

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
                elif isinstance(ch, str):
                    content = ch

        # Robust plan parsing with multiple fallback strategies
        steps = _parse_plan_content(content)

        # Try to parse as DAG first
        from src.core.orchestration.dag_parser import (
            _parse_dag_content,
            _convert_flat_to_dag,
        )

        dag = _parse_dag_content(content)
        if dag:
            # Successfully parsed DAG format
            steps = [
                {
                    "step_id": s.step_id,
                    "description": s.description,
                    "files": s.files,
                    "depends_on": s.depends_on,
                }
                for s in dag.steps.values()
            ]
            logger.info(f"planning_node: parsed DAG plan with {len(steps)} steps")
        elif steps:
            # Fall back to flat list converted to DAG
            dag = _convert_flat_to_dag(steps)
            logger.info(
                f"planning_node: converted flat plan to DAG with {len(steps)} steps"
            )

        # MC-6: Guard against runaway plans that would never complete.
        MAX_PLAN_STEPS = 50
        if steps and len(steps) > MAX_PLAN_STEPS:
            logger.warning(
                f"planning_node: plan has {len(steps)} steps which exceeds "
                f"MAX_PLAN_STEPS={MAX_PLAN_STEPS}; truncating to first {MAX_PLAN_STEPS} steps"
            )
            steps = steps[:MAX_PLAN_STEPS]

        if steps:
            # Bash restriction pre-flight: warn if any step description contains a
            # restricted command.  This surfaces the issue at plan time rather than
            # causing a mid-execution rejection that forces an unnecessary replan.
            try:
                from src.tools._security import RESTRICTED_COMMANDS

                _restricted_keywords = sorted(
                    RESTRICTED_COMMANDS, key=len, reverse=True
                )
                for _i, _step in enumerate(steps):
                    _desc = str(_step.get("description", "")).lower()
                    for _kw in _restricted_keywords:
                        if _kw in _desc:
                            logger.warning(
                                "planning_node: step %d description contains restricted "
                                "bash command %r — this step will require user approval "
                                "at execution time: %r",
                                _i,
                                _kw,
                                _step.get("description", "")[:80],
                            )
                            _step.setdefault("warnings", []).append(
                                f"contains restricted command '{_kw}' — requires user approval"
                            )
                            break
            except Exception:
                pass  # never block plan generation

            # Persist plan to session store
            try:
                import json as _json

                if orchestrator and hasattr(orchestrator, "session_store"):
                    _sid = getattr(orchestrator, "_current_task_id", None)
                    logger.debug("session_store: write session=%r site=planning_node:add_plan", _sid)
                    orchestrator.session_store.add_plan(
                        session_id=_sid, plan=_json.dumps(steps), status="created",
                    )
                    # H-03: persist for cross-session resumption (save_plan was never called)
                    if hasattr(orchestrator.session_store, "save_plan"):
                        try:
                            orchestrator.session_store.save_plan(
                                session_id=_sid or "unknown", plan=steps, task=task or "", step=0,
                            )
                        except Exception as _sp_exc:
                            logger.warning("planning_node: save_plan failed: %s", _sp_exc)
            except Exception as _ss_exc:
                logger.warning("planning_node: session_store write failed: %s", _ss_exc)

            # 4.4: Persist plan to JSON file for cross-session persistence
            _save_last_plan(working_dir, steps, task, 0)

            # Write human-readable TODO.md so user can see the plan
            try:
                from src.tools.todo_tools import manage_todo, notify_rbw

                step_descriptions = [
                    s.get("description", f"Step {i + 1}") for i, s in enumerate(steps)
                ]
                manage_todo(
                    action="create", workdir=working_dir, steps=step_descriptions
                )
                # Best-effort in-process safety-net: request centralized notifier to
                # update orchestrator._session_read_files and invalidate caches.
                try:
                    notify_rbw(working_dir, orchestrator=orchestrator)
                except Exception:
                    pass
                logger.info(f"planning_node: wrote TODO.md with {len(steps)} steps")
            except Exception as _te:
                logger.warning(f"planning_node: failed to write TODO.md: {_te}")

            from src.core.orchestration.dag_parser import _convert_flat_to_dag

            dag = _convert_flat_to_dag(steps)
            waves = _dag_waves(dag, "new-plan")
            return _build_resolved_plan_result(
                current_plan=steps,
                current_step=0,
                plan_attempts=plan_attempts,
                relevant_files=relevant_files,
                key_symbols=key_symbols,
                affected_files=_extract_affected_files(steps),
                execution_waves=waves,
            )
    except Exception as e:
        logger.error(f"planning_node: plan generation failed: {e}")

    # F7: Guaranteed fallback — never return an empty plan.
    # An empty plan causes perception → planning → perception loops.
    # Return a single-step plan from the task description so execution can proceed.
    if not current_plan and task:
        fallback_plan = [{"description": task[:200], "action": None}]
        logger.warning("planning_node: plan parse failed, using single-step fallback")
        from src.core.orchestration.dag_parser import _convert_flat_to_dag

        dag = _convert_flat_to_dag(fallback_plan)
        waves = _dag_waves(dag, "fallback-plan")
        return _build_resolved_plan_result(
            current_plan=fallback_plan,
            current_step=0,
            plan_attempts=plan_attempts,
            relevant_files=relevant_files,
            key_symbols=key_symbols,
            affected_files=_extract_affected_files(fallback_plan),
            execution_waves=waves,
        )

    from src.core.orchestration.dag_parser import _convert_flat_to_dag

    dag = _convert_flat_to_dag(current_plan)
    waves = _dag_waves(dag, "existing-plan")
    return _build_resolved_plan_result(
        current_plan=current_plan,
        current_step=current_step,
        plan_attempts=plan_attempts,
        relevant_files=relevant_files,
        key_symbols=key_symbols,
        affected_files=_extract_affected_files(current_plan),
        execution_waves=waves,
    )


def _extract_affected_files(steps: list) -> list:
    """GAP-S2: Extract file paths from plan step descriptions.

    Scans each step's ``description`` and ``files`` fields for file-path
    patterns (anything that looks like ``path/to/file.ext``).  Returns a
    deduplicated list of relative path strings.

    The list is used by ``Orchestrator.execute_tool()`` to enforce write-scope
    restrictions — only files in this list may be written during the current
    task.  An empty return value means "no restriction active".
    """
    import re as _re

    # Matches relative / absolute paths with a recognised extension.
    _FILE_PAT = _re.compile(
        r"\b([\w./\-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|yaml|yml|json|md|txt|toml|cfg|ini|sh|bash|html|css|scss|sql))\b"
    )
    seen: set = set()
    result: list = []
    for step in steps:
        # Explicit ``files`` list takes priority — it is already structured.
        for f in step.get("files") or []:
            if isinstance(f, str) and f not in seen:
                seen.add(f)
                result.append(f)
        # Also scan the free-text description for path mentions.
        desc = str(step.get("description") or "")
        for m in _FILE_PAT.finditer(desc):
            p = m.group(1)
            # Skip pure-extension tokens like ".py" or paths starting with ".."
            if p.startswith("..") or "/" not in p and "." == p[0]:
                continue
            if p not in seen:
                seen.add(p)
                result.append(p)
    return result


def _parse_plan_content(content: str) -> list:
    """
    Robust plan parsing with multiple fallback strategies.

    Tries in order:
    1. JSON array extraction (most robust)
    2. Markdown code block JSON
    3. Structured regex parsing for numbered/bulleted lists
    4. Line-by-line fallback
    """
    if not content:
        return []

    # Strategy 1: Try JSON array extraction

    # Look for JSON array in content
    json_match = re.search(r"\[[\s\S]*\]", content)
    if json_match:
        try:
            parsed = json.loads(json_match.group(0))
            if isinstance(parsed, list) and len(parsed) > 0:
                steps = []
                for item in parsed:
                    if isinstance(item, dict):
                        desc = (
                            item.get("description")
                            or item.get("step")
                            or item.get("text")
                            or str(item)
                        )
                        steps.append({"description": desc, "action": None})
                    elif isinstance(item, str):
                        steps.append({"description": item, "action": None})
                if steps:
                    logger.info(
                        f"planning_node: parsed JSON plan with {len(steps)} steps"
                    )
                    return steps
        except (json.JSONDecodeError, Exception):
            pass

    # Strategy 2: Look for markdown code block with JSON
    code_block_match = re.search(
        r"```(?:json)?\s*(\[[\s\S]*?\])\s*```", content, re.IGNORECASE
    )
    if code_block_match:
        try:
            parsed = json.loads(code_block_match.group(1))
            if isinstance(parsed, list):
                steps = []
                for item in parsed:
                    if isinstance(item, dict):
                        desc = item.get("description") or item.get("step") or str(item)
                        steps.append({"description": desc, "action": None})
                    elif isinstance(item, str):
                        steps.append({"description": item, "action": None})
                if steps:
                    logger.info(
                        f"planning_node: parsed code block JSON with {len(steps)} steps"
                    )
                    return steps
        except (json.JSONDecodeError, Exception):
            pass

    # Strategy 3: Structured regex for numbered/bulleted lists
    # Match patterns like: "1. Step description" or "- Step description" or "* Step description"
    # WR-5 fix: collect structured lines (numbered/bullet) and free-text lines
    # separately.  If ANY structured lines are found, use only those — this
    # prevents analysis-context preamble sentences (which appear BEFORE the
    # numbered list) from becoming spurious plan steps.
    structured_lines = []
    freetext_lines = []

    # Pattern for numbered items: 1., 2., 1), 2), etc.
    numbered_pattern = r"^\s*(\d+[\.\)]\s+)(.+)$"
    # Pattern for bullet items: -, *, •, etc.
    bullet_pattern = r"^\s*([\-\*•]\s+)(.+)$"

    action_words = [
        "read",
        "write",
        "edit",
        "create",
        "delete",
        "update",
        "modify",
        "add",
        "remove",
        "run",
        "test",
        "check",
        "verify",
        "install",
        "import",
    ]

    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue

        # Skip markdown table rows (start and end with |)
        if line.startswith("|") and line.endswith("|"):
            continue

        # Skip file/directory listing lines e.g. ".DS_Store (file)", "src/ (directory)"
        if re.match(r"^\.?\S+\s+\((?:file|directory|dir)\)$", line, re.IGNORECASE):
            continue

        # Skip conversational filler lines
        lower_line = line.lower()
        skip_phrases = [
            "here is",
            "here's",
            "plan:",
            "steps:",
            "task:",
            "to do:",
            "sure,",
            "okay,",
        ]
        if any(lower_line.startswith(phrase) for phrase in skip_phrases):
            continue
        if lower_line in ["no steps needed", "no plan needed", "i cannot", "i'm sorry"]:
            continue

        # Skip metadata lines (e.g. PLAN_STEPS: 1, COMPLEXITY: simple)
        if re.match(r"^[A-Z_]+\s*[:=]\s*\S", line):
            continue

        # Try numbered pattern
        match = re.match(numbered_pattern, line)
        if match:
            desc = match.group(2).strip()
            if desc:
                structured_lines.append(desc)
            continue

        # Try bullet pattern
        match = re.match(bullet_pattern, line)
        if match:
            desc = match.group(2).strip()
            if desc:
                structured_lines.append(desc)
            continue

        # Free-text fallback (only used when no structured lines found)
        if len(line) < 200 and any(word in lower_line for word in action_words):
            freetext_lines.append(line)

    # Prefer structured lines; fall back to free-text only when none were found.
    plan_lines = structured_lines if structured_lines else freetext_lines

    if plan_lines:
        steps = [{"description": desc, "action": None} for desc in plan_lines]
        logger.info(f"planning_node: parsed regex plan with {len(steps)} steps")
        return steps

    # Strategy 4: Last resort - only if content looks like a genuine task description
    # (contains action words and is not metadata / file listing output)
    if content and len(content.strip()) < 500:
        stripped = content.strip()
        lower_stripped = stripped.lower()
        # Reject metadata-style output (PLAN_STEPS: 1 etc.)
        if re.match(r"^[A-Z_]+\s*[:=]", stripped):
            return []
        # Reject if the content looks like a file/directory listing
        # (every non-empty line matches the "name (file|directory)" pattern)
        non_empty_lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
        if non_empty_lines and all(
            re.match(r"^\.?\S+\s+\((?:file|directory|dir)\)$", ln, re.IGNORECASE)
            for ln in non_empty_lines
        ):
            return []
        # Require at least one action word so bare file listings / status messages
        # don't become single-step plans; count whole-word occurrences to avoid
        # false positives like "test_dir" matching "test".
        if (
            stripped
            and not stripped.startswith("```")
            and any(
                re.search(r"\b" + word + r"\b", lower_stripped) for word in action_words
            )
        ):
            logger.info("planning_node: falling back to single-step plan")
            return [{"description": stripped[:200], "action": None}]

    return []
