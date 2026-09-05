"""Stable UTF-8 output for redirected CLI streams on every supported OS."""

from __future__ import annotations

import sys


def configure_standard_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            # StringIO, IDE capture, and closed streams may not support it.
            continue
