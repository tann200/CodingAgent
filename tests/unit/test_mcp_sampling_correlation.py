import json

from src.core.orchestration.event_bus import new_correlation_id
from src.core.orchestration.mcp_stdio_server import MCPStdioServer, JsonRpcRequest


class _StubOrch:
    async def call_model(self, messages, max_tokens=256):
        # Return the current correlation id observed in this context
        from src.core.orchestration.event_bus import get_correlation_id

        return get_correlation_id()


def test_sampling_create_preserves_correlation() -> None:
    cid = new_correlation_id()
    orch = _StubOrch()
    server = MCPStdioServer(orchestrator=orch)

    req = JsonRpcRequest(
        method="sampling/create",
        params={
            "messages": [{"role": "user", "content": {"text": "hi"}}],
            "maxTokens": 1,
        },
    )
    resp_str = server._handle_request(req)
    resp = json.loads(resp_str)
    result = resp.get("result", {})
    # sampling/create returns result.content.text when available
    content = None
    if isinstance(result.get("content"), dict):
        content = result.get("content", {}).get("text")
    else:
        content = result.get("content")

    assert content == cid
