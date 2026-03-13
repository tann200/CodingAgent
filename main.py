"""Top-level shim kept for backward compatibility.
This file delegates to `src.main` which contains the real entrypoint.
"""
from __future__ import annotations

if __name__ == '__main__':
    # Delegate to src.main so existing scripts calling main.py continue to work
    try:
        from src.main import main
        raise SystemExit(main())
    except Exception as e:
        print(f"Failed to invoke src.main: {e}")
        raise
