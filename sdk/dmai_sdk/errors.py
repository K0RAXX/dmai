"""The one exception the SDK raises.

Everything that can go wrong for a caller -- a bad name, a level of zero, a
save that is not a save -- arrives as `TableError` with a message written for
a person.  Engine exceptions are translated at the boundary rather than
allowed through, so a host application catches one type, not five from three
packages it never imported.
"""

from __future__ import annotations


class TableError(RuntimeError):
    """The table could not do what was asked, and why."""


__all__ = ["TableError"]
