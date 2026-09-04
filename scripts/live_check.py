"""A live smoke test against the real Anthropic API.

Not part of the test suite: it costs money and needs credentials, and
`python -m pytest` must stay free and offline. Run it by hand after changing
anything in `dmai/ai/providers/claude.py`.

    python scripts/live_check.py

What it proves that the offline suite cannot:

* the request shape is one the API actually accepts -- in particular that
  `fallbacks="default"` and its beta header are valid together, which shows up
  as `fallbacks accepted: True` below (a 400 would silently flip it off);
* `messages.parse` really returns a validated `ActionInterpretation`;
* the usage and model fields this code reads exist on a real response.

Every call is small and the running cost is printed at the end.
"""

from __future__ import annotations

import os
import sys

from dmai.ai.dm_agent.agent import DungeonMaster
from dmai.ai.dm_agent.prompts import system_prompt
from dmai.ai.providers.claude import ClaudeProvider
from dmai.engine.models import Campaign, CampaignSettings, NarrativeStyle, StoryTone
from dmai.engine.models.actions import ActionInterpretation
from dmai.engine.session import GameSession

MODEL = os.environ.get("DMAI_LIVE_MODEL", "claude-opus-5")


def build_table() -> GameSession:
    """A small scene with enough in it to be worth narrating."""
    session = GameSession.create(
        Campaign(
            name="Ashes of Emberfall",
            premise="A caravan vanished on the Cinder Road.",
            setting="A soot-stained mining town under a failing pass.",
            settings=CampaignSettings(
                tone=StoryTone.DARK, style=NarrativeStyle.DESCRIPTIVE, rng_seed=1234
            ),
        )
    )
    player = session.add_player("James", is_host=True)
    session.add_character("Vale", "human", "fighter", 3, player_id=player.id)
    inn = session.atlas.add_location(
        session.journal,
        "The Ash & Anchor",
        kind="building",
        description="Low beams, wet wool, a fire that never quite catches.",
    )
    session.atlas.enter(session.journal, inn.id)
    session.atlas.add_npc(
        session.journal, "Marda Quill", location_id=inn.id, role="innkeeper"
    )
    quest = session.quests.add(
        session.journal, "The Cinder Road", objectives=["Find the caravan"]
    )
    session.quests.accept(session.journal, quest.id)
    return session


def rule(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def main() -> int:
    session = build_table()
    vale = next(iter(session.state.party.values()))
    provider = ClaudeProvider(MODEL)
    spent = 0.0

    rule(f"1. narration -- does the API accept fallbacks=\"default\"?")
    completion = provider.complete(
        system=system_prompt(session),
        messages=[
            {
                "role": "user",
                "content": (
                    "SCENE\nThe Ash & Anchor, evening. Marda Quill is behind the bar.\n\n"
                    "FACTS:\n- Vale rolled Investigation 17 against DC 13 -- success.\n"
                    "- Vale found a torn strap of caravan harness under the settle.\n\n"
                    "Narrate this for the table in about 90 words."
                ),
            }
        ],
        max_tokens=1024,
    )
    spent += completion.cost_usd
    print(f"\n{completion.text}\n")
    print(f"  served by       : {completion.model}")
    print(f"  fallbacks accepted: {provider._fallbacks_usable}")
    print(f"  fell back       : {completion.meta['fell_back']}")
    print(f"  tokens          : {completion.input_tokens} in / {completion.output_tokens} out")
    print(f"  cached          : {completion.meta['cached_tokens']}")
    print(f"  cost            : ${completion.cost_usd:.4f}")

    if not provider._fallbacks_usable:
        print(
            "\n  !! The beta was rejected and this narration came from the plain\n"
            "     endpoint. Fallbacks are NOT active on this account."
        )

    rule("2. interpretation -- does messages.parse return a valid schema?")
    parsed = provider.parse(
        system=system_prompt(session),
        messages=[
            {
                "role": "user",
                "content": (
                    f"ACTING: Vale [{vale.id}]\n"
                    "PLAYER: I lean on the bar and ask Marda who else was on the road that night."
                ),
            }
        ],
        schema=ActionInterpretation,
        max_tokens=1024,
    )
    spent += parsed.cost_usd
    interpretation = parsed.parsed
    assert isinstance(interpretation, ActionInterpretation), "parse returned the wrong type"
    print(f"  intent      : {interpretation.intent.value}")
    print(f"  summary     : {interpretation.summary}")
    print(f"  checks      : {[(c.skill, c.dc) for c in interpretation.checks]}")
    print(f"  rationale   : {interpretation.rationale}")
    print(f"  cost        : ${parsed.cost_usd:.4f}")

    rule("3. a full DM turn through the reasoning loop")
    dm = DungeonMaster(session, provider)
    action = session.player_says(
        "I search the common room for anything the caravan left behind",
        character_id=vale.id,
    )
    result = dm.take_turn(action)
    spent += result.cost_usd
    for roll in result.rolls:
        print(f"  roll: {' '.join(roll.describe().split())}")
    print(f"\n{result.narration}\n")
    print(f"  interpreted by : {dm.last.interpreted_by}")
    print(f"  narrated by    : {dm.last.narrated_by} ({dm.last.served_by})")
    print(f"  degraded       : {dm.last.degraded or 'nothing'}")
    print(f"  cost           : ${result.cost_usd:.4f}")

    rule("4. does the narration honour the facts?")
    print("  Read the prose above: it must not report a roll the engine did not")
    print("  make, and the numbers in it must match the roll lines shown.")

    rule(f"TOTAL SPEND: ${spent:.4f}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - a live check reports, it does not raise
        print(f"\nLIVE CHECK FAILED: {type(exc).__name__}: {exc}")
        sys.exit(1)
