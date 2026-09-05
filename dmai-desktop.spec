# PyInstaller spec for the desktop window.
#
#     pyinstaller dmai-desktop.spec
#
# Two things have to travel with the binary or the window opens on nothing:
# the page itself (`dmai/desktop/web`) and the rules packs, which the engine
# reads as data rather than importing.  `dmai.desktop.app.web_root` looks under
# `sys._MEIPASS` first, which is where PyInstaller unpacks these at run time.
#
# The CLI's terminal libraries are excluded -- this is the desktop app, not the
# whole toolbox.  `anthropic` is deliberately *not* excluded: an AI Dungeon
# Master is the point of the product, so if the package is installed when the
# binary is built it travels with it, and the window offers the Claude DM.  When
# it is absent the build simply omits it and the window runs the offline DM,
# which is the same degradation the source install has.

from importlib.util import find_spec
from pathlib import Path

HERE = Path(SPECPATH)

datas = [
    (str(HERE / "dmai" / "desktop" / "web"), "dmai/desktop/web"),
    (str(HERE / "rules_packs"), "rules_packs"),
]

hiddenimports = ["dmai.desktop.app", "dmai.desktop.bridge"]

# `ClaudeProvider` imports the SDK inside a method rather than at module level,
# so that `dmai.ai` loads on a machine without it.  That lazy import is
# invisible to PyInstaller's static analysis, which would otherwise ship a
# binary whose Claude DM fails on first use with "install anthropic".  Naming it
# here fixes that -- and only when it is actually installed, so a build on a
# machine without it still succeeds and simply ships the offline DM.
if find_spec("anthropic"):
    hiddenimports.append("anthropic")

analysis = Analysis(
    [str(HERE / "dmai" / "desktop" / "__main__.py")],
    pathex=[str(HERE)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "typer",
        "rich",
        "fastapi",
        "uvicorn",
        "sqlalchemy",
        "alembic",
        "tkinter",
        "matplotlib",
        "numpy",
        "pytest",
    ],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="DungeonMaster",
    debug=False,
    strip=False,
    upx=False,
    # A GUI app: no console window behind the table.
    console=False,
)

collected = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="DungeonMaster",
)
