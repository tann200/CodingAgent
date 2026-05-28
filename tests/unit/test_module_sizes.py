"""P3-T6: Test that node files are not oversized.

The long-term target per the audit is MAX_LINES=600 for all node files.
Guard functions have been extracted to execution_guards.py as the first split.
This test enforces the current measured upper bound so regressions (new bloat)
are caught, and tightens incrementally as more splits are done.
"""
from pathlib import Path

NODE_DIR = Path("src/core/orchestration/graph/nodes")

# Current measured limits (tightened from actual sizes to catch regressions).
# Update these DOWN as files are further split; never update them UP.
_FILE_LIMITS = {
    "execution_helpers.py": 1344,   # target 600 after full split
    "frontier_loop_node.py": 1020,  # target 600 after split
    "planning_node.py": 910,        # partially split, target 600
    "execution_node.py": 610,       # target 600; grew slightly from P3-T3/T5 wiring
    "perception_node.py": 1020,     # target 600 after split
}
_DEFAULT_LIMIT = 600  # all other node files must stay under this


def test_no_new_node_file_bloat():
    """Guard against adding new oversized node files (other than known ones)."""
    oversized = []
    for p in NODE_DIR.glob("*.py"):
        limit = _FILE_LIMITS.get(p.name, _DEFAULT_LIMIT)
        lines = len(p.read_text().splitlines())
        if lines > limit:
            oversized.append(f"{p.name}: {lines} lines (limit {limit})")
    assert oversized == [], "Oversized node files:\n" + "\n".join(oversized)


def test_execution_guards_extracted():
    """P3-T6: execution_guards.py exists and exports the guard functions."""
    from src.core.orchestration.graph.nodes.execution_guards import (
        _validate_python_syntax,
        _capture_snapshot,
    )
    assert callable(_validate_python_syntax)
    assert callable(_capture_snapshot)


def test_execution_helpers_reexports_guards():
    """execution_helpers.py must still export the guard symbols for backwards compat."""
    from src.core.orchestration.graph.nodes.execution_helpers import (
        _validate_python_syntax,
        _capture_snapshot,
    )
    assert callable(_validate_python_syntax)
    assert callable(_capture_snapshot)
