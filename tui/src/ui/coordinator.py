from __future__ import annotations
from typing import Tuple

from src.ui.logging import get_logger

logger = get_logger("coordinator")


class Coordinator:
    def get_sidebar_data(self) -> Tuple[int, int]:
        return 0, 0
