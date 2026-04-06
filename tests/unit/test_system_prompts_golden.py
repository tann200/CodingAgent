"""Golden file tests for SystemPromptBuilder (S10-C).

Strategy
--------
For each representative prompt configuration, the *static* part of the
system prompt (the part that is cache-eligible and provider/role-specific)
is rendered and compared against a committed golden file under
``tests/fixtures/golden_prompts/``.

We test the static part only because the dynamic part contains volatile
values (current date, git branch, working directory, AGENT.md content).

Updating golden files
---------------------
If a template intentionally changes, regenerate the golden files by
running the suite with the ``UPDATE_GOLDEN=1`` environment variable:

    UPDATE_GOLDEN=1 python -m pytest tests/unit/test_system_prompts_golden.py -v

The test will write the new rendered output and then pass.  Commit the
updated ``.golden`` files alongside the template change.

Test IDs
--------
  SPG-1   Default provider + operational role
  SPG-2   Anthropic provider + operational role
  SPG-3   OpenAI provider + operational role
  SPG-4   Plan-mode active adds plan_reminder section
  SPG-5   just_switched_from_plan adds build_switch section
  SPG-6   Steps-warning injected near max_steps limit
  SPG-7   Internal agent (COMPACTION_AGENT) → minimal prompt
  SPG-8   BUILD_AGENT with prompt_override → override wins
  SPG-9   build_combined includes dynamic boundary sentinel
  SPG-10  Role suffix is included for expert role
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path
from typing import Tuple
from unittest.mock import patch, MagicMock

import pytest

from src.core.prompts import PromptContext, SystemPromptBuilder, reload_templates
from src.core.orchestration.agent_types import (
    AgentDefinition,
    COMPACTION_AGENT,
    BUILD_AGENT,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GOLDEN_DIR = Path(__file__).parent.parent / "fixtures" / "golden_prompts"
_UPDATE = os.environ.get("UPDATE_GOLDEN", "").strip().lower() in ("1", "true", "yes")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stable_ctx(**kwargs) -> PromptContext:
    """Build a PromptContext with volatile fields neutralised for golden comparison.

    We override the dynamic helpers via monkeypatching in the fixture, but
    ``cwd`` is fixed to a deterministic path here so the env-block path is
    stable across machines.
    """
    defaults: dict = dict(
        provider_id="default",
        model_id="test-model",
        cwd=Path("/workspace/project"),
        role="operational",
        plan_mode_active=False,
        just_switched_from_plan=False,
        steps_taken=0,
        max_steps=30,
        available_skills=[],
        extra_dynamic_sections=[],
    )
    defaults.update(kwargs)
    return PromptContext(**defaults)


def _render_static(ctx: PromptContext) -> str:
    """Return only the static part of the system prompt."""
    static, _ = SystemPromptBuilder.build(ctx)
    return static


def _golden_path(test_id: str) -> Path:
    return _GOLDEN_DIR / f"{test_id}.golden"


def _assert_golden(test_id: str, rendered: str) -> None:
    """Compare *rendered* against the golden file for *test_id*.

    If UPDATE_GOLDEN=1, write the golden file instead and pass.
    """
    path = _golden_path(test_id)
    if _UPDATE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
        return  # always passes when updating

    if not path.is_file():
        pytest.fail(
            f"Golden file missing: {path}\nRun with UPDATE_GOLDEN=1 to generate it."
        )

    expected = path.read_text(encoding="utf-8")
    if rendered != expected:
        # Produce a readable diff in the failure message
        import difflib

        diff = "\n".join(
            difflib.unified_diff(
                expected.splitlines(),
                rendered.splitlines(),
                fromfile=f"{test_id}.golden (expected)",
                tofile=f"{test_id}.golden (actual)",
                lineterm="",
            )
        )
        pytest.fail(
            f"System prompt output changed for {test_id}.\n"
            "If the change is intentional, run with UPDATE_GOLDEN=1 to accept it.\n\n"
            + diff
        )


# ---------------------------------------------------------------------------
# Fixture: patch volatile helpers so static output is deterministic
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _stable_builder(monkeypatch):
    """Patch the git-branch helper and instruction_loader so golden files are
    machine-independent."""
    monkeypatch.setattr(
        "src.core.prompts.system_prompt_builder.SystemPromptBuilder._get_git_branch",
        classmethod(lambda cls, cwd: "main"),
    )
    # Patch instruction_loader to return a stable empty string
    mock_loader = MagicMock()
    mock_loader.build_runtime_context.return_value = ""
    monkeypatch.setattr(
        "src.core.prompts.system_prompt_builder.instruction_loader",
        mock_loader,
        raising=False,
    )
    reload_templates()
    yield
    reload_templates()


# ---------------------------------------------------------------------------
# SPG-1  Default provider + operational role
# ---------------------------------------------------------------------------


def test_spg1_default_operational():
    ctx = _stable_ctx(provider_id="default", role="operational")
    rendered = _render_static(ctx)
    _assert_golden("spg1_default_operational", rendered)
    # Structural invariants (hold regardless of exact template wording)
    assert len(rendered) > 50, "static prompt should not be empty"
    assert "plan_reminder" not in rendered.lower()
    assert "build_switch" not in rendered.lower()


# ---------------------------------------------------------------------------
# SPG-2  Anthropic provider + operational role
# ---------------------------------------------------------------------------


def test_spg2_anthropic_operational():
    ctx = _stable_ctx(provider_id="anthropic", role="operational")
    rendered = _render_static(ctx)
    _assert_golden("spg2_anthropic_operational", rendered)
    assert len(rendered) > 50


# ---------------------------------------------------------------------------
# SPG-3  OpenAI provider + operational role
# ---------------------------------------------------------------------------


def test_spg3_openai_operational():
    ctx = _stable_ctx(provider_id="openai", role="operational")
    rendered = _render_static(ctx)
    _assert_golden("spg3_openai_operational", rendered)
    assert len(rendered) > 50


# ---------------------------------------------------------------------------
# SPG-4  Plan-mode active adds plan_reminder section
# ---------------------------------------------------------------------------


def test_spg4_plan_mode_active():
    ctx = _stable_ctx(plan_mode_active=True)
    rendered = _render_static(ctx)
    _assert_golden("spg4_plan_mode_active", rendered)
    # The plan_reminder template should inject something distinct
    assert rendered != _render_static(_stable_ctx(plan_mode_active=False))


# ---------------------------------------------------------------------------
# SPG-5  just_switched_from_plan adds build_switch section
# ---------------------------------------------------------------------------


def test_spg5_build_switch():
    ctx = _stable_ctx(just_switched_from_plan=True)
    rendered = _render_static(ctx)
    _assert_golden("spg5_build_switch", rendered)
    assert rendered != _render_static(_stable_ctx(just_switched_from_plan=False))


# ---------------------------------------------------------------------------
# SPG-6  Steps-warning injected near max_steps limit
# ---------------------------------------------------------------------------


def test_spg6_steps_warning():
    # max_steps - steps_taken = 2, which is <= threshold (3)
    ctx = _stable_ctx(steps_taken=28, max_steps=30)
    rendered = _render_static(ctx)
    _assert_golden("spg6_steps_warning", rendered)
    # Without the warning it must be shorter
    ctx_no_warn = _stable_ctx(steps_taken=0, max_steps=30)
    assert len(rendered) > len(_render_static(ctx_no_warn))


# ---------------------------------------------------------------------------
# SPG-7  Internal agent → minimal prompt (COMPACTION_AGENT)
# ---------------------------------------------------------------------------


def test_spg7_internal_agent():
    ctx = _stable_ctx(agent=COMPACTION_AGENT)
    rendered = _render_static(ctx)
    _assert_golden("spg7_internal_agent", rendered)
    # Internal agents must not include role suffix or plan-mode noise
    assert "## Role:" not in rendered
    # Must be much shorter than a normal static prompt
    normal = _render_static(_stable_ctx())
    assert len(rendered) < len(normal)


# ---------------------------------------------------------------------------
# SPG-8  BUILD_AGENT renders a stable static prompt (no custom override)
# ---------------------------------------------------------------------------


def test_spg8_build_agent():
    ctx = _stable_ctx(agent=BUILD_AGENT)
    rendered = _render_static(ctx)
    _assert_golden("spg8_build_agent", rendered)
    # BUILD_AGENT uses the default template (no prompt_override)
    assert BUILD_AGENT.prompt_override is None  # confirmed — uses base template
    assert len(rendered) > 50


# ---------------------------------------------------------------------------
# SPG-9  build_combined includes the dynamic boundary sentinel
# ---------------------------------------------------------------------------


def test_spg9_build_combined_boundary():
    from src.core.prompts.system_prompt_builder import _DYNAMIC_BOUNDARY

    ctx = _stable_ctx()
    combined = SystemPromptBuilder.build_combined(ctx)
    _assert_golden(
        "spg9_build_combined_static_part", combined.split(_DYNAMIC_BOUNDARY)[0].rstrip()
    )
    assert _DYNAMIC_BOUNDARY in combined


# ---------------------------------------------------------------------------
# SPG-10  Role suffix is included for a non-default role
# ---------------------------------------------------------------------------


def test_spg10_role_suffix():
    ctx_default = _stable_ctx(role="operational")
    ctx_expert = _stable_ctx(role="expert")
    rendered_default = _render_static(ctx_default)
    rendered_expert = _render_static(ctx_expert)
    _assert_golden("spg10_role_expert", rendered_expert)
    # Different roles must produce different static output
    assert rendered_expert != rendered_default
