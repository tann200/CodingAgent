"""Application entrypoint moved into src package.

This module now delegates entirely to `src.ui.app.CodingAgentApp` which
is responsible for choosing Textual vs headless behavior. This centralizes
startup logic and avoids duplicated Textual detection.
"""

from __future__ import annotations

import os
import sys
from typing import Optional


# tiny debug helper
def _dbg(msg: str) -> None:
    try:
        print(msg, flush=True)
    except Exception:
        pass
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        p = os.path.join(root, "tmp_debug_main.log")
        with open(p, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


# Ensure project root is on sys.path when executed as script
if __package__ is None:
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _root not in sys.path:
        sys.path.insert(0, _root)
        _dbg(f"[src.main] Inserted project root into sys.path: {_root}")


def main(argv: Optional[list] = None) -> int:
    argv = argv or sys.argv[1:]
    _dbg(f"[src.main] main() starting; argv={argv}")

    try:
        from src.ui.app import CodingAgentApp, AppConfig

        cfg = AppConfig()
        app = CodingAgentApp(config=cfg)
        _dbg("[src.main] Delegating startup to CodingAgentApp.run()")
        app.run()
        _dbg("[src.main] app.run() returned")
        return 0
    except Exception as e:
        _dbg(f"[src.main] Failed to start app: {e}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
