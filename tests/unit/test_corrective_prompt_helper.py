"""Unit tests for the perception_node corrective prompt helper.

These tests verify the high-level semantics of _select_corrective_prompt
without asserting exact multi-line formatting. They focus on stable
substrings that express the intent of each variant.
"""

from src.core.orchestration.graph.nodes.perception_node import (
    _select_corrective_prompt,
)


def test_select_corrective_prompt_attempt_variants():
    # 1st attempt: gentle reminder + fallback to 'respond' suggestion
    p1 = _select_corrective_prompt(attempt=1, model_tier=None, truncated_yaml=False)
    assert "Please provide a valid YAML tool call" in p1
    assert "respond" in p1 or "respond'" in p1

    # 2nd attempt: prescriptive example and fenced YAML
    p2 = _select_corrective_prompt(attempt=2, model_tier=None, truncated_yaml=False)
    assert "Please output a valid YAML tool call block now" in p2
    assert "```yaml" in p2

    # 3rd+ attempt: firmer guidance
    p3 = _select_corrective_prompt(attempt=5, model_tier=None, truncated_yaml=False)
    assert (
        "Please provide a valid YAML tool call" in p3
        or "Important: Please provide a valid YAML tool call" in p3
    )


def test_select_corrective_prompt_truncated_yaml():
    t = _select_corrective_prompt(attempt=1, model_tier=None, truncated_yaml=True)
    assert "cut off" in t or "may have been" in t
    assert "```yaml" in t
