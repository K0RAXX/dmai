"""The load-bearing constraint of this project.

`dmai.engine` must stay pure: no imports of the AI layer, the web API, the
persistence layer or the UI, and no network or subprocess use.  That purity is
the whole reason the desktop app, the browser page and the OpenClaw agent can
be interchangeable clients of one engine -- and the reason the engine can be
tested without ever calling a language model.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1] / "dmai" / "engine"

#: Sibling packages the engine may never depend on.
FORBIDDEN_PACKAGES = {"ai", "api", "persistence", "web", "desktop", "cli", "integrations"}

#: Modules that would let the engine reach outside the process.
FORBIDDEN_MODULES = {
    "requests", "httpx", "aiohttp", "socket", "urllib", "subprocess",
    "fastapi", "uvicorn", "sqlalchemy", "anthropic", "openai", "webview",
}


def engine_files() -> list[Path]:
    return sorted(p for p in ENGINE.rglob("*.py"))


def imported_names(tree: ast.AST) -> list[tuple[str, int]]:
    """Every module name imported by this file, with its line number."""
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import, stays inside the engine
                continue
            if node.module:
                found.append((node.module, node.lineno))
    return found


@pytest.mark.parametrize("path", engine_files(), ids=lambda p: p.name)
def test_engine_does_not_import_outer_layers(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    rel = path.relative_to(ENGINE.parent.parent)

    for name, lineno in imported_names(tree):
        parts = name.split(".")
        root = parts[0]

        if root == "dmai" and len(parts) > 1 and parts[1] in FORBIDDEN_PACKAGES:
            pytest.fail(
                f"{rel}:{lineno} imports '{name}'. The engine must not depend on "
                f"dmai.{parts[1]} -- move the shared piece into dmai.engine instead."
            )

        if root in FORBIDDEN_MODULES:
            pytest.fail(
                f"{rel}:{lineno} imports '{name}'. The engine must stay free of "
                f"I/O and model calls so it can be tested without a network."
            )


def test_engine_package_is_not_empty() -> None:
    assert engine_files(), "no engine modules found -- is the layout right?"
