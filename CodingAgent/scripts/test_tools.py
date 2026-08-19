#!/usr/bin/env python3
"""Script to test tool registry and orchestrator read/write sandbox behavior."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.orchestration.orchestrator import Orchestrator, example_registry


def main():
    adapter_path = ROOT / 'src' / 'config' / 'providers.json'
    # create an orchestrator with default working_dir (repo-root/output)
    orch = Orchestrator(None, tool_registry=example_registry())
    print('Working dir:', orch.working_dir)
    # perform write_file tool test
    write_tc = {'name': 'write_file', 'arguments': {'path': 'test_dir/hello.txt', 'content': 'hello world'}}
    pre = orch.preflight_check(write_tc)
    print('preflight:', pre)
    if not pre.get('ok'):
        print('preflight failed', pre)
        return
    res = orch.execute_tool(write_tc)
    print('write result:', res)
    # read back
    read_tc = {'name': 'read_file', 'arguments': {'path': str(orch.working_dir / 'test_dir' / 'hello.txt')}}
    res2 = orch.execute_tool(read_tc)
    print('read result:', res2)


if __name__ == '__main__':
    main()

