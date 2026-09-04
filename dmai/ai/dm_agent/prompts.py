"""What the DM is told, and what it is shown.

Two prompts, because the reasoning loop has two passes (spec section 9):

* **Interpretation** asks *what is this player trying to do, and what must be
  rolled?*  It returns an `ActionInterpretation` and never any prose.
* **Narration** is given the facts the engine already resolved and asked to
  describe them.  It is never asked what happened -- only how it looked.

That split is the load-bearing idea of the whole AI layer.  The model proposes;
the engine disposes.  Every rule in `NARRATION_RULES` exists because the failure
it prevents is one that would make an AI DM untrustworthy: reporting a roll
nobody made, telling a player what their character feels, or letting a nice
sentence quietly overwrite a hit point total.
"""

from __future__ import annotations

from dmai.ai.narrative.style import style_directives, target_words
from dmai.engine.models.campaign import Campaign
from dmai.engine.models.memory import Memory
from dmai.engine.session import GameSession

#: Spec section 28, near enough verbatim: how an experienced GM behaves.
PERSONALITY = """\
You are the Dungeon Master for an ongoing tabletop campaign. You are not an
assistant and you are not a chatbot; you are the person running the game.

How you run it:
- Say "yes" when a reasonable idea would work.
- Say "yes, but..." when it would work at a cost.
- Say "no" only when the logic of the world requires it, and say why in-world.
- Reward creativity. An unexpected solution that fits the fiction is a success,
  not a problem to be routed around.
- Let failure happen. Do not rescue the party from their own choices.
- The world is yours. The characters are the players'. Never state what a
  player character thinks, feels, decides or says.
- Describe consequences and let the players choose. Never choose for them.
- Keep continuity. If it happened, it stays happened."""

#: Spec section 29.  The distinction the DM must be able to draw.
ANTI_RAILROADING = """\
Player agency outranks your plans. Distinguish:
- PLAYER CHOICE: theirs alone. Never overridden, never quietly undone.
- WORLD CONSEQUENCE: what follows from that choice, which you do control.
- NARRATIVE OPPORTUNITY: an offer they may refuse without penalty.
- REQUIRED EVENT: rare, and only when the campaign genuinely demands it.

If the party derails the expected story, adapt the campaign to them. Do not
block, retcon, or repeatedly re-offer a path they have declined."""

#: The boundary between the model and the engine.  Non-negotiable.
NARRATION_RULES = """\
HARD RULES -- these override tone, style and everything else:
- The dice have already been rolled and the results are given to you below.
  Narrate those results. Never invent, alter, re-roll or predict a roll, a hit
  point total, a damage number or a success or failure.
- If a fact is not in the state or the facts given to you, it did not happen.
  You may invent sensory colour and incidental detail; you may not invent
  outcomes, loot, NPC knowledge, or rules results.
- Never reveal DM-only information: unfound secrets, unrevealed motives,
  monster statistics, or what is behind an undiscovered door.
- Never show your reasoning. If the players need to know why a check happened,
  say it in one clause: "That will take a Perception check."
- Write only the DM's voice and NPC dialogue. Never write a player's line.
- Do not ask "what do you do?" more than once in a response, and never after a
  question you have just asked."""

#: The interpretation pass gets its own, much narrower instructions.
INTERPRETER = """\
You interpret one player's stated action for a tabletop RPG engine.

Return only the structured interpretation. You are NOT narrating and you are
NOT deciding what happens -- the engine resolves everything you ask for.

Guidance:
- intent: what the character is actually trying to do.
- checks: the rule resolutions genuinely required. Ask for a check only when
  failure is possible AND interesting. Talking to a friendly NPC, walking
  across a room, or looking at something in plain sight need no check.
- dc: 5 very easy, 10 easy, 15 moderate, 20 hard, 25 very hard.
- target_ids: use ids exactly as they appear in the scene. If you are unsure
  who or what is meant, leave it empty rather than guessing -- the engine will
  resolve the name.
- mutates_state: false for questions about the rules or the character sheet.
- rationale: one short player-facing clause explaining any check you asked for.
  Never internal reasoning."""


def system_prompt(session: GameSession) -> str:
    """The DM's standing instructions for this campaign.

    Identical on every turn of a session by construction -- campaign facts only,
    nothing per-action -- so a provider can cache it and the table pays for the
    persona once rather than once per action.
    """
    campaign: Campaign = session.campaign
    parts = [
        PERSONALITY,
        "",
        ANTI_RAILROADING,
        "",
        NARRATION_RULES,
        "",
        "CAMPAIGN",
        f"Name: {campaign.name}",
    ]
    if campaign.premise:
        parts.append(f"Premise: {campaign.premise}")
    if campaign.setting:
        parts.append(f"Setting: {campaign.setting}")
    parts += [
        f"Ruleset: {session.rules.name}",
        "",
        "STYLE",
        style_directives(campaign.settings),
    ]
    return "\n".join(parts)


def scene_block(session: GameSession, *, player_id: str | None = None) -> str:
    """The state the DM can see right now, compactly.

    Built from `briefing()` so the resume path and the per-action path show the
    DM the same world, and so nothing DM-only leaks into a player-facing prompt.
    """
    briefing = session.briefing()
    where = briefing["where"]
    lines = [
        "SCENE",
        f"Location: {where['location'] or 'unmapped'}"
        + (f" -- {where['description']}" if where["description"] else ""),
        f"Time: {briefing['when']['time']}, weather {briefing['when']['weather']}",
    ]
    if where["npcs_present"]:
        lines.append(f"Present: {', '.join(where['npcs_present'])}")
    if where["exits"]:
        lines.append(f"Exits: {', '.join(where['exits'])}")

    lines.append("")
    lines.append("PARTY")
    for character in briefing["who"]:
        conditions = f", {', '.join(character['conditions'])}" if character["conditions"] else ""
        lines.append(
            f"- {character['name']} [{character['id']}]: level {character['level']} "
            f"{character['class']}, {character['hp']} hp{conditions}"
        )

    combat = briefing["what_is_happening"]
    if combat["in_combat"]:
        lines += [
            "",
            "COMBAT",
            f"Round {combat['round']}; it is {combat['turn']}'s turn.",
        ]
        enemies = [
            f"- {c.name} [{c.id}]: {c.hp.current}/{c.hp.maximum} hp"
            for c in session.state.bestiary.values()
            if not c.dead
        ]
        if enemies:
            lines += ["Enemies still standing:", *enemies]

    if briefing["active_quests"]:
        lines.append("")
        lines.append("ACTIVE QUESTS")
        for quest in briefing["active_quests"]:
            objectives = "; ".join(quest["objectives"]) or "no open objectives"
            lines.append(f"- {quest['title']}: {objectives}")

    if briefing["players_know"]:
        lines.append("")
        lines.append("THE PARTY KNOWS")
        lines += [f"- {fact}" for fact in briefing["players_know"][-8:]]

    return "\n".join(lines)


def memory_block(memories: list[Memory]) -> str:
    if not memories:
        return ""
    lines = ["MEMORY (things the DM should still be holding on to)"]
    lines += [f"- {m.content}" for m in memories]
    return "\n".join(lines)


def interpretation_prompt(
    session: GameSession, text: str, *, actor_name: str = "", actor_id: str = ""
) -> str:
    """Pass 1: what is this player trying to do?"""
    who = f"{actor_name} [{actor_id}]" if actor_name else "the party"
    return "\n\n".join(
        part
        for part in (
            scene_block(session),
            f"ACTING: {who}",
            f"PLAYER: {text}",
        )
        if part
    )


def narration_prompt(
    session: GameSession,
    *,
    text: str,
    facts: list[str],
    memories: list[Memory],
    rationale: str = "",
    recent: list[str] | None = None,
) -> str:
    """Pass 2: describe what the engine has already decided.

    `facts` is the whole of what the DM may treat as having happened. It is
    built from events the engine appended while resolving this action, so the
    model is narrating the log rather than authoring it.
    """
    sections = [scene_block(session)]
    if memories:
        sections.append(memory_block(memories))
    if recent:
        sections.append("RECENTLY\n" + "\n".join(f"- {line}" for line in recent))
    sections.append(f"PLAYER: {text}")
    sections.append(
        "FACTS:\n" + "\n".join(f"- {fact}" for fact in facts)
        if facts
        else "FACTS:\n- Nothing mechanical happened; this is pure roleplay."
    )
    if rationale:
        sections.append(f"You may mention, in one clause: {rationale}")
    sections.append(
        f"Narrate the result for the table in about "
        f"{target_words(session.campaign.settings)} words. "
        "Describe only the facts above and the world around them."
    )
    return "\n\n".join(sections)


def recap_prompt(session: GameSession, digest: str) -> str:
    """Turn the engine's mechanical recap into the campaign's voice (spec 30)."""
    return (
        "Here is a factual summary of the session that just ended.\n\n"
        f"{digest}\n\n"
        "Retell it as a session recap for the players: what happened, what they "
        "learned, what changed, and what is still unresolved. Add no events that "
        "are not listed above."
    )


def resume_prompt(session: GameSession, recent: list[str]) -> str:
    """Bring the table back to the moment they left (spec section 31)."""
    return "\n\n".join(
        [
            scene_block(session),
            "RECENTLY\n" + "\n".join(f"- {line}" for line in recent),
            "The table is sitting back down. In a short paragraph, remind them "
            "where they are, who is with them, and what is pressing. End by "
            "asking what they do. Add nothing that is not above.",
        ]
    )


__all__ = [
    "ANTI_RAILROADING",
    "INTERPRETER",
    "NARRATION_RULES",
    "PERSONALITY",
    "interpretation_prompt",
    "memory_block",
    "narration_prompt",
    "recap_prompt",
    "resume_prompt",
    "scene_block",
    "system_prompt",
]
