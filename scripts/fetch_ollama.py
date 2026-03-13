#!/usr/bin/env python3
"""Fetch Ollama /api/tags, /api/show, /api/generate and print pretty JSON output.

Usage:
  python3 scripts/fetch_ollama.py

This script will try common host/port combos (127.0.0.1 and localhost; ports 11434 and 63771).
It prints responses and helpful diagnostics.
"""
import json
import sys
import requests

HOSTS = ["127.0.0.1", "localhost"]
PORTS = [11434, 63771]
MODEL = "qwen3.5:9b"


def try_request(method, url, **kwargs):
    try:
        r = requests.request(method, url, timeout=10, **kwargs)
        return (r.status_code, r.text)
    except Exception as e:
        return (None, f"ERROR: {e}")


def pretty_print(title, status, body):
    print('\n' + '='*10 + f' {title} ' + '='*10)
    print('Status:', status)
    try:
        j = json.loads(body)
        print(json.dumps(j, indent=2, ensure_ascii=False))
    except Exception:
        print(body[:2000])


def main():
    for host in HOSTS:
        for port in PORTS:
            base = f'http://{host}:{port}'
            print('\n\n' + '#' * 10 + f' Testing {base} ' + '#' * 10)
            # /api/tags
            status, body = try_request('GET', base + '/api/tags')
            pretty_print(base + '/api/tags', status, body)

            # /api/show
            status, body = try_request('POST', base + '/api/show', json={'model': MODEL})
            pretty_print(base + '/api/show', status, body)

            # /api/generate
            payload = {'model': MODEL, 'prompt': 'Why is the sky blue?', 'stream': False}
            status, body = try_request('POST', base + '/api/generate', json=payload)
            pretty_print(base + '/api/generate', status, body)

    print('\nDone. If you get connection errors, ensure Ollama is running and listening on the host:port shown by `netstat -an | grep 11434` or `ps aux | grep ollama`.')

if __name__ == '__main__':
    main()

