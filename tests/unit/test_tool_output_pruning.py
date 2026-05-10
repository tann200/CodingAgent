import json

from src.core.context.tool_output_pruning import prune_stale_tool_outputs


def _tool_result(tool_name: str, *, ok: bool = True, extra: str = ""):
    return {
        "role": "user",
        "content": json.dumps(
            {
                "tool_execution_result": {
                    "tool_name": tool_name,
                    "ok": ok,
                    "content": f"payload {extra}",
                }
            }
        ),
    }


def test_prune_stale_tool_outputs_prunes_older_results_but_keeps_recent_ones():
    messages = [
        _tool_result("old-tool"),
        {"role": "assistant", "content": "thinking"},
        _tool_result("recent-tool-1"),
        _tool_result("recent-tool-2", ok=False),
    ]
    pruned = prune_stale_tool_outputs(messages, stale_after_turns=2)

    old_payload = json.loads(pruned[0]["content"])
    assert old_payload["tool_execution_result"]["_pruned"] is True
    assert old_payload["tool_execution_result"]["tool_name"] == "old-tool"
    assert pruned[2] == messages[2]
    assert pruned[3] == messages[3]


def test_prune_stale_tool_outputs_keeps_matching_current_step_hint_and_invalid_json():
    matching = _tool_result("hinted-tool", extra="edit src/app.py")
    invalid = {"role": "user", "content": "tool_execution_result not-json"}

    pruned = prune_stale_tool_outputs(
        [matching, invalid],
        current_step_hint="src/app.py",
        stale_after_turns=0,
    )

    assert pruned[0] == matching
    assert pruned[1] == invalid
