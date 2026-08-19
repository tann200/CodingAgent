from pathlib import Path
from typing import Any, Mapping


def _build_perception_messages(
    builder: Any,
    state: Mapping[str, Any],
    orchestrator: Any,
    adapter: Any,
    retrieved_snippets: list,
    active_skills: list,
    tools_list: list,
    history_for_prompt: list,
    perception_role: str,
    active_model_name: str | None,
    *,
    get_context_budget: Any,
    get_agent_settings: Any,
) -> list:
    """Build prompt messages and apply the perception-specific injections."""
    try:
        max_tokens = get_context_budget() if get_context_budget is not None else 6000
    except Exception:
        max_tokens = 6000

    provider_capabilities = {}
    try:
        if orchestrator and hasattr(orchestrator, "get_provider_capabilities"):
            provider_capabilities = orchestrator.get_provider_capabilities()
    except Exception:
        provider_capabilities = {}

    messages = builder.build_prompt(
        role_name=perception_role,
        active_skills=active_skills,
        task_description=state["task"],
        tools=tools_list,
        conversation=history_for_prompt,
        retrieved_snippets=retrieved_snippets,
        max_tokens=max_tokens,
        provider_capabilities=provider_capabilities,
        model_tier=state.get("model_tier"),
        model_name=active_model_name or "",
        include_prior_context=False,
    )

    try:
        prior_context_block = ""
        if (state.get("rounds") or 0) == 0:
            try:
                prior_context_block = builder.inject_prior_session_memories(
                    task=state.get("task", ""), limit=3
                )
            except Exception:
                prior_context_block = ""
        if prior_context_block and messages and messages[0].get("role") == "system":
            messages[0] = {
                **messages[0],
                "content": prior_context_block + "\n\n" + messages[0]["content"],
            }
    except Exception:
        pass

    try:
        if (
            (state.get("rounds") or 0) == 0
            and messages
            and messages[0].get("role") == "system"
        ):
            session_store = (
                getattr(orchestrator, "session_store", None) if orchestrator else None
            )
            if session_store and hasattr(session_store, "read_recent_decisions"):
                recent = session_store.read_recent_decisions(max_entries=5)
                if recent:
                    decision_lines = "\n".join(
                        f"- {decision.get('decision', '')} ({decision.get('created_at', '')})"
                        for decision in recent
                    )
                    messages[0] = {
                        **messages[0],
                        "content": (
                            f"## Recent task decisions (cross-session memory)\n{decision_lines}\n\n"
                            + messages[0]["content"]
                        ),
                    }
    except Exception:
        pass

    try:
        turn_count_now = int((state.get("turn_count") or 0))
        project_max_turns: int | None = None
        try:
            if get_agent_settings is not None:
                project_settings = get_agent_settings()
                if (
                    project_settings is not None
                    and project_settings.max_turns is not None
                ):
                    project_max_turns = project_settings.max_turns
        except Exception:
            pass

        max_turns_now = int(state.get("max_turns") or project_max_turns or 50)
        near_limit = turn_count_now >= (max_turns_now - 2)
        if near_limit and messages and messages[0].get("role") == "system":
            try:
                template_path = (
                    Path(__file__).parent.parent.parent.parent
                    / "prompts"
                    / "templates"
                    / "max_steps.txt"
                )
                if template_path.exists():
                    max_steps_text = template_path.read_text(encoding="utf-8").strip()
                    if max_steps_text:
                        messages[0] = {
                            **messages[0],
                            "content": messages[0]["content"] + f"\n\n{max_steps_text}",
                        }
            except Exception:
                pass
    except Exception:
        pass

    try:
        current_round = state.get("rounds") or 0
        if current_round > 0:
            injection_source = state.get("_pending_injections_source")
            if injection_source is not None and callable(
                getattr(injection_source, "pop_pending_injections", None)
            ):
                injected_messages = injection_source.pop_pending_injections()
                for injected_text in injected_messages:
                    reminder = (
                        "<system-reminder>\n"
                        "The user sent the following message:\n"
                        f"{injected_text}\n\n"
                        "Please address this message and continue with your tasks.\n"
                        "</system-reminder>"
                    )
                    messages.append({"role": "user", "content": reminder})
    except Exception:
        pass

    return messages
