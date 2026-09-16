/* The engine's front door -- a port of dmai/engine/session.py.
 *
 * The Dungeon Master must be an engine that interfaces talk to, not a GUI that
 * contains an AI.  GameSession is that engine: it owns one campaign's event
 * log and wires every subsystem to it, so the HTML client, the desktop window
 * and any future adapter all drive the same object and see the same game.
 *
 * Three things this class is careful about:
 *
 * - One write path.  Everything goes through the Journal, so the campaign can
 *   always be rebuilt from its log, rolled back to a checkpoint, and audited.
 * - No I/O.  Saving is the library's job; narration is the DM's.  A session
 *   runs happily with neither.
 * - Seats, not omniscience.  logFor filters the record to what one player is
 *   allowed to know.
 */
(function (DMAI) {
  'use strict';

  var EventType = DMAI.EventType;
  var Visibility = DMAI.Visibility;
  var QuestStatus = DMAI.QuestStatus;

  /** One campaign, fully wired: rules, dice, log, and every subsystem. */
  function GameSession(campaign, options) {
    var settings = options || {};
    this.rules = settings.rules
      || DMAI.loadRules(campaign.settings.rules_pack, settings.pack);
    this.dice = settings.dice || new DMAI.DiceEngine(campaign.settings.rng_seed);

    this.store = new DMAI.EventStore(campaign.id, settings.events || []);
    this.state = DMAI.rebuild(campaign, this.store);
    this.journal = new DMAI.Journal(this.state, this.store);

    this.checks = new DMAI.CheckResolver(this.rules, this.dice);
    this.combat = new DMAI.CombatEngine(this.rules, this.dice);
    this.inventory = new DMAI.InventoryManager(this.rules);
    this.encounters = new DMAI.EncounterGenerator(this.rules, this.dice);
    this.quests = new DMAI.QuestTracker();
    this.atlas = new DMAI.Atlas();
    this.world = new DMAI.WorldSimulator(this.rules, this.dice, this.quests);
  }

  // --- lifecycle -----------------------------------------------------------

  /** Open a brand-new campaign and log its first event. */
  GameSession.create = function (campaign, options) {
    var session = new GameSession(campaign, options);
    session.journal.record(EventType.CAMPAIGN_CREATED, {
      target_id: campaign.id,
      summary: 'Campaign created: ' + campaign.name + '.',
      campaign: DMAI.deepCopy(campaign)
    });
    return session;
  };

  /** Reopen a saved campaign by replaying its log. */
  GameSession.restore = function (campaign, events, options) {
    return new GameSession(campaign, Object.assign({}, options, { events: events }));
  };

  Object.defineProperty(GameSession.prototype, 'campaign', {
    get: function () { return this.state.campaign; }
  });

  /** Start a sitting.  Its recap covers the events from here on. */
  GameSession.prototype.beginSession = function () {
    var sitting = {
      id: DMAI.newId('sess'),
      campaign_id: this.campaign.id,
      started_at: DMAI.utcnow(),
      ended_at: null,
      first_event_seq: this.store.head() + 1,
      last_event_seq: 0,
      recap: ''
    };
    this.journal.record(EventType.SESSION_STARTED, {
      target_id: sitting.id,
      summary: 'Session begins. ' + DMAI.describeTime(this.state.world.time) + '.',
      session: sitting
    });
    return sitting;
  };

  /** Close a sitting and attach its recap. */
  GameSession.prototype.endSession = function (sitting, recap) {
    var closed = DMAI.deepCopy(sitting);
    closed.ended_at = DMAI.utcnow();
    closed.last_event_seq = this.store.head();
    closed.recap = recap || this.recap(sitting.first_event_seq).narrative;
    this.journal.record(EventType.SESSION_ENDED, {
      target_id: closed.id, summary: 'Session ends.', session: closed
    });
    return closed;
  };

  // --- the table -----------------------------------------------------------

  /** Seat a human.  external_id is how a chat adapter finds them again. */
  GameSession.prototype.addPlayer = function (name, options) {
    var settings = options || {};
    var player = {
      id: DMAI.newId('player'),
      name: name,
      external_id: settings.external_id || null,
      character_ids: [],
      is_host: Boolean(settings.is_host)
    };
    this.journal.record(EventType.PLAYER_JOINED, {
      actor_id: player.id,
      summary: name + ' joins the table.',
      player: player
    });
    return this.state.players[player.id];
  };

  GameSession.prototype.playerByExternalId = function (externalId) {
    var players = this.state.players;
    return Object.keys(players)
      .map(function (id) { return players[id]; })
      .filter(function (player) { return player.external_id === externalId; })[0] || null;
  };

  /** Roll up a character and put them in the party. */
  GameSession.prototype.addCharacter = function (name, options) {
    var settings = options || {};
    var character = DMAI.createCharacter(
      this.rules,
      name,
      settings.species || 'human',
      settings.character_class || 'fighter',
      settings.level || 1,
      Object.assign({ dice: this.dice }, settings)
    );
    this.journal.record(EventType.CHARACTER_CREATED, {
      actor_id: character.id,
      summary: character.name + ', ' + character.species + ' '
        + character['class'] + ', joins the party.',
      character: character
    });
    return this.state.party[character.id];
  };

  /** Bring an already-built sheet in from the character vault. */
  GameSession.prototype.adoptCharacter = function (sheet, playerId) {
    var character = DMAI.importCharacter(sheet);
    character.id = DMAI.newId(
      (character.name.split(/\s+/)[0] || 'pc').toLowerCase()
    );
    character.player_id = playerId || null;
    this.journal.record(EventType.CHARACTER_CREATED, {
      actor_id: character.id,
      summary: character.name + ', ' + character.species + ' '
        + character['class'] + ', joins the party.',
      character: character
    });
    return this.state.party[character.id];
  };

  /** Write an amended sheet back to the log -- the one way to edit a character. */
  GameSession.prototype.updateCharacter = function (character, summary) {
    var updated = DMAI.deepCopy(character);
    this.journal.record(EventType.CHARACTER_UPDATED, {
      actor_id: updated.id,
      summary: summary || (updated.name + "'s sheet is updated."),
      character: updated
    });
    return this.state.party[updated.id];
  };

  /** Advance a character one level, hit points and all. */
  GameSession.prototype.awardLevel = function (characterId, classKey) {
    var character = this.state.party[characterId];
    if (!character) {
      throw new DMAI.CharacterError('no character ' + characterId + ' in the party');
    }
    var payload = DMAI.levelUp(this.rules, character, classKey);
    this.journal.record(EventType.LEVEL_UP, Object.assign({
      actor_id: characterId,
      creature_id: characterId,
      summary: character.name + ' reaches level ' + payload.level + '.'
    }, payload));
    return this.state.party[characterId];
  };

  /** Split experience across the living party, as the table expects. */
  GameSession.prototype.awardExperience = function (amount, reason) {
    var party = this.state.party;
    var living = Object.keys(party)
      .map(function (id) { return party[id]; })
      .filter(function (character) { return !character.dead; });
    if (!living.length || amount <= 0) { return; }

    var share = Math.floor(amount / living.length);
    var self = this;
    living.forEach(function (character) {
      var updated = DMAI.deepCopy(character);
      updated.experience += share;
      self.journal.record(EventType.CHARACTER_UPDATED, {
        actor_id: character.id,
        summary: character.name + ' gains ' + share + ' XP'
          + (reason ? ' (' + reason + ')' : '') + '.',
        character: updated,
        experience_gained: share
      });
    });
  };

  GameSession.prototype.partyList = function () {
    var party = this.state.party;
    return Object.keys(party).map(function (id) { return party[id]; });
  };

  GameSession.prototype.bestiaryList = function () {
    var bestiary = this.state.bestiary;
    return Object.keys(bestiary).map(function (id) { return bestiary[id]; });
  };

  // --- the bestiary --------------------------------------------------------

  /** Bring monsters onto the board and log each one. */
  GameSession.prototype.spawn = function (monsterKey, count, rollHitPoints) {
    var quantity = count || 1;
    var dice = (rollHitPoints === undefined || rollHitPoints) ? this.dice : null;
    var creatures = quantity > 1
      ? DMAI.spawnGroup(this.rules, monsterKey, quantity, { dice: dice })
      : [DMAI.spawnMonster(this.rules, monsterKey, { dice: dice })];

    var self = this;
    creatures.forEach(function (creature) {
      self.journal.record(EventType.CREATURE_SPAWNED, {
        target_id: creature.id,
        summary: creature.name + ' appears.',
        creature: creature,
        visibility: Visibility.DM_ONLY
      });
    });
    return creatures.map(function (creature) {
      return self.state.bestiary[creature.id];
    });
  };

  /** Build a fight for this party, spawn it, and roll initiative. */
  GameSession.prototype.startEncounter = function (difficulty, options) {
    var settings = options || {};
    var party = this.partyList().filter(function (character) {
      return !character.dead;
    });
    if (!party.length) {
      throw new DMAI.EncounterError('the party has nobody left to fight');
    }

    var built = this.encounters.combatEncounter(party, difficulty, settings);
    var encounter = built.encounter;
    var self = this;
    var spawned = [];

    Object.keys(encounter.roster).forEach(function (key) {
      spawned = spawned.concat(self.spawn(key, encounter.roster[key]));
    });
    encounter.creature_ids = spawned.map(function (creature) { return creature.id; });

    this.journal.record(EventType.ENCOUNTER_STARTED, {
      target_id: encounter.id,
      summary: encounter.description,
      encounter: encounter
    });

    var participants = party.map(function (character) { return character.id; })
      .concat(encounter.creature_ids);
    this.combat.start(this.journal, participants, {
      encounter_id: encounter.id,
      surprised_ids: settings.surprised_ids
    });
    return { encounter: encounter, budget: built.budget, creatures: spawned };
  };

  /** Close out a fight: award the experience, resolve the encounter. */
  GameSession.prototype.finishEncounter = function (encounterId, options) {
    var settings = options || {};
    var encounter = this.state.encounters[encounterId];
    if (!encounter) { return null; }
    this.journal.record(EventType.ENCOUNTER_ENDED, {
      target_id: encounterId,
      summary: settings.summary || (encounter.name + ' is over.'),
      encounter_id: encounterId,
      abandoned: Boolean(settings.abandoned)
    });
    if (!settings.abandoned && encounter.xp_value) {
      this.awardExperience(encounter.xp_value, encounter.name);
    }
    return this.state.encounters[encounterId];
  };

  // --- narration and talk --------------------------------------------------

  GameSession.prototype.narrate = function (text, options) {
    var settings = options || {};
    return this.journal.record(EventType.DM_NARRATION, {
      summary: text,
      text: text,
      visibility: settings.visibility || Visibility.PUBLIC,
      audience_id: settings.audience_id || null
    });
  };

  GameSession.prototype.npcSays = function (npcId, text) {
    var npc = this.state.world.npcs[npcId];
    var name = npc ? npc.name : npcId;
    return this.journal.record(EventType.NPC_DIALOGUE, {
      actor_id: npcId,
      summary: name + ': ' + text,
      text: text,
      speaker: name
    });
  };

  /** A note passed to one player -- private information. */
  GameSession.prototype.whisper = function (playerId, text) {
    return this.journal.record(EventType.PRIVATE_MESSAGE, {
      summary: text,
      text: text,
      visibility: Visibility.PRIVATE,
      audience_id: playerId
    });
  };

  /** Log raw player input before anything interprets it. */
  GameSession.prototype.recordAction = function (action) {
    var character = this.state.party[action.character_id];
    var name = character ? character.name : 'A player';
    return this.journal.record(EventType.PLAYER_ACTION, {
      actor_id: action.character_id,
      summary: name + ': ' + action.text,
      action_id: action.id,
      player_id: action.player_id,
      text: action.text
    });
  };

  /** Convenience wrapper: build the action, log it, hand it back. */
  GameSession.prototype.playerSays = function (text, options) {
    var settings = options || {};
    var action = {
      id: DMAI.newId('act'),
      campaign_id: this.campaign.id,
      player_id: settings.player_id || null,
      character_id: settings.character_id || null,
      text: text
    };
    this.recordAction(action);
    return action;
  };

  /** Roll where everyone can see it: never a hidden result. */
  GameSession.prototype.roll = function (expression, options) {
    var settings = options || {};
    var result = this.dice.roll(expression, {
      reason: settings.reason || 'roll',
      mode: settings.mode,
      dc: settings.dc,
      actor_id: settings.actor_id
    });
    this.journal.record(EventType.DICE_ROLLED, {
      actor_id: settings.actor_id || null,
      summary: DMAI.describeRoll(result),
      roll: result
    });
    return result;
  };

  /** File a fact the DM should still know three sessions from now. */
  GameSession.prototype.remember = function (content, options) {
    var settings = options || {};
    var memory = {
      id: DMAI.newId('mem'),
      tier: settings.tier || DMAI.MemoryTier.SESSION,
      kind: settings.kind || DMAI.MemoryKind.FACT,
      content: content,
      subjects: settings.subjects || [],
      importance: settings.importance === undefined ? 50 : settings.importance,
      visibility: settings.visibility || Visibility.PUBLIC,
      created_at: DMAI.utcnow(),
      source_event_id: settings.source_event_id || null
    };
    this.journal.record(EventType.MEMORY_ADDED, {
      summary: content, memory: memory, visibility: memory.visibility
    });
    return memory;
  };

  /** A human DM overrode the engine.  Logged, never hidden. */
  GameSession.prototype.dmOverride = function (description, data) {
    return this.journal.record(EventType.DM_OVERRIDE, Object.assign({
      summary: description, description: description
    }, data || {}));
  };

  // --- checkpoints and rollback --------------------------------------------

  GameSession.prototype.checkpoint = function (label, automatic) {
    var mark = DMAI.checkpointFor(this.campaign.id, this.store, label, automatic);
    // Point the mark at its own CHECKPOINT event rather than the event before
    // it, so rolling back to a checkpoint keeps the checkpoint.
    mark.event_seq = this.store.head() + 1;
    this.journal.record(EventType.CHECKPOINT, {
      target_id: mark.id,
      summary: label ? 'Checkpoint: ' + label : 'Checkpoint.',
      checkpoint: mark
    });
    return mark;
  };

  /** Every checkpoint in the log, oldest first. */
  GameSession.prototype.checkpoints = function () {
    return this.store.all()
      .filter(function (event) {
        return event.type === EventType.CHECKPOINT && event.data.checkpoint;
      })
      .map(function (event) { return event.data.checkpoint; });
  };

  /** Rewind the campaign to a point in the log and refold the state. */
  GameSession.prototype.rollback = function (seq) {
    this.state = this.journal.rollbackTo(seq);
    return this.state;
  };

  GameSession.prototype.rollbackToCheckpoint = function (checkpoint) {
    return this.rollback(checkpoint.event_seq);
  };

  // --- reading the table ---------------------------------------------------

  /** The log as one seat sees it.  The DM seat sees everything. */
  GameSession.prototype.logFor = function (playerId, options) {
    var settings = options || {};
    var visible = this.store.visibleTo(playerId, settings.is_dm);
    return settings.limit ? visible.slice(-settings.limit) : visible;
  };

  /** The narrative line of the log: what a client would print. */
  GameSession.prototype.transcript = function (playerId, options) {
    var settings = options || {};
    return this.logFor(playerId, {
      is_dm: settings.is_dm,
      limit: settings.limit === undefined ? 50 : settings.limit
    }).filter(function (event) { return event.summary; })
      .map(function (event) { return event.summary; });
  };

  /**
   * A session summary, assembled from the log.
   *
   * Deliberately mechanical.  The narrative line here is a plain digest of
   * what the log says happened; the DM is expected to rewrite it in the
   * campaign's voice, and it must never invent what this does not contain.
   */
  GameSession.prototype.recap = function (since) {
    var from = since || 0;
    var events = this.store.all().filter(function (event) {
      return event.seq >= from;
    });
    var state = this.state;

    function summaries() {
      var types = Array.prototype.slice.call(arguments);
      return events.filter(function (event) {
        return types.indexOf(event.type) !== -1 && event.summary;
      }).map(function (event) { return event.summary; });
    }

    var combats = events.filter(function (event) {
      return event.type === EventType.COMBAT_ENDED;
    });

    var recap = {
      campaign: this.campaign.name,
      events_covered: events.length,
      discoveries: summaries(EventType.DISCOVERY),
      npc_interactions: summaries(EventType.NPC_MET, EventType.NPC_ATTITUDE_CHANGED),
      combats: combats.map(function (event) {
        return event.summary || 'A fight ended.';
      }),
      deaths: summaries(EventType.CREATURE_DIED),
      loot: summaries(EventType.ITEM_GAINED),
      progression: summaries(EventType.LEVEL_UP),
      quests: summaries(EventType.QUEST_ADDED, EventType.QUEST_UPDATED),
      world_changes: summaries(EventType.WORLD_EVENT, EventType.WEATHER_CHANGED),
      decisions: summaries(EventType.STORY_DECISION),
      unresolved: this.quests.pending(this.journal).map(function (consequence) {
        return consequence.effect;
      }),
      open_objectives: this.quests.openObjectives(this.journal).map(function (entry) {
        return entry.quest.title + ': ' + entry.objective.description;
      }),
      location: DMAI.currentLocation(state)
        ? DMAI.currentLocation(state).name : 'somewhere unmapped',
      time: DMAI.describeTime(state.world.time)
    };
    recap.narrative = digest(recap);
    return recap;
  };

  /**
   * Everything needed to pick a campaign back up.
   *
   * This is the payload the DM is handed on resume.  It answers the questions
   * from state rather than from a transcript, so a campaign resumes correctly
   * even if the chat history is long gone.
   */
  GameSession.prototype.briefing = function () {
    var state = this.state;
    var self = this;
    var location = DMAI.currentLocation(state);

    return {
      who: this.partyList().map(function (character) {
        var player = state.players[character.player_id];
        return {
          id: character.id,
          name: character.name,
          level: character.level,
          'class': character['class'],
          species: character.species,
          hp: character.hp.current + '/' + character.hp.maximum,
          conditions: character.conditions.map(function (condition) {
            return condition.type;
          }),
          player: player ? player.name : null
        };
      }),
      where: {
        location: location ? location.name : null,
        description: location ? location.description : '',
        npcs_present: this.atlas.npcsAt(
          this.journal, state.current_location_id || ''
        ).map(function (npc) { return npc.name; }),
        exits: this.atlas.neighbours(
          this.journal, state.current_location_id || ''
        ).map(function (place) { return place.name; })
      },
      when: {
        time: DMAI.describeTime(state.world.time),
        day: state.world.time.day,
        weather: state.world.weather
      },
      what_happened: this.transcript(null, { is_dm: true, limit: 25 }),
      what_is_happening: {
        in_combat: state.combat.active,
        round: state.combat.round,
        turn: DMAI.currentCombatant(state.combat)
          ? DMAI.currentCombatant(state.combat).creature_id : null,
        encounter: Object.keys(state.encounters)
          .map(function (id) { return state.encounters[id]; })
          .filter(function (encounter) {
            return !encounter.resolved && !encounter.abandoned;
          }).map(function (encounter) { return encounter.name; })[0] || null
      },
      players_know: state.story.discoveries,
      players_do_not_know: unknownSecrets(state),
      npcs_know: knownByNpcs(state),
      factions: Object.keys(state.world.factions).map(function (id) {
        var faction = state.world.factions[id];
        return {
          name: faction.name,
          party_standing: faction.party_standing,
          next_move: faction.agenda[0] || null
        };
      }),
      active_quests: this.quests.byStatus(this.journal, QuestStatus.ACTIVE)
        .map(function (quest) {
          return {
            title: quest.title,
            summary: quest.summary,
            objectives: quest.objectives.filter(function (objective) {
              return !objective.completed;
            }).map(function (objective) { return objective.description; })
          };
        }),
      pending_consequences: this.quests.pending(this.journal).map(function (consequence) {
        return {
          trigger: consequence.trigger,
          effect: consequence.effect,
          due_day: consequence.due_day
        };
      }),
      memories: state.memories.slice()
        .sort(function (left, right) { return right.importance - left.importance; })
        .slice(0, 20)
        .map(function (memory) { return memory.content; })
    };
  };

  function unknownSecrets(state) {
    var secrets = [];
    Object.keys(state.world.locations).forEach(function (id) {
      (state.world.locations[id].secrets || []).forEach(function (secret) {
        if (!secret.known_by.length) { secrets.push(secret.content); }
      });
    });
    Object.keys(state.world.npcs).forEach(function (id) {
      (state.world.npcs[id].secrets || []).forEach(function (secret) {
        if (!secret.known_by.length) { secrets.push(secret.content); }
      });
    });
    return secrets;
  }

  function knownByNpcs(state) {
    var known = {};
    Object.keys(state.world.npcs).forEach(function (id) {
      var npc = state.world.npcs[id];
      if ((npc.knowledge || []).length) { known[npc.name] = npc.knowledge; }
    });
    return known;
  }

  /**
   * Turn the recap's parts into a few plain sentences.
   * Kept dull on purpose: this is the factual floor the DM narrates over.
   */
  function digest(recap) {
    var lines = ['The party is at ' + recap.location + ' (' + recap.time + ').'];
    [
      ['Discovered', 'discoveries'],
      ['Fought', 'combats'],
      ['Gained', 'loot'],
      ['Quests', 'quests'],
      ['The world', 'world_changes']
    ].forEach(function (pair) {
      var items = recap[pair[1]];
      if (items.length) {
        lines.push(pair[0] + ': ' + items.slice(0, 5).join('; ')
          + (items.length > 5 ? '...' : ''));
      }
    });
    if (recap.open_objectives.length) {
      lines.push('Still open: ' + recap.open_objectives.slice(0, 5).join('; '));
    }
    if (recap.unresolved.length) {
      lines.push('Unresolved: ' + recap.unresolved.slice(0, 5).join('; '));
    }
    return lines.join('\n');
  }

  DMAI.GameSession = GameSession;
})(window.DMAI = window.DMAI || {});
