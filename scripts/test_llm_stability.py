import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

import asyncio
from typing import Any, Dict
from src.core.inference.adapters.lm_studio_adapter import LmStudioAdapter
from src.core.inference.adapters.ollama_adapter import OllamaAdapter
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def run_stability_test(
    adapter: Any, adapter_name: str, num_requests: int = 100
) -> int:
    """Run *num_requests* parallel requests against *adapter* and return failure count."""
    logger.info(f"--- Running stability test for {adapter_name} ---")
    failures = 0

    _prompt_msg = [
        {
            "role": "user",
            "content": "Write a short, simple python function that sums two numbers.",
        }
    ]

    tasks = []
    for _ in range(num_requests):
        tasks.append(
            asyncio.create_task(asyncio.to_thread(adapter.generate, _prompt_msg))
        )

    responses = await asyncio.gather(*tasks, return_exceptions=True)

    for i, res in enumerate(responses):
        if isinstance(res, Exception):
            failures += 1
            logger.error(f"Request {i + 1}/{num_requests} FAILED: {res}")
        else:
            resp: Dict[str, Any] = res  # type: ignore[assignment]
            choices = resp.get("choices") or []
            content = ""
            if choices and isinstance(choices[0], dict):
                msg = choices[0].get("message") or {}
                content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
            usage = resp.get("usage") or {}
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            logger.info(
                f"Request {i + 1}/{num_requests} SUCCEEDED. "
                f"Tokens: P={prompt_tokens}/C={completion_tokens}, "
                f"Output: {content[:80].strip()}..."
            )

    if failures == 0:
        logger.info(
            f"--- {adapter_name} Stability Test PASSED: All {num_requests} requests succeeded! ---"
        )
    else:
        logger.error(
            f"--- {adapter_name} Stability Test FAILED: {failures}/{num_requests} requests failed. ---"
        )

    return failures


async def main() -> None:
    lm_studio = LmStudioAdapter()
    ollama = OllamaAdapter()

    # Run tests sequentially to avoid overwhelming local servers
    lm_studio_failures = await run_stability_test(lm_studio, "LM Studio")
    ollama_failures = await run_stability_test(ollama, "Ollama")

    if lm_studio_failures > 0 or ollama_failures > 0:
        logger.error("One or more stability tests failed.")
        exit(1)
    else:
        logger.info("All stability tests passed successfully!")


if __name__ == "__main__":
    asyncio.run(main())
