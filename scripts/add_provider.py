t#!/usr/bin/env python3
"""Simple CLI to add a provider to src/config/providers.json for development and testing.
Usage: python scripts/add_provider.py --name lm_studio --type lm_studio --base_url http://localhost:1234
"""
import argparse
import json
from pathlib import Path

DEFAULT_PATH = Path(__file__).parents[1] / 'src' / 'config' / 'providers.json'


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--name', required=True)
    p.add_argument('--type', required=True)
    p.add_argument('--base_url', required=True)
    p.add_argument('--api_key', required=False)
    p.add_argument('--models', nargs='*', default=[])
    args = p.parse_args()

    cfg = {
        'name': args.name,
        'type': args.type,
        'base_url': args.base_url,
        'api_key': args.api_key,
        'models': args.models,
    }

    DEFAULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_PATH.write_text(json.dumps([cfg], indent=2), encoding='utf-8')
    print(f'Wrote provider to {DEFAULT_PATH}')


if __name__ == '__main__':
    main()

