"""Story style controls (spec section 14).

Tone, narrative style and DM behaviour are table preferences, and this module
turns them into prompt language.  The rule the spec states and this module
enforces is that **style never overrides game integrity**: a "generous" DM is
warmer about failure, not looser about the DC, and every directive below is
about voice and framing rather than about numbers.

Length is part of style too.  A "concise" table and a "literary" one want very
different amounts of prose for the same event, and getting that wrong is the
fastest way for an AI DM to feel like a chatbot.
"""

from __future__ import annotations

from dmai.engine.models.campaign import (
    CampaignSettings,
    DMBehavior,
    NarrativeStyle,
    StoryTone,
)

TONE_DIRECTIVES: dict[StoryTone, str] = {
    StoryTone.LIGHTHEARTED: "Keep it warm and buoyant. Danger is real but the world is kind.",
    StoryTone.HEROIC: "Play it heroic: the stakes are large, courage is rewarded, and the party are people worth following.",
    StoryTone.SERIOUS: "Play it straight and grounded. No winking at the audience.",
    StoryTone.DARK: "Keep it dark. Victories cost something, and the world does not apologise.",
    StoryTone.GRIM: "Grim and unsentimental. Survival is an achievement; mercy is scarce and expensive.",
    StoryTone.HORROR: "Build dread through detail and restraint. What is implied unsettles more than what is shown.",
    StoryTone.COMEDIC: "Let it be funny. Play absurdity straight and let the world be the joke, never the players.",
    StoryTone.POLITICAL: "Foreground faction interests, leverage and the cost of alliances.",
    StoryTone.MYSTERY: "Dole out information. Every scene should leave one honest question unanswered.",
    StoryTone.EPIC: "Write large. Landscapes, histories, and consequences that outlive the people in them.",
}

STYLE_DIRECTIVES: dict[NarrativeStyle, str] = {
    NarrativeStyle.CONCISE: "Two or three sentences. No scene-setting the players did not ask for.",
    NarrativeStyle.CONVERSATIONAL: "Talk like a friend running a game at a table: plain, quick, direct.",
    NarrativeStyle.DESCRIPTIVE: "A short, sensory paragraph. Name what a character would actually notice.",
    NarrativeStyle.CINEMATIC: "Frame it like a shot: motion, sound, and one image that lands.",
    NarrativeStyle.LITERARY: "Take the time for rhythm and a real image, but never at the cost of clarity.",
}

#: Roughly how much prose each style wants, in words.
STYLE_LENGTHS: dict[NarrativeStyle, int] = {
    NarrativeStyle.CONCISE: 45,
    NarrativeStyle.CONVERSATIONAL: 70,
    NarrativeStyle.DESCRIPTIVE: 110,
    NarrativeStyle.CINEMATIC: 120,
    NarrativeStyle.LITERARY: 160,
}

BEHAVIOR_DIRECTIVES: dict[DMBehavior, str] = {
    DMBehavior.STRICT: "Hold the line on rules and consequences. Do not soften a result after the fact.",
    DMBehavior.NEUTRAL: "Be even-handed. Let the dice and the fiction fall where they fall.",
    DMBehavior.GENEROUS: "Look for the reading that lets a clever idea work. Be warm about failure -- but never change a roll that has landed.",
    DMBehavior.CHAOTIC: "Follow the wilder branch when two are equally valid. Surprise is welcome; unfairness is not.",
    DMBehavior.RULES_FOCUSED: "Name the mechanic you are applying in a short clause, so the table can follow the maths.",
    DMBehavior.STORY_FOCUSED: "Favour momentum and consequence over procedure. Resolve minor mechanics briskly.",
}


def style_directives(settings: CampaignSettings) -> str:
    """The style block of the system prompt, for one table's settings."""
    lines = [
        f"TONE: {TONE_DIRECTIVES[settings.tone]}",
        f"VOICE: {STYLE_DIRECTIVES[settings.style]}",
        f"LENGTH: aim for about {STYLE_LENGTHS[settings.style]} words unless the moment demands otherwise.",
        f"MANNER: {BEHAVIOR_DIRECTIVES[settings.dm_behavior]}",
        f"GENRE: {settings.genre}.",
    ]
    if settings.themes:
        lines.append(f"THEMES the table wants explored: {', '.join(settings.themes)}.")
    if settings.sandbox_mode:
        lines.append(
            "SANDBOX: this world runs whether or not the party engages. "
            "Let factions act off-screen and let the party ignore the main plot."
        )
    lines.append(
        "Style governs voice only. It never changes a DC, a roll, or a rule outcome."
    )
    return "\n".join(lines)


def target_words(settings: CampaignSettings) -> int:
    return STYLE_LENGTHS[settings.style]


__all__ = [
    "BEHAVIOR_DIRECTIVES",
    "STYLE_DIRECTIVES",
    "STYLE_LENGTHS",
    "TONE_DIRECTIVES",
    "style_directives",
    "target_words",
]
