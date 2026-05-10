from types import SimpleNamespace

import pytest


class _Builder:
    def __init__(self, working_dir=None):
        self.working_dir = working_dir

    def build_prompt(self, **kwargs):
        return [{"role": "user", "content": kwargs["task_description"]}]


def _resolve_caps(_orchestrator):
    return {}


def _extract_response(response, *, parse_tool_block, logger=None):
    content = response["choices"][0]["message"].get("content", "")
    return content, None, parse_tool_block(content)


def _parse_tool_block(content):
    if content == "TOOL_CALL":
        return {"name": "read_file", "arguments": {"path": "app.py"}}
    return None


@pytest.mark.asyncio
async def test_generate_action_for_plan_step_canceled_before_call():
    from src.core.orchestration.graph.nodes.execution_helpers import (
        generate_action_for_plan_step,
    )

    cancel_event = SimpleNamespace(is_set=lambda: True)
    orchestrator = SimpleNamespace(
        tool_registry=SimpleNamespace(tools={}, get_openai_functions=lambda: []),
        cancel_event=cancel_event,
    )

    tool_call, updated_plan, content, early = await generate_action_for_plan_step(
        state={"working_dir": "/tmp", "history": [], "cancel_event": cancel_event},
        orchestrator=orchestrator,
        current_plan=[{"description": "step 1"}],
        current_step=0,
        original_task="task",
        execution_max_prompt_tokens=4000,
        context_builder_cls=_Builder,
        resolve_provider_capabilities=_resolve_caps,
        call_model_fn=None,
        parse_tool_block=_parse_tool_block,
        extract_tool_call_from_response_fn=_extract_response,
        logger=SimpleNamespace(info=lambda *a, **k: None),
    )

    assert tool_call is None
    assert updated_plan == [{"description": "step 1"}]
    assert content == ""
    assert early["errors"] == ["canceled"]


@pytest.mark.asyncio
async def test_generate_action_for_plan_step_timeout_returns_wait_for_user():
    from src.core.orchestration.graph.nodes.execution_helpers import (
        generate_action_for_plan_step,
    )

    async def _slow_call(*args, **kwargs):
        raise TimeoutError()

    logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    orchestrator = SimpleNamespace(
        tool_registry=SimpleNamespace(tools={}, get_openai_functions=lambda: []),
        cancel_event=None,
    )

    tool_call, updated_plan, content, early = await generate_action_for_plan_step(
        state={"working_dir": "/tmp", "history": [], "session_id": "s1"},
        orchestrator=orchestrator,
        current_plan=[{"description": "step 1"}],
        current_step=0,
        original_task="task",
        execution_max_prompt_tokens=4000,
        context_builder_cls=_Builder,
        resolve_provider_capabilities=_resolve_caps,
        call_model_fn=_slow_call,
        parse_tool_block=_parse_tool_block,
        extract_tool_call_from_response_fn=_extract_response,
        logger=logger,
    )

    assert tool_call is None
    assert updated_plan == [{"description": "step 1"}]
    assert content == ""
    assert early["next_action"] == "wait_for_user"
    assert early["errors"] == ["llm_timeout:120s"]


@pytest.mark.asyncio
async def test_generate_action_for_plan_step_handles_context_overflow():
    from src.core.orchestration.graph.nodes.execution_helpers import (
        generate_action_for_plan_step,
    )

    async def _overflow_call(*args, **kwargs):
        return {"context_overflow": True}

    logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    orchestrator = SimpleNamespace(
        tool_registry=SimpleNamespace(tools={}, get_openai_functions=lambda: []),
        cancel_event=None,
    )
    history = [{"role": "user", "content": str(i)} for i in range(10)]

    tool_call, updated_plan, content, early = await generate_action_for_plan_step(
        state={"working_dir": "/tmp", "history": history, "session_id": "s1"},
        orchestrator=orchestrator,
        current_plan=[{"description": "step 1"}],
        current_step=0,
        original_task="task",
        execution_max_prompt_tokens=4000,
        context_builder_cls=_Builder,
        resolve_provider_capabilities=_resolve_caps,
        call_model_fn=_overflow_call,
        parse_tool_block=_parse_tool_block,
        extract_tool_call_from_response_fn=_extract_response,
        logger=logger,
    )

    assert tool_call is None
    assert updated_plan == [{"description": "step 1"}]
    assert content == ""
    assert early["errors"] == ["context_overflow"]
    assert len(early["_compacted_history"]) == 6


@pytest.mark.asyncio
async def test_generate_action_for_plan_step_updates_plan_on_tool_call():
    from src.core.orchestration.graph.nodes.execution_helpers import (
        generate_action_for_plan_step,
    )

    async def _call_model(*args, **kwargs):
        return {"choices": [{"message": {"content": "TOOL_CALL"}}]}

    logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    orchestrator = SimpleNamespace(
        tool_registry=SimpleNamespace(
            tools={"read_file": {"description": "Read a file"}},
            get_openai_functions=lambda: [],
        ),
        cancel_event=None,
    )
    plan = [{"description": "step 1"}]

    tool_call, updated_plan, content, early = await generate_action_for_plan_step(
        state={"working_dir": "/tmp", "history": [], "session_id": "s1"},
        orchestrator=orchestrator,
        current_plan=plan,
        current_step=0,
        original_task="task",
        execution_max_prompt_tokens=4000,
        context_builder_cls=_Builder,
        resolve_provider_capabilities=_resolve_caps,
        call_model_fn=_call_model,
        parse_tool_block=_parse_tool_block,
        extract_tool_call_from_response_fn=_extract_response,
        logger=logger,
    )

    assert early is None
    assert content == "TOOL_CALL"
    assert tool_call == {"name": "read_file", "arguments": {"path": "app.py"}}
    assert updated_plan is not plan
    assert updated_plan[0]["action"] == tool_call
