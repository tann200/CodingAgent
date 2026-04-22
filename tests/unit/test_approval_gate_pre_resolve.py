import threading

import src.core.orchestration.approval_gate as ag


def test_pre_resolve_bash_then_register():
    ag.reset_approval_gate()
    key = "pre-bash"

    # Resolve before registering
    ag.resolve_bash_gate(key, approved=False)

    gate = ag.register_bash_gate(key)
    assert gate.is_set()
    assert ag.is_bash_pending(key) is False
    assert ag.is_bash_denied(key)


def test_pre_resolve_tool_then_register():
    ag.reset_approval_gate()
    key = "pre-tool"

    # Resolve before registering
    ag.resolve_tool_gate(key, approved=True)

    gate = ag.register_tool_gate(key)
    assert gate.is_set()
    assert ag.is_tool_pending(key) is False
    assert not ag.is_tool_denied(key)
