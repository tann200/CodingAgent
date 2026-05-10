from src.core.orchestration.graph.nodes.perception_compaction import (
    _bootstrap_history_for_prompt,
)


def test_bootstrap_history_for_prompt_uses_compacted_base_and_appends_unseen_raw_turns():
    result = _bootstrap_history_for_prompt(
        {
            "_compacted_history": [
                {"role": "system", "content": "compact-a"},
                {"role": "assistant", "content": "compact-b"},
            ],
            "history": [
                {"role": "system", "content": "compact-a"},
                {"role": "assistant", "content": "compact-b"},
                {"role": "user", "content": "new-turn"},
            ],
        }
    )

    assert result == [
        {"role": "system", "content": "compact-a"},
        {"role": "assistant", "content": "compact-b"},
        {"role": "user", "content": "new-turn"},
    ]


def test_bootstrap_history_for_prompt_falls_back_to_raw_history_without_compacted_base():
    result = _bootstrap_history_for_prompt(
        {
            "history": [
                {"role": "user", "content": "one"},
                {"role": "assistant", "content": "two"},
            ]
        }
    )

    assert result == [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
    ]
