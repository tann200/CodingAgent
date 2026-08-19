"""Small tests to ensure builder reuses canonical tool-set constants.

These prevent accidental divergence where functions define local literal
sets of read/query tools instead of referencing the module-level constants.
"""

import inspect


def test_check_no_plan_fast_path_uses_canonical_tool_sets():
    from src.core.orchestration.graph import builder

    src = inspect.getsource(builder._check_no_plan_fast_path)
    # Strip comment lines to match other tests' style
    code_lines = [l for l in src.splitlines() if not l.lstrip().startswith("#")]
    code = "\n".join(code_lines)

    # Must reference the canonical constants
    assert "read_only_tools = READ_ONLY_TOOLS" in code, (
        "_check_no_plan_fast_path must reuse READ_ONLY_TOOLS rather than define a local set"
    )
    assert "_query_tools = QUERY_TOOLS" in code, (
        "_check_no_plan_fast_path must reuse QUERY_TOOLS rather than define a local set"
    )

    # Must not contain a local literal set initializer for these variables
    assert "read_only_tools = {" not in code, (
        "Found local literal read_only_tools set in _check_no_plan_fast_path; expected canonical constant usage"
    )
    assert "_query_tools = {" not in code, (
        "Found local literal _query_tools set in _check_no_plan_fast_path; expected canonical constant usage"
    )
