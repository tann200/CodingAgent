"""Terminal control-sequence sanitizer for untrusted TUI text sinks (TASK-37).

Strips ANSI CSI, OSC, DCS, APC, PM, SOS, standalone ESC sequences, and
unsafe C0/C1 control characters (preserving newline ``\\n`` and tab ``\\t``)
so repository files, tool output, or model output cannot inject terminal
escape sequences or spoof terminal titles.
"""

from __future__ import annotations

import re

# CSI: ESC [ params intermediates final-byte
_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
# OSC: ESC ] ... (BEL or ST)
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
# DCS / SOS / PM / APC: ESC [PX^_] ... ST
_ST_SEQ_RE = re.compile(r"\x1b[PX^_][^\x1b]*(?:\x1b\\|\x07)")
# Two-byte ESC sequences (e.g. ESC c, ESC 7, ESC ( B)
_ESC_OTHER_RE = re.compile(r"\x1b(?:[@-Z\\-_]|[ -/]+[@-~])")
# C1 8-bit control characters (0x80-0x9F) and unsafe C0 bytes (except \t=0x09, \n=0x0A)
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def strip_terminal_controls(text: str) -> str:
    """Remove terminal escape and control sequences while keeping ``\\n`` and ``\\t``."""
    if not text:
        return ""
    s = str(text)
    if "\x1b" in s:
        s = _OSC_RE.sub("", s)
        s = _ST_SEQ_RE.sub("", s)
        s = _CSI_RE.sub("", s)
        s = _ESC_OTHER_RE.sub("", s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return _CTRL_RE.sub("", s)
