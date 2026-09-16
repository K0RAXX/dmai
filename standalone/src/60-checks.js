/* Ability checks, skill checks and contests.
 *
 * A port of dmai/engine/characters/checks.py.  The DM decides *that* a check
 * is needed and how hard it is; this module decides what the dice say.
 * Keeping the two apart is what stops a narrator from quietly deciding that
 * the rogue succeeded.
 */
(function (DMAI) {
  'use strict';

  var Ability = DMAI.Ability;
  var ConditionType = DMAI.ConditionType;
  var EventType = DMAI.EventType;
  var RollMode = DMAI.RollMode;

  var CheckError = DMAI.defineError('CheckError');

  //: Conditions that make a physical check harder.
  var PHYSICAL_DISADVANTAGE = [
    ConditionType.POISONED, ConditionType.FRIGHTENED,
    ConditionType.RESTRAINED, ConditionType.PRONE
  ];

  //: Conditions that make anything relying on sight harder.
  var SIGHT_DISADVANTAGE = [ConditionType.BLINDED];
  var SIGHT_SKILLS = ['perception', 'investigation', 'sleight_of_hand', 'stealth'];
  var PHYSICAL_SKILLS = ['athletics', 'acrobatics', 'stealth', 'sleight_of_hand'];

  /** Rolls the checks the DM asks for, against the active ruleset. */
  function CheckResolver(rules, dice) {
    this.rules = rules;
    this.dice = dice;
  }

  /** The bonus and a label, resolving a skill to its governing ability. */
  CheckResolver.prototype.modifierFor = function (creature, skill, ability) {
    if (skill) {
      if (!DMAI.SKILL_ABILITIES[skill]) {
        throw new CheckError('unknown skill: ' + skill);
      }
      return { bonus: this.rules.skillModifier(creature, skill), label: DMAI.readable(skill) };
    }
    if (ability) {
      return { bonus: this.rules.modifierOf(creature, ability), label: ability };
    }
    throw new CheckError('a check needs either a skill or an ability');
  };

  /** Disadvantage the conditions imply, so the DM need not remember. */
  CheckResolver.prototype.conditionMode = function (creature, skill, ability) {
    var types = (creature.conditions || []).map(function (condition) {
      return condition.type;
    });
    var has = function (list) {
      return list.some(function (type) { return types.indexOf(type) !== -1; });
    };

    if (has(SIGHT_DISADVANTAGE)
      && (SIGHT_SKILLS.indexOf(skill) !== -1 || ability === Ability.WIS)) {
      return RollMode.DISADVANTAGE;
    }
    var physical = ability === Ability.STR || ability === Ability.DEX
      || PHYSICAL_SKILLS.indexOf(skill) !== -1;
    if (has(PHYSICAL_DISADVANTAGE) && physical) {
      return RollMode.DISADVANTAGE;
    }
    if (types.indexOf(ConditionType.EXHAUSTION) !== -1) {
      return RollMode.DISADVANTAGE;
    }
    return RollMode.NORMAL;
  };

  /** Resolve one check and log it. */
  CheckResolver.prototype.check = function (journal, creatureId, options) {
    var settings = options || {};
    var creature = DMAI.findCreature(journal.state, creatureId);
    if (!creature) {
      throw new CheckError('no such creature: ' + creatureId);
    }

    var skill = settings.skill || null;
    var ability = settings.ability || null;
    var dc = settings.dc === undefined ? 15 : settings.dc;
    var resolved = this.modifierFor(creature, skill, ability);
    var mode = settings.mode || this.conditionMode(creature, skill, ability);

    var result = this.dice.roll(
      resolved.bonus ? '1d20' + DMAI.signed(resolved.bonus) : '1d20',
      {
        reason: settings.reason || (creature.name + ' ' + resolved.label + ' check'),
        mode: mode,
        dc: dc,
        actor_id: creatureId
      }
    );

    journal.record(EventType.CHECK_RESOLVED, {
      actor_id: creatureId,
      summary: DMAI.describeRoll(result),
      audience_id: settings.audience_id || null,
      visibility: settings.visibility || DMAI.Visibility.PUBLIC,
      skill: skill,
      ability: ability,
      dc: dc,
      roll: result
    });
    return result;
  };

  /** No roll: what this character notices without trying. */
  CheckResolver.prototype.passive = function (creature, skill) {
    return 10 + this.rules.skillModifier(creature, skill);
  };

  /** A saving throw, logged as a save rather than a bare roll. */
  CheckResolver.prototype.save = function (journal, creatureId, ability, options) {
    var settings = options || {};
    var creature = DMAI.findCreature(journal.state, creatureId);
    if (!creature) {
      throw new CheckError('no such creature: ' + creatureId);
    }
    var bonus = this.rules.saveModifier(creature, ability);
    var result = this.dice.roll(
      bonus ? '1d20' + DMAI.signed(bonus) : '1d20',
      {
        reason: settings.reason || (creature.name + ' ' + ability + ' save'),
        mode: settings.mode || RollMode.NORMAL,
        dc: settings.dc,
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

  /**
   * Two creatures roll against each other; ties favour the defender.
   *
   * Returns both rolls and the winner's id (null on a tie), so the narration
   * layer can describe a near-miss as a near-miss.
   */
  CheckResolver.prototype.contest = function (journal, firstId, secondId, options) {
    var settings = options || {};
    // The first roll sets the target number, so it has no DC of its own.
    var first = this.check(journal, firstId, {
      skill: settings.first_skill, dc: null, reason: settings.reason || 'contest'
    });
    var second = this.check(journal, secondId, {
      skill: settings.second_skill, dc: first.total, reason: settings.reason || 'contest'
    });
    return {
      first: first,
      second: second,
      winner_id: second.total >= first.total ? secondId : firstId
    };
  };

  /** Resolve a check request produced by the DM's interpretation pass. */
  CheckResolver.prototype.resolveRequest = function (journal, request) {
    if (request.kind === 'saving_throw') {
      if (!request.ability) {
        throw new CheckError('a saving throw needs an ability');
      }
      return this.save(journal, request.actor_id, request.ability, {
        dc: request.dc, reason: request.reason
      });
    }

    if (request.expression) {
      var result = this.dice.roll(request.expression, {
        reason: request.reason || 'roll',
        dc: request.dc,
        actor_id: request.actor_id,
        target_id: request.target_id
      });
      journal.record(EventType.DICE_ROLLED, {
        actor_id: request.actor_id,
        target_id: request.target_id,
        summary: DMAI.describeRoll(result),
        roll: result
      });
      return result;
    }

    return this.check(journal, request.actor_id, {
      skill: request.skill,
      ability: request.ability,
      dc: request.dc === undefined || request.dc === null ? 15 : request.dc,
      reason: request.reason
    });
  };

  DMAI.CheckError = CheckError;
  DMAI.CheckResolver = CheckResolver;
})(window.DMAI = window.DMAI || {});
