"""P3-T6: Test that node files are not oversized.

The long-term target per the audit is MAX_LINES=600 for all node files.
This test enforces the current measured upper bound so regressions (new bloat)
are caught.
"""
from pathlib import Path

NODE_DIR = Path("src/core/orchestration/graph/nodes")

# Current measured limits (tightened from actual sizes to catch regressions).
_FILE_LIMITS = {
    "frontier_loop_node.py": 899,
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
