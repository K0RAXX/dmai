/* The event log and the reducer that turns it into game state.
 *
 * A port of dmai/engine/models/events.py and dmai/engine/state.py.  The rule
 * this module enforces is that game state is never edited directly by anything
 * above the engine.  Every change is an append to an event log, and the state
 * is the result of folding that log.  Two properties follow:
 *
 * - Replay.  rebuild(campaign, events) reproduces the state exactly, so a
 *   campaign can be reconstructed from its log alone.
 * - Rollback.  A checkpoint is just an event sequence number; rolling back is
 *   truncating the log and folding again.
 *
 * To make replay exact, mutating events carry *absolute* results rather than
 * deltas -- hp_current: 4, not damage: 3.  A delta applied twice is a bug; an
 * absolute value applied twice is the same value.
 */
(function (DMAI) {
  'use strict';

  var Visibility = DMAI.Visibility;
  var ConditionType = DMAI.ConditionType;

  var EventType = {
    // campaign lifecycle
    CAMPAIGN_CREATED: 'campaign_created',
    SESSION_STARTED: 'session_started',
    SESSION_ENDED: 'session_ended',
    CHECKPOINT: 'checkpoint',
    PLAYER_JOINED: 'player_joined',

    // characters
    CHARACTER_CREATED: 'character_created',
    CHARACTER_UPDATED: 'character_updated',
    LEVEL_UP: 'level_up',
    HP_CHANGED: 'hp_changed',
    CONDITION_ADDED: 'condition_added',
    CONDITION_REMOVED: 'condition_removed',
    DEATH_SAVE: 'death_save',
    CREATURE_DIED: 'creature_died',
    CREATURE_SPAWNED: 'creature_spawned',
    CREATURE_REMOVED: 'creature_removed',
    RESOURCE_CHANGED: 'resource_changed',

    // inventory
    ITEM_GAINED: 'item_gained',
    ITEM_LOST: 'item_lost',
    ITEM_EQUIPPED: 'item_equipped',
    CURRENCY_CHANGED: 'currency_changed',

    // dice and checks
    DICE_ROLLED: 'dice_rolled',
    CHECK_RESOLVED: 'check_resolved',
    SAVE_RESOLVED: 'save_resolved',

    // combat
    COMBAT_STARTED: 'combat_started',
    INITIATIVE_ROLLED: 'initiative_rolled',
    COMBAT_UPDATED: 'combat_updated',
    TURN_STARTED: 'turn_started',
    TURN_ENDED: 'turn_ended',
    ATTACK: 'attack',
    DAMAGE_DEALT: 'damage_dealt',
    HEALING: 'healing',
    MOVEMENT: 'movement',
    COMBAT_ENDED: 'combat_ended',
    ENEMY_FLED: 'enemy_fled',
    ENEMY_SURRENDERED: 'enemy_surrendered',

    // world and story
    LOCATION_ADDED: 'location_added',
    LOCATION_ENTERED: 'location_entered',
    LOCATION_DISCOVERED: 'location_discovered',
    NPC_ADDED: 'npc_added',
    FACTION_ADDED: 'faction_added',
    NPC_MET: 'npc_met',
    NPC_ATTITUDE_CHANGED: 'npc_attitude_changed',
    QUEST_ADDED: 'quest_added',
    QUEST_UPDATED: 'quest_updated',
    OBJECTIVE_COMPLETED: 'objective_completed',
    DISCOVERY: 'discovery',
    STORY_DECISION: 'story_decision',
    CONSEQUENCE_ADDED: 'consequence_added',
    CONSEQUENCE_RESOLVED: 'consequence_resolved',
    TIME_ADVANCED: 'time_advanced',
    WEATHER_CHANGED: 'weather_changed',
    WORLD_EVENT: 'world_event',
    ENCOUNTER_STARTED: 'encounter_started',
    ENCOUNTER_ENDED: 'encounter_ended',

    // narrative
    PLAYER_ACTION: 'player_action',
    DM_NARRATION: 'dm_narration',
    NPC_DIALOGUE: 'npc_dialogue',
    PRIVATE_MESSAGE: 'private_message',
    MEMORY_ADDED: 'memory_added',

    // human DM overrides
    DM_OVERRIDE: 'dm_override'
  };

  var EventStoreError = DMAI.defineError('EventStoreError');

  /** Build an unnumbered event.  The store assigns seq on append. */
  function makeEvent(type, fields) {
    var settings = fields || {};
    var data = {};
    Object.keys(settings).forEach(function (key) {
      if (['actor_id', 'target_id', 'summary', 'visibility', 'audience_id']
        .indexOf(key) === -1) {
        data[key] = settings[key];
      }
    });
    return {
      id: DMAI.newId('evt'),
      //: Monotonic position in the campaign log; assigned by the event store.
      seq: 0,
      type: type,
      timestamp: DMAI.utcnow(),
      actor_id: settings.actor_id || null,
      target_id: settings.target_id || null,
      //: Type-specific data.  Kept loose on purpose so new event types do not
      //: require a migration; readers must tolerate missing keys.
      data: data,
      //: Player-facing summary, if this event should appear in the log.
      summary: settings.summary || '',
      visibility: settings.visibility || Visibility.PUBLIC,
      //: When visibility is PRIVATE, which player may see it.
      audience_id: settings.audience_id || null
    };
  }

  function eventVisibleTo(event, playerId, isDm) {
    if (isDm) { return true; }
    if (event.visibility === Visibility.PUBLIC) { return true; }
    if (event.visibility === Visibility.DM_ONLY) { return false; }
    return Boolean(playerId) && event.audience_id === playerId;
  }

  // --- the store -----------------------------------------------------------

  /**
   * An append-only, monotonically numbered log for one campaign.
   *
   * Sequence numbers start at 1 and are assigned here, never by callers, so
   * two clients writing through the same engine cannot collide.
   */
  function EventStore(campaignId, events) {
    this.campaign_id = campaignId;
    this._events = [];
    var self = this;
    (events || []).forEach(function (event) { self._adopt(event); });
  }

  /** Take an event that already carries a sequence number (a load). */
  EventStore.prototype._adopt = function (event) {
    if (event.seq <= this.head()) {
      throw new EventStoreError(
        'event ' + event.id + ' has seq ' + event.seq
        + ', which is not after ' + this.head()
      );
    }
    this._events.push(event);
  };

  EventStore.prototype.head = function () {
    return this._events.length ? this._events[this._events.length - 1].seq : 0;
  };

  EventStore.prototype.length = function () { return this._events.length; };

  /** Number an event and add it to the log. */
  EventStore.prototype.append = function (event) {
    event.seq = this.head() + 1;
    this._events.push(event);
    return event;
  };

  EventStore.prototype.all = function () { return this._events.slice(); };

  /** Events after seq -- what a reconnecting client needs. */
  EventStore.prototype.since = function (seq) {
    return this._events.filter(function (event) { return event.seq > seq; });
  };

  EventStore.prototype.upto = function (seq) {
    return this._events.filter(function (event) { return event.seq <= seq; });
  };

  /**
   * The log as one seat at the table sees it.
   *
   * Private information is filtered here and nowhere else, so there is a
   * single place to test that player B never learns about player A's trap.
   */
  EventStore.prototype.visibleTo = function (playerId, isDm) {
    return this._events.filter(function (event) {
      return eventVisibleTo(event, playerId, isDm);
    });
  };

  /** Drop everything after seq.  Returns the discarded events. */
  EventStore.prototype.truncateTo = function (seq) {
    if (seq < 0) {
      throw new EventStoreError('cannot truncate to a negative seq: ' + seq);
    }
    var dropped = this._events.filter(function (event) { return event.seq > seq; });
    this._events = this._events.filter(function (event) { return event.seq <= seq; });
    return dropped;
  };

  // --- game state ----------------------------------------------------------

  function newGameTime(fields) {
    return Object.assign({ day: 1, hour: 8, minute: 0 }, fields || {});
  }

  function advanceClock(clock, minutes) {
    var total = clock.minute + minutes;
    clock.minute = ((total % 60) + 60) % 60;
    var hours = clock.hour + Math.floor(total / 60);
    clock.hour = ((hours % 24) + 24) % 24;
    clock.day += Math.floor(hours / 24);
    return clock;
  }

  function timeOfDay(clock) {
    if (clock.hour >= 5 && clock.hour < 12) { return 'morning'; }
    if (clock.hour >= 12 && clock.hour < 17) { return 'afternoon'; }
    if (clock.hour >= 17 && clock.hour < 21) { return 'evening'; }
    return 'night';
  }

  function describeTime(clock) {
    var hour = ('0' + clock.hour).slice(-2);
    var minute = ('0' + clock.minute).slice(-2);
    return 'Day ' + clock.day + ', ' + hour + ':' + minute
      + ' (' + timeOfDay(clock) + ')';
  }

  function newCampaign(fields) {
    return Object.assign({
      id: DMAI.newId('camp'),
      name: 'Untitled Campaign',
      premise: '',
      setting: '',
      settings: newCampaignSettings(),
      created_at: DMAI.utcnow(),
      updated_at: DMAI.utcnow(),
      //: Schema version, so old saves can be migrated rather than rejected.
      save_version: 1
    }, fields || {});
  }

  /** Table preferences.  Style never overrides game integrity. */
  function newCampaignSettings(fields) {
    return Object.assign({
      tone: 'heroic',
      style: 'descriptive',
      dm_behavior: 'neutral',
      difficulty: 'moderate',
      rules_pack: 'srd51',
      //: Emergent play: the world advances whether or not the party engages.
      sandbox_mode: false,
      //: Deterministic play for reproducible sessions.
      rng_seed: null,
      genre: 'high fantasy',
      themes: [],
      //: Which DM answers for this table.  An id, never a credential.
      ai_provider: 'offline',
      ai_model: ''
    }, fields || {});
  }

  /** The single source of truth for one campaign, folded from the log. */
  function newGameState(campaign) {
    return {
      campaign: campaign,
      players: {},
      //: Player characters, keyed by character id.
      party: {},
      //: Monsters and other transient combatants for the current scene.
      bestiary: {},

      world: {
        locations: {}, npcs: {}, factions: {},
        time: newGameTime(), weather: 'clear', world_events: []
      },
      story: {
        premise: '', quests: {}, decisions: [],
        consequences: [], discoveries: []
      },
      combat: newCombatState(),
      encounters: {},
      memories: [],

      current_location_id: null,
      //: Position in the event log this state reflects.
      last_event_seq: 0
    };
  }

  function newCombatState(fields) {
    return Object.assign({
      //: Empty until a fight actually starts.  A generated id on a combat that
      //: never happened would be the one part of state a replay could not
      //: reproduce, so there isn't one.
      id: '',
      active: false,
      round: 0,
      turn_index: 0,
      //: Initiative order, highest first.
      order: [],
      encounter_id: null
    }, fields || {});
  }

  function combatantActive(combatant) {
    return !(combatant.fled || combatant.surrendered);
  }

  function currentCombatant(combat) {
    var live = combat.order.filter(combatantActive);
    if (!live.length || !combat.active) { return null; }
    return combat.order[combat.turn_index % combat.order.length];
  }

  /** Look a combatant up wherever it lives: party, bestiary, or world. */
  function findCreature(state, creatureId) {
    if (!creatureId) { return null; }
    return state.party[creatureId]
      || state.bestiary[creatureId]
      || state.world.npcs[creatureId]
      || null;
  }

  function currentLocation(state) {
    if (!state.current_location_id) { return null; }
    return state.world.locations[state.current_location_id] || null;
  }

  // --- reducers ------------------------------------------------------------
  //
  // Each takes the state and one event and mutates the state in place.  All
  // are written to be safe to run twice: absolute values, idempotent inserts.

  var HANDLERS = {};

  function handles(types, handler) {
    types.forEach(function (type) { HANDLERS[type] = handler; });
  }

  function subject(state, event) {
    var creatureId = event.data.creature_id || event.target_id || event.actor_id;
    return findCreature(state, creatureId);
  }

  handles([EventType.PLAYER_JOINED], function (state, event) {
    var player = DMAI.deepCopy(event.data.player);
    state.players[player.id] = player;
  });

  handles([EventType.CHARACTER_CREATED, EventType.CHARACTER_UPDATED],
    function (state, event) {
      var character = DMAI.deepCopy(event.data.character);
      state.party[character.id] = character;
      var player = state.players[character.player_id];
      if (player && player.character_ids.indexOf(character.id) === -1) {
        player.character_ids = player.character_ids.concat([character.id]);
      }
    });

  handles([EventType.CREATURE_SPAWNED], function (state, event) {
    var creature = DMAI.deepCopy(event.data.creature);
    state.bestiary[creature.id] = creature;
  });

  handles([EventType.CREATURE_REMOVED], function (state, event) {
    delete state.bestiary[event.data.creature_id || event.target_id];
  });

  handles([EventType.LEVEL_UP], function (state, event) {
    var creature = subject(state, event);
    if (!creature) { return; }
    creature.level = parseInt(event.data.level, 10);
    creature.proficiency_bonus = parseInt(event.data.proficiency_bonus, 10);
    creature.hp.maximum = parseInt(event.data.hp_maximum, 10);
    creature.hp.current = parseInt(event.data.hp_current, 10);
    if (event.data.experience !== undefined) {
      creature.experience = parseInt(event.data.experience, 10);
    }
  });

  handles([EventType.HP_CHANGED, EventType.DAMAGE_DEALT, EventType.HEALING],
    function (state, event) {
      var creature = subject(state, event);
      if (!creature) { return; }
      if (event.data.hp_current !== undefined) {
        creature.hp.current = parseInt(event.data.hp_current, 10);
      }
      if (event.data.hp_temporary !== undefined) {
        creature.hp.temporary = parseInt(event.data.hp_temporary, 10);
      }
      if (event.data.hp_maximum !== undefined) {
        creature.hp.maximum = parseInt(event.data.hp_maximum, 10);
      }
      if (event.data.death_saves_reset) {
        creature.death_saves = { successes: 0, failures: 0, stable: false };
      }
    });

  handles([EventType.CONDITION_ADDED], function (state, event) {
    var creature = subject(state, event);
    if (!creature) { return; }
    var condition = DMAI.deepCopy(event.data.condition);
    var existing = creature.conditions.filter(function (candidate) {
      return candidate.type === condition.type;
    })[0];
    if (!existing) {
      creature.conditions = creature.conditions.concat([condition]);
    } else {
      // Exhaustion tracks a level; the rest simply refresh.
      existing.level = condition.level;
      existing.duration_rounds = condition.duration_rounds;
    }
  });

  handles([EventType.CONDITION_REMOVED], function (state, event) {
    var creature = subject(state, event);
    if (!creature) { return; }
    creature.conditions = creature.conditions.filter(function (condition) {
      return condition.type !== event.data.condition_type;
    });
  });

  handles([EventType.DEATH_SAVE], function (state, event) {
    var creature = subject(state, event);
    if (!creature) { return; }
    creature.death_saves = {
      successes: parseInt(event.data.successes, 10),
      failures: parseInt(event.data.failures, 10),
      stable: Boolean(event.data.stable)
    };
  });

  handles([EventType.CREATURE_DIED], function (state, event) {
    var creature = subject(state, event);
    if (!creature) { return; }
    creature.dead = true;
    creature.hp.current = 0;
    creature.hp.temporary = 0;
    if (!DMAI.hasCondition(creature, ConditionType.UNCONSCIOUS)) {
      creature.conditions = creature.conditions.concat([{
        type: ConditionType.UNCONSCIOUS, source: 'death',
        duration_rounds: null, level: 1, notes: null
      }]);
    }
  });

  handles([EventType.RESOURCE_CHANGED], function (state, event) {
    var creature = subject(state, event);
    if (!creature) { return; }
    creature.resources[event.data.resource] = [
      parseInt(event.data.current, 10), parseInt(event.data.maximum, 10)
    ];
  });

  handles([EventType.ITEM_GAINED], function (state, event) {
    var creature = subject(state, event);
    if (!creature) { return; }
    var item = DMAI.deepCopy(event.data.item);
    var stackable = ['consumable', 'misc', 'treasure'].indexOf(item.kind) !== -1;
    var stack = creature.inventory.filter(function (candidate) {
      return candidate.name === item.name && candidate.kind === item.kind;
    })[0];
    if (stack && stackable) {
      stack.quantity += item.quantity;
    } else {
      creature.inventory = creature.inventory.concat([item]);
    }
  });

  handles([EventType.ITEM_LOST], function (state, event) {
    var creature = subject(state, event);
    if (!creature) { return; }
    var quantity = parseInt(event.data.quantity, 10) || 0;
    var remaining = [];
    creature.inventory.forEach(function (item) {
      if (item.id !== event.data.item_id) {
        remaining.push(item);
        return;
      }
      if (quantity && item.quantity > quantity) {
        item.quantity -= quantity;
        remaining.push(item);
      }
    });
    creature.inventory = remaining;
  });

  handles([EventType.ITEM_EQUIPPED], function (state, event) {
    var creature = subject(state, event);
    if (!creature) { return; }
    var equipped = event.data.equipped === undefined ? true : Boolean(event.data.equipped);
    creature.inventory.forEach(function (item) {
      if (item.id === event.data.item_id) { item.equipped = equipped; }
    });
    if (event.data.armor_class !== undefined) {
      creature.armor_class = parseInt(event.data.armor_class, 10);
    }
  });

  handles([EventType.CURRENCY_CHANGED], function (state, event) {
    var creature = subject(state, event);
    if (!creature) { return; }
    creature.currency = DMAI.deepCopy(event.data.currency);
  });

  /**
   * Combat carries its whole state.
   *
   * A round of combat is small and changes in several places at once (turn
   * index, action economy, who has fled), so shipping the entire CombatState
   * is both simpler and safer to replay than a dozen field-level deltas.
   */
  handles([
    EventType.COMBAT_STARTED, EventType.INITIATIVE_ROLLED,
    EventType.COMBAT_UPDATED, EventType.TURN_STARTED, EventType.TURN_ENDED,
    EventType.MOVEMENT, EventType.ENEMY_FLED, EventType.ENEMY_SURRENDERED
  ], function (state, event) {
    if (event.data.combat) {
      state.combat = DMAI.deepCopy(event.data.combat);
    }
  });

  handles([EventType.COMBAT_ENDED], function (state, event) {
    if (event.data.combat) {
      state.combat = DMAI.deepCopy(event.data.combat);
    }
    state.combat.active = false;
    if (event.data.clear_bestiary) {
      var kept = {};
      Object.keys(state.bestiary).forEach(function (id) {
        if (!state.bestiary[id].dead) { kept[id] = state.bestiary[id]; }
      });
      state.bestiary = kept;
    }
  });

  handles([EventType.LOCATION_ADDED], function (state, event) {
    var location = DMAI.deepCopy(event.data.location);
    state.world.locations[location.id] = location;
  });

  handles([EventType.LOCATION_ENTERED], function (state, event) {
    var locationId = event.data.location_id || event.target_id;
    state.current_location_id = locationId;
    var location = state.world.locations[locationId];
    if (location) { location.discovered = true; }
  });

  handles([EventType.LOCATION_DISCOVERED], function (state, event) {
    var location = state.world.locations[event.data.location_id];
    if (location) { location.discovered = true; }
  });

  handles([EventType.NPC_ADDED], function (state, event) {
    var npc = DMAI.deepCopy(event.data.npc);
    state.world.npcs[npc.id] = npc;
    var location = state.world.locations[npc.location_id];
    if (location && location.npc_ids.indexOf(npc.id) === -1) {
      location.npc_ids = location.npc_ids.concat([npc.id]);
    }
  });

  handles([EventType.FACTION_ADDED], function (state, event) {
    var faction = DMAI.deepCopy(event.data.faction);
    state.world.factions[faction.id] = faction;
  });

  handles([EventType.NPC_ATTITUDE_CHANGED], function (state, event) {
    var npc = state.world.npcs[event.data.npc_id || event.actor_id];
    if (!npc) { return; }
    npc.attitudes[event.data.character_id] = event.data.attitude;
  });

  handles([EventType.QUEST_ADDED, EventType.QUEST_UPDATED], function (state, event) {
    var quest = DMAI.deepCopy(event.data.quest);
    state.story.quests[quest.id] = quest;
  });

  handles([EventType.OBJECTIVE_COMPLETED], function (state, event) {
    var quest = state.story.quests[event.data.quest_id];
    if (!quest) { return; }
    quest.objectives.forEach(function (objective) {
      if (objective.id === event.data.objective_id) { objective.completed = true; }
    });
    if (questIsComplete(quest) && quest.status === DMAI.QuestStatus.ACTIVE) {
      quest.status = DMAI.QuestStatus.COMPLETED;
    }
  });

  handles([EventType.DISCOVERY], function (state, event) {
    var fact = event.data.content || event.summary;
    if (fact && state.story.discoveries.indexOf(fact) === -1) {
      state.story.discoveries = state.story.discoveries.concat([fact]);
    }
  });

  handles([EventType.CONSEQUENCE_ADDED], function (state, event) {
    var consequence = DMAI.deepCopy(event.data.consequence);
    var known = state.story.consequences.some(function (candidate) {
      return candidate.id === consequence.id;
    });
    if (!known) {
      state.story.consequences = state.story.consequences.concat([consequence]);
    }
  });

  handles([EventType.CONSEQUENCE_RESOLVED], function (state, event) {
    state.story.consequences.forEach(function (consequence) {
      if (consequence.id === event.data.consequence_id) {
        consequence.resolved = true;
      }
    });
  });

  handles([EventType.TIME_ADVANCED], function (state, event) {
    state.world.time.day = parseInt(event.data.day, 10);
    state.world.time.hour = parseInt(event.data.hour, 10);
    state.world.time.minute = parseInt(event.data.minute, 10);
  });

  handles([EventType.WEATHER_CHANGED], function (state, event) {
    state.world.weather = event.data.weather;
  });

  handles([EventType.WORLD_EVENT], function (state, event) {
    var description = event.data.description || event.summary;
    if (description) {
      state.world.world_events = state.world.world_events.concat([description]);
    }
  });

  handles([EventType.ENCOUNTER_STARTED], function (state, event) {
    var encounter = DMAI.deepCopy(event.data.encounter);
    state.encounters[encounter.id] = encounter;
  });

  handles([EventType.ENCOUNTER_ENDED], function (state, event) {
    var encounter = state.encounters[event.data.encounter_id];
    if (!encounter) { return; }
    var abandoned = Boolean(event.data.abandoned);
    encounter.abandoned = abandoned;
    encounter.resolved = !abandoned;
  });

  handles([EventType.MEMORY_ADDED], function (state, event) {
    var memory = DMAI.deepCopy(event.data.memory);
    var known = state.memories.some(function (candidate) {
      return candidate.id === memory.id;
    });
    if (!known) { state.memories = state.memories.concat([memory]); }
  });

  handles([EventType.STORY_DECISION], function (state, event) {
    var decision = event.data.decision || event.summary;
    if (decision) {
      state.story.decisions = state.story.decisions.concat([decision]);
    }
  });

  function questIsComplete(quest) {
    var required = quest.objectives.filter(function (objective) {
      return !objective.optional;
    });
    return required.length > 0 && required.every(function (objective) {
      return objective.completed;
    });
  }

  // --- folding -------------------------------------------------------------

  /**
   * Fold one event into the state.  Unknown types are log-only, not errors.
   *
   * Tolerating unknown types is what lets a save written by a newer build open
   * in an older one without corrupting the parts it does understand.
   */
  function applyEvent(state, event) {
    var handler = HANDLERS[event.type];
    if (handler) { handler(state, event); }
    state.last_event_seq = Math.max(state.last_event_seq, event.seq);
    return state;
  }

  /** Reconstruct a campaign's state from its event log alone. */
  function rebuild(campaign, events) {
    var state = newGameState(campaign);
    (events.all ? events.all() : events).forEach(function (event) {
      applyEvent(state, event);
    });
    return state;
  }

  /**
   * The one write path: append to the log, then fold into the state.
   *
   * Every subsystem -- combat, inventory, the world simulator -- is handed a
   * Journal rather than a raw state, which is how the invariant at the top of
   * this module is enforced by construction instead of by convention.
   */
  function Journal(state, store) {
    this.state = state;
    this.store = store;
  }

  /** Log one fact and apply it.  Returns the numbered event. */
  Journal.prototype.record = function (type, fields) {
    var event = this.store.append(makeEvent(type, fields || {}));
    applyEvent(this.state, event);
    return event;
  };

  /** Truncate the log at seq and refold.  The state object is replaced. */
  Journal.prototype.rollbackTo = function (seq) {
    this.store.truncateTo(seq);
    this.state = rebuild(this.state.campaign, this.store.all());
    return this.state;
  };

  /** Name the current position in the log so it can be returned to. */
  function checkpointFor(campaignId, store, label, automatic) {
    return {
      id: DMAI.newId('ckpt'),
      campaign_id: campaignId,
      label: label || '',
      event_seq: store.head(),
      created_at: DMAI.utcnow(),
      automatic: Boolean(automatic)
    };
  }

  DMAI.EventType = EventType;
  DMAI.EventStore = EventStore;
  DMAI.EventStoreError = EventStoreError;
  DMAI.Journal = Journal;
  DMAI.makeEvent = makeEvent;
  DMAI.eventVisibleTo = eventVisibleTo;
  DMAI.applyEvent = applyEvent;
  DMAI.rebuild = rebuild;
  DMAI.checkpointFor = checkpointFor;
  DMAI.newCampaign = newCampaign;
  DMAI.newCampaignSettings = newCampaignSettings;
  DMAI.newGameState = newGameState;
  DMAI.newCombatState = newCombatState;
  DMAI.newGameTime = newGameTime;
  DMAI.advanceClock = advanceClock;
  DMAI.timeOfDay = timeOfDay;
  DMAI.describeTime = describeTime;
  DMAI.combatantActive = combatantActive;
  DMAI.currentCombatant = currentCombatant;
  DMAI.findCreature = findCreature;
  DMAI.currentLocation = currentLocation;
  DMAI.questIsComplete = questIsComplete;
})(window.DMAI = window.DMAI || {});
