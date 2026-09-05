"""The offline guarantee.

This package's whole claim is that a campaign runs with nothing outside the
process.  A dependency list cannot prove that -- a library can be declared and
never used, or used and never declared -- so the guarantee is tested as
behaviour: a socket trap is installed, a full campaign is played through it, and
any outbound connection anywhere beneath `Table` fails the build.

The static half is here too, because a network import that only fires on an
error path would not show up in a happy-path game.
"""

from __future__ import annotations

import ast
import socket
import sys
from pathlib import Path

import pytest

from dmai_sdk import Table

PACKAGE = Path(__file__).resolve().parents[1] / "dmai_sdk"

#: Anything that would let the SDK reach outside the process.
FORBIDDEN_MODULES = {
    "socket", "ssl", "http", "urllib", "urllib3", "requests", "httpx",
    "aiohttp", "websockets", "ftplib", "telnetlib", "smtplib",
    "subprocess", "anthropic", "openai", "fastapi", "uvicorn",
}


class NetworkUsed(AssertionError):
    """A socket was opened while the SDK was supposed to be offline."""


@pytest.fixture
def no_network(monkeypatch):
    """Make any attempt to open a socket a loud test failure."""

    def trap(*args, **kwargs):
        raise NetworkUsed("the SDK opened a socket")

    monkeypatch.setattr(socket, "socket", trap)
    monkeypatch.setattr(socket, "create_connection", trap)
    monkeypatch.setattr(socket, "getaddrinfo", trap)
    return trap


# --- behaviour -------------------------------------------------------------


def test_a_whole_campaign_runs_with_the_network_trapped(no_network, tmp_path):
    """The load-bearing test of this package."""
    table = Table.new("Ashes of Emberfall", premise="A caravan vanished.", seed=1234)
    table.add_hero("Vale", cls="fighter", level=2)
    table.add_hero("Rook", cls="rogue", species="halfling", level=2)

    table.narrate("Smoke hangs over the Cinder Road.")
    table.act("I search the ruts for tracks")
    table.act("I draw my blade and listen")
    table.roll("2d6+3", reason="Foraging")

    mark = table.mark("Before the treeline")
    table.act("I step into the trees")
    table.rewind(mark)

    saved = table.save(tmp_path / "emberfall.dmai")
    reopened = Table.load(saved)

    assert reopened.name == "Ashes of Emberfall"
    assert [h.name for h in reopened.heroes] == ["Vale", "Rook"]


def test_importing_the_sdk_does_not_import_a_model_client():
    """A fresh import must not pull `anthropic` in behind the caller's back."""
    assert "anthropic" not in sys.modules
    assert "httpx" not in sys.modules


def test_the_dungeon_master_is_always_the_offline_one():
    table = Table.new("Ashes of Emberfall", seed=1)
    assert table._dm.provider.id == "offline"
    # There is no seam to point it at a model: no provider argument exists.
    with pytest.raises(TypeError):
        Table.new("Nope", provider="claude")  # type: ignore[call-arg]


# --- static ----------------------------------------------------------------


def sdk_files() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


@pytest.mark.parametrize("path", sdk_files(), ids=lambda p: p.name)
def test_no_module_imports_a_network_library(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            names = [node.module]

        for name in names:
            root = name.split(".")[0]
            if root in FORBIDDEN_MODULES:
                pytest.fail(
                    f"{path.name}:{node.lineno} imports {name!r}. The SDK must "
                    "stay free of anything that can leave the process."
                )


def test_the_package_is_not_empty():
    assert sdk_files(), "no SDK modules found -- is the layout right?"
