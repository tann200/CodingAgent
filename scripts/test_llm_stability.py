import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parents[1]))

import asyncio
from src.core.inference.llm_client import LLMClient
from src.adapters.lm_studio_adapter import LMStudioAdapter
from src.adapters.ollama_adapter import OllamaAdapter
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def run_stability_test(client: LLMClient, num_requests: int = 100):
    logger.info(f"--- Running stability test for {client.name} ---")
    failures = 0
    
    tasks = []
    for i in range(num_requests):
        tasks.append(
            asyncio.create_task(
                # Use asyncio.to_thread for the sync generate method
                asyncio.to_thread(
                    client.generate,
                    prompt="Write a short, simple python function that sums two numbers."
                )
            )
        )
    
    responses = await asyncio.gather(*tasks, return_exceptions=True)
    
    for i, res in enumerate(responses):
        if isinstance(res, Exception):
            failures += 1
            logger.error(f"Request {i+1}/{num_requests} FAILED: {res}")
        else:
            logger.info(
                f"Request {i+1}/{num_requests} SUCCEEDED. "
                f"Latency: {res.latency:.2f}s, "
                f"Tokens: P={res.prompt_tokens}/C={res.completion_tokens}, "
                f"Output: {res.content[:80].strip()}..."
            )
            
    if failures == 0:
        logger.info(f"--- {client.name} Stability Test PASSED: All {num_requests} requests succeeded! ---")
    else:
        logger.error(f"--- {client.name} Stability Test FAILED: {failures}/{num_requests} requests failed. ---")
    
    return failures

async def main():
    lm_studio = LMStudioAdapter()
    ollama = OllamaAdapter()
    
    # Run tests sequentially to avoid overwhelming local servers
    lm_studio_failures = await run_stability_test(lm_studio)
    ollama_failures = await run_stability_test(ollama)
    
    if lm_studio_failures > 0 or ollama_failures > 0:
        logger.error("One or more stability tests failed.")
        exit(1)
    else:
        logger.info("All stability tests passed successfully!")

if __name__ == "__main__":
    asyncio.run(main())
