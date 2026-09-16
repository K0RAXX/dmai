/* The map, the people on it, the powers behind them, and the clock.
 *
 * A port of dmai/engine/world/atlas.py and world/simulation.py.  Where the
 * combat engine owns *this fight*, the atlas owns *everything that is still
 * true when the fight is over*: places, the routes between them, the NPCs
 * standing in them, and the factions whose plans run underneath.
 *
 * Locations, NPCs and factions are each written to the log whole, because the
 * reducers replace the record they name.  Re-recording an amended copy is
 * therefore both the way to create one of these and the way to edit one, and
 * replay stays exact either way.
 */
(function (DMAI) {
  'use strict';

  var Attitude = DMAI.Attitude;
  var ConditionType = DMAI.ConditionType;
  var EventType = DMAI.EventType;
  var Visibility = DMAI.Visibility;

  var AtlasError = DMAI.defineError('AtlasError');

  //: Hostile to allied, in order.  shiftAttitude walks this ladder rather than
  //: jumping, so one rude remark cannot turn an ally into an enemy.
  var ATTITUDE_LADDER = [
    Attitude.HOSTILE, Attitude.UNFRIENDLY, Attitude.INDIFFERENT,
    Attitude.FRIENDLY, Attitude.ALLIED
  ];

  //: Faction standing runs -100 (open war) to +100 (sworn allies).
  var STANDING_RANGE = [-100, 100];

  function newLocation(fields) {
    return Object.assign({
      id: DMAI.newId('loc'),
      name: 'Somewhere',
      kind: 'place',  // settlement, dungeon, wilderness, building, room...
      description: '',
      parent_id: null,
      connections: [],
      npc_ids: [],
      //: Things a character can find here, gated behind checks.
      features: [],
      secrets: [],
      discovered: false,
      notes: ''
    }, fields || {});
  }

  function newFaction(fields) {
    return Object.assign({
      id: DMAI.newId('faction'),
      name: 'A faction',
      description: '',
      goals: [],
      //: Long-running plans advanced by the sandbox simulator.
      agenda: [],
      //: faction_id -> standing from -100 (war) to +100 (allied)
      relations: {},
      //: Party standing with this faction.
      party_standing: 0,
      power: 50,
      headquarters_id: null,
      member_ids: []
    }, fields || {});
  }

  function newSecret(content, visibility) {
    return {
      id: DMAI.newId('secret'),
      content: content,
      visibility: visibility || Visibility.DM_ONLY,
      //: Character/NPC ids that currently know this.
      known_by: []
    };
  }

  /** The write path for the world, plus the queries clients need. */
  function Atlas() {}

  // --- places --------------------------------------------------------------

  /** Put a place on the map, optionally wiring it to its neighbours. */
  Atlas.prototype.addLocation = function (journal, name, options) {
    var settings = options || {};
    var location = newLocation({
      name: name,
      kind: settings.kind || 'place',
      description: settings.description || '',
      parent_id: settings.parent_id || null,
      features: settings.features || [],
      secrets: settings.secrets || [],
      discovered: Boolean(settings.discovered),
      notes: settings.notes || ''
    });
    journal.record(EventType.LOCATION_ADDED, {
      target_id: location.id,
      summary: name + ' is added to the map.',
      location: location,
      visibility: settings.discovered ? Visibility.PUBLIC : Visibility.DM_ONLY
    });
    var self = this;
    (settings.connect_to || []).forEach(function (neighbourId) {
      self.connect(journal, location.id, neighbourId);
    });
    return journal.state.world.locations[location.id];
  };

  /** Open a route.  Two-way by default, because most doors are. */
  Atlas.prototype.connect = function (journal, fromId, toId, oneWay) {
    var origin = this._location(journal, fromId);
    var destination = this._location(journal, toId);
    this._link(journal, origin, toId);
    if (!oneWay) { this._link(journal, destination, fromId); }
  };

  Atlas.prototype._link = function (journal, location, neighbourId) {
    if (location.connections.indexOf(neighbourId) !== -1) { return; }
    var updated = DMAI.deepCopy(location);
    updated.connections = updated.connections.concat([neighbourId]);
    this._writeLocation(journal, updated, '');
  };

  /** Move the party.  Entering a place also discovers it. */
  Atlas.prototype.enter = function (journal, locationId, summary) {
    var location = this._location(journal, locationId);
    journal.record(EventType.LOCATION_ENTERED, {
      target_id: locationId,
      summary: summary || ('The party arrives at ' + location.name + '.'),
      location_id: locationId,
      location_name: location.name
    });
    return journal.state.world.locations[locationId];
  };

  /** Mark a place known without going there -- a map, a rumour, a name. */
  Atlas.prototype.discover = function (journal, locationId) {
    var location = this._location(journal, locationId);
    if (!location.discovered) {
      journal.record(EventType.LOCATION_DISCOVERED, {
        target_id: locationId,
        summary: 'The party learns of ' + location.name + '.',
        location_id: locationId
      });
    }
    return journal.state.world.locations[locationId];
  };

  /** Amend a place's description -- the DM improvised a detail worth keeping. */
  Atlas.prototype.describe = function (journal, locationId, description) {
    var location = DMAI.deepCopy(this._location(journal, locationId));
    location.description = description;
    this._writeLocation(journal, location, location.name + ' is described.');
    return journal.state.world.locations[locationId];
  };

  Atlas.prototype.addFeature = function (journal, locationId, feature) {
    var location = DMAI.deepCopy(this._location(journal, locationId));
    if (location.features.indexOf(feature) === -1) {
      location.features = location.features.concat([feature]);
      this._writeLocation(journal, location, location.name + ': ' + feature);
    }
    return journal.state.world.locations[locationId];
  };

  /** Where you can get to from here. */
  Atlas.prototype.neighbours = function (journal, locationId) {
    var location = journal.state.world.locations[locationId];
    if (!location) { return []; }
    return location.connections
      .map(function (id) { return journal.state.world.locations[id]; })
      .filter(Boolean);
  };

  Atlas.prototype.npcsAt = function (journal, locationId) {
    var npcs = journal.state.world.npcs;
    return Object.keys(npcs)
      .map(function (id) { return npcs[id]; })
      .filter(function (npc) { return npc.location_id === locationId; });
  };

  // --- people --------------------------------------------------------------

  /** Place a person in the world.  Accepts a built NPC or just a name. */
  Atlas.prototype.addNpc = function (journal, npc, options) {
    var settings = options || {};
    if (typeof npc === 'string') {
      npc = Object.assign(DMAI.newCreature({
        id: DMAI.newId('npc'),
        name: npc,
        kind: DMAI.CreatureKind.NPC
      }), {
        role: settings.role || '',
        personality: DMAI.newPersonality(),
        knowledge: [], secrets: [], attitudes: {}, relationships: {},
        emotional_state: 'calm', dialogue_style: '',
        location_id: null, faction_id: null
      });
    } else {
      npc = DMAI.deepCopy(npc);
      if (settings.role) { npc.role = settings.role; }
    }

    if (settings.location_id) {
      this._location(journal, settings.location_id);  // fail early on a bad room
      npc.location_id = settings.location_id;
    }
    if (settings.faction_id) { npc.faction_id = settings.faction_id; }

    journal.record(EventType.NPC_ADDED, {
      target_id: npc.id,
      summary: npc.name + ' enters the campaign.',
      npc: npc,
      visibility: Visibility.DM_ONLY
    });
    if (settings.faction_id) { this._enrol(journal, settings.faction_id, npc.id); }
    return journal.state.world.npcs[npc.id];
  };

  /** People do not stay where the DM left them. */
  Atlas.prototype.moveNpc = function (journal, npcId, locationId) {
    var npc = this._npc(journal, npcId);
    if (locationId) { this._location(journal, locationId); }
    var previous = npc.location_id;

    if (previous && journal.state.world.locations[previous]) {
      var room = DMAI.deepCopy(journal.state.world.locations[previous]);
      room.npc_ids = room.npc_ids.filter(function (id) { return id !== npcId; });
      this._writeLocation(journal, room, npc.name + ' leaves ' + room.name + '.');
    }

    var moved = DMAI.deepCopy(npc);
    moved.location_id = locationId;
    this._writeNpc(journal, moved, npc.name + ' moves.');
    return journal.state.world.npcs[npcId];
  };

  /** First contact.  Logs the meeting and reveals the NPC to the table. */
  Atlas.prototype.meet = function (journal, npcId, characterIds) {
    var npc = this._npc(journal, npcId);
    journal.record(EventType.NPC_MET, {
      target_id: npcId,
      summary: 'The party meets ' + npc.name + (npc.role ? ', ' + npc.role + '.' : '.'),
      npc_id: npcId,
      npc_name: npc.name,
      character_ids: characterIds || []
    });
    return npc;
  };

  Atlas.prototype.setAttitude = function (journal, npcId, characterId, attitude, reason) {
    var npc = this._npc(journal, npcId);
    journal.record(EventType.NPC_ATTITUDE_CHANGED, {
      actor_id: npcId,
      target_id: characterId,
      summary: npc.name + ' is now ' + attitude + ' toward the party'
        + (reason ? ' (' + reason + ')' : '') + '.',
      npc_id: npcId,
      character_id: characterId,
      attitude: attitude,
      reason: reason || ''
    });
    return attitude;
  };

  /**
   * Nudge an attitude one rung along the ladder.
   *
   * Attitudes move by degrees, never by leaps: one rude remark cannot turn a
   * sworn ally into an enemy, which is what keeps social play consequential
   * rather than volatile.
   */
  Atlas.prototype.shiftAttitude = function (journal, npcId, characterId, steps, reason) {
    var npc = this._npc(journal, npcId);
    var current = npc.attitudes[characterId] || Attitude.INDIFFERENT;
    var index = ATTITUDE_LADDER.indexOf(current);
    var moved = DMAI.clamp(index + steps, 0, ATTITUDE_LADDER.length - 1);
    if (moved === index) { return current; }
    return this.setAttitude(journal, npcId, characterId, ATTITUDE_LADDER[moved], reason);
  };

  Atlas.prototype.attitudeToward = function (npc, characterId) {
    return npc.attitudes[characterId] || Attitude.INDIFFERENT;
  };

  /** Give an NPC something to know -- and, later, something to let slip. */
  Atlas.prototype.teachNpc = function (journal, npcId, fact) {
    var npc = DMAI.deepCopy(this._npc(journal, npcId));
    if (npc.knowledge.indexOf(fact) === -1) {
      npc.knowledge = npc.knowledge.concat([fact]);
      this._writeNpc(journal, npc, '');
    }
    return journal.state.world.npcs[npcId];
  };

  // --- powers --------------------------------------------------------------

  Atlas.prototype.addFaction = function (journal, name, options) {
    var settings = options || {};
    var faction = newFaction({
      name: name,
      description: settings.description || '',
      goals: settings.goals || [],
      agenda: settings.agenda || [],
      party_standing: settings.party_standing || 0,
      power: settings.power === undefined ? 50 : settings.power,
      headquarters_id: settings.headquarters_id || null
    });
    journal.record(EventType.FACTION_ADDED, {
      target_id: faction.id,
      summary: name + ' is a power in this world.',
      faction: faction,
      visibility: Visibility.DM_ONLY
    });
    return journal.state.world.factions[faction.id];
  };

  /** Move the party's standing with a faction, bounded at both ends. */
  Atlas.prototype.shiftStanding = function (journal, factionId, delta, reason) {
    var faction = this._faction(journal, factionId);
    var updated = DMAI.deepCopy(faction);
    updated.party_standing = DMAI.clamp(
      faction.party_standing + delta, STANDING_RANGE[0], STANDING_RANGE[1]
    );
    journal.record(EventType.FACTION_ADDED, {
      target_id: factionId,
      summary: faction.name + ' regards the party differently'
        + (reason ? ' (' + reason + ')' : '') + '.',
      faction: updated,
      visibility: Visibility.DM_ONLY
    });
    return journal.state.world.factions[factionId];
  };

  Atlas.prototype._enrol = function (journal, factionId, npcId) {
    var faction = journal.state.world.factions[factionId];
    if (!faction || faction.member_ids.indexOf(npcId) !== -1) { return; }
    var updated = DMAI.deepCopy(faction);
    updated.member_ids = updated.member_ids.concat([npcId]);
    journal.record(EventType.FACTION_ADDED, {
      target_id: factionId, faction: updated, visibility: Visibility.DM_ONLY
    });
  };

  // --- internals -----------------------------------------------------------

  Atlas.prototype._location = function (journal, locationId) {
    var location = journal.state.world.locations[locationId];
    if (!location) {
      throw new AtlasError('no location ' + locationId + ' on this map');
    }
    return location;
  };

  Atlas.prototype._npc = function (journal, npcId) {
    var npc = journal.state.world.npcs[npcId];
    if (!npc) {
      throw new AtlasError('no NPC ' + npcId + ' in this campaign');
    }
    return npc;
  };

  Atlas.prototype._faction = function (journal, factionId) {
    var faction = journal.state.world.factions[factionId];
    if (!faction) {
      throw new AtlasError('no faction ' + factionId + ' in this campaign');
    }
    return faction;
  };

  Atlas.prototype._writeLocation = function (journal, location, summary) {
    journal.record(EventType.LOCATION_ADDED, {
      target_id: location.id,
      summary: summary,
      location: location,
      visibility: location.discovered ? Visibility.PUBLIC : Visibility.DM_ONLY
    });
  };

  Atlas.prototype._writeNpc = function (journal, npc, summary) {
    journal.record(EventType.NPC_ADDED, {
      target_id: npc.id, summary: summary, npc: npc, visibility: Visibility.DM_ONLY
    });
  };

  // --- the simulator -------------------------------------------------------
  //
  // Sandbox mode is the reason this exists.  Time passes, weather turns,
  // factions advance the plans they had before the party arrived, and the
  // debts the world owes for earlier player decisions come due.  None of that
  // requires a language model: the simulator decides *that* something happens
  // and records it, and the DM is free to narrate it well.

  var SHORT_REST_MINUTES = 60;
  var LONG_REST_MINUTES = 8 * 60;

  //: Rolled on 1d8 when the weather is left to chance.  Deliberately coarse:
  //: the weather is set dressing, and the DM can override it at any time.
  var WEATHER_TABLE = [
    'clear', 'clear', 'overcast', 'overcast',
    'light rain', 'fog', 'heavy rain', 'storm'
  ];

  //: A faction acts roughly once a day of in-world time.
  var FACTION_TICK_HOURS = 24;

  /** Advances the clock and everything that moves with it. */
  function WorldSimulator(rules, dice, tracker) {
    this.rules = rules;
    this.dice = dice;
    this.tracker = tracker || new DMAI.QuestTracker();
  }

  /**
   * Move the world clock.  The event carries the resulting time, not the
   * delta, so replaying it twice lands on the same hour.
   */
  WorldSimulator.prototype.advanceTime = function (journal, minutes, reason) {
    if (minutes <= 0) { return; }
    var clock = DMAI.advanceClock(DMAI.deepCopy(journal.state.world.time), minutes);
    journal.record(EventType.TIME_ADVANCED, {
      summary: reason || (describeDuration(minutes) + ' passes. It is '
        + DMAI.describeTime(clock) + '.'),
      day: clock.day,
      hour: clock.hour,
      minute: clock.minute,
      minutes_elapsed: minutes,
      reason: reason || ''
    });
  };

  WorldSimulator.prototype.setWeather = function (journal, weather, reason) {
    if (weather === journal.state.world.weather) { return weather; }
    journal.record(EventType.WEATHER_CHANGED, {
      summary: 'The weather turns ' + weather + '.',
      weather: weather,
      reason: reason || ''
    });
    return journal.state.world.weather;
  };

  /** Let the dice decide.  Seeded, so a replayed campaign gets the same sky. */
  WorldSimulator.prototype.rollWeather = function (journal) {
    var roll = this.dice.roll('1d8', { reason: 'weather' });
    return this.setWeather(journal, WEATHER_TABLE[roll.total - 1]);
  };

  /**
   * An hour's breather.  Each creature may spend hit dice to heal.
   *
   * hitDice maps creature id to dice spent; anyone left out simply rests.
   * Returns hit points restored, per creature.
   */
  WorldSimulator.prototype.shortRest = function (journal, creatureIds, hitDice) {
    this.advanceTime(journal, SHORT_REST_MINUTES, 'The party takes a short rest.');
    var spent = hitDice || {};
    var healed = {};
    var self = this;

    creatureIds.forEach(function (creatureId) {
      var creature = self._require(journal, creatureId);
      var count = Math.max(0, spent[creatureId] || 0);
      if (!count || creature.hp.current >= creature.hp.maximum) {
        healed[creatureId] = 0;
        return;
      }
      var die = self._hitDie(creature);
      var constitution = DMAI.creatureModifier(creature, DMAI.Ability.CON);
      var expression = count + 'd' + die
        + (constitution ? DMAI.signed(count * constitution) : '');
      var roll = self.dice.roll(expression, {
        reason: creature.name + ' spends ' + count + ' hit dice',
        actor_id: creatureId
      });
      healed[creatureId] = self._restore(
        journal, creature, Math.max(0, roll.total),
        creature.name + ' binds their wounds.', {}
      );
    });
    return healed;
  };

  /**
   * Eight hours.  Hit points full, resources back, one level of exhaustion
   * gone, death saves cleared -- but the dead stay dead.
   */
  WorldSimulator.prototype.longRest = function (journal, creatureIds) {
    this.advanceTime(journal, LONG_REST_MINUTES, 'The party takes a long rest.');
    var self = this;

    creatureIds.forEach(function (creatureId) {
      var creature = self._require(journal, creatureId);
      if (creature.dead) { return; }
      self._restore(
        journal, creature, creature.hp.maximum - creature.hp.current,
        creature.name + ' wakes rested.',
        { reset_death_saves: true, temporary: 0 }
      );
      Object.keys(creature.resources).forEach(function (name) {
        var maximum = creature.resources[name][1];
        journal.record(EventType.RESOURCE_CHANGED, {
          target_id: creatureId,
          creature_id: creatureId,
          summary: creature.name + ' recovers ' + name + '.',
          resource: name,
          current: maximum,
          maximum: maximum
        });
      });
      self._easeExhaustion(journal, creatureId);
    });
  };

  /** Something happened that the party did not see. */
  WorldSimulator.prototype.worldEvent = function (journal, description, visibility) {
    journal.record(EventType.WORLD_EVENT, {
      summary: description,
      description: description,
      visibility: visibility || Visibility.DM_ONLY
    });
  };

  /**
   * Every faction takes one step down its agenda.
   *
   * The step is *removed* from the agenda and recorded as a world event, so a
   * plan cannot silently run twice and the log shows the world's progress even
   * for schemes the party never noticed.
   */
  WorldSimulator.prototype.advanceFactions = function (journal) {
    var happened = [];
    var factions = journal.state.world.factions;
    var self = this;

    Object.keys(factions).forEach(function (id) {
      var faction = factions[id];
      if (!faction.agenda.length) { return; }
      var step = faction.agenda[0];
      var advanced = DMAI.deepCopy(faction);
      advanced.agenda = advanced.agenda.slice(1);
      journal.record(EventType.FACTION_ADDED, {
        target_id: faction.id,
        summary: faction.name + ' advances its plans.',
        faction: advanced,
        visibility: Visibility.DM_ONLY
      });
      self.worldEvent(journal, faction.name + ': ' + step);
      happened.push(faction.name + ': ' + step);
    });
    return happened;
  };

  /** Pay what the world owes on or before today. */
  WorldSimulator.prototype.fireDueConsequences = function (journal) {
    var today = journal.state.world.time.day;
    var fired = [];
    var self = this;

    this.tracker.pending(journal).forEach(function (consequence) {
      if (consequence.due_day === null || consequence.due_day > today) { return; }
      self.worldEvent(journal, consequence.effect, Visibility.PUBLIC);
      self.tracker.resolve(journal, consequence.id);
      fired.push(consequence.effect);
    });
    return fired;
  };

  /**
   * Advance time and let the world act on it.
   *
   * Factions move only in sandbox mode and only once a day, so a party that
   * spends a week in one town still finds the world changed when they leave.
   */
  WorldSimulator.prototype.tick = function (journal, minutes, reason) {
    var clock = journal.state.world.time;
    var before = clock.day * 24 + clock.hour;
    this.advanceTime(journal, minutes, reason);
    clock = journal.state.world.time;
    var after = clock.day * 24 + clock.hour;

    var happened = this.fireDueConsequences(journal);
    if (journal.state.campaign.settings.sandbox_mode) {
      var days = Math.floor((after - before) / FACTION_TICK_HOURS);
      for (var index = 0; index < days; index += 1) {
        happened = happened.concat(this.advanceFactions(journal));
      }
    }
    return happened;
  };

  // --- internals -----------------------------------------------------------

  /** A creature's hit die, defaulting to d8 for anything classless. */
  WorldSimulator.prototype._hitDie = function (creature) {
    try {
      var classData = this.rules.characterClass(DMAI.classKeyOf(this.rules, creature));
      return parseInt((classData || {}).hit_die, 10) || 8;
    } catch (error) {
      return 8;
    }
  };

  /** Write an absolute hit-point total, the way replay needs it. */
  WorldSimulator.prototype._restore = function (journal, creature, amount, summary, options) {
    var settings = options || {};
    var target = Math.min(
      creature.hp.maximum, creature.hp.current + Math.max(0, amount)
    );
    var restored = target - creature.hp.current;
    var wasDying = DMAI.isDying(creature);

    var payload = {
      target_id: creature.id,
      creature_id: creature.id,
      summary: summary,
      amount: restored,
      hp_current: target
    };
    if (settings.temporary !== undefined) { payload.hp_temporary = settings.temporary; }
    if (settings.reset_death_saves) { payload.death_saves_reset = true; }
    journal.record(EventType.HEALING, payload);

    if (wasDying && target > 0) {
      journal.record(EventType.CONDITION_REMOVED, {
        target_id: creature.id,
        creature_id: creature.id,
        condition_type: ConditionType.UNCONSCIOUS,
        summary: creature.name + ' regains consciousness.'
      });
    }
    return restored;
  };

  WorldSimulator.prototype._easeExhaustion = function (journal, creatureId) {
    var creature = DMAI.findCreature(journal.state, creatureId);
    if (!creature) { return; }
    var exhaustion = creature.conditions.filter(function (condition) {
      return condition.type === ConditionType.EXHAUSTION;
    })[0];
    if (!exhaustion) { return; }

    if (exhaustion.level <= 1) {
      journal.record(EventType.CONDITION_REMOVED, {
        target_id: creatureId,
        creature_id: creatureId,
        summary: creature.name + ' is no longer exhausted.',
        condition_type: ConditionType.EXHAUSTION
      });
      return;
    }
    journal.record(EventType.CONDITION_ADDED, {
      target_id: creatureId,
      creature_id: creatureId,
      summary: creature.name + "'s exhaustion eases.",
      condition: {
        type: ConditionType.EXHAUSTION,
        source: exhaustion.source,
        duration_rounds: null,
        level: exhaustion.level - 1,
        notes: null
      }
    });
  };

  WorldSimulator.prototype._require = function (journal, creatureId) {
    var creature = DMAI.findCreature(journal.state, creatureId);
    if (!creature) {
      throw new AtlasError('no creature ' + creatureId + ' in this campaign');
    }
    return creature;
  };

  function describeDuration(minutes) {
    if (minutes < 60) { return minutes + ' minutes'; }
    var hours = Math.floor(minutes / 60);
    var remainder = minutes % 60;
    if (remainder) { return hours + 'h' + ('0' + remainder).slice(-2); }
    return hours + ' hour' + (hours !== 1 ? 's' : '');
  }

  DMAI.AtlasError = AtlasError;
  DMAI.Atlas = Atlas;
  DMAI.WorldSimulator = WorldSimulator;
  DMAI.ATTITUDE_LADDER = ATTITUDE_LADDER;
  DMAI.STANDING_RANGE = STANDING_RANGE;
  DMAI.WEATHER_TABLE = WEATHER_TABLE;
  DMAI.SHORT_REST_MINUTES = SHORT_REST_MINUTES;
  DMAI.LONG_REST_MINUTES = LONG_REST_MINUTES;
  DMAI.FACTION_TICK_HOURS = FACTION_TICK_HOURS;
  DMAI.newLocation = newLocation;
  DMAI.newFaction = newFaction;
  DMAI.newSecret = newSecret;
  DMAI.describeDuration = describeDuration;
})(window.DMAI = window.DMAI || {});
