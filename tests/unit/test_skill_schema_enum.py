from __future__ import annotations

from src.tools._registry import build_registry


def test_load_skill_schema_has_enum():
    """Ensure the load_skill OpenAI function schema contains an enum for 'name'."""
    reg = build_registry(include_echo=True)
    functions = reg.get_openai_functions()
    load_skill_fn = None
    for f in functions:
        fn = f.get("function", {})
        if fn.get("name") == "load_skill":
            load_skill_fn = fn
            break
    assert load_skill_fn is not None, "load_skill not found in OpenAI function schemas"
    params = load_skill_fn.get("parameters", {}).get("properties", {})
    name_prop = params.get("name")
    assert name_prop is not None, "load_skill parameters should include 'name'"
    enum = name_prop.get("enum")
    assert enum is not None and isinstance(enum, list) and len(enum) > 0, (
        "'name' should have a non-empty enum of skill names"
    )
