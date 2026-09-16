/* Encounter generation and budgeting.
 *
 * A port of dmai/engine/encounters/generator.py.  Combat encounters are built
 * against the party's experience-point budget, so "moderate" means moderate
 * for *this* party rather than for an imaginary one.  The non-combat kinds --
 * social, exploration, puzzles, traps, mysteries -- carry structure (what
 * succeeds, what fails, what it is worth) without prescribing prose.
 *
 * The generator is deterministic under a seeded DiceEngine, which is what lets
 * a whole session be replayed.
 */
(function (DMAI) {
  'use strict';

  var Difficulty = DMAI.Difficulty;

  var EncounterError = DMAI.defineError('EncounterError');

  var EncounterKind = {
    COMBAT: 'combat', SOCIAL: 'social', EXPLORATION: 'exploration',
    PUZZLE: 'puzzle', TRAP: 'trap', MYSTERY: 'mystery', TRAVEL: 'travel',
    ENVIRONMENTAL: 'environmental', RANDOM: 'random'
  };

  //: Multiplier applied to a group's raw experience value.  More bodies means
  //: a harder fight than the sum of its parts, because they all act every round.
  var GROUP_MULTIPLIERS = [[1, 1.0], [2, 1.5], [3, 2.0], [7, 2.5], [11, 3.0], [15, 4.0]];

  //: How far over or under budget a generated fight may land.
  var BUDGET_TOLERANCE = 0.35;

  //: Non-combat encounter skeletons, keyed by kind.  Each is a shape the DM
  //: fills in: the engine supplies the mechanics, never the fiction.
  var NON_COMBAT_TEMPLATES = {};
  NON_COMBAT_TEMPLATES[EncounterKind.SOCIAL] = {
    name: 'A conversation with something at stake',
    skills: ['persuasion', 'insight', 'deception', 'intimidation'],
    success: ['the other party concedes something they wanted to keep'],
    failure: ['the door closes, politely or otherwise']
  };
  NON_COMBAT_TEMPLATES[EncounterKind.EXPLORATION] = {
    name: 'Ground that does not want to be crossed',
    skills: ['survival', 'athletics', 'perception'],
    success: ['the party arrives with time and strength to spare'],
    failure: ['the party arrives late, loud, or short a resource']
  };
  NON_COMBAT_TEMPLATES[EncounterKind.PUZZLE] = {
    name: 'A lock that is not a lock',
    skills: ['investigation', 'arcana', 'history'],
    success: ['the way opens'],
    failure: ['the way stays shut; another route must be found']
  };
  NON_COMBAT_TEMPLATES[EncounterKind.TRAP] = {
    name: 'Something waiting to be triggered',
    skills: ['perception', 'investigation', 'sleight_of_hand'],
    success: ['spotted and disarmed before it mattered'],
    failure: ['it goes off, and the noise carries']
  };
  NON_COMBAT_TEMPLATES[EncounterKind.MYSTERY] = {
    name: 'A question with an inconvenient answer',
    skills: ['investigation', 'insight', 'history'],
    success: ['the party learns who benefits'],
    failure: ['the trail goes cold and someone else moves first']
  };
  NON_COMBAT_TEMPLATES[EncounterKind.TRAVEL] = {
    name: 'A day on the road',
    skills: ['survival', 'perception', 'animal_handling'],
    success: ['uneventful miles'],
    failure: ['a delay, and whatever the delay attracts']
  };
  NON_COMBAT_TEMPLATES[EncounterKind.ENVIRONMENTAL] = {
    name: 'The place itself is the problem',
    skills: ['athletics', 'acrobatics', 'survival'],
    success: ['the hazard is crossed'],
    failure: ['the hazard takes its toll']
  };

  function newEncounter(fields) {
    return Object.assign({
      id: DMAI.newId('enc'),
      kind: EncounterKind.COMBAT,
      name: '',
      description: '',
      difficulty: Difficulty.MODERATE,
      location_id: null,
      //: Creature ids participating (monsters are added to the campaign roster).
      creature_ids: [],
      //: What ends this encounter well, and what ends it badly.
      success_conditions: [],
      failure_conditions: [],
      rewards: [],
      //: The monster keys to spawn, so a client can build the fight itself.
      roster: {},
      resolved: false,
      //: The DM may abandon an encounter the players have made irrelevant.
      abandoned: false
    }, fields || {});
  }

  /** The encounter multiplier for a group of this size. */
  function groupMultiplier(count) {
    var multiplier = 1.0;
    GROUP_MULTIPLIERS.forEach(function (entry) {
      if (count >= entry[0]) { multiplier = entry[1]; }
    });
    return multiplier;
  }

  /** Sum the per-character budgets for the whole party. */
  function partyThreshold(rules, party, difficulty) {
    var label = String(difficulty || Difficulty.MODERATE);
    if (label === Difficulty.CUSTOM) { label = Difficulty.MODERATE; }
    return party.reduce(function (total, member) {
      return total + rules.xpThreshold(member.level, label);
    }, 0);
  }

  /** 1.0 is exactly on budget; 1.4 is a fight to remember. */
  function budgetLoad(budget) {
    return budget.threshold ? budget.adjusted / budget.threshold : 0;
  }

  function describeBudget(budget) {
    return budget.difficulty + ' for ' + budget.party_size + ': '
      + budget.adjusted + ' adjusted XP against a ' + budget.threshold
      + ' budget (' + budgetLoad(budget).toFixed(2) + 'x)';
  }

  /** Builds encounters that fit the party in front of it. */
  function EncounterGenerator(rules, dice) {
    this.rules = rules;
    this.dice = dice;
  }

  /** Every monster in the pack that has an experience value, cheapest first. */
  EncounterGenerator.prototype._catalogue = function () {
    var monsters = this.rules.monsters || {};
    var entries = Object.keys(monsters)
      .filter(function (key) { return monsters[key].xp; })
      .map(function (key) { return { key: key, block: monsters[key] }; });
    if (!entries.length) {
      throw new EncounterError(
        'rules pack ' + this.rules.id + ' has no usable bestiary'
      );
    }
    return entries.sort(function (left, right) {
      return left.block.xp - right.block.xp;
    });
  };

  /** Score a proposed monster line-up against the party's budget. */
  EncounterGenerator.prototype.budget = function (party, difficulty, composition) {
    if (!party.length) {
      throw new EncounterError('an encounter needs a party to be measured against');
    }
    var budget = {
      difficulty: difficulty || Difficulty.MODERATE,
      party_size: party.length,
      threshold: partyThreshold(this.rules, party, difficulty),
      //: Raw monster experience, before the group multiplier.
      spent: 0,
      //: Experience after the group multiplier -- what the budget is judged on.
      adjusted: 0,
      monsters: []
    };
    if (!composition) { return budget; }

    var count = 0;
    var self = this;
    Object.keys(composition).forEach(function (key) {
      var quantity = composition[key];
      var block = self.rules.monster(key);
      if (!block) {
        throw new EncounterError(
          'no creature "' + key + '" in rules pack ' + self.rules.id
        );
      }
      budget.spent += (parseInt(block.xp, 10) || 0) * quantity;
      for (var index = 0; index < quantity; index += 1) {
        budget.monsters.push(key);
      }
      count += quantity;
    });
    budget.adjusted = Math.floor(budget.spent * groupMultiplier(count));
    return budget;
  };

  /**
   * Choose a monster line-up that lands close to the budget.
   *
   * The approach is deliberately simple and predictable: pick a keystone
   * creature the budget can afford, then add cheaper company until the
   * adjusted total is close enough.  Predictable beats clever here -- a DM
   * needs to be able to look at the result and see why it was chosen.
   */
  EncounterGenerator.prototype.compose = function (party, difficulty, options) {
    var settings = options || {};
    var maxMonsters = settings.max_monsters || 8;
    var threshold = partyThreshold(this.rules, party, difficulty);
    var catalogue = this._catalogue();

    if (settings.theme && settings.theme.length) {
      var themed = catalogue.filter(function (entry) {
        return settings.theme.indexOf(entry.key) !== -1;
      });
      if (themed.length) { catalogue = themed; }
    }

    var affordable = catalogue.filter(function (entry) {
      return entry.block.xp <= threshold;
    });
    var pool = affordable.length ? affordable : catalogue.slice(0, 1);
    var keystone = pool[pool.length > 1 ? this.dice.rng.randrange(pool.length) : 0];

    var composition = {};
    composition[keystone.key] = 1;
    var count = 1;

    var cheap = catalogue.filter(function (entry) {
      return entry.block.xp <= Math.max(1, keystone.block.xp);
    });
    var filler = (cheap.length ? cheap : catalogue)[0];

    while (count < maxMonsters) {
      var trial = Object.assign({}, composition);
      trial[filler.key] = (trial[filler.key] || 0) + 1;
      var scored = this.budget(party, difficulty, trial);
      if (budgetLoad(scored) > 1 + BUDGET_TOLERANCE) { break; }
      composition = trial;
      count += 1;
      if (budgetLoad(scored) >= 1 - BUDGET_TOLERANCE) { break; }
    }

    return this.budget(party, difficulty, composition);
  };

  /** Build a combat encounter and the budget that justifies it. */
  EncounterGenerator.prototype.combatEncounter = function (party, difficulty, options) {
    var settings = options || {};
    var scored = settings.composition
      ? this.budget(party, difficulty, settings.composition)
      : this.compose(party, difficulty, { theme: settings.theme });

    var roster = {};
    scored.monsters.forEach(function (key) {
      roster[key] = (roster[key] || 0) + 1;
    });

    var self = this;
    var listing = Object.keys(roster).map(function (key) {
      return roster[key] + 'x ' + ((self.rules.monster(key) || {}).name || key);
    }).join(', ');

    var encounter = newEncounter({
      kind: EncounterKind.COMBAT,
      name: settings.name || listing,
      description: 'A fight: ' + listing + '.',
      difficulty: scored.difficulty,
      location_id: settings.location_id || null,
      success_conditions: ['the opposition is defeated, driven off, or talked down'],
      failure_conditions: ['the party is defeated, captured, or forced to withdraw'],
      rewards: [scored.spent + ' XP'],
      // The keys live on the encounter so a client can spawn the creatures
      // itself; creature_ids is filled in once they actually exist.
      roster: roster,
      xp_value: scored.spent
    });
    return { encounter: encounter, budget: scored };
  };

  /**
   * A social, exploration, puzzle, trap, mystery or travel encounter.
   *
   * The engine's contribution is the difficulty class and the shape of success
   * and failure; the fiction is the DM's job.
   */
  EncounterGenerator.prototype.challengeEncounter = function (kind, party, difficulty, options) {
    var settings = options || {};
    var template = NON_COMBAT_TEMPLATES[kind];
    if (!template) {
      throw new EncounterError(
        kind + ' encounters have no template; build it explicitly'
      );
    }

    var label = String(difficulty || Difficulty.MODERATE);
    var dc = this.difficultyClass(label);
    var known = Object.keys(Difficulty).map(function (key) { return Difficulty[key]; });

    return newEncounter({
      kind: kind,
      name: settings.name || template.name,
      description: settings.description || (template.name + ' (DC ' + dc + ').'),
      difficulty: known.indexOf(label) !== -1 ? label : Difficulty.CUSTOM,
      location_id: settings.location_id || null,
      success_conditions: template.success.slice(),
      failure_conditions: template.failure.slice(),
      skills: template.skills.slice(),
      dc: dc,
      rewards: []
    });
  };

  /** Map a difficulty label onto the pack's DC table. */
  EncounterGenerator.prototype.difficultyClass = function (difficulty) {
    if (this.rules.difficultyDc) { return this.rules.difficultyDc(difficulty); }
    return { easy: 10, moderate: 15, hard: 20, deadly: 25 }[difficulty] || 15;
  };

  /** Roll for what kind of thing happens next. */
  EncounterGenerator.prototype.randomKind = function (allowCombat) {
    var pool = Object.keys(NON_COMBAT_TEMPLATES);
    if (allowCombat === undefined || allowCombat) {
      pool = [EncounterKind.COMBAT].concat(pool);
    }
    return pool[this.dice.rng.randrange(pool.length)];
  };

  DMAI.EncounterError = EncounterError;
  DMAI.EncounterKind = EncounterKind;
  DMAI.EncounterGenerator = EncounterGenerator;
  DMAI.NON_COMBAT_TEMPLATES = NON_COMBAT_TEMPLATES;
  DMAI.GROUP_MULTIPLIERS = GROUP_MULTIPLIERS;
  DMAI.BUDGET_TOLERANCE = BUDGET_TOLERANCE;
  DMAI.newEncounter = newEncounter;
  DMAI.groupMultiplier = groupMultiplier;
  DMAI.partyThreshold = partyThreshold;
  DMAI.budgetLoad = budgetLoad;
  DMAI.describeBudget = describeBudget;
})(window.DMAI = window.DMAI || {});
