"""Entry point for ``python -m src.core.evaluation``."""

import sys

from src.core.evaluation.cli import main

if __name__ == "__main__":
    sys.exit(main())
