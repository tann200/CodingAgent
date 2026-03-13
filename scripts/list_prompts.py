#!/usr/bin/env python3
from pathlib import Path
import argparse
import json
import re

try:
    import yaml
    _HAS_YAML = True
except Exception:
    yaml = None
    _HAS_YAML = False


def parse_front_matter(text: str):
    if not text or not text.startswith('---'):
        return None
    m = re.match(r'^---\s*\n(.*?)(\n---\s*\n)', text, flags=re.S)
    if not m:
        return None
    body = m.group(1)
    if _HAS_YAML and yaml is not None:
        try:
            return yaml.safe_load(body)
        except Exception:
            return None
    # fallback simple parser
    out = {}
    for line in body.splitlines():
        if ':' not in line:
            continue
        k, v = line.split(':', 1)
        out[k.strip()] = v.strip().strip('"')
    return out


def list_prompts(brain_dir: Path):
    results = []
    for p in sorted(brain_dir.rglob('*.md')):
        try:
            txt = p.read_text(encoding='utf-8')
        except Exception:
            txt = ''
        fm = parse_front_matter(txt)
        results.append({'path': str(p), 'front_matter': fm})
    return results


def main():
    parser = argparse.ArgumentParser(description='List agent prompts and front-matter metadata from agent-brain')
    parser.add_argument('--brain', default='agent-brain', help='Path to agent-brain folder')
    parser.add_argument('--json', action='store_true', help='Output JSON')
    args = parser.parse_args()

    bd = Path(args.brain)
    if not bd.exists():
        print('agent-brain directory not found:', bd)
        return
    prompts = list_prompts(bd)
    if args.json:
        print(json.dumps(prompts, indent=2, ensure_ascii=False))
    else:
        for p in prompts:
            print(p['path'])
            if p['front_matter']:
                for k, v in p['front_matter'].items():
                    print(f'  {k}: {v}')
            print()

if __name__ == '__main__':
    main()

