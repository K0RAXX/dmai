/* SRD 5.1 rules -- a port of dmai/engine/rules/srd51.py.
 *
 * The rules themselves live in the embedded pack (10-rules-pack.js, generated
 * from rules_packs/).  This class only knows how to interpret that data, which
 * is what lets a user drop in their own pack without touching code.
 */
(function (DMAI) {
  'use strict';

  var Ability = DMAI.Ability;
  var DIFFICULTY_INDEX = { easy: 0, moderate: 1, medium: 1, hard: 2, deadly: 3 };
  var BODY_ARMOR_TYPES = { light: true, medium: true, heavy: true };

  var RulesPackError = DMAI.defineError('RulesPackError');

  /** Everything the engine needs to ask of a ruleset, backed by pack data. */
  function RulesEngine(packId, packData) {
    var pack = packData || (DMAI.PACKS || {})[packId || 'srd51'];
    if (!pack) {
      throw new RulesPackError('rules pack not found: ' + packId);
    }
    this.pack = pack.pack || {};
    this.classes = pack.classes || {};
    this.speciesData = pack.species || {};
    this.weapons = pack.weapons || {};
    this.armors = pack.armor || {};
    this.monsters = pack.monsters || {};

    this.id = this.pack.id || packId || 'srd51';
    this.name = this.pack.name || 'Rules';
    this.attribution = this.pack.attribution || '';
  }

  /** Every pack the bundle carries, for a rules-pack picker. */
  RulesEngine.available = function () {
    return Object.keys(DMAI.PACKS || {});
  };

  function loadRules(packId, packData) {
    return new RulesEngine(packId || 'srd51', packData);
  }

  // --- core maths ----------------------------------------------------------

  RulesEngine.prototype.abilityModifier = function (score) {
    return Math.floor((score - 10) / 2);
  };

  RulesEngine.prototype.proficiencyBonus = function (level) {
    var table = this.pack.proficiency_by_level || [2];
    return table[DMAI.clamp(level, 1, table.length) - 1];
  };

  RulesEngine.prototype.modifierOf = function (creature, ability) {
    return this.abilityModifier(creature.abilities[ability]);
  };

  RulesEngine.prototype.skillModifier = function (creature, skill) {
    var ability = DMAI.SKILL_ABILITIES[skill];
    if (!ability) {
      throw new RulesPackError('unknown skill: ' + skill);
    }
    var multiplier = (creature.skill_proficiencies || {})[skill] || 0;
    return this.modifierOf(creature, ability)
      + multiplier * creature.proficiency_bonus;
  };

  RulesEngine.prototype.saveModifier = function (creature, ability) {
    var base = this.modifierOf(creature, ability);
    if ((creature.saving_throw_proficiencies || []).indexOf(ability) !== -1) {
      base += creature.proficiency_bonus;
    }
    return base;
  };

  /** Passive perception and friends: 10 + modifier. */
  RulesEngine.prototype.passiveScore = function (creature, skill) {
    return 10 + this.skillModifier(creature, skill);
  };

  /**
   * Recompute armour class from equipped gear.
   *
   * Monsters and NPCs carry a flat AC from their stat block and no equipment,
   * so with nothing equipped the stored value is returned untouched.  A player
   * character is different: they have no stat-block AC to fall back on, so
   * stripping their last piece of armour drops them to unarmoured defence
   * rather than leaving them mysteriously still wearing plate.
   *
   * (The Python engine returns the stored value here for every creature kind,
   * which leaves a character who takes their armour off still benefiting from
   * it.  This build does not.)
   */
  RulesEngine.prototype.armorClass = function (creature) {
    var equipped = (creature.inventory || []).filter(function (item) {
      return item.equipped;
    });
    if (!equipped.length) {
      return creature.kind === DMAI.CreatureKind.PLAYER
        ? 10 + this.modifierOf(creature, Ability.DEX)
        : creature.armor_class;
    }

    var body = null;
    var shieldBonus = 0;
    equipped.forEach(function (item) {
      var type = (item.properties || {}).type;
      if (BODY_ARMOR_TYPES[type] && !body) { body = item; }
      if (type === 'shield') {
        shieldBonus += parseInt((item.properties || {}).ac_bonus, 10) || 0;
      }
    });

    var dex = this.modifierOf(creature, Ability.DEX);
    if (!body) {
      return 10 + dex + shieldBonus;
    }

    var cap = body.properties.dex_cap;
    if (cap !== null && cap !== undefined) {
      dex = Math.min(dex, parseInt(cap, 10));
    }
    return (parseInt(body.properties.base_ac, 10) || 10) + dex + shieldBonus;
  };

  /** Fixed-average hit points: the full die at level 1, the average after. */
  RulesEngine.prototype.maxHitPoints = function (classKey, level, conModifier) {
    var klass = this.classes[classKey];
    var die = klass ? parseInt(klass.hit_die, 10) : 8;
    var average = Math.floor(die / 2) + 1;
    var total = die + conModifier + (level - 1) * (average + conModifier);
    return Math.max(1, total);
  };

  // --- combat --------------------------------------------------------------

  /** Which ability governs this weapon: ranged uses DEX, finesse the better. */
  RulesEngine.prototype._weaponAbility = function (creature, weapon) {
    if (!weapon) { return Ability.STR; }
    var properties = weapon.properties || [];
    if (properties.indexOf('ranged') !== -1) { return Ability.DEX; }
    if (properties.indexOf('finesse') !== -1) {
      return this.modifierOf(creature, Ability.DEX)
        > this.modifierOf(creature, Ability.STR) ? Ability.DEX : Ability.STR;
    }
    return Ability.STR;
  };

  /** Find a named attack on a monster stat block. */
  function statBlockAttack(creature, name) {
    var wanted = String(name).toLowerCase();
    var found = (creature.attacks || []).filter(function (attack) {
      return String(attack.name || '').toLowerCase() === wanted;
    });
    return found.length ? found[0] : null;
  }

  /**
   * Stat-block attacks carry an explicit bonus; characters compute one.
   * A key prefixed with an at-sign names an attack on the stat block: "@Bite".
   */
  RulesEngine.prototype.attackBonus = function (creature, weaponKey) {
    if (weaponKey && weaponKey.charAt(0) === '@') {
      var attack = statBlockAttack(creature, weaponKey.slice(1));
      if (attack) { return parseInt(attack.bonus, 10) || 0; }
    }
    var weapon = weaponKey ? this.weapons[weaponKey] : null;
    var ability = this._weaponAbility(creature, weapon);
    return this.modifierOf(creature, ability) + creature.proficiency_bonus;
  };

  /** The damage dice for an attack, already including the ability modifier. */
  RulesEngine.prototype.damageExpression = function (creature, weaponKey, critical) {
    if (weaponKey && weaponKey.charAt(0) === '@') {
      var attack = statBlockAttack(creature, weaponKey.slice(1));
      if (attack) {
        return {
          expression: critical ? criticalDice(attack.damage) : attack.damage,
          damage_type: attack.damage_type || DMAI.DamageType.BLUDGEONING
        };
      }
    }

    var weapon = (weaponKey ? this.weapons[weaponKey] : null) || this.weapons.unarmed;
    var modifier = this.modifierOf(creature, this._weaponAbility(creature, weapon));
    var expression = critical ? criticalDice(weapon.damage) : weapon.damage;
    if (modifier) {
      expression = expression + DMAI.signed(modifier);
    }
    return { expression: expression, damage_type: weapon.damage_type };
  };

  /** A critical hit doubles the dice, never the modifier. */
  function criticalDice(expression) {
    return String(expression).replace(/(\d*)d(\d+)/g, function (all, count, sides) {
      return (parseInt(count || '1', 10) * 2) + 'd' + sides;
    });
  }

  /**
   * Apply damage through temporary hit points and resistances.
   * Mutates the creature it is given -- always call it on a probe copy.
   * Returns the amount actually dealt, which is what the event log records.
   */
  RulesEngine.prototype.applyDamage = function (creature, amount, damageType) {
    if (amount <= 0) { return 0; }
    if ((creature.immunities || []).indexOf(damageType) !== -1) { return 0; }
    if ((creature.resistances || []).indexOf(damageType) !== -1) {
      amount = Math.floor(amount / 2);
    }
    if ((creature.vulnerabilities || []).indexOf(damageType) !== -1) {
      amount *= 2;
    }

    var absorbed = Math.min(creature.hp.temporary, amount);
    creature.hp.temporary -= absorbed;
    creature.hp.current = Math.max(0, creature.hp.current - (amount - absorbed));
    return amount;
  };

  /** Heal up to the maximum.  Returns the hit points actually restored. */
  RulesEngine.prototype.heal = function (creature, amount) {
    if (amount <= 0) { return 0; }
    var before = creature.hp.current;
    creature.hp.current = Math.min(creature.hp.maximum, creature.hp.current + amount);
    if (creature.hp.current > 0) {
      creature.death_saves = { successes: 0, failures: 0, stable: false };
    }
    return creature.hp.current - before;
  };

  // --- content lookup ------------------------------------------------------

  RulesEngine.prototype.weapon = function (key) { return this.weapons[key] || null; };
  RulesEngine.prototype.armor = function (key) { return this.armors[key] || null; };
  RulesEngine.prototype.monster = function (key) { return this.monsters[key] || null; };
  RulesEngine.prototype.species = function (key) { return this.speciesData[key] || null; };
  RulesEngine.prototype.characterClass = function (key) { return this.classes[key] || null; };

  RulesEngine.prototype.classKeys = function () { return Object.keys(this.classes); };
  RulesEngine.prototype.speciesKeys = function () { return Object.keys(this.speciesData); };
  RulesEngine.prototype.monsterKeys = function () { return Object.keys(this.monsters); };
  RulesEngine.prototype.weaponKeys = function () { return Object.keys(this.weapons); };
  RulesEngine.prototype.armorKeys = function () { return Object.keys(this.armors); };

  /** Every buyable thing in the pack, for a shop or a "give item" picker. */
  RulesEngine.prototype.catalogue = function () {
    var entries = [];
    var self = this;
    Object.keys(this.weapons).forEach(function (key) {
      var weapon = self.weapons[key];
      entries.push({
        key: key, name: weapon.name, kind: DMAI.ItemKind.WEAPON,
        cost_gp: weapon.cost_gp || 0, weight: weapon.weight || 0,
        detail: weapon.damage + ' ' + weapon.damage_type
      });
    });
    Object.keys(this.armors).forEach(function (key) {
      var armor = self.armors[key];
      var kind = armor.type === 'shield' ? DMAI.ItemKind.SHIELD : DMAI.ItemKind.ARMOR;
      entries.push({
        key: key, name: armor.name, kind: kind,
        cost_gp: armor.cost_gp || 0, weight: armor.weight || 0,
        detail: armor.type === 'shield'
          ? '+' + armor.ac_bonus + ' AC'
          : 'AC ' + armor.base_ac + (armor.dex_cap === null || armor.dex_cap === undefined
            ? ' + DEX'
            : (armor.dex_cap ? ' + DEX (max ' + armor.dex_cap + ')' : ''))
      });
    });
    return entries.sort(function (left, right) {
      return left.name.localeCompare(right.name);
    });
  };

  // --- encounter budgeting -------------------------------------------------

  /** Party XP budget for one character at this level and difficulty. */
  RulesEngine.prototype.xpThreshold = function (level, difficulty) {
    var bounded = DMAI.clamp(level, 1, 20);
    var index = DIFFICULTY_INDEX[String(difficulty).toLowerCase()];
    if (index === undefined) { index = 1; }
    return parseInt((this.pack.xp_thresholds || {})[String(bounded)][index], 10);
  };

  RulesEngine.prototype.difficultyDc = function (label) {
    var table = this.pack.difficulty_dc || {};
    return parseInt(table[label], 10) || 15;
  };

  DMAI.RulesEngine = RulesEngine;
  DMAI.RulesPackError = RulesPackError;
  DMAI.loadRules = loadRules;
  DMAI.criticalDice = criticalDice;
})(window.DMAI = window.DMAI || {});
