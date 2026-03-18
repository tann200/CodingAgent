"""Simulate TUI calls by sending OpenAI-style messages to llm_manager.call_model.

Usage:
  RUN_INTEGRATION=1 python scripts/simulate_tui.py --prompt "Why is the sky blue?"

This script requires Ollama running locally for meaningful results.
"""

import argparse
import asyncio
from pathlib import Path
from src.core.inference.llm_manager import call_model


def build_messages(system_prompt_path: str, user_prompt: str):
    sys_text = (
        Path(system_prompt_path).read_text(encoding="utf-8")
        if Path(system_prompt_path).exists()
        else ""
    )
    messages = [
        {"role": "system", "content": sys_text},
        {"role": "user", "content": user_prompt},
    ]
    return messages


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--system", default="agent-brain/system_prompt_coding.md")
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--provider", default=None)
    parser.add_argument("--model", default=None)
    args = parser.parse_args()

    messages = build_messages(args.system, args.prompt)
    print("Sending messages:")
    for m in messages:
        print(f"{m['role']}: {m['content'][:120]}")
    try:
        resp = await call_model(
            messages,
            provider=args.provider,
            model=args.model,
            stream=False,
            format_json=False,
        )
        print("Response:")
        print(resp)
    except Exception as e:
        print("call_model failed:", e)


if __name__ == "__main__":
    asyncio.run(main())
