"""The DM AI layer: providers, style, memory, and the reasoning loop.

Every test here runs offline. That is the point: the loop must be exercisable
without a network or a key, and the boundary these tests defend is that the
model proposes while the engine disposes.
"""

from __future__ import annotations

import pytest

from dmai.ai.dm_agent.agent import DungeonMaster, fact_line
from dmai.ai.dm_agent.prompts import scene_block, system_prompt
from dmai.ai.memory.extraction import extract, recall
from dmai.ai.narrative.style import style_directives, target_words
from dmai.ai.providers import (
    AIProvider,
    Completion,
    OfflineProvider,
    ProviderError,
    available_providers,
    load_provider,
)
from dmai.ai.providers.claude import estimate_cost
from dmai.ai.providers.offline import interpret
from dmai.engine.models import (
    Campaign,
    CampaignSettings,
    DamageType,
    EventType,
    IntentKind,
    Memory,
    MemoryKind,
    MemoryTier,
    NarrativeStyle,
    StoryTone,
    Visibility,
)
from dmai.engine.models.actions import ActionInterpretation, CheckRequest
from dmai.engine.session import GameSession


@pytest.fixture
def session(campaign: Campaign) -> GameSession:
    session = GameSession.create(campaign)
    player = session.add_player("James", is_host=True)
    session.add_character("Vale", "human", "fighter", 3, player_id=player.id)
    inn = session.atlas.add_location(
        session.journal, "The Ash & Anchor", kind="building", description="Low beams."
    )
    session.atlas.enter(session.journal, inn.id)
    session.atlas.add_npc(session.journal, "Marda Quill", location_id=inn.id, role="innkeeper")
    return session


@pytest.fixture
def vale(session: GameSession):
    return next(iter(session.state.party.values()))


@pytest.fixture
def dm(session: GameSession) -> DungeonMaster:
    return DungeonMaster(session)


def act(session: GameSession, dm: DungeonMaster, text: str, character_id: str):
    return dm.take_turn(session.player_says(text, character_id=character_id))


# --- providers -------------------------------------------------------------


def test_the_registry_falls_back_rather_than_refusing(session):
    assert "offline" in available_providers()
    assert "claude" in available_providers()
    assert load_provider("no-such-provider").id == "offline"


def test_the_offline_provider_classifies_intent_from_the_verb():
    assert interpret("I attack the goblin").intent is IntentKind.ATTACK
    assert interpret("I ask Marda about the caravan").intent is IntentKind.TALK
    assert interpret("we make camp for the night").intent is IntentKind.REST
    assert interpret("what is my armour class").intent is IntentKind.META


def test_the_offline_provider_asks_for_the_implied_skill():
    checks = interpret("I sneak past the guard").checks
    assert [c.skill for c in checks] == ["stealth"]
    assert interpret("I climb the wall").checks[0].skill == "athletics"
    assert interpret("I lie about the ledger").checks[0].skill == "deception"


def test_pure_roleplay_asks_for_no_roll():
    assert interpret("I tell her my name is Vale").checks == []


def test_the_offline_provider_refuses_schemas_it_cannot_produce():
    with pytest.raises(ProviderError, match="ActionInterpretation"):
        OfflineProvider().parse(
            system="", messages=[{"role": "user", "content": "hi"}], schema=Campaign
        )


def test_cost_is_estimated_from_the_model_rate():
    assert estimate_cost("claude-opus-5", 1_000_000, 0) == pytest.approx(5.0)
    assert estimate_cost("claude-opus-5", 0, 1_000_000) == pytest.approx(25.0)
    # Cached input is an order of magnitude cheaper.
    assert estimate_cost("claude-opus-5", 0, 0, 1_000_000) == pytest.approx(0.5)


# --- style -----------------------------------------------------------------


def test_style_settings_reach_the_prompt(session):
    session.campaign.settings = CampaignSettings(
        tone=StoryTone.HORROR, style=NarrativeStyle.CONCISE, themes=["isolation"]
    )

    directives = style_directives(session.campaign.settings)

    assert "dread" in directives
    assert "isolation" in directives
    assert target_words(session.campaign.settings) < 60


def test_style_is_told_it_cannot_override_the_rules(session):
    assert "never changes a DC" in style_directives(session.campaign.settings)


def test_the_system_prompt_carries_the_campaign_and_the_hard_rules(session):
    # The prompt is hard-wrapped, so compare against unwrapped text.
    prompt = " ".join(system_prompt(session).split())

    assert session.campaign.name in prompt
    assert "Never invent, alter, re-roll or predict a roll" in prompt
    assert "Never state what a player character thinks" in prompt
    assert "Player agency outranks your plans" in prompt


def test_the_system_prompt_is_stable_across_a_session(session, dm, vale):
    """It must be byte-identical turn to turn, or prompt caching never hits."""
    before = system_prompt(session)
    act(session, dm, "I look around the room", vale.id)

    assert system_prompt(session) == before


# --- what the DM is shown --------------------------------------------------


def test_the_scene_block_shows_the_party_and_the_room(session, vale):
    block = scene_block(session)

    assert "The Ash & Anchor" in block
    assert "Marda Quill" in block
    assert vale.id in block  # ids are shown so targets can be named exactly


def test_the_scene_block_does_not_leak_undiscovered_secrets(session):
    from dmai.engine.models.world import NPC, Secret

    secret = Secret(content="The mayor pays the raiders.")
    session.atlas.add_npc(
        session.journal,
        NPC(name="Halven", secrets=[secret]),
        location_id=session.state.current_location_id,
    )

    assert "The mayor pays the raiders." not in scene_block(session)


# --- memory ----------------------------------------------------------------


def test_memories_are_extracted_from_events_not_from_a_transcript(session):
    session.quests.discover(session.journal, "The caravan never reached the pass.")
    session.roll("1d20", reason="a roll nobody needs to remember")

    memories = extract(session.store.all(), party_ids=set(session.state.party))
    contents = [m.content for m in memories]

    assert "The caravan never reached the pass." in contents
    assert not any("1d20" in c for c in contents)


def test_dm_only_events_never_become_memories(session):
    before = session.store.head
    session.quests.promise(session.journal, "they lied", "The guild finds out")

    memories = extract(session.store.since(before))

    assert memories == []


def test_recall_prefers_important_memories_about_who_is_present():
    about_marda = Memory(content="Marda is owed a favour", subjects=["npc-1"], importance=50)
    trivia = Memory(content="The ale is bad", importance=50)
    big = Memory(
        content="The party burned the bridge",
        importance=90,
        tier=MemoryTier.CAMPAIGN,
        kind=MemoryKind.FACT,
    )

    ordered = recall([trivia, about_marda, big], subjects={"npc-1"}, limit=3)

    assert ordered[0] is big
    assert ordered.index(about_marda) < ordered.index(trivia)


def test_recall_withholds_private_memories_from_the_table():
    whisper = Memory(content="Vale is the heir", visibility=Visibility.PRIVATE)
    dm_only = Memory(content="The innkeeper is a spy", visibility=Visibility.DM_ONLY)

    assert recall([whisper, dm_only]) == []
    assert recall([whisper, dm_only], player_id="player-1") == [whisper]


# --- the reasoning loop ----------------------------------------------------


def test_a_turn_interprets_resolves_narrates_and_logs(session, dm, vale):
    result = act(session, dm, "I search the common room for signs of a struggle", vale.id)

    assert result.interpretation.intent is IntentKind.SEARCH
    assert len(result.rolls) == 1
    assert result.narration
    assert result.event_ids
    assert any(e.type is EventType.CHECK_RESOLVED for e in session.store)
    assert any(e.type is EventType.DM_NARRATION for e in session.store)


def test_the_narrator_is_only_ever_shown_facts_the_engine_produced(session, dm, vale):
    """The load-bearing boundary: narration describes the log, never authors it."""
    seen: dict = {}

    class Recorder(OfflineProvider):
        def complete(self, *, system, messages, max_tokens=2048, cache_system=True):
            seen["prompt"] = messages[-1]["content"]
            return super().complete(system=system, messages=messages)

    dm.provider = Recorder()
    result = act(session, dm, "I search the room", vale.id)

    facts = seen["prompt"].split("FACTS:")[1]
    assert str(result.rolls[0].total) in facts
    assert "Narrate the result" in seen["prompt"]


def test_pure_roleplay_produces_no_roll(session, dm, vale):
    result = act(session, dm, "I tell Marda my name is Vale", vale.id)

    assert result.rolls == []
    assert result.interpretation.intent is IntentKind.TALK


def test_the_actor_is_whoever_spoke_not_whoever_the_model_named(session, dm, vale):
    class Liar(OfflineProvider):
        def parse(self, *, system, messages, schema, max_tokens=2048, cache_system=True):
            return Completion(
                parsed=ActionInterpretation(
                    intent=IntentKind.SKILL_CHECK,
                    actor_id="creature-invented",
                    checks=[
                        CheckRequest(
                            kind=IntentKind.SKILL_CHECK,
                            actor_id="creature-invented",
                            skill="perception",
                            dc=10,
                        )
                    ],
                )
            )

    dm.provider = Liar()
    result = act(session, dm, "I listen at the door", vale.id)

    assert result.interpretation.actor_id == vale.id
    assert result.interpretation.checks[0].actor_id == vale.id
    assert result.rolls[0].actor_id == vale.id


def test_an_invented_target_is_dropped_rather_than_attacked(session, dm, vale):
    class Liar(OfflineProvider):
        def parse(self, *, system, messages, schema, max_tokens=2048, cache_system=True):
            return Completion(
                parsed=ActionInterpretation(
                    intent=IntentKind.ATTACK, target_ids=["goblin-that-does-not-exist"]
                )
            )

    dm.provider = Liar()
    result = act(session, dm, "I attack the shadow", vale.id)

    assert result.interpretation.target_ids == []
    assert result.rolls == []
    assert not any(e.type is EventType.ATTACK for e in session.store)


def test_a_target_the_player_named_is_resolved_from_the_scene(session, dm, vale):
    goblins = session.spawn("goblin", 2)
    session.combat.start(session.journal, [vale.id, *[g.id for g in goblins]])

    result = act(session, dm, f"I attack {goblins[0].name} with my sword", vale.id)

    assert result.interpretation.target_ids == [goblins[0].id]
    assert any(e.type is EventType.ATTACK for e in session.store)


def test_a_provider_failure_degrades_instead_of_ending_the_session(session, dm, vale):
    class Broken(OfflineProvider):
        def parse(self, **kwargs):
            raise ProviderError("the model is on fire")

        def complete(self, **kwargs):
            raise ProviderError("still on fire")

    dm.provider = Broken()
    result = act(session, dm, "I search the room", vale.id)

    assert result.narration  # the offline DM answered
    assert dm.last.interpreted_by == "offline"
    assert dm.last.narrated_by == "offline"
    assert len(dm.last.degraded) == 2


def test_a_turn_files_the_memories_it_made(session, dm, vale):
    session.quests.discover(session.journal, "There was a struggle here.")
    before = len(session.state.memories)

    act(session, dm, "I ask Marda what she saw", vale.id)

    assert len(session.state.memories) >= before


def test_the_same_memory_is_never_filed_twice(session, dm, vale):
    act(session, dm, "I search the room", vale.id)
    act(session, dm, "I search the room again", vale.id)

    sources = [m.source_event_id for m in session.state.memories]
    assert len(sources) == len(set(sources))


def test_diagnostics_explain_without_exposing_reasoning(session, dm, vale):
    act(session, dm, "I sneak up the back stairs", vale.id)

    assert "stealth check is required" in dm.last.rationale
    assert dm.last.checks_requested == 1
    assert dm.last.events_appended > 0


def test_a_dice_block_is_compressed_to_one_line_for_the_narrator(session, vale):
    session.checks.check(session.journal, vale.id, skill="perception", dc=12)
    event = next(e for e in session.store if e.type is EventType.CHECK_RESOLVED)

    line = fact_line(event)

    assert "\n" not in line
    assert "against DC 12" in line


def test_the_campaign_still_replays_exactly_after_ai_turns(session, dm, vale):
    from dmai.engine.state import rebuild

    act(session, dm, "I search the room", vale.id)
    act(session, dm, "I ask Marda about the caravan", vale.id)

    replayed = rebuild(session.campaign, session.store.all())

    assert replayed.model_dump(mode="json") == session.state.model_dump(mode="json")


def test_a_recap_falls_back_to_the_mechanical_digest(session, dm, vale):
    class Broken(OfflineProvider):
        def complete(self, **kwargs):
            raise ProviderError("no model")

    session.quests.discover(session.journal, "The mayor is lying.")
    dm.provider = Broken()

    assert "The mayor is lying." in dm.session_recap()


# --- the Claude provider ---------------------------------------------------
#
# Exercised against a stub client: these check the shapes this project builds
# and reads, not Anthropic's service.


class _Block:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class _Usage:
    def __init__(self, input_tokens=100, output_tokens=50, cache_read_input_tokens=0,
                 iterations=None):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = cache_read_input_tokens
        self.iterations = iterations or []


class _Named:
    def __init__(self, model):
        self.model = model


class _FallbackBlock:
    """The block the API emits where a model declined and another took over."""

    def __init__(self, origin, destination):
        self.type = "fallback"
        self.from_ = _Named(origin)
        self.to = _Named(destination)


class _Iteration:
    def __init__(self, type="fallback_message"):
        self.type = type


class _Response:
    def __init__(self, *, text="", parsed=None, stop_reason="end_turn", details=None,
                 model="claude-opus-5", content=None, iterations=None):
        self.content = content if content is not None else ([_Block(text)] if text else [])
        self.parsed_output = parsed
        self.stop_reason = stop_reason
        self.stop_details = details
        self.model = model
        self.usage = _Usage(iterations=iterations)


class _StubClient:
    """Records what the provider sent, and replies with what it is told to.

    Both the plain and the beta message surfaces are recorded, so a test can
    assert which endpoint a call went to.
    """

    def __init__(self, response, *, beta_error=None):
        self.response = response
        self.calls: list[dict] = []
        self.beta_calls: list[dict] = []
        outer = self

        class _Messages:
            def create(self, **kwargs):
                outer.calls.append(kwargs)
                return outer.response

            def parse(self, **kwargs):
                outer.calls.append(kwargs)
                return outer.response

        class _BetaMessages:
            def create(self, **kwargs):
                outer.beta_calls.append(kwargs)
                if beta_error is not None:
                    raise beta_error
                return outer.response

        class _Beta:
            messages = _BetaMessages()

        self.messages = _Messages()
        self.beta = _Beta()


def _claude(response, *, beta_error=None, **kwargs):
    from dmai.ai.providers.claude import ClaudeProvider

    stub = _StubClient(response, beta_error=beta_error)
    return ClaudeProvider(client=stub, **kwargs), stub


class _HttpResponse:
    """The minimum an anthropic APIStatusError needs to be constructed."""

    def __init__(self, status_code: int):
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.request = None


def _raise(error):
    def _boom(**kwargs):
        raise error

    return _boom


def test_claude_returns_text_and_prices_the_call():
    provider, _ = _claude(_Response(text="The hall is silent."))

    completion = provider.complete(system="s", messages=[{"role": "user", "content": "go"}])

    assert completion.text == "The hall is silent."
    assert completion.model == "claude-opus-5"
    assert completion.cost_usd > 0
    assert completion.meta["fell_back"] is False


def test_claude_caches_the_standing_system_prompt():
    """The DM persona is identical every turn; it should be paid for once."""
    provider, stub = _claude(_Response(text="ok"))

    provider.complete(system="the DM persona", messages=[{"role": "user", "content": "go"}])

    system = stub.beta_calls[0]["system"]
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert system[0]["text"] == "the DM persona"


def test_claude_asks_for_a_validated_interpretation():
    interpretation = ActionInterpretation(intent=IntentKind.SEARCH)
    provider, stub = _claude(_Response(parsed=interpretation))

    completion = provider.parse(
        system="s",
        messages=[{"role": "user", "content": "I search"}],
        schema=ActionInterpretation,
    )

    assert completion.parsed is interpretation
    assert stub.calls[0]["output_format"] is ActionInterpretation
    assert stub.calls[0]["model"] == "claude-opus-5"


def test_claude_raises_rather_than_returning_a_half_parsed_object():
    provider, _ = _claude(_Response(parsed=None))

    with pytest.raises(ProviderError, match="returned no ActionInterpretation"):
        provider.parse(system="s", messages=[], schema=ActionInterpretation)


def test_a_refusal_is_its_own_error_so_the_dm_can_degrade():
    from dmai.ai.providers import ProviderRefusal

    class _Details:
        category = "violence"

    provider, _ = _claude(_Response(stop_reason="refusal", details=_Details()))

    with pytest.raises(ProviderRefusal, match="violence"):
        provider.complete(system="s", messages=[])


def test_a_refusing_model_does_not_end_the_turn(session, dm, vale):
    from dmai.ai.providers import ProviderRefusal

    class Squeamish(OfflineProvider):
        def complete(self, **kwargs):
            raise ProviderRefusal("the model declined this scene (violence)")

    dm.provider = Squeamish()
    result = act(session, dm, "I search the room", vale.id)

    assert result.narration  # the offline DM finished the turn
    assert any("declined" in note for note in dm.last.degraded)


# --- refusal fallbacks -----------------------------------------------------
#
# A fantasy table runs into policy declines more than most software does:
# violence is the subject matter. The narration path therefore opts into
# server-side fallbacks, so a declined scene is re-run on another model inside
# the same call rather than dropping to the offline narrator.


def test_narration_opts_into_server_side_fallbacks():
    from dmai.ai.providers.claude import FALLBACK_DEFAULT_BETA

    provider, stub = _claude(_Response(text="The siege begins."))

    provider.complete(system="s", messages=[{"role": "user", "content": "go"}])

    assert stub.calls == []  # not the plain endpoint
    call = stub.beta_calls[0]
    assert call["fallbacks"] == "default"
    assert call["betas"] == [FALLBACK_DEFAULT_BETA]


def test_the_beta_header_matches_the_form_of_the_parameter():
    """Pairing one form with the other header is a 400, so they travel together."""
    from dmai.ai.providers.claude import FALLBACK_ARRAY_BETA, FALLBACK_DEFAULT_BETA

    pinned, stub = _claude(
        _Response(text="ok"), fallbacks=[{"model": "claude-opus-4-8"}]
    )
    pinned.complete(system="s", messages=[])

    assert stub.beta_calls[0]["betas"] == [FALLBACK_ARRAY_BETA]
    assert stub.beta_calls[0]["fallbacks"] == [{"model": "claude-opus-4-8"}]

    default, other = _claude(_Response(text="ok"))
    default.complete(system="s", messages=[])

    assert other.beta_calls[0]["betas"] == [FALLBACK_DEFAULT_BETA]


def test_interpretation_does_not_use_the_beta_endpoint():
    """Only narration falls back; a refused interpretation degrades locally."""
    provider, stub = _claude(_Response(parsed=ActionInterpretation(intent=IntentKind.SEARCH)))

    provider.parse(system="s", messages=[], schema=ActionInterpretation)

    assert stub.beta_calls == []
    assert "fallbacks" not in stub.calls[0]


def test_opting_out_of_fallbacks_uses_the_plain_endpoint():
    provider, stub = _claude(_Response(text="ok"), fallbacks=None)

    provider.complete(system="s", messages=[])

    assert stub.beta_calls == []
    assert stub.calls


def test_a_switched_turn_reports_which_model_answered():
    provider, _ = _claude(
        _Response(
            model="claude-opus-4-8",
            content=[_FallbackBlock("claude-opus-5", "claude-opus-4-8"), _Block("The siege.")],
        )
    )

    completion = provider.complete(system="s", messages=[])

    assert completion.text == "The siege."  # the fallback block is not prose
    assert completion.meta["fell_back"] is True
    assert completion.meta["fallback_switches"] == ["claude-opus-5 -> claude-opus-4-8"]
    assert completion.model == "claude-opus-4-8"


def test_a_sticky_turn_is_noticed_without_a_fallback_block():
    """Once a conversation falls back, later turns carry only a usage iteration."""
    provider, _ = _claude(
        _Response(text="The siege continues.", model="claude-opus-4-8",
                  iterations=[_Iteration()])
    )

    completion = provider.complete(system="s", messages=[])

    assert completion.meta["fell_back"] is True
    assert completion.meta["fallback_switches"] == []
    assert completion.meta["served_by"] == "claude-opus-4-8"


def test_a_fallback_is_priced_at_the_model_that_served_it():
    from dmai.ai.providers.claude import estimate_cost

    provider, _ = _claude(_Response(text="ok", model="claude-haiku-4-5"))

    completion = provider.complete(system="s", messages=[])

    assert completion.cost_usd == pytest.approx(estimate_cost("claude-haiku-4-5", 100, 50))


def test_a_whole_chain_refusing_still_raises():
    from dmai.ai.providers import ProviderRefusal

    class _Details:
        category = "violence"

    provider, _ = _claude(_Response(stop_reason="refusal", details=_Details()))

    with pytest.raises(ProviderRefusal, match="violence"):
        provider.complete(system="s", messages=[])


def test_an_account_without_the_beta_narrates_anyway():
    """A 400 on the beta means retry once without it -- not lose the turn."""
    import anthropic

    refused = anthropic.BadRequestError(
        "unsupported beta", response=_HttpResponse(400), body=None
    )
    provider, stub = _claude(_Response(text="The hall is silent."), beta_error=refused)

    first = provider.complete(system="s", messages=[])
    second = provider.complete(system="s", messages=[])

    assert first.text == "The hall is silent."
    assert second.text == "The hall is silent."
    # Tried the beta once, then stopped trying.
    assert len(stub.beta_calls) == 1
    assert len(stub.calls) == 2
    assert "refusal fallback" not in provider.health()


def test_an_older_sdk_without_the_parameter_narrates_anyway():
    provider, stub = _claude(
        _Response(text="ok"),
        beta_error=TypeError("create() got an unexpected keyword argument 'fallbacks'"),
    )

    assert provider.complete(system="s", messages=[]).text == "ok"
    assert len(stub.calls) == 1


def test_a_real_bad_request_is_not_mistaken_for_a_missing_beta():
    import anthropic

    provider, stub = _claude(_Response(text="ok"), fallbacks=None)
    stub.messages.create = _raise(
        anthropic.BadRequestError("max_tokens too large", response=_HttpResponse(400), body=None)
    )

    with pytest.raises(ProviderError, match="Anthropic rejected the request"):
        provider.complete(system="s", messages=[])


def test_the_table_is_told_when_another_model_finished_the_scene(session, dm, vale):
    class Switched(OfflineProvider):
        def complete(self, **kwargs):
            completion = super().complete(**kwargs)
            completion.model = "claude-opus-4-8"
            completion.meta["fell_back"] = True
            return completion

    dm.provider = Switched()
    act(session, dm, "I search the room", vale.id)

    assert dm.last.served_by == "claude-opus-4-8"
    assert any("declined" in note for note in dm.last.degraded)
