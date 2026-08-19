"""Wait for a model to be loaded in LM Studio's /v1/models endpoint.

Usage:
LM_STUDIO_URL should be provided as env var or CLI arg (same as diagnose script).
Model id should be provided as the second arg or via MODEL_ID env var.

Example:
LM_STUDIO_URL=http://localhost:1234/v1 MODEL_ID=qwen3.5:9b python3 scripts/wait_for_model.py --timeout 600

This will poll /v1/models every few seconds until the model has at least one
loaded_instances entry, or until timeout.
"""
import os
import sys
import time
import requests

LM_STUDIO_URL = os.getenv('LM_STUDIO_URL') or (sys.argv[1] if len(sys.argv) > 1 else None)
MODEL_ID = os.getenv('MODEL_ID') or (sys.argv[2] if len(sys.argv) > 2 else None)

# Allow flags: --timeout seconds
import argparse
parser = argparse.ArgumentParser()
parser.add_argument('--timeout', type=int, default=600, help='overall timeout seconds')
parser.add_argument('--interval', type=int, default=5, help='poll interval seconds')
parser.add_argument('--model', type=str, default=None, help='model id to wait for')
args, _ = parser.parse_known_args()

if args.model:
    MODEL_ID = args.model

if not LM_STUDIO_URL:
    print('ERROR: LM_STUDIO_URL is required. Set ENV or pass as first arg.')
    print('Example: LM_STUDIO_URL=http://localhost:1234/v1 MODEL_ID=qwen3.5:9b python3 scripts/wait_for_model.py')
    sys.exit(2)

if not MODEL_ID:
    print('ERROR: MODEL_ID is required. Set ENV or pass as second arg or --model flag.')
    sys.exit(2)

v1_base = LM_STUDIO_URL.rstrip('/')
print(f'Waiting for model {MODEL_ID} to be loaded on {v1_base} (timeout={args.timeout}s)')

start = time.time()
while True:
    try:
        r = requests.get(f"{v1_base}/models", timeout=10)
        if r.status_code == 200:
            data = r.json()
            # find model entry
            for m in data.get('data', []):
                if isinstance(m, dict) and m.get('id') == MODEL_ID:
                    loaded = m.get('loaded_instances') or []
                    if len(loaded) > 0:
                        print(f"Model {MODEL_ID} is loaded (instances: {len(loaded)})")
                        sys.exit(0)
                    else:
                        print(f"Model {MODEL_ID} not loaded yet; loaded_instances is empty")
                        break
        else:
            print(f"GET /models returned {r.status_code}; body: {r.text[:200]}")
    except Exception as e:
        print('Request failed:', e)

    if time.time() - start > args.timeout:
        print(f"Timeout waiting for model {MODEL_ID} after {args.timeout}s")
        sys.exit(1)
    time.sleep(args.interval)

