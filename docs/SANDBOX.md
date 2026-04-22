Sandboxing (bubblewrap)
=======================

This project supports optional sandboxed execution using bubblewrap (`bwrap`). The
implementation detects `bwrap` at import time and runs a lightweight probe
(`bwrap --version`) to ensure the binary responds before enabling sandboxed
execution.

Behavior
--------
- If `bwrap` is available and responds to `--version` the library will use it to
  run tools inside a constrained container. The default sandbox level is
  controlled by `CODINGAGENT_SANDBOX_LEVEL` (default: `workspace`).
- If `bwrap` is not available or the probe fails, the code falls back to
  non-sandboxed `subprocess.run` and emits a `system.warning` event at startup
  when the default sandbox level is not `off`.

Developer notes
---------------
- The probe runs with a 3s timeout during import; it is defensive and will not
  raise on import errors.
- The sandbox wrapper builds a conservative mount list rather than ro-binding
  `/` directly. It also sets `--die-with-parent` to avoid leaving orphaned
  processes.

If you want to force sandboxing off in environments without `bwrap`, set
`CODINGAGENT_SANDBOX_LEVEL=off` in your environment.
