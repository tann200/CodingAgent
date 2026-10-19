"""CodingAgent TUI package.

The canonical source tree is located in `tui.tui_src`.
For backwards compatibility with legacy scripts and tests, `tui.src` is provided
as a dynamic alias to `tui.tui_src`.
"""

from __future__ import annotations

import importlib
import sys
from importlib.util import spec_from_loader
from typing import Any

from . import tui_src


class _TuiSrcAliasFinder:
    """Meta-path finder that transparently redirects any `tui.src.*` import to `tui.tui_src.*`."""

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        if fullname == "tui.src" or fullname.startswith("tui.src."):
            target_name = "tui.tui_src" + fullname[len("tui.src"):]
            try:
                mod = importlib.import_module(target_name)
            except Exception:
                return None

            sys.modules[fullname] = mod

            class _ForwardLoader:
                def create_module(self, spec: Any) -> Any:
                    return mod

                def exec_module(self, module: Any) -> None:
                    pass

            return spec_from_loader(fullname, _ForwardLoader())
        return None


if not any(isinstance(finder, _TuiSrcAliasFinder) for finder in sys.meta_path):
    sys.meta_path.insert(0, _TuiSrcAliasFinder())

# Direct alias for sys.modules["tui.src"]
sys.modules.setdefault("tui.src", tui_src)
if hasattr(tui_src, "ui"):
    sys.modules.setdefault("tui.src.ui", tui_src.ui)
