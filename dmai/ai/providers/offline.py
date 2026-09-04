"""A DM that needs no network, no key and no model.

Every other provider is optional; this one is the floor.  It exists so that

* the test suite can exercise the whole reasoning loop without a model,
* a table with no API key can still play, and
* a refusal or an outage degrades the prose rather than ending the session.

It is a real implementation, not a stub.  Interpretation is keyword
classification over the player's own words -- the same job a model does, done
worse but honestly -- and narration restates the facts the engine resolved, in
order, as plain sentences.  What it will not do is invent an outcome: it never
sees a die and never reports one the engine did not roll.

Nothing here resolves ids.  Turning "the goblin" into a creature id is a lookup
against game state, which the agent does for *every* provider, so a model's
hallucinated target and this provider's silence fail the same way.
"""

from __future__ import annotations

import re
from typing import TypeVar

from pydantic import BaseModel

from dmai.engine.models.actions import ActionInterpretation, CheckRequest, IntentKind

from .base import AIProvider, Completion, Message, ProviderError

T = TypeVar("T", bound=BaseModel)

#: Verb -> intent.  Ordered most specific first; the first hit wins.
INTENT_WORDS: list[tuple[IntentKind, tuple[str, ...]]] = [
    (IntentKind.ATTACK, ("attack", "hit", "strike", "swing", "stab", "slash", "shoot", "fire at", "punch", "kill")),
    (IntentKind.CAST_SPELL, ("cast", "spell", "incant", "channel")),
    (IntentKind.REST, ("rest", "sleep", "camp", "make camp", "long rest", "short rest")),
    (IntentKind.USE_ITEM, ("use", "drink", "quaff", "equip", "wield", "read the", "light the")),
    (IntentKind.SEARCH, ("search", "look for", "examine", "inspect", "investigate", "study", "check for")),
    (IntentKind.TALK, ("ask", "tell", "say", "talk", "speak", "greet", "persuade", "convince", "threaten", "lie", "bargain", "haggle")),
    (IntentKind.MOVE, ("go", "walk", "run", "head", "travel", "enter", "leave", "climb", "sneak", "follow", "return")),
    (IntentKind.META, ("what are my", "how much hp", "what is my", "rules", "how do i", "can i see my")),
]

#: Phrase -> skill.  Checked before intent, because "sneak into the vault" is a
#: stealth check whichever way the verb is classified.
SKILL_WORDS: dict[str, tuple[str, ...]] = {
    "stealth": ("sneak", "hide", "quietly", "unseen", "slip past", "creep"),
    "perception": ("listen", "look around", "notice", "watch", "keep an eye", "scan"),
    "investigation": ("search", "examine", "inspect", "investigate", "study", "look for"),
    "persuasion": ("persuade", "convince", "plead", "reason with", "ask nicely", "negotiate"),
    "deception": ("lie", "bluff", "pretend", "disguise", "trick", "mislead"),
    "intimidation": ("threaten", "intimidate", "menace", "scare"),
    "insight": ("read them", "sense motive", "tell if", "gauge", "judge whether"),
    "athletics": ("climb", "jump", "swim", "shove", "force the", "break down", "lift"),
    "acrobatics": ("tumble", "balance", "flip", "vault"),
    "arcana": ("magical", "arcane", "runes", "sigil", "enchantment"),
    "medicine": ("stabilise", "stabilize", "bandage", "treat the wound", "first aid"),
    "survival": ("track", "forage", "navigate", "follow the trail"),
    "history": ("recall", "remember the", "legend", "chronicle"),
    "religion": ("prayer", "holy", "divine", "shrine"),
    "animal_handling": ("calm the", "soothe the horse", "ride", "tame"),
    "sleight_of_hand": ("pickpocket", "palm", "lift the purse", "pick the lock"),
}

#: Actions that change the world even when nothing is rolled.
MUTATING_INTENTS = {
    IntentKind.ATTACK,
    IntentKind.CAST_SPELL,
    IntentKind.MOVE,
    IntentKind.USE_ITEM,
    IntentKind.REST,
    IntentKind.SEARCH,
    IntentKind.SKILL_CHECK,
}

#: Skill checks the DM asks for by default when a skill is implied.
DEFAULT_DC = 13


class OfflineProvider(AIProvider):
    """Keyword interpretation and plain-spoken narration, computed locally."""

    id = "offline"
    name = "Offline DM"
    offline = True

    def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        max_tokens: int = 2048,
        cache_system: bool = True,
    ) -> Completion:
        """Restate the facts the engine established, as sentences.

        The agent hands narration a FACTS block; anything outside it is context
        this provider is not equipped to embroider, so it is left alone.
        """
        prompt = messages[-1]["content"] if messages else ""
        facts = _facts_from(prompt)
        if not facts:
            return Completion(text="", model=self.id)
        return Completion(text=" ".join(_as_sentence(f) for f in facts), model=self.id)

    def parse(
        self,
        *,
        system: str,
        messages: list[Message],
        schema: type[T],
        max_tokens: int = 2048,
        cache_system: bool = True,
    ) -> Completion:
        if schema is not ActionInterpretation:
            raise ProviderError(
                f"{self.name} can only produce ActionInterpretation, not {schema.__name__}; "
                "configure a model provider for this."
            )
        text = _player_text(messages[-1]["content"] if messages else "")
        return Completion(parsed=interpret(text), model=self.id)

    def health(self) -> str:
        return "Offline DM ready (no model configured; narration will be terse)"


def interpret(text: str) -> ActionInterpretation:
    """Classify one line of player input.  Deterministic and side-effect free."""
    lowered = text.lower().strip()
    skill = _skill_for(lowered)
    intent = _intent_for(lowered, skill)

    checks: list[CheckRequest] = []
    rationale = ""
    if intent is IntentKind.ATTACK:
        rationale = "An attack is resolved by the combat engine."
    elif skill:
        # The actor is filled in by the agent, which knows whose turn it is.
        checks.append(
            CheckRequest(
                kind=IntentKind.SKILL_CHECK,
                actor_id="",
                skill=skill,
                dc=DEFAULT_DC,
                reason=f"{_readable(skill)} check",
            )
        )
        intent = IntentKind.SKILL_CHECK if intent is IntentKind.FREEFORM else intent
        rationale = (
            f"The DM determined that {_article(_readable(skill))} "
            f"{_readable(skill)} check is required."
        )

    return ActionInterpretation(
        intent=intent,
        summary=text.strip(),
        checks=checks,
        mutates_state=intent in MUTATING_INTENTS,
        rationale=rationale,
    )


def _readable(skill: str) -> str:
    return skill.replace("_", " ")


def _article(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


def _intent_for(lowered: str, skill: str | None) -> IntentKind:
    for intent, words in INTENT_WORDS:
        if any(word in lowered for word in words):
            return intent
    return IntentKind.SKILL_CHECK if skill else IntentKind.FREEFORM


def _skill_for(lowered: str) -> str | None:
    for skill, phrases in SKILL_WORDS.items():
        if any(phrase in lowered for phrase in phrases):
            return skill
    return None


def _player_text(prompt: str) -> str:
    """Pull the player's line back out of the prompt the agent built."""
    match = re.search(r"^PLAYER:\s*(.+)$", prompt, flags=re.MULTILINE)
    return match.group(1).strip() if match else prompt.strip()


def _facts_from(prompt: str) -> list[str]:
    """The lines under a FACTS: heading, which is what the agent resolved."""
    block = re.search(r"^FACTS:\s*\n(.*?)(?:\n\s*\n|\Z)", prompt, re.MULTILINE | re.DOTALL)
    if not block:
        return []
    return [
        line.strip().lstrip("-").strip()
        for line in block.group(1).splitlines()
        if line.strip().lstrip("-").strip()
    ]


def _as_sentence(fact: str) -> str:
    return fact if fact.endswith((".", "!", "?")) else f"{fact}."


__all__ = ["DEFAULT_DC", "INTENT_WORDS", "SKILL_WORDS", "OfflineProvider", "interpret"]
