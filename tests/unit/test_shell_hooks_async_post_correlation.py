import asyncio

from src.core.orchestration.event_bus import new_correlation_id
from src.core.orchestration.shell_hooks import ShellHookRunner, HookResult


class CapturingShellHookRunner(ShellHookRunner):
    def run_post(self, tool_name: str, args: dict, result: dict) -> HookResult:
        from src.core.orchestration.event_bus import get_correlation_id

        cid = get_correlation_id()
        return HookResult.allow([cid or ""])  # return correlation id as message


def test_async_run_post_preserves_correlation() -> None:
    cid = new_correlation_id()
    runner = CapturingShellHookRunner()

    # run the async wrapper and ensure the correlation id was observed inside run_post
    res = asyncio.run(runner.async_run_post("tool", {}, {"ok": True}))
    assert isinstance(res, HookResult)
    assert res.messages and res.messages[0] == cid
