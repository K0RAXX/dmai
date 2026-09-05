"""Layer C -- the desktop window.

A client of `GameSession`, exactly as the CLI and the OpenClaw adapter are.
Importing this package must not require pywebview: `dmai.desktop.bridge` is
plain Python and is tested headless, and only `dmai.desktop.app` needs a
window toolkit.
"""

from __future__ import annotations

__all__ = ["DesktopBridge", "main"]


def __getattr__(name: str):
    """Import lazily so `dmai.desktop.bridge` works with no GUI installed."""
    if name == "DesktopBridge":
        from dmai.desktop.bridge import DesktopBridge

        return DesktopBridge
    if name == "main":
        from dmai.desktop.app import main

        return main
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
