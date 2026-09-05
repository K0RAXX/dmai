"""A whole campaign in one file, with the network unplugged.

    python examples/quickstart.py

Seeded, so it prints the same game every time -- which is the point: this is
what makes the SDK usable as a fixture as well as a toy.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Run from a checkout as well as from an installed package: without this, the
# only thing on the path is examples/ itself.  An installed `dmai-sdk` wins,
# because it is already importable and this append changes nothing.
sys.path.append(str(Path(__file__).resolve().parents[1]))

from dmai_sdk import Table  # noqa: E402

SAVE = Path(__file__).parent / "emberfall.dmai"


def main() -> None:
    table = Table.new(
        "Ashes of Emberfall",
        premise="A caravan vanished on the Cinder Road.",
        setting="The Emberlands",
        seed=1234,
    )
    print(f"# {table.name}\n")

    vale = table.add_hero("Vale", cls="fighter", level=2, player="James")
    rook = table.add_hero("Rook", cls="rogue", species="halfling", level=2)
    for hero in (vale, rook):
        print(f"  {hero}")

    print(f"\n  Scene: {table.scene()}\n")

    table.narrate(
        "Smoke hangs over the Cinder Road where the caravan should have been. "
        "Wheel ruts end mid-stride, as if the wagons were lifted rather than driven off."
    )

    mark = table.mark("Before the treeline")

    for line in (
        "I search the ruts for tracks",
        "I draw my blade and listen to the treeline",
    ):
        turn = table.act(line)
        print(f"> {line}")
        for roll in turn.rolls:
            print(f"    {roll}")
        print(f"    {turn.narration}\n")

    # Rewinding truncates the log, so what comes back is the campaign as it
    # actually was -- not a snapshot taken alongside it.
    print(f"  {table.events} events; rewinding to {mark}")
    table.rewind(mark)
    print(f"  {table.events} events after the rewind\n")

    table.save(SAVE)
    reopened = Table.load(SAVE)
    print(f"  Saved and reopened: {reopened!r}")
    print(f"\n  Recap\n  -----\n  {reopened.recap()}")

    SAVE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
