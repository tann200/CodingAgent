from pathlib import Path

from src.core.context.agent_brain_loading import (
    load_prompt_directory,
    merge_workspace_skill_overrides,
)


def test_load_prompt_directory_reads_markdown_files(tmp_path: Path):
    roles_dir = tmp_path / "roles"
    roles_dir.mkdir()
    (roles_dir / "operational.md").write_text("Role text", encoding="utf-8")
    (roles_dir / "strategic.md").write_text("Plan text", encoding="utf-8")

    loaded = load_prompt_directory(
        roles_dir,
        lambda path: path.read_text(encoding="utf-8"),
    )

    assert loaded == {"operational": "Role text", "strategic": "Plan text"}


def test_merge_workspace_skill_overrides_prefers_workspace_content(tmp_path: Path):
    builtin = {"debug": "builtin debug", "review": "builtin review"}
    ws1 = tmp_path / ".codingAgent" / "skills"
    ws1.mkdir(parents=True)
    (ws1 / "debug.md").write_text("workspace debug", encoding="utf-8")
    ws2 = tmp_path / ".claude" / "skills"
    ws2.mkdir(parents=True)
    (ws2 / "extra.md").write_text("extra skill", encoding="utf-8")

    merged = merge_workspace_skill_overrides(
        builtin,
        [ws1, ws2],
        lambda path: path.read_text(encoding="utf-8"),
    )

    assert merged["debug"] == "workspace debug"
    assert merged["review"] == "builtin review"
    assert merged["extra"] == "extra skill"
