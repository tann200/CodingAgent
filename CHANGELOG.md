# Changelog

All notable changes to this project are recorded in this file.

## Unreleased (todo-tools-atomic-save)

- Centralized and hardened TODO persistence (`src/tools/todo_tools.py`):
  - Atomic writes for TODO.md and todo.json using temp files, fsync, atomic replace,
    backups and restore-on-failure.
  - Per-workdir advisory locking via `_FileLock`: prefer `fcntl.flock` when
    available; fallback to exclusive lockfile (O_EXCL) with diagnostic contents.
  - Conservative stale-lock reclaim using host + TTL; reclaim on network
    filesystems is disabled by default and can be overridden with
    `TODO_ALLOW_STALE_RECLAIM_ON_NFS=1`.

- Read-before-write (RBW) notifier consolidation and ContextBuilder cache
  invalidation helpers; best-effort metrics for RBW failures.

-- In-process metrics (lock and RBW counters) kept in-memory. No built-in
  Prometheus export is provided.

- Tests & stress tooling:
  - Unit tests for locking, stale reclaim, and RBW metrics.
  - Cross-process stress runner and worker scripts under `tests/utils/`.

-- Documentation:
  - `docs/TODO_METRICS.md` documents the in-process metrics and how to read
    them programmatically.
  - Updated `docs/IMPLEMENTATION_TASKS.md` and `README.md` to reference metrics.

### Notes / Caveats

- Reclaiming stale locks on network filesystems is inherently risky; the
  implementation errs on the side of safety and requires an explicit override
  to allow reclaim on NFS/CIFS/SMB.
-- Metrics are intentionally lightweight and in-process to keep the project
  dependency-free for solo development.
