# Changelog

All notable changes to this project are recorded in this file.

## Unreleased

- Orchestration routing & router purity fixes (`src/core/orchestration/graph/builder.py`):
  - Rewrote `route_after_perception` to make routing deterministic and
    precedence-aware:
    - Top-level short-circuits for clarification and context overflow.
    - Robust extraction of `next_action` from multiple shapes (string/dict).
    - First-round behavior respects `task_complexity` and `model_tier` (NANO/SMALL
      fast-paths, LARGE/FRONTIER planning shortcuts).
    - Subsequent-round precedence: read-only tools favour analysis while write
      or unknown tools favour execution.
  - Added canonical constants for tool-type checks and ensured routers use
    them rather than duplicated literal sets.
  - Implemented backward-compatible, pure wrapper routers (typed and
    docstring-safe) required by tests.
  - Ensured wrappers do not call token budget compaction helpers or mutate
    the provided state (purity enforced to satisfy tests).

- CI / repo housekeeping:
  - Added tests to verify canonical constants usage and router purity.
  - Cherry-picked/merged routing fixes into `main` and removed temporary
    local branches created during the merge process.

### Notes

- The vectorstore/LanceDB branch was intentionally left unmerged; no LanceDB
  code was introduced in the routing fixes. Local temporary branches and the
  local vectorstore branch were removed per request.
