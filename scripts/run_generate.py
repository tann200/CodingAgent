#!/usr/bin/env python3
import json
import os
import sys
import types

# Ensure repo root is on path
ROOT = os.path.dirname(os.path.dirname(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.adapters.ollama_adapter import OllamaAdapter


def main():
    config_path = os.path.join(ROOT, 'src/config/providers.json')
    adapter = OllamaAdapter(config_path)

    prompt = "You are a strict JSON-only assistant. Respond ONLY with valid JSON.\nReturn an object with keys: summary (string), explanation (string).\nUser question: Why is the sky blue?"
    try:
        res = adapter.generate(prompt, stream=False, format_json=True)
        print('\n=== Raw result ===')
        # Handle generator (streamed) or normal dict
        if isinstance(res, types.GeneratorType) or hasattr(res, '__iter__') and not isinstance(res, (dict, list, str)):
            print('Received streaming generator response; collecting chunks...')
            chunks = []
            for chunk in res:
                chunks.append(chunk)
            print(json.dumps(chunks, indent=2, ensure_ascii=False))
            # try to find a final response chunk with 'response' or similar
            final = None
            for c in reversed(chunks):
                if isinstance(c, dict) and ('response' in c or 'message' in c or 'done' in c):
                    final = c
                    break
            if final is None:
                print('\nNo final chunk detected; streaming chunks shown above.')
                return
            res = final
        else:
            # safe to dump dict/list/str
            try:
                print(json.dumps(res, indent=2, ensure_ascii=False))
            except TypeError:
                print(res)

        parsed = None
        if isinstance(res, dict):
            parsed = res.get('response')

        print('\n=== Parsed response ===')
        if isinstance(parsed, dict):
            summary = parsed.get('summary')
            explanation = parsed.get('explanation')
            print('summary:', summary)
            print('explanation:', explanation)
        else:
            print('Response was not parsed JSON. Raw response:')
            print(res.get('response_raw') if isinstance(res, dict) else res)

    except Exception as e:
        print('Error during generation:', repr(e))


if __name__ == '__main__':
    main()

