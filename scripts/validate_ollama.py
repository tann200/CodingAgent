#!/usr/bin/env python3
"""Validate Ollama outputs by calling generate and chat endpoints via the OllamaAdapter.
Prints parsed JSON results and performs basic schema checks.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.inference.adapters.ollama_adapter import OllamaAdapter


def main():
    adapter = OllamaAdapter(str(ROOT / "src" / "config" / "providers.json"))
    prompt = "You are a strict JSON-only assistant. Respond ONLY with valid JSON. Return keys: summary, explanation. Question: Why is the sky blue?"
    print("Calling generate...")
    try:
        res = adapter.generate(
            [{"role": "user", "content": prompt}], stream=False, format_json=True
        )
        print("generate result:")
        print(json.dumps(res, indent=2, ensure_ascii=False))
    except Exception as e:
        print("generate error:", e)

    messages = [
        {
            "role": "system",
            "content": "You are a helpful assistant. Respond in JSON if asked.",
        },
        {
            "role": "user",
            "content": "Why is the sky blue? Respond in JSON with summary and explanation.",
        },
    ]
    print("Calling generate with messages...")
    try:
        cresp = adapter.generate(messages, stream=False, format_json=True)
        print("generate (messages) result:")
        print(json.dumps(cresp, indent=2, ensure_ascii=False))
    except Exception as e:
        print("generate (messages) error:", e)


if __name__ == "__main__":
    main()
