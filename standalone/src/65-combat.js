/* The combat subsystem, and the tactics that drive enemies.
 *
 * A port of dmai/engine/combat/engine.py and combat/tactics.py.  Everything
 * here writes through a Journal, so a fight leaves behind a complete,
 * replayable record: the initiative rolls, every attack roll against a stated
 * armour class, every point of damage, every death save.
 *
 * Three decisions worth knowing about:
 *
 * - Nothing is mutated directly.  Damage is computed on a throwaway copy of
 *   the target using the ruleset's own resistance logic, and the *resulting*
 *   hit points are written into the event.  Replay is therefore exact.
 * - Zero hit points is not death for a player character.  Players fall
 *   unconscious and start making death saves; monsters and NPCs die.  Massive
 *   damage kills either outright, as the SRD says.
 * - The turn order is the source of truth for whose turn it is, and it is
 *   advanced only by advanceTurn, which also expires conditions and forces
 *   death saves.  No other code moves the pointer.
 */
(function (DMAI) {
  'use strict';

  var ConditionType = DMAI.ConditionType;
  var CreatureKind = DMAI.CreatureKind;
  var EventType = DMAI.EventType;
  var Outcome = DMAI.Outcome;
  var RollMode = DMAI.RollMode;
  var Visibility = DMAI.Visibility;

  var CombatError = DMAI.defineError('CombatError');

  //: Conditions that take a creature out of the fight entirely.
  var INCAPACITATING = [
    ConditionType.UNCONSCIOUS, ConditionType.PARALYZED, ConditionType.PETRIFIED,
    ConditionType.STUNNED, ConditionType.INCAPACITATED
  ];

  //: Conditions that give attackers advantage against the sufferer.
  var GIVES_ATTACKER_ADVANTAGE = [
    ConditionType.BLINDED, ConditionType.PARALYZED, ConditionType.PETRIFIED,
    ConditionType.RESTRAINED, ConditionType.STUNNED, ConditionType.UNCONSCIOUS,
    ConditionType.PRONE
  ];

  var ATTACKER_DISADVANTAGE = [
    ConditionType.BLINDED, ConditionType.POISONED, ConditionType.FRIGHTENED
  ];

  var DEATH_SAVE_DC = 10;

  /** Runs fights.  Holds no state of its own beyond the rules and the dice. */
  function CombatEngine(rules, dice) {
    this.rules = rules;
    this.dice = dice;
  }

  // --- helpers -------------------------------------------------------------

  CombatEngine.prototype._require = function (journal, creatureId) {
    var creature = DMAI.findCreature(journal.state, creatureId);
    if (!creature) {
      throw new CombatError('no such creature in this campaign: ' + creatureId);
    }
    return creature;
  };

  /** Alive, conscious, and not held by an incapacitating condition. */
  function canAct(creature) {
    if (!DMAI.canAct(creature)) { return false; }
    return !(creature.conditions || []).some(function (condition) {
      return INCAPACITATING.indexOf(condition.type) !== -1;
    });
  }
  CombatEngine.canAct = canAct;

  function combatantIn(combat, creatureId) {
    return combat.order.filter(function (combatant) {
      return combatant.creature_id === creatureId;
    })[0] || null;
  }

  /** Everyone still in the fight and able to take a turn. */
  CombatEngine.prototype.activeCombatants = function (journal) {
    var state = journal.state;
    return state.combat.order.filter(function (combatant) {
      if (!DMAI.combatantActive(combatant)) { return false; }
      var creature = DMAI.findCreature(state, combatant.creature_id);
      return Boolean(creature) && canAct(creature);
    });
  };

  /** (players, opposition) among those still able to fight. */
  CombatEngine.prototype.sides = function (journal) {
    var live = this.activeCombatants(journal);
    return {
      players: live.filter(function (combatant) { return combatant.is_player; }),
      enemies: live.filter(function (combatant) { return !combatant.is_player; })
    };
  };

  /** A fight ends when one side can no longer act. */
  CombatEngine.prototype.isOver = function (journal) {
    var sides = this.sides(journal);
    return !sides.players.length || !sides.enemies.length;
  };

  // --- starting and ending -------------------------------------------------

  /**
   * Roll initiative and open the fight.
   *
   * Initiative is 1d20 + dexterity modifier, and ties break on the dexterity
   * modifier itself so the order is deterministic under a seed.
   */
  CombatEngine.prototype.start = function (journal, participantIds, options) {
    var settings = options || {};
    if (journal.state.combat.active) {
      throw new CombatError('combat is already running; end it before starting another');
    }
    if (participantIds.length < 2) {
      throw new CombatError('a fight needs at least two participants');
    }

    var self = this;
    var surprised = settings.surprised_ids || [];
    var rolls = participantIds.map(function (creatureId) {
      var creature = self._require(journal, creatureId);
      var dexterity = DMAI.creatureModifier(creature, DMAI.Ability.DEX);
      var result = self.dice.roll(
        dexterity ? '1d20' + DMAI.signed(dexterity) : '1d20',
        { reason: creature.name + ' initiative', actor_id: creatureId }
      );
      return {
        combatant: {
          creature_id: creatureId,
          initiative: result.total,
          //: Dexterity modifier, kept for deterministic tie-breaking.
          initiative_tiebreak: dexterity,
          is_player: creature.kind === CreatureKind.PLAYER,
          has_acted: false,
          movement_remaining: creature.speed,
          action_used: false,
          bonus_action_used: false,
          reaction_used: false,
          fled: false,
          surrendered: false,
          //: Lost the first round to an ambush; their opening turn is skipped.
          surprised: surprised.indexOf(creatureId) !== -1
        },
        roll: result,
        name: creature.name
      };
    });

    var order = rolls.slice().sort(function (left, right) {
      return (right.combatant.initiative - left.combatant.initiative)
        || (right.combatant.initiative_tiebreak - left.combatant.initiative_tiebreak)
        || ((right.combatant.is_player ? 1 : 0) - (left.combatant.is_player ? 1 : 0));
    }).map(function (entry) { return entry.combatant; });

    var combat = DMAI.newCombatState({
      id: DMAI.newId('combat'),
      active: true,
      round: 1,
      turn_index: 0,
      order: order,
      encounter_id: settings.encounter_id || null
    });

    journal.record(EventType.COMBAT_STARTED, {
      summary: 'Combat begins.',
      combat: combat,
      encounter_id: settings.encounter_id || null,
      initiative: rolls.map(function (entry) {
        return {
          creature_id: entry.combatant.creature_id,
          name: entry.name,
          total: entry.combatant.initiative,
          roll: entry.roll
        };
      })
    });
    this._beginTurn(journal);
    return journal.state.combat;
  };

  /** Close the fight and, by default, sweep away the slain. */
  CombatEngine.prototype.end = function (journal, reason, clearBestiary) {
    var combat = DMAI.deepCopy(journal.state.combat);
    combat.active = false;
    journal.record(EventType.COMBAT_ENDED, {
      summary: reason || 'Combat ends.',
      reason: reason || '',
      combat: combat,
      clear_bestiary: clearBestiary === undefined ? true : clearBestiary,
      rounds: combat.round
    });
  };

  // --- turn order ----------------------------------------------------------

  /**
   * Open a combatant's turn: refresh their action economy and log it.
   *
   * `combat` is the turn-order state to open, with the pointer already moved;
   * the TURN_STARTED event carries it, so moving the pointer and starting the
   * turn are one atomic entry in the log rather than two.
   *
   * A surprised creature loses this turn instead of taking it, which is
   * recorded rather than silently skipped so the log explains the gap.
   */
  CombatEngine.prototype._beginTurn = function (journal, combatState) {
    var combat = DMAI.deepCopy(combatState || journal.state.combat);
    if (!combat.active || !combat.order.length) { return null; }

    var combatant = combat.order[combat.turn_index % combat.order.length];
    var creature = DMAI.findCreature(journal.state, combatant.creature_id);

    combatant.has_acted = false;
    combatant.action_used = false;
    combatant.bonus_action_used = false;
    combatant.reaction_used = false;
    combatant.movement_remaining = creature ? creature.speed : 30;

    var skipped = '';
    if (combatant.surprised) {
      combatant.surprised = false;
      skipped = 'surprised';
    } else if (creature && !canAct(creature)) {
      skipped = 'unable to act';
    }

    var who = creature ? creature.name : combatant.creature_id;
    journal.record(EventType.TURN_STARTED, {
      actor_id: combatant.creature_id,
      summary: skipped
        ? who + ': turn skipped (' + skipped + ').'
        : who + "'s turn.",
      combat: combat,
      round: combat.round,
      skipped: skipped
    });

    // A dying character makes a death save at the start of their turn.
    if (creature && DMAI.isDying(creature) && creature.kind === CreatureKind.PLAYER) {
      if (!creature.death_saves.stable) {
        this.deathSave(journal, combatant.creature_id);
      }
    }

    if (skipped) { return this.advanceTurn(journal); }
    return combatant;
  };

  CombatEngine.prototype.current = function (journal) {
    var combatant = DMAI.currentCombatant(journal.state.combat);
    if (!combatant) { return null; }
    return DMAI.findCreature(journal.state, combatant.creature_id);
  };

  /**
   * End this turn and open the next one, wrapping into a new round.
   * Returns the combatant now on turn, or null if the fight is over.
   */
  CombatEngine.prototype.advanceTurn = function (journal) {
    var combat = DMAI.deepCopy(journal.state.combat);
    if (!combat.active || !combat.order.length) { return null; }

    var ending = combat.order[combat.turn_index % combat.order.length];
    ending.has_acted = true;
    journal.record(EventType.TURN_ENDED, {
      actor_id: ending.creature_id,
      combat: combat
    });
    this._expireConditions(journal, ending.creature_id);

    if (this.isOver(journal)) {
      this.end(journal, 'One side can no longer fight.');
      return null;
    }

    combat = DMAI.deepCopy(journal.state.combat);
    var size = combat.order.length;
    var start = combat.turn_index;

    // Walk forward to the next combatant who can still take a turn.  Each time
    // the walk passes the top of the order, a round has elapsed -- which is
    // how a fight where half the order is unconscious still counts rounds.
    for (var step = 1; step <= size; step += 1) {
      var index = (start + step) % size;
      var candidate = combat.order[index];
      var creature = DMAI.findCreature(journal.state, candidate.creature_id);
      if (!DMAI.combatantActive(candidate) || !creature || creature.dead) { continue; }
      combat.turn_index = index;
      combat.round += Math.floor((start + step) / size);
      return this._beginTurn(journal, combat);
    }

    this.end(journal, 'No combatant can act.');
    return null;
  };

  /** Tick down timed conditions at the end of a creature's turn. */
  CombatEngine.prototype._expireConditions = function (journal, creatureId) {
    var creature = DMAI.findCreature(journal.state, creatureId);
    if (!creature) { return; }
    creature.conditions.slice().forEach(function (condition) {
      if (condition.duration_rounds === null || condition.duration_rounds === undefined) {
        return;
      }
      var remaining = condition.duration_rounds - 1;
      if (remaining <= 0) {
        journal.record(EventType.CONDITION_REMOVED, {
          target_id: creatureId,
          creature_id: creatureId,
          condition_type: condition.type,
          summary: creature.name + ' is no longer ' + condition.type + '.'
        });
      } else {
        var refreshed = DMAI.deepCopy(condition);
        refreshed.duration_rounds = remaining;
        journal.record(EventType.CONDITION_ADDED, {
          target_id: creatureId,
          creature_id: creatureId,
          condition: refreshed
        });
      }
    });
  };

  // --- attacks -------------------------------------------------------------

  /** Advantage from the target's condition; disadvantage from the attacker's. */
  CombatEngine.prototype.attackMode = function (attacker, target) {
    var advantage = (target.conditions || []).some(function (condition) {
      return GIVES_ATTACKER_ADVANTAGE.indexOf(condition.type) !== -1;
    });
    var disadvantage = (attacker.conditions || []).some(function (condition) {
      return ATTACKER_DISADVANTAGE.indexOf(condition.type) !== -1;
    }) || DMAI.hasCondition(attacker, ConditionType.PRONE);

    if (advantage && !disadvantage) { return RollMode.ADVANTAGE; }
    if (disadvantage && !advantage) { return RollMode.DISADVANTAGE; }
    return RollMode.NORMAL;
  };

  /** Resolve one attack: roll, compare to armour class, deal damage. */
  CombatEngine.prototype.attack = function (journal, attackerId, targetId, weaponKey, options) {
    var settings = options || {};
    var attacker = this._require(journal, attackerId);
    var target = this._require(journal, targetId);

    if (!canAct(attacker)) {
      throw new CombatError(attacker.name + ' cannot act');
    }
    if (target.dead) {
      throw new CombatError(target.name + ' is already dead');
    }

    if (weaponKey === undefined || weaponKey === null) {
      weaponKey = this.defaultWeapon(attacker);
    }

    var bonus = this.rules.attackBonus(attacker, weaponKey);
    var armorClass = this.rules.armorClass(target);
    var mode = settings.mode || this.attackMode(attacker, target);

    var attackRoll = this.dice.roll(
      bonus ? '1d20' + DMAI.signed(bonus) : '1d20',
      {
        reason: attacker.name + ' attacks ' + target.name,
        mode: mode,
        dc: armorClass,
        actor_id: attackerId,
        target_id: targetId
      }
    );

    var critical = attackRoll.outcome === Outcome.CRITICAL_SUCCESS;
    var autoMiss = attackRoll.outcome === Outcome.CRITICAL_FAILURE;
    var hit = critical || (!autoMiss && attackRoll.total >= armorClass);

    var outcome = {
      attacker_id: attackerId,
      target_id: targetId,
      attack_roll: attackRoll,
      hit: hit,
      critical: critical,
      damage_roll: null,
      damage_dealt: 0,
      damage_type: null,
      target_dropped: false,
      target_died: false,
      events: []
    };

    outcome.events.push(journal.record(EventType.ATTACK, {
      actor_id: attackerId,
      target_id: targetId,
      summary: attacker.name + ' attacks ' + target.name + ' with '
        + this.weaponName(attacker, weaponKey) + ': '
        + (critical ? 'critical hit' : (hit ? 'hit' : 'miss')) + '.',
      weapon: weaponKey,
      armor_class: armorClass,
      hit: hit,
      critical: critical,
      roll: attackRoll
    }));

    if (settings.spend_action === undefined || settings.spend_action) {
      this._spendAction(journal, attackerId);
    }

    if (!hit) { return outcome; }

    var damage = this.rules.damageExpression(attacker, weaponKey, critical);
    var damageRoll = this.dice.roll(damage.expression, {
      reason: this.weaponName(attacker, weaponKey) + ' damage',
      actor_id: attackerId,
      target_id: targetId
    });
    outcome.damage_roll = damageRoll;
    outcome.damage_type = damage.damage_type;

    var applied = this.dealDamage(
      journal, targetId, Math.max(0, damageRoll.total), damage.damage_type,
      { source_id: attackerId }
    );
    outcome.damage_dealt = applied.dealt;
    outcome.events = outcome.events.concat(applied.events);
    outcome.target_dropped = applied.dropped;
    outcome.target_died = applied.died;
    return outcome;
  };

  /**
   * What this creature swings when nobody said.
   * Stat-block attacks win (a wolf bites), then an equipped weapon, then fists.
   */
  CombatEngine.prototype.defaultWeapon = function (creature) {
    if ((creature.attacks || []).length) {
      return '@' + (creature.attacks[0].name || '');
    }
    var wielded = (creature.inventory || []).filter(function (item) {
      return item.equipped && item.kind === DMAI.ItemKind.WEAPON;
    })[0];
    if (wielded && wielded.properties && wielded.properties.key) {
      return String(wielded.properties.key);
    }
    return 'unarmed';
  };

  /** Every attack this creature could make, for a client's attack picker. */
  CombatEngine.prototype.attackOptions = function (creature) {
    var self = this;
    var options = (creature.attacks || []).map(function (attack) {
      return {
        key: '@' + attack.name,
        name: attack.name,
        bonus: parseInt(attack.bonus, 10) || 0,
        damage: attack.damage + ' ' + (attack.damage_type || '')
      };
    });
    (creature.inventory || []).forEach(function (item) {
      if (item.kind !== DMAI.ItemKind.WEAPON || !item.equipped) { return; }
      var key = (item.properties || {}).key;
      if (!key) { return; }
      var damage = self.rules.damageExpression(creature, key, false);
      options.push({
        key: key,
        name: item.name,
        bonus: self.rules.attackBonus(creature, key),
        damage: damage.expression + ' ' + damage.damage_type
      });
    });
    if (!options.length) {
      var fists = self.rules.damageExpression(creature, 'unarmed', false);
      options.push({
        key: 'unarmed',
        name: 'Unarmed Strike',
        bonus: self.rules.attackBonus(creature, 'unarmed'),
        damage: fists.expression + ' ' + fists.damage_type
      });
    }
    return options;
  };

  CombatEngine.prototype.weaponName = function (creature, weaponKey) {
    if (weaponKey && weaponKey.charAt(0) === '@') { return weaponKey.slice(1); }
    var weapon = weaponKey ? this.rules.weapon(weaponKey) : null;
    return weapon ? weapon.name : 'a bare fist';
  };

  CombatEngine.prototype._spendAction = function (journal, creatureId) {
    var combat = journal.state.combat;
    if (!combat.active) { return; }
    var updated = DMAI.deepCopy(combat);
    var combatant = combatantIn(updated, creatureId);
    if (!combatant) { return; }
    combatant.action_used = true;
    journal.record(EventType.COMBAT_UPDATED, {
      actor_id: creatureId, combat: updated, action_used: true
    });
  };

  // --- damage, healing, death ----------------------------------------------

  /**
   * Apply damage.  Returns {dealt, events, dropped, died}.
   *
   * The ruleset's resistance maths is run against a throwaway copy so the
   * event can carry the resulting hit points rather than a delta.
   */
  CombatEngine.prototype.dealDamage = function (journal, targetId, amount, damageType, options) {
    var settings = options || {};
    var target = this._require(journal, targetId);
    var probe = DMAI.deepCopy(target);
    var dealt = this.rules.applyDamage(probe, amount, damageType);

    // Read the hit points the blow landed against *before* the event is
    // recorded.  `target` is the live creature, and recording the event folds
    // it through the reducer -- so after this point target.hp.current is
    // already the post-damage value, and measuring overkill against it would
    // score every killing blow as massive damage.  (The Python engine reads
    // it afterwards, which is why a character taking exactly their maximum in
    // one hit dies outright there instead of falling unconscious.)
    var hpBefore = target.hp.current + target.hp.temporary;
    var maximumBefore = target.hp.maximum;
    var isPlayer = target.kind === CreatureKind.PLAYER;

    var events = [journal.record(EventType.DAMAGE_DEALT, {
      actor_id: settings.source_id || null,
      target_id: targetId,
      creature_id: targetId,
      summary: target.name + ' takes ' + dealt + ' ' + damageType + ' damage.',
      amount: dealt,
      requested: amount,
      damage_type: damageType,
      reason: settings.reason || '',
      hp_current: probe.hp.current,
      hp_temporary: probe.hp.temporary
    })];

    if (probe.hp.current > 0) {
      return { dealt: dealt, events: events, dropped: false, died: false };
    }

    // Overkill: damage past zero equal to the maximum is instant death.
    var overkill = dealt - hpBefore;
    var instant = overkill >= maximumBefore;

    if (instant || !isPlayer) {
      events.push(this._kill(journal, targetId, settings.source_id));
      return { dealt: dealt, events: events, dropped: true, died: true };
    }

    events.push(journal.record(EventType.CONDITION_ADDED, {
      target_id: targetId,
      creature_id: targetId,
      summary: target.name + ' falls unconscious and begins dying.',
      condition: {
        type: ConditionType.UNCONSCIOUS, source: 'zero hit points',
        duration_rounds: null, level: 1, notes: null
      }
    }));
    events.push(journal.record(EventType.DEATH_SAVE, {
      target_id: targetId,
      creature_id: targetId,
      successes: 0, failures: 0, stable: false,
      summary: target.name + ' is dying.'
    }));
    return { dealt: dealt, events: events, dropped: true, died: false };
  };

  CombatEngine.prototype._kill = function (journal, creatureId, sourceId) {
    var creature = this._require(journal, creatureId);
    var event = journal.record(EventType.CREATURE_DIED, {
      actor_id: sourceId || null,
      target_id: creatureId,
      creature_id: creatureId,
      summary: creature.name + ' dies.'
    });
    this._retireFromOrder(journal, creatureId);
    return event;
  };

  /** A dead combatant stops taking turns but stays in the log. */
  CombatEngine.prototype._retireFromOrder = function (journal, creatureId) {
    var combat = journal.state.combat;
    if (!combat.active) { return; }
    var updated = DMAI.deepCopy(combat);
    var combatant = combatantIn(updated, creatureId);
    if (!combatant) { return; }
    combatant.has_acted = true;
    journal.record(EventType.COMBAT_UPDATED, {
      target_id: creatureId, combat: updated, retired: true
    });
  };

  /** Restore hit points.  A healed dying character wakes up. */
  CombatEngine.prototype.heal = function (journal, targetId, amount, options) {
    var settings = options || {};
    var target = this._require(journal, targetId);
    if (target.dead) {
      throw new CombatError(target.name + ' is dead; healing will not help');
    }

    var probe = DMAI.deepCopy(target);
    var restored = this.rules.heal(probe, amount);
    var wasDying = DMAI.isDying(target);

    journal.record(EventType.HEALING, {
      actor_id: settings.source_id || null,
      target_id: targetId,
      creature_id: targetId,
      summary: target.name + ' recovers ' + restored + ' hit points.',
      amount: restored,
      reason: settings.reason || 'healing',
      hp_current: probe.hp.current,
      hp_temporary: probe.hp.temporary,
      death_saves_reset: true
    });

    if (wasDying && probe.hp.current > 0) {
      journal.record(EventType.CONDITION_REMOVED, {
        target_id: targetId,
        creature_id: targetId,
        condition_type: ConditionType.UNCONSCIOUS,
        summary: target.name + ' regains consciousness.'
      });
    }
    return restored;
  };

  /**
   * One death saving throw.  Three of either ends the question.
   *
   * A natural 20 puts the character back on their feet with one hit point;
   * a natural 1 counts as two failures.
   */
  CombatEngine.prototype.deathSave = function (journal, creatureId) {
    var creature = this._require(journal, creatureId);
    if (!DMAI.isDying(creature)) {
      throw new CombatError(creature.name + ' is not dying');
    }

    var result = this.dice.roll('1d20', {
      reason: creature.name + ' death saving throw',
      dc: DEATH_SAVE_DC,
      actor_id: creatureId
    });
    var successes = creature.death_saves.successes;
    var failures = creature.death_saves.failures;

    if (result.outcome === Outcome.CRITICAL_SUCCESS) {
      journal.record(EventType.DEATH_SAVE, {
        target_id: creatureId, creature_id: creatureId,
        successes: 0, failures: 0, stable: false, roll: result,
        summary: creature.name + ' rallies on a natural 20.'
      });
      this.heal(journal, creatureId, 1, { reason: 'rallied on a natural 20' });
      return result;
    }

    if (result.outcome === Outcome.CRITICAL_FAILURE) {
      failures += 2;
    } else if (result.total >= DEATH_SAVE_DC) {
      successes += 1;
    } else {
      failures += 1;
    }

    var stable = successes >= 3;
    journal.record(EventType.DEATH_SAVE, {
      target_id: creatureId,
      creature_id: creatureId,
      successes: Math.min(successes, 3),
      failures: Math.min(failures, 3),
      stable: stable,
      roll: result,
      summary: stable
        ? creature.name + ' stabilises.'
        : creature.name + ': ' + successes + ' successes, ' + failures + ' failures.'
    });
    if (failures >= 3) { this._kill(journal, creatureId); }
    return result;
  };

  /** A saving throw, logged as a save rather than a bare roll. */
  CombatEngine.prototype.savingThrow = function (journal, creatureId, ability, dc, options) {
    var settings = options || {};
    var creature = this._require(journal, creatureId);
    var bonus = this.rules.saveModifier(creature, ability);
    var result = this.dice.roll(
      bonus ? '1d20' + DMAI.signed(bonus) : '1d20',
      {
        reason: settings.reason || (creature.name + ' ' + ability + ' save'),
        mode: settings.mode || RollMode.NORMAL,
        dc: dc,
        actor_id: creatureId
      }
    );
    journal.record(EventType.SAVE_RESOLVED, {
      actor_id: creatureId,
      summary: DMAI.describeRoll(result),
      ability: ability,
      roll: result
    });
    return result;
  };

  // --- morale and disengagement --------------------------------------------

  /**
   * Does this creature keep fighting?
   *
   * Enemies are not damage sponges: a bandit whose friends are dead and whose
   * own blood is on the floor would rather be elsewhere.  A high morale
   * resists that; a roll under the adjusted score holds the line.
   */
  CombatEngine.prototype.moraleCheck = function (journal, creatureId) {
    var creature = this._require(journal, creatureId);
    if (creature.kind === CreatureKind.PLAYER) { return true; }

    var morale = creature.morale;
    if (creature.hp.maximum) {
      var wounded = 1 - (creature.hp.current / creature.hp.maximum);
      morale -= Math.floor(wounded * 40);
    }

    var sides = this.sides(journal);
    if (sides.enemies.length === 1) { morale -= 15; }
    if (sides.players.length && sides.enemies.length
      && sides.players.length > sides.enemies.length * 2) {
      morale -= 15;
    }

    var target = Math.max(1, morale);
    var result = this.dice.roll('d%', {
      reason: creature.name + ' morale', dc: target, actor_id: creatureId
    });
    var holds = result.total <= target;

    journal.record(EventType.DICE_ROLLED, {
      actor_id: creatureId,
      // Players should feel a bandit waver, not read the die that decided it.
      visibility: Visibility.DM_ONLY,
      summary: creature.name + ' morale: ' + (holds ? 'holds' : 'breaks') + '.',
      roll: result,
      adjusted_morale: target,
      holds: holds
    });
    return holds;
  };

  CombatEngine.prototype.flee = function (journal, creatureId, reason) {
    var creature = this._require(journal, creatureId);
    var updated = DMAI.deepCopy(journal.state.combat);
    var combatant = combatantIn(updated, creatureId);
    if (!combatant) {
      throw new CombatError(creature.name + ' is not in this fight');
    }
    combatant.fled = true;
    journal.record(EventType.ENEMY_FLED, {
      actor_id: creatureId,
      summary: creature.name + ' flees' + (reason ? ': ' + reason : '.'),
      combat: updated,
      reason: reason || ''
    });
  };

  CombatEngine.prototype.surrender = function (journal, creatureId, terms) {
    var creature = this._require(journal, creatureId);
    var updated = DMAI.deepCopy(journal.state.combat);
    var combatant = combatantIn(updated, creatureId);
    if (!combatant) {
      throw new CombatError(creature.name + ' is not in this fight');
    }
    combatant.surrendered = true;
    journal.record(EventType.ENEMY_SURRENDERED, {
      actor_id: creatureId,
      summary: creature.name + ' surrenders' + (terms ? ': ' + terms : '.'),
      combat: updated,
      terms: terms || ''
    });
  };

  CombatEngine.prototype.addCondition = function (journal, creatureId, type, options) {
    var settings = options || {};
    var creature = this._require(journal, creatureId);
    journal.record(EventType.CONDITION_ADDED, {
      target_id: creatureId,
      creature_id: creatureId,
      summary: creature.name + ' is ' + type + '.',
      condition: {
        type: type,
        source: settings.source || null,
        duration_rounds: settings.duration_rounds === undefined
          ? null : settings.duration_rounds,
        level: settings.level || 1,
        notes: settings.notes || null
      }
    });
  };

  CombatEngine.prototype.removeCondition = function (journal, creatureId, type) {
    var creature = this._require(journal, creatureId);
    journal.record(EventType.CONDITION_REMOVED, {
      target_id: creatureId,
      creature_id: creatureId,
      condition_type: type,
      summary: creature.name + ' is no longer ' + type + '.'
    });
  };

  // --- tactics -------------------------------------------------------------
  //
  // The spec is explicit that enemies have goals beyond attacking until dead:
  // a bandit may flee, a guard may call for reinforcements, a wolf may protect
  // the pack, a captain may negotiate.  This is a decision the engine can
  // reach without a language model, so a fight is playable with no provider
  // configured at all.  It is the floor, not the ceiling.

  var Tactic = {
    ATTACK: 'attack',
    FLEE: 'flee',
    SURRENDER: 'surrender',
    NEGOTIATE: 'negotiate',
    PROTECT: 'protect',
    CALL_FOR_HELP: 'call_for_help',
    //: Nothing useful to do: no reachable target, no way out.
    WAIT: 'wait'
  };

  //: Below this fraction of maximum hit points a creature starts thinking
  //: about its own skin rather than the fight.
  var WOUNDED_FRACTION = 0.35;

  //: An intelligence score at or above this can bargain instead of bleeding.
  var NEGOTIATION_INTELLIGENCE = 8;

  function isWounded(creature) {
    if (!creature || !creature.hp.maximum) { return false; }
    return creature.hp.current / creature.hp.maximum <= WOUNDED_FRACTION;
  }

  function hasGoal(creature, words) {
    var text = (creature.goals || []).concat([creature.tactics || ''])
      .join(' ').toLowerCase();
    return words.some(function (word) { return text.indexOf(word) !== -1; });
  }

  /**
   * Pick whom to attack.
   *
   * Monsters are not tacticians with full information, but they can see who is
   * bleeding: the weakest reachable enemy is chosen, which produces the focus
   * fire that makes a fight feel dangerous without needing a model to say so.
   */
  function chooseTarget(engine, journal, actorId) {
    var actor = DMAI.findCreature(journal.state, actorId);
    if (!actor) { return null; }
    var wantPlayers = actor.kind !== CreatureKind.PLAYER;

    var candidates = [];
    engine.activeCombatants(journal).forEach(function (combatant) {
      if (combatant.creature_id === actorId) { return; }
      if (combatant.is_player !== wantPlayers) { return; }
      var creature = DMAI.findCreature(journal.state, combatant.creature_id);
      if (creature) { candidates.push(creature); }
    });

    if (!candidates.length) { return null; }
    candidates.sort(function (left, right) {
      return (left.hp.current - right.hp.current)
        || (left.armor_class - right.armor_class)
        || left.name.localeCompare(right.name);
    });
    return candidates[0].id;
  }

  /**
   * Decide what this creature does with its turn.
   *
   * The order of these tests is the interesting part.  Self-preservation is
   * checked before aggression, and a creature protecting something checks that
   * before either -- a mother bear does not flee her cubs.
   */
  function decide(engine, journal, actorId) {
    var actor = DMAI.findCreature(journal.state, actorId);
    if (!actor) {
      return { tactic: Tactic.WAIT, actor_id: actorId, target_id: null, reason: 'creature not found' };
    }

    var sides = engine.sides(journal);
    var allies = sides.enemies.filter(function (combatant) {
      return combatant.creature_id !== actorId;
    });
    var targetId = chooseTarget(engine, journal, actorId);

    if (!targetId) {
      return { tactic: Tactic.WAIT, actor_id: actorId, target_id: null, reason: 'nothing left to fight' };
    }

    // Something worth dying for overrides everything else.
    if (hasGoal(actor, ['protect', 'guard', 'defend']) && allies.length) {
      var weakest = allies.map(function (combatant) {
        return DMAI.findCreature(journal.state, combatant.creature_id);
      }).filter(Boolean).sort(function (left, right) {
        return left.hp.current - right.hp.current;
      })[0];
      if (weakest && isWounded(weakest)) {
        return {
          tactic: Tactic.PROTECT, actor_id: actorId, target_id: targetId,
          reason: 'stands over ' + weakest.name
        };
      }
    }

    if (isWounded(actor)) {
      var holds = engine.moraleCheck(journal, actorId);
      if (!holds) {
        var intelligent = actor.abilities.intelligence >= NEGOTIATION_INTELLIGENCE;
        var outnumbered = !allies.length && sides.players.length > 1;

        if (intelligent && hasGoal(actor, ['escape', 'flee', 'survive', 'never be captured'])) {
          return {
            tactic: Tactic.FLEE, actor_id: actorId, target_id: null,
            reason: 'badly hurt and looking for a way out'
          };
        }
        if (intelligent && outnumbered) {
          return {
            tactic: Tactic.SURRENDER, actor_id: actorId, target_id: null,
            reason: 'surrounded, wounded, and out of ideas'
          };
        }
        if (intelligent) {
          return {
            tactic: Tactic.NEGOTIATE, actor_id: actorId, target_id: targetId,
            reason: 'would rather talk than bleed'
          };
        }
        return {
          tactic: Tactic.FLEE, actor_id: actorId, target_id: null,
          reason: 'wounded and frightened'
        };
      }
    }

    if (hasGoal(actor, ['reinforce', 'call', 'alarm', 'obey'])
      && allies.length && isWounded(actor)) {
      return {
        tactic: Tactic.CALL_FOR_HELP, actor_id: actorId, target_id: targetId,
        reason: 'shouts for help'
      };
    }

    return {
      tactic: Tactic.ATTACK, actor_id: actorId, target_id: targetId,
      reason: 'presses the attack'
    };
  }

  /**
   * Decide and act.  Returns what was decided, for the narration layer.
   *
   * PROTECT, NEGOTIATE and CALL_FOR_HELP still attack -- an enemy shouting for
   * the guard is not standing idle -- but the decision is returned so the DM
   * can say what it looked like.
   */
  function takeTurn(engine, journal, actorId) {
    var decision = decide(engine, journal, actorId);
    var attacking = [
      Tactic.ATTACK, Tactic.PROTECT, Tactic.NEGOTIATE, Tactic.CALL_FOR_HELP
    ];

    if (decision.tactic === Tactic.FLEE) {
      engine.flee(journal, actorId, decision.reason);
    } else if (decision.tactic === Tactic.SURRENDER) {
      engine.surrender(journal, actorId, decision.reason);
    } else if (decision.target_id && attacking.indexOf(decision.tactic) !== -1) {
      decision.outcome = engine.attack(journal, actorId, decision.target_id);
    }
    return decision;
  }

  DMAI.CombatError = CombatError;
  DMAI.CombatEngine = CombatEngine;
  DMAI.DEATH_SAVE_DC = DEATH_SAVE_DC;
  DMAI.Tactic = Tactic;
  DMAI.WOUNDED_FRACTION = WOUNDED_FRACTION;
  DMAI.NEGOTIATION_INTELLIGENCE = NEGOTIATION_INTELLIGENCE;
  DMAI.chooseTarget = chooseTarget;
  DMAI.decideTactic = decide;
  DMAI.takeMonsterTurn = takeTurn;
})(window.DMAI = window.DMAI || {});
