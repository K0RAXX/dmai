/* The Dungeon Master: the reasoning loop, and the DM that needs no network.
 *
 * A port of dmai/ai/providers/offline.py, ai/memory/extraction.py and
 * ai/dm_agent/agent.py.  The loop is:
 *
 *   PLAYER INPUT -> interpret -> read state -> decide what must be rolled
 *     -> resolve mechanics -> update world/character state
 *     -> narrate the consequence -> update memory -> respond
 *
 * The whole design rests on one boundary.  The DM is asked twice, and neither
 * time is it asked what happened:
 *
 * 1. Interpret.  "What is this player trying to do, and what must be rolled?"
 * 2. Narrate.  "Here is what the engine resolved; describe it."
 *
 * The facts handed over are the summaries of events the engine actually
 * appended while resolving the action -- so the DM narrates the log rather
 * than authoring it.  Every id is checked against real state before it is
 * used, so a hallucinated target is dropped rather than acted on.
 *
 * The offline provider is a real implementation, not a stub.  Interpretation
 * is keyword classification over the player's own words -- the same job a
 * model does, done worse but honestly -- and narration restates the facts the
 * engine resolved, in order, as plain sentences.  What it will not do is
 * invent an outcome: it never sees a die and never reports one the engine did
 * not roll.
 */
(function (DMAI) {
  'use strict';

  var EventType = DMAI.EventType;
  var Visibility = DMAI.Visibility;

  var IntentKind = {
    ATTACK: 'attack',
    CAST_SPELL: 'cast_spell',
    SKILL_CHECK: 'skill_check',
    SAVING_THROW: 'saving_throw',
    MOVE: 'move',
    TALK: 'talk',
    SEARCH: 'search',
    USE_ITEM: 'use_item',
    REST: 'rest',
    //: Pure roleplay or a question; no mechanics required.
    FREEFORM: 'freeform',
    //: Out-of-character question about rules or state.
    META: 'meta'
  };

  //: Verb -> intent.  Ordered most specific first; the first hit wins.
  var INTENT_WORDS = [
    [IntentKind.ATTACK, ['attack', 'hit ', 'strike', 'swing', 'stab', 'slash',
      'shoot', 'fire at', 'punch', 'kill']],
    [IntentKind.CAST_SPELL, ['cast', 'spell', 'incant', 'channel']],
    [IntentKind.REST, ['rest', 'sleep', 'camp', 'make camp', 'long rest', 'short rest']],
    [IntentKind.USE_ITEM, ['use ', 'drink', 'quaff', 'equip', 'wield',
      'read the', 'light the']],
    [IntentKind.SEARCH, ['search', 'look for', 'examine', 'inspect',
      'investigate', 'study', 'check for']],
    [IntentKind.TALK, ['ask', 'tell', 'say', 'talk', 'speak', 'greet', 'persuade',
      'convince', 'threaten', 'lie', 'bargain', 'haggle']],
    [IntentKind.MOVE, ['go ', 'walk', 'run', 'head ', 'travel', 'enter', 'leave',
      'climb', 'sneak', 'follow', 'return']],
    [IntentKind.META, ['what are my', 'how much hp', 'what is my', 'rules',
      'how do i', 'can i see my']]
  ];

  //: Phrase -> skill.  Checked before intent, because "sneak into the vault"
  //: is a stealth check whichever way the verb is classified.
  var SKILL_WORDS = {
    stealth: ['sneak', 'hide', 'quietly', 'unseen', 'slip past', 'creep'],
    perception: ['listen', 'look around', 'notice', 'watch', 'keep an eye', 'scan'],
    investigation: ['search', 'examine', 'inspect', 'investigate', 'study', 'look for'],
    persuasion: ['persuade', 'convince', 'plead', 'reason with', 'ask nicely', 'negotiate'],
    deception: ['lie', 'bluff', 'pretend', 'disguise', 'trick', 'mislead'],
    intimidation: ['threaten', 'intimidate', 'menace', 'scare'],
    insight: ['read them', 'sense motive', 'tell if', 'gauge', 'judge whether'],
    athletics: ['climb', 'jump', 'swim', 'shove', 'force the', 'break down', 'lift'],
    acrobatics: ['tumble', 'balance', 'flip', 'vault'],
    arcana: ['magical', 'arcane', 'runes', 'sigil', 'enchantment'],
    medicine: ['stabilise', 'stabilize', 'bandage', 'treat the wound', 'first aid'],
    survival: ['track', 'forage', 'navigate', 'follow the trail'],
    history: ['recall', 'remember the', 'legend', 'chronicle'],
    religion: ['prayer', 'holy', 'divine', 'shrine'],
    animal_handling: ['calm the', 'soothe the horse', 'ride', 'tame'],
    sleight_of_hand: ['pickpocket', 'palm', 'lift the purse', 'pick the lock']
  };

  //: Actions that change the world even when nothing is rolled.
  var MUTATING_INTENTS = [
    IntentKind.ATTACK, IntentKind.CAST_SPELL, IntentKind.MOVE,
    IntentKind.USE_ITEM, IntentKind.REST, IntentKind.SEARCH,
    IntentKind.SKILL_CHECK
  ];

  //: The DC asked for when a skill is implied but no difficulty was stated.
  var DEFAULT_DC = 13;

  function article(word) {
    return 'aeiou'.indexOf(String(word).charAt(0).toLowerCase()) !== -1 ? 'an' : 'a';
  }

  function skillFor(lowered) {
    var found = null;
    Object.keys(SKILL_WORDS).some(function (skill) {
      var hit = SKILL_WORDS[skill].some(function (phrase) {
        return lowered.indexOf(phrase) !== -1;
      });
      if (hit) { found = skill; }
      return hit;
    });
    return found;
  }

  function intentFor(lowered, skill) {
    var found = null;
    INTENT_WORDS.some(function (entry) {
      var hit = entry[1].some(function (word) {
        return lowered.indexOf(word) !== -1;
      });
      if (hit) { found = entry[0]; }
      return hit;
    });
    if (found) { return found; }
    return skill ? IntentKind.SKILL_CHECK : IntentKind.FREEFORM;
  }

  /** Classify one line of player input.  Deterministic and side-effect free. */
  function interpret(text) {
    var lowered = String(text || '').toLowerCase().trim();
    var skill = skillFor(lowered);
    var intent = intentFor(lowered, skill);

    var checks = [];
    var rationale = '';

    if (intent === IntentKind.ATTACK) {
      rationale = 'An attack is resolved by the combat engine.';
    } else if (skill) {
      var label = DMAI.readable(skill);
      // The actor is filled in by the agent, which knows whose turn it is.
      checks.push({
        kind: IntentKind.SKILL_CHECK,
        actor_id: '',
        target_id: null,
        ability: null,
        skill: skill,
        dc: DEFAULT_DC,
        expression: null,
        reason: label + ' check'
      });
      if (intent === IntentKind.FREEFORM) { intent = IntentKind.SKILL_CHECK; }
      rationale = 'The DM determined that ' + article(label) + ' '
        + label + ' check is required.';
    }

    return {
      intent: intent,
      summary: String(text || '').trim(),
      actor_id: null,
      target_ids: [],
      checks: checks,
      state_queries: [],
      mutates_state: MUTATING_INTENTS.indexOf(intent) !== -1,
      rationale: rationale
    };
  }

  /**
   * The offline narrator: restate the facts the engine established, as
   * sentences.  Anything outside the facts is context this provider is not
   * equipped to embroider, so it is left alone.
   */
  function OfflineProvider() {
    this.id = 'offline';
    this.name = 'Offline DM';
    this.offline = true;
  }

  OfflineProvider.prototype.narrate = function (facts) {
    if (!facts.length) { return ''; }
    return facts.map(function (fact) {
      return /[.!?]$/.test(fact) ? fact : fact + '.';
    }).join(' ');
  };

  OfflineProvider.prototype.interpret = function (text) {
    return interpret(text);
  };

  OfflineProvider.prototype.health = function () {
    return 'Offline DM ready (no model configured; narration will be terse)';
  };

  // --- memory --------------------------------------------------------------
  //
  // The DM does not carry a transcript.  It carries typed memories extracted
  // from the event log, scored, and retrieved by relevance.  Extraction is
  // rule-based on purpose: what matters about an event is already typed in the
  // log, so reading it back costs nothing and cannot hallucinate.

  //: Event type -> [kind, tier, importance].  An event type absent from this
  //: map is not memorable: dice rolls, turn markers and movement are noise a
  //: week later, and keeping them would crowd out what matters.
  var MEMORABLE = {};
  MEMORABLE[EventType.DISCOVERY] = [DMAI.MemoryKind.DISCOVERY, DMAI.MemoryTier.CAMPAIGN, 70];
  MEMORABLE[EventType.STORY_DECISION] = [DMAI.MemoryKind.FACT, DMAI.MemoryTier.CAMPAIGN, 80];
  MEMORABLE[EventType.CONSEQUENCE_ADDED] = [DMAI.MemoryKind.PROMISE, DMAI.MemoryTier.CAMPAIGN, 75];
  MEMORABLE[EventType.CONSEQUENCE_RESOLVED] = [DMAI.MemoryKind.CONSEQUENCE, DMAI.MemoryTier.CAMPAIGN, 65];
  MEMORABLE[EventType.QUEST_ADDED] = [DMAI.MemoryKind.FACT, DMAI.MemoryTier.CAMPAIGN, 60];
  MEMORABLE[EventType.OBJECTIVE_COMPLETED] = [DMAI.MemoryKind.FACT, DMAI.MemoryTier.SESSION, 50];
  MEMORABLE[EventType.NPC_MET] = [DMAI.MemoryKind.RELATIONSHIP, DMAI.MemoryTier.CAMPAIGN, 55];
  MEMORABLE[EventType.NPC_ATTITUDE_CHANGED] = [DMAI.MemoryKind.RELATIONSHIP, DMAI.MemoryTier.CAMPAIGN, 65];
  MEMORABLE[EventType.CREATURE_DIED] = [DMAI.MemoryKind.FACT, DMAI.MemoryTier.SESSION, 60];
  MEMORABLE[EventType.LEVEL_UP] = [DMAI.MemoryKind.FACT, DMAI.MemoryTier.CAMPAIGN, 55];
  MEMORABLE[EventType.WORLD_EVENT] = [DMAI.MemoryKind.LORE, DMAI.MemoryTier.WORLD, 50];
  MEMORABLE[EventType.LOCATION_ENTERED] = [DMAI.MemoryKind.FACT, DMAI.MemoryTier.SESSION, 30];
  MEMORABLE[EventType.COMBAT_ENDED] = [DMAI.MemoryKind.FACT, DMAI.MemoryTier.SESSION, 45];

  //: A player character dying is the most memorable thing that can happen.
  var DEATH_IMPORTANCE = 95;

  /**
   * Turn a stretch of log into the memories worth keeping.
   *
   * Events the table never saw stay out: a memory the DM can quote is a memory
   * the DM can leak, and DM-only knowledge belongs in the world state.
   */
  function extractMemories(events, partyIds) {
    var party = partyIds || [];
    var memories = [];

    events.forEach(function (event) {
      var entry = MEMORABLE[event.type];
      if (!entry || !event.summary) { return; }
      if (event.visibility === Visibility.DM_ONLY) { return; }

      var importance = entry[2];
      if (event.type === EventType.CREATURE_DIED
        && party.indexOf(event.target_id) !== -1) {
        importance = DEATH_IMPORTANCE;
      }

      memories.push({
        id: DMAI.newId('mem'),
        tier: entry[1],
        kind: entry[0],
        content: event.summary,
        subjects: [event.actor_id, event.target_id].filter(Boolean),
        importance: importance,
        visibility: event.visibility,
        created_at: DMAI.utcnow(),
        source_event_id: event.id
      });
    });
    return memories;
  }

  /**
   * The memories most worth putting in front of the DM right now.
   *
   * Scored on importance, tier durability, and whether the memory is about
   * someone in the scene -- so an NPC the party is talking to pulls up what
   * they promised that NPC three sessions ago.
   */
  function recallMemories(memories, options) {
    var settings = options || {};
    var here = settings.subjects || [];
    var limit = settings.limit === undefined ? 12 : settings.limit;

    var durability = {};
    durability[DMAI.MemoryTier.CAMPAIGN] = 20;
    durability[DMAI.MemoryTier.WORLD] = 10;
    durability[DMAI.MemoryTier.SESSION] = 5;
    durability[DMAI.MemoryTier.SHORT_TERM] = 0;

    function score(memory) {
      var relevance = memory.subjects.some(function (subject) {
        return here.indexOf(subject) !== -1;
      }) ? 40 : 0;
      return memory.importance + relevance + (durability[memory.tier] || 0);
    }

    return memories
      .filter(function (memory) {
        // DM-only memories never reach text that reaches a player.
        if (memory.visibility === Visibility.PUBLIC) { return true; }
        return memory.visibility === Visibility.PRIVATE && settings.player_id;
      })
      .slice()
      .sort(function (left, right) {
        return score(right) - score(left) || right.importance - left.importance;
      })
      .slice(0, limit);
  }

  // --- the loop ------------------------------------------------------------

  //: Event types whose summaries are mechanical facts the narrator must honour.
  var FACT_EVENTS = [
    EventType.CHECK_RESOLVED, EventType.SAVE_RESOLVED, EventType.DICE_ROLLED,
    EventType.ATTACK, EventType.DAMAGE_DEALT, EventType.HEALING,
    EventType.CREATURE_DIED, EventType.CONDITION_ADDED,
    EventType.CONDITION_REMOVED, EventType.ITEM_GAINED, EventType.ITEM_LOST,
    EventType.CURRENCY_CHANGED, EventType.LOCATION_ENTERED,
    EventType.TIME_ADVANCED, EventType.COMBAT_STARTED, EventType.COMBAT_ENDED,
    EventType.OBJECTIVE_COMPLETED, EventType.DISCOVERY, EventType.WORLD_EVENT
  ];

  /**
   * One event as a single line of fact.
   *
   * A dice event's summary is the multi-line block the table sees in the log.
   * That block is for players; a narrator wants one sentence, so rolls are
   * compressed here rather than pasted in.
   */
  function factLine(event) {
    var roll = event.data.roll;
    if (roll && roll.total !== undefined) {
      return DMAI.summariseRoll(roll);
    }
    return event.summary.split(/\s+/).join(' ');
  }

  /** An AI DM bound to one campaign session. */
  function DungeonMaster(session, provider) {
    this.session = session;
    //: Always present, always local.  The DM degrades to this rather than
    //: failing a turn, because a table mid-scene cannot wait for an outage.
    this.fallback = new OfflineProvider();
    this.provider = provider || this.fallback;
    this.last = { provider: this.provider.id, degraded: [] };
  }

  /** Run one player action all the way through. */
  DungeonMaster.prototype.takeTurn = function (action) {
    var session = this.session;
    var diagnostics = {
      provider: this.provider.id,
      interpreted_by: '',
      narrated_by: '',
      rationale: '',
      checks_requested: 0,
      events_appended: 0,
      degraded: []
    };

    session.recordAction(action);
    var before = session.store.head();

    var interpretation = this.provider.interpret(action.text);
    diagnostics.interpreted_by = this.provider.id;
    interpretation = this._ground(interpretation, action);
    diagnostics.checks_requested = interpretation.checks.length;
    diagnostics.rationale = interpretation.rationale;

    var rolls = this._resolve(interpretation, action);

    var appended = session.store.since(before);
    var facts = appended
      .filter(function (event) {
        return FACT_EVENTS.indexOf(event.type) !== -1 && event.summary;
      })
      .map(factLine);

    var narration = this.provider.narrate(facts, {
      action: action,
      interpretation: interpretation,
      memories: recallMemories(session.state.memories, {
        subjects: interpretation.target_ids.concat([interpretation.actor_id])
      }),
      recent: session.transcript(null, { limit: 6 })
    });
    if (!narration && this.provider !== this.fallback) {
      diagnostics.degraded.push('narration: provider returned nothing');
      narration = this.fallback.narrate(facts);
      diagnostics.narrated_by = this.fallback.id;
    } else {
      diagnostics.narrated_by = this.provider.id;
    }

    if (narration) { session.narrate(narration); }

    this._remember(appended);
    diagnostics.events_appended = session.store.head() - before;
    this.last = diagnostics;

    return {
      action_id: action.id,
      interpretation: interpretation,
      rolls: rolls,
      event_ids: session.store.since(before).map(function (event) { return event.id; }),
      narration: narration,
      diagnostics: diagnostics
    };
  };

  /**
   * Bind an interpretation to reality before anything acts on it.
   *
   * The actor is whoever spoke, not whoever the DM named.  Target ids that do
   * not exist are dropped and re-derived from the player's own words, so an
   * invented creature can never be attacked.
   */
  DungeonMaster.prototype._ground = function (interpretation, action) {
    var state = this.session.state;
    var grounded = DMAI.deepCopy(interpretation);
    var actorId = action.character_id || '';

    grounded.actor_id = actorId || grounded.actor_id;
    grounded.target_ids = grounded.target_ids.filter(function (id) {
      return DMAI.findCreature(state, id) || state.world.locations[id];
    });
    if (!grounded.target_ids.length) {
      grounded.target_ids = this._targetsNamedIn(action.text);
    }

    grounded.checks = grounded.checks.map(function (check) {
      var bound = DMAI.deepCopy(check);
      if (!DMAI.findCreature(state, bound.actor_id)) { bound.actor_id = actorId; }
      if (bound.target_id && !DMAI.findCreature(state, bound.target_id)) {
        bound.target_id = null;
      }
      return bound;
    }).filter(function (check) {
      // Nobody real to roll it: drop the check, not the turn.
      return Boolean(DMAI.findCreature(state, check.actor_id));
    });
    return grounded;
  };

  /** Find creatures the player named, by matching names in the scene. */
  DungeonMaster.prototype._targetsNamedIn = function (text) {
    var state = this.session.state;
    var lowered = String(text).toLowerCase();
    var candidates = [];

    [state.bestiary, state.world.npcs, state.party].forEach(function (pool) {
      Object.keys(pool).forEach(function (id) { candidates.push(pool[id]); });
    });

    return candidates
      .filter(function (creature) {
        return !creature.dead && lowered.indexOf(creature.name.toLowerCase()) !== -1;
      })
      .map(function (creature) { return creature.id; });
  };

  /** Run everything the interpretation asked for, through the engine. */
  DungeonMaster.prototype._resolve = function (interpretation, action) {
    var session = this.session;
    var rolls = [];

    if (interpretation.intent === IntentKind.ATTACK) {
      rolls = rolls.concat(this._resolveAttack(interpretation, action));
    }

    interpretation.checks.forEach(function (check) {
      try {
        rolls.push(session.checks.resolveRequest(session.journal, check));
      } catch (error) {
        session.journal.record(EventType.DM_OVERRIDE, {
          summary: 'A requested check could not be resolved: ' + error.message,
          visibility: Visibility.DM_ONLY
        });
      }
    });
    return rolls;
  };

  /** An attack goes through the combat engine or it does not happen. */
  DungeonMaster.prototype._resolveAttack = function (interpretation, action) {
    var session = this.session;
    var attackerId = interpretation.actor_id || action.character_id || '';
    var targetId = interpretation.target_ids[0];

    if (!attackerId || !targetId) {
      // Nobody was named and nothing in the scene matched: say so rather than
      // silently swinging at the air.
      session.journal.record(EventType.DM_OVERRIDE, {
        summary: 'No target in this scene matched that attack.',
        visibility: Visibility.DM_ONLY
      });
      return [];
    }
    try {
      var outcome = session.combat.attack(
        session.journal, attackerId, targetId, null,
        { spend_action: session.state.combat.active }
      );
      return [outcome.attack_roll, outcome.damage_roll].filter(Boolean);
    } catch (error) {
      session.journal.record(EventType.DM_OVERRIDE, {
        summary: 'That attack could not be resolved: ' + error.message,
        visibility: Visibility.DM_ONLY
      });
      return [];
    }
  };

  /** File what is worth keeping, and nothing else. */
  DungeonMaster.prototype._remember = function (events) {
    var session = this.session;
    var party = Object.keys(session.state.party);
    var known = session.state.memories.map(function (memory) {
      return memory.source_event_id;
    });

    extractMemories(events, party).forEach(function (memory) {
      if (known.indexOf(memory.source_event_id) !== -1) { return; }
      session.journal.record(EventType.MEMORY_ADDED, {
        summary: memory.content, memory: memory, visibility: memory.visibility
      });
    });
  };

  /**
   * Narrate the opening of a scene, or the campaign itself.
   *
   * The offline DM has no prose of its own, so it opens with what is actually
   * true: the premise, the place, and who is standing there.
   */
  DungeonMaster.prototype.openScene = function (direction) {
    var session = this.session;
    var lines = [];

    if (direction) {
      lines.push(direction);
    } else {
      if (session.campaign.premise) { lines.push(session.campaign.premise); }
      var location = DMAI.currentLocation(session.state);
      if (location) {
        lines.push('You are at ' + location.name
          + (location.description ? '. ' + location.description : '.'));
      }
      var party = session.partyList();
      if (party.length) {
        lines.push('With you: ' + party.map(function (character) {
          return character.name + ' (' + character.species + ' '
            + character['class'] + ', level ' + character.level + ')';
        }).join(', ') + '.');
      }
      lines.push(DMAI.describeTime(session.state.world.time)
        + ', ' + session.state.world.weather + '. What do you do?');
    }

    var text = lines.join(' ');
    if (text) { session.narrate(text); }
    return text;
  };

  /** Bring the table back to where they left off. */
  DungeonMaster.prototype.resume = function () {
    var text = this.session.recap().narrative;
    if (text) { this.session.narrate(text); }
    return text;
  };

  /** The session summary.  The mechanical recap is always true, if never pretty. */
  DungeonMaster.prototype.sessionRecap = function (since) {
    return this.session.recap(since).narrative;
  };

  DMAI.IntentKind = IntentKind;
  DMAI.INTENT_WORDS = INTENT_WORDS;
  DMAI.SKILL_WORDS = SKILL_WORDS;
  DMAI.DEFAULT_DC = DEFAULT_DC;
  DMAI.MEMORABLE = MEMORABLE;
  DMAI.FACT_EVENTS = FACT_EVENTS;
  DMAI.OfflineProvider = OfflineProvider;
  DMAI.DungeonMaster = DungeonMaster;
  DMAI.interpretAction = interpret;
  DMAI.extractMemories = extractMemories;
  DMAI.recallMemories = recallMemories;
  DMAI.factLine = factLine;
})(window.DMAI = window.DMAI || {});
