#!/usr/bin/env python3
import json
from src.tools.todo_tools import get_lock_metrics, get_rbw_metrics


def main() -> None:
    out = {
        "lock_metrics": get_lock_metrics(),
        "rbw_metrics": get_rbw_metrics(),
    }
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
