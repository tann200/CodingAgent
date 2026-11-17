from __future__ import annotations

import re
from pathlib import Path

from src.config.toolsets.loader import get_toolset_for_role, validate_all_toolsets
from src.core.orchestration.registry_builder import example_registry

_BANNED_REFS = (
    "grep_search",
    "bash_sandboxed",
    "fetch_web_page",
    "finish_task",
    "file_search",
    "list_directory",
    ".agent-context",
)

# Backtick-quoted lowercase identifiers in tool-calling instructions must map to live tools
# unless explicitly listed as non-tool technical tokens (like paths/extensions/methods).
_NON_TOOL_TOKENS = frozenset(
    {
        "in_progress",
        "old_string",
        "new_string",
        "toml",
        "json",
        "yaml",
        "plain_text",
        "pyproject.toml",
        "requirements.txt",
        "package.json",
    }
)
_TOOL_LIKE_RE = re.compile(
    r"`([a-z][a-z0-9]*(?:_(?:file|files|dir|patch|atomic|search|page|diagnostics|"
    r"hover|definition|references|symbols|rename|status|diff|log|commit|restore|stash|"
    r"todo|user|task|tasks|parallel|message|mistake|save|checkpoint))+|"
    r"bash|grep|find_files|list_files|glob)`"
)


def test_prompt_templates_and_brain_have_no_stale_tool_refs_or_yaml_mandates():
    roots = [
        Path("src/core/prompts/templates"),
        Path("src/config/agent-brain"),
    ]
    live_tools = set(example_registry().tools.keys())

    for root in roots:
        for md_file in root.rglob("*.md"):
            text = md_file.read_text(encoding="utf-8")
            for banned in _BANNED_REFS:
                assert banned not in text, f"Stale reference {banned!r} found in {md_file}"
            for match in _TOOL_LIKE_RE.findall(text):
                if match in _NON_TOOL_TOKENS:
                    continue
                assert match in live_tools, (
                    f"Prompt {md_file} references unknown tool {match!r} not in live registry"
                )

    for fname in (
        "src/config/agent-brain/identity/SOUL.md",
        "src/config/agent-brain/identity/LAWS.md",
        "src/config/agent-brain/roles/operational-gemma4.md",
    ):
        txt = Path(fname).read_text(encoding="utf-8")
        assert "```yaml" not in txt, f"YAML block mandate still present in {fname}"
    assert "`respond`" not in Path("src/config/agent-brain/roles/operational-gemma4.md").read_text(
        encoding="utf-8"
    )


def test_role_toolsets_fail_closed_and_all_toolsets_registered():
    assert get_toolset_for_role("scout") == "analysis"
    assert get_toolset_for_role("explore") == "analysis"
    assert get_toolset_for_role("researcher") == "analysis"
    assert get_toolset_for_role("unknown_role_xyz") == "analysis"
    assert get_toolset_for_role("") == "analysis"

    assert get_toolset_for_role("coder") == "coding"
    assert get_toolset_for_role("operational") == "coding"
    assert get_toolset_for_role("planner") == "planning"
    assert get_toolset_for_role("reviewer") == "review"
    assert get_toolset_for_role("debugger") == "debug"

    # All configured YAML/JSON toolsets must validate against live example_registry()
    errors = validate_all_toolsets(example_registry(), fail_closed=False)
    assert errors == {}
