from src.core.orchestration.agent_brain import load_system_prompt


def test_load_default_system_prompt_contains_role():
    txt = load_system_prompt(None)
    assert txt is not None, "load_system_prompt(None) returned None; expected coding agent prompt"
    # Ensure it looks like the coding agent prompt (has Role and Local Coding Agent)
    assert 'operational' in txt.lower() or 'expert coder' in txt.lower()


def test_load_named_agent_by_filename():
    # attempt to load by agent name 'strategic'
    txt2 = load_system_prompt('strategic')
    assert txt2 is not None, "load_system_prompt('strategic') failed to load agent-brain/roles/strategic.md"
    assert 'strategic' in txt2.lower() or 'role' in txt2.lower()

