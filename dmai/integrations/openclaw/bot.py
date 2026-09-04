"""The line protocol OpenClaw talks to the Dungeon Master.

One JSON object per line in on stdin, one JSON object per line out on stdout.
That shape is deliberate: it is the lowest common denominator every transport
already has -- a pipe, a subprocess, a socket -- so OpenClaw does not need a
Python import, a port, or a running server to drive a campaign.

    -> {"op": "message", "external_id": "telegram:44", "text": "I search the room"}
    <- {"ok": true, "op": "message", "result": {"narration": "...", ...}}

Two rules hold the whole thing up:

* **A bad message never stops the bot.**  Anything short of stdin closing comes
  back as ``{"ok": false, "error": ...}`` and the loop continues.  A DM that
  dies because one player sent malformed JSON is not a DM.
* **Only allowlisted operations run** (`CALLABLE_OPERATIONS`).  The caller is a
  chat transport carrying text from strangers, so it dispatches by name against
  a fixed list rather than by attribute lookup.

Every reply is written and flushed immediately, because a pipe reader that is
waiting on a buffer looks exactly like a hung game.
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from dmai.integrations.openclaw.adapter import (
    CALLABLE_OPERATIONS,
    OpenClawAdapter,
    OpenClawError,
)

#: Shorthand for the operation a chat transport sends most: route a message
#: from a chat identity to its table and answer it.
MESSAGE_OP = "message"

PROTOCOL_VERSION = 1


def describe() -> dict:
    """What this bot can do -- enough for OpenClaw to advertise a tool schema."""
    return {
        "protocol": PROTOCOL_VERSION,
        "operations": sorted(CALLABLE_OPERATIONS),
        "shorthand": {
            MESSAGE_OP: "route a player message by external_id (args: external_id, text)",
        },
    }


def handle(adapter: OpenClawAdapter, request: dict) -> dict:
    """Turn one request object into one response object.  Never raises."""
    op = request.get("op")
    if not op:
        return _error(None, "every request needs an \"op\"")

    args = request.get("args")
    if args is None:
        # Convenience: flat requests carry their arguments at the top level,
        # so a transport does not have to nest a dict to send two strings.
        args = {k: v for k, v in request.items() if k not in {"op", "id"}}
    if not isinstance(args, dict):
        return _error(op, '"args" must be an object', request.get("id"))

    if op == MESSAGE_OP:
        op = "handle_message"

    try:
        result = adapter.dispatch(op, args)
    except OpenClawError as exc:
        return _error(op, str(exc), request.get("id"))
    except Exception as exc:  # noqa: BLE001
        # A bug in the engine must still come back as a reply: the transport
        # needs an answer for the player, and the traceback belongs in the log.
        return _error(op, f"{type(exc).__name__}: {exc}", request.get("id"))

    response = {"ok": True, "op": op, "result": result}
    if request.get("id") is not None:
        response["id"] = request["id"]
    return response


def serve(
    adapter: OpenClawAdapter,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    """Read requests until the input closes.  Returns a process exit code."""
    source = stdin if stdin is not None else sys.stdin
    sink = stdout if stdout is not None else sys.stdout

    for line in source:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            _write(sink, _error(None, f"invalid JSON: {exc}"))
            continue
        if not isinstance(request, dict):
            _write(sink, _error(None, "a request must be a JSON object"))
            continue
        _write(sink, handle(adapter, request))
    return 0


def _write(sink: TextIO, payload: dict) -> None:
    sink.write(json.dumps(payload, default=_fallback) + "\n")
    sink.flush()


def _fallback(value: Any) -> str:
    """Anything the engine hands back that JSON cannot express, as text."""
    return str(value)


def _error(op: str | None, message: str, request_id: Any = None) -> dict:
    response: dict = {"ok": False, "op": op, "error": message}
    if request_id is not None:
        response["id"] = request_id
    return response


__all__ = ["MESSAGE_OP", "PROTOCOL_VERSION", "describe", "handle", "serve"]
