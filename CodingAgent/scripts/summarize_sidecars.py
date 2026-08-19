#!/usr/bin/env python3
"""Summarize session_store_write_failure sidecar JSON files.

Usage:
  python3 scripts/summarize_sidecars.py --artifacts tests/_ci_artifacts --out tests/_ci_artifacts/parsed_sidecars_summary_detailed.json

This script scans the provided artifacts directories for
"session_store_write_failure_*.json" sidecars, extracts a representative
call-site (first frame of the captured call_stack when present), and
aggregates counts by call-site, thread, and session id. The output is
written as a JSON summary that's easy to inspect or feed into further
analysis.
"""

from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List
import collections
import os


def find_sidecars(artifacts_dir: str) -> List[str]:
    p = Path(artifacts_dir)
    if not p.exists():
        return []
    return [str(f) for f in p.rglob("session_store_write_failure_*.json")]


def summarize(paths: List[str]) -> Dict:
    total = 0
    call_sites = collections.Counter()
    threads = collections.Counter()
    per_site_threads: Dict[str, collections.Counter] = {}
    per_site_sessions: Dict[str, collections.Counter] = {}
    example_for_site: Dict[str, str] = {}

    for p in sorted(paths):
        try:
            data = json.loads(Path(p).read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"Failed to parse {p}: {exc}", file=sys.stderr)
            continue
        total += 1

        call_stack = data.get("call_stack")
        if isinstance(call_stack, list) and len(call_stack) > 0:
            fr = call_stack[0]
            filename = os.path.basename(fr.get("file") or "")
            lineno = fr.get("lineno") or ""
            func = fr.get("function") or ""
            call_site = f"{filename}:{lineno}:{func}"
        else:
            call_site = data.get("call_site") or "unknown"

        call_sites[call_site] += 1

        thread = data.get("thread") or data.get("thread_name") or "unknown"
        threads[thread] += 1
        per_site_threads.setdefault(call_site, collections.Counter())[thread] += 1

        orig = data.get("original_session_id", None)
        sid = orig if orig is not None else data.get("session_id")
        sid_key = str(sid)
        per_site_sessions.setdefault(call_site, collections.Counter())[sid_key] += 1

        if call_site not in example_for_site:
            example_for_site[call_site] = p

    call_site_list = []
    for cs, cnt in call_sites.most_common():
        call_site_list.append(
            {
                "call_site": cs,
                "count": cnt,
                "threads": per_site_threads.get(cs, {}).most_common(),
                "session_id_counts": per_site_sessions.get(cs, {}).most_common(),
                "example_sidecar": example_for_site.get(cs),
            }
        )

    return {
        "total_sidecars": total,
        "call_sites": call_site_list,
        "top_threads": threads.most_common(),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--artifacts",
        "-a",
        nargs="+",
        default=["tests/_ci_artifacts"],
        help="Artifacts directories to scan",
    )
    parser.add_argument(
        "--out", "-o", default=None, help="Optional output path for JSON summary"
    )
    args = parser.parse_args()

    all_paths: List[str] = []
    for ad in args.artifacts:
        all_paths.extend(find_sidecars(ad))

    if not all_paths:
        print("No sidecar files found in the provided artifacts dirs.", file=sys.stderr)
        sys.exit(2)

    summary = summarize(all_paths)
    out_text = json.dumps(summary, indent=2)

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(out_text, encoding="utf-8")
        print(f"Wrote summary to {out_path}")
    else:
        print(out_text)


if __name__ == "__main__":
    main()
