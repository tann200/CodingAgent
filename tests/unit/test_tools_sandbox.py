import os
import sys
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.core.orchestration.orchestrator import Orchestrator, example_registry  # noqa: E402


def test_write_and_read_within_working_dir(tmp_path):
    # Create a temporary repo root by pointing working_dir inside tmp_path
    reg = example_registry()
    orch = Orchestrator(None, tool_registry=reg, working_dir=str(tmp_path), allow_external_working_dir=True)
    # write file
    tc = {'name': 'write_file', 'arguments': {'path': 'sub/hello.txt', 'content': 'hi'}}
    pre = orch.preflight_check(tc)
    assert pre['ok']
    res = orch.execute_tool(tc)
    assert res['ok']
    # read back
    read_tc = {'name': 'read_file', 'arguments': {'path': str(tmp_path / 'sub' / 'hello.txt')}}
    r2 = orch.execute_tool(read_tc)
    assert r2['ok']
    assert 'hi' in r2['result']['content']


def test_write_outside_working_dir(tmp_path):
    reg = example_registry()
    orch = Orchestrator(None, tool_registry=reg, working_dir=str(tmp_path), allow_external_working_dir=True)
    # attempt to write outside working dir
    outside = str(Path(tmp_path).parent / 'outside.txt')
    tc = {'name': 'write_file', 'arguments': {'path': outside, 'content': 'bad'}}
    pre = orch.preflight_check(tc)
    assert not pre['ok']
