"""The desktop window.

A native window (pywebview) whose contents are the medieval table in
`web/`, and whose only channel into the game is `DesktopBridge`.  There is no
server here: no port is opened, no HTTP is spoken, and the page is loaded from
disk.  The window and the engine share a process and talk through a function
call, which is why the desktop client works with the network unplugged.

The window is a peer of the CLI, not a wrapper around it.  Both are clients of
one `GameSession`; delete either and the game loses nothing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dmai.desktop.bridge import DesktopBridge

#: Where the page lives.  Packaged builds (PyInstaller) unpack to a temporary
#: directory and set `sys._MEIPASS`, so the assets are found either way.
def web_root() -> Path:
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        candidate = Path(bundled) / "dmai" / "desktop" / "web"
        if candidate.is_dir():
            return candidate
    return Path(__file__).resolve().parent / "web"


TITLE = "DungeonMaster AI"
MIN_SIZE = (940, 600)
DEFAULT_SIZE = (1360, 880)

#: How much of the screen the window may take.  Height is the tighter of the
#: two on purpose: pywebview centres a new window, and a screen size includes
#: the taskbar, so a window sized to 90% of the height opens with its bottom
#: edge -- the action bar -- underneath it.  A table whose input is hidden is
#: unusable, so the vertical margin buys clearance for a task bar at either
#: edge.
SCREEN_MARGIN_X = 0.92
SCREEN_MARGIN_Y = 0.84


def fit_to_screen(width: int, height: int) -> tuple[int, int]:
    """Shrink a preferred size to something the display can actually show.

    pywebview reports screens in the same logical units it takes for window
    sizes, so this comparison holds on a scaled display -- which is where the
    problem shows up, a 1080p panel at 125% having only 864 logical rows.
    """
    try:
        import webview

        screens = list(webview.screens)
    except Exception:  # noqa: BLE001 - a headless or odd display is not fatal
        return width, height
    if not screens:
        return width, height

    screen = screens[0]
    available_w = int(screen.width * SCREEN_MARGIN_X)
    available_h = int(screen.height * SCREEN_MARGIN_Y)
    return (
        max(MIN_SIZE[0], min(width, available_w)),
        max(MIN_SIZE[1], min(height, available_h)),
    )

#: Parchment, so the frame does not flash white before the page paints.
BACKGROUND = "#1a1410"


class Window:
    """The window, its bridge, and the file dialogs the bridge cannot open.

    `DesktopBridge` deliberately knows nothing about pywebview -- it takes and
    returns paths as strings so it stays testable headless.  Choosing the path
    is a window concern, so the two dialog operations live here and are exposed
    to the page alongside the bridge's own.
    """

    def __init__(self, bridge: DesktopBridge):
        self.bridge = bridge
        self._window = None

    # --- exposed to the page ------------------------------------------------

    def choose_export(self, campaign_id: str, suggested: str = "campaign") -> dict:
        """Ask where to write a bundle, then write it."""
        import webview

        chosen = self._window.create_file_dialog(
            webview.SAVE_DIALOG,
            save_filename=f"{_slug(suggested)}.json",
            file_types=("Campaign bundle (*.json)",),
        )
        if not chosen:
            return {"ok": False, "cancelled": True, "error": "Export cancelled."}
        path = chosen if isinstance(chosen, str) else chosen[0]
        return self.bridge.export_campaign(campaign_id, path)

    def choose_import(self) -> dict:
        """Ask for a bundle, then read it in as a new campaign."""
        import webview

        chosen = self._window.create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=False,
            file_types=("Campaign bundle (*.json)", "All files (*.*)"),
        )
        if not chosen:
            return {"ok": False, "cancelled": True, "error": "Import cancelled."}
        return self.bridge.import_campaign(chosen[0])

    def quit(self) -> dict:
        """Close the table cleanly, then the window."""
        result = self.bridge.close_campaign()
        if self._window is not None:
            self._window.destroy()
        return result

    # --- lifecycle ----------------------------------------------------------

    def api(self) -> object:
        """The object the page sees as `pywebview.api`.

        Both this and the bridge are merged onto one namespace so the page has
        a single vocabulary; pywebview exposes public methods only, which is
        why every internal on the bridge is underscored.
        """
        return _Api(self, self.bridge)

    def run(self, *, debug: bool = False) -> None:
        import webview

        index = web_root() / "index.html"
        if not index.is_file():
            raise SystemExit(f"the desktop UI is missing: {index}")

        width, height = fit_to_screen(*DEFAULT_SIZE)
        self._window = webview.create_window(
            TITLE,
            str(index),
            js_api=self.api(),
            width=width,
            height=height,
            min_size=MIN_SIZE,
            background_color=BACKGROUND,
            text_select=True,
        )
        # Save the campaign if the player closes the window with the frame's
        # own button rather than the one on the page.
        self._window.events.closing += self._on_closing
        webview.start(debug=debug)

    def _on_closing(self) -> bool:
        self.bridge.close_campaign()
        return True


class _Api:
    """One flat namespace over the window and the bridge.

    Attribute lookup is explicit rather than `getattr` on a list of objects,
    so nothing on either object is reachable from the page by accident.
    """

    #: Window-side operations: the ones that need a native dialog.
    WINDOW_OPERATIONS = ("choose_export", "choose_import", "quit")

    #: Bridge-side operations, in the order the UI uses them.
    BRIDGE_OPERATIONS = (
        "list_campaigns",
        "options",
        "create_campaign",
        "open_campaign",
        "close_campaign",
        "delete_campaign",
        "add_character",
        "play_as",
        "act",
        "roll",
        "narrate",
        "open_scene",
        "resume",
        "recap",
        "checkpoint",
        "rollback",
        "view",
        "sheet",
    )

    def __init__(self, window: Window, bridge: DesktopBridge):
        for name in self.WINDOW_OPERATIONS:
            setattr(self, name, getattr(window, name))
        for name in self.BRIDGE_OPERATIONS:
            setattr(self, name, getattr(bridge, name))


def _slug(text: str) -> str:
    keep = [c if c.isalnum() or c in "-_ " else "" for c in text]
    return "".join(keep).strip().replace(" ", "-").lower() or "campaign"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="dmai-desktop",
        description="DungeonMaster AI, in a window.",
    )
    parser.add_argument(
        "--root", type=Path, default=None, help="Where campaigns are stored."
    )
    parser.add_argument(
        "--provider", default=None, help="Override every campaign's DM AI for this run."
    )
    parser.add_argument("--model", default=None, help="Override the model id.")
    parser.add_argument(
        "--debug", action="store_true", help="Open the webview's inspector."
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        import webview  # noqa: F401
    except ImportError:
        print(
            "The desktop window needs pywebview.\n"
            '    pip install "dmai[desktop]"',
            file=sys.stderr,
        )
        return 1

    bridge = DesktopBridge(args.root, provider=args.provider, model=args.model)
    Window(bridge).run(debug=args.debug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
