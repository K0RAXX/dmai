/* Characters, creatures and the bestiary.
 *
 * A port of dmai/engine/models/character.py, characters/builder.py and
 * characters/monsters.py.  The builder turns a small, human-sized description
 * -- a name, a species, a class, a level -- into a fully derived sheet: hit
 * points, armour class, proficiency bonus, saving throws and starting gear,
 * all read from the active rules pack rather than hard-coded here.
 *
 * Everything is pure: the caller supplies the rules and, for rolled ability
 * scores, a seeded DiceEngine.  The same inputs always give the same sheet.
 */
(function (DMAI) {
  'use strict';

  var Ability = DMAI.Ability;
  var ItemKind = DMAI.ItemKind;
  var CreatureKind = DMAI.CreatureKind;

  var CharacterError = DMAI.defineError('CharacterError');
  var BestiaryError = DMAI.defineError('BestiaryError');

  //: The SRD standard array, highest first.
  var STANDARD_ARRAY = [15, 14, 13, 12, 10, 8];

  //: Point-buy cost of each score, and the default budget.
  var POINT_BUY_COST = { 8: 0, 9: 1, 10: 2, 11: 3, 12: 4, 13: 5, 14: 7, 15: 9 };
  var POINT_BUY_BUDGET = 27;

  //: Ability order used when nothing better is known, after the class primaries.
  var FALLBACK_PRIORITY = [
    Ability.CON, Ability.DEX, Ability.WIS, Ability.STR, Ability.CHA, Ability.INT
  ];

  var ScoreMethod = {
    STANDARD: 'standard',    // the standard array, assigned by class priority
    ROLLED: 'rolled',        // 4d6 drop lowest, six times
    POINT_BUY: 'point_buy',  // 27 points, 8-15 before species bonuses
    MANUAL: 'manual'         // exactly what the caller passed
  };

  // --- constructors --------------------------------------------------------

  function newAbilities(fields) {
    return Object.assign({
      strength: 10, dexterity: 10, constitution: 10,
      intelligence: 10, wisdom: 10, charisma: 10
    }, fields || {});
  }

  function newHitPoints(maximum, current) {
    return {
      current: current === undefined ? maximum : current,
      maximum: maximum,
      temporary: 0
    };
  }

  function newPersonality(fields) {
    return Object.assign({
      traits: [], ideals: [], bonds: [], flaws: [],
      appearance: '', backstory: '', alignment: null
    }, fields || {});
  }

  function newItem(fields) {
    return Object.assign({
      id: DMAI.newId('item'),
      name: 'Item',
      kind: ItemKind.MISC,
      quantity: 1,
      weight: 0,
      value_gp: 0,
      description: '',
      equipped: false,
      //: Free-form rules payload from the active pack (damage dice, AC, ...).
      properties: {}
    }, fields || {});
  }

  /**
   * Anything with hit points and a turn in initiative.
   *
   * Player characters, friendly NPCs and monsters share this shape so the
   * combat engine never needs to branch on who is fighting.
   */
  function newCreature(fields) {
    return Object.assign({
      id: DMAI.newId('creature'),
      name: 'Creature',
      kind: CreatureKind.MONSTER,
      level: 1,
      species: '',
      'class': '',

      abilities: newAbilities(),
      hp: newHitPoints(10),
      armor_class: 10,
      speed: 30,
      proficiency_bonus: 2,

      //: Skill name -> proficiency multiplier (0 none, 1 proficient, 2 expertise).
      skill_proficiencies: {},
      saving_throw_proficiencies: [],

      conditions: [],
      resistances: [],
      vulnerabilities: [],
      immunities: [],

      inventory: [],
      currency: DMAI.newCurrency(),
      death_saves: { successes: 0, failures: 0, stable: false },
      //: Set by the CREATURE_DIED event.  Zero hit points alone means *dying*,
      //: which is a state a character can be pulled back out of.
      dead: false,

      //: Spell slots, rage uses, ki points -> {"name": [current, maximum]}
      resources: {},
      spells: [],

      //: Stat-block attacks, straight from the rules pack.
      attacks: [],

      //: Monster tactics, read by the combat behaviour system.
      morale: 50,
      goals: [],
      tactics: ''
    }, fields || {});
  }

  /** A player character: a creature plus the things only players have. */
  function newCharacter(fields) {
    return Object.assign(newCreature({ kind: CreatureKind.PLAYER }), {
      pronouns: 'they/them',
      background: '',
      personality: newPersonality(),
      experience: 0,
      //: Which human player controls this sheet.
      player_id: null
    }, fields || {});
  }

  // --- derived reads -------------------------------------------------------

  function isAlive(creature) { return !creature.dead; }
  function isDown(creature) { return creature.hp.current <= 0; }
  function isBloodied(creature) {
    return creature.hp.current <= Math.floor(creature.hp.maximum / 2);
  }
  /** Down but not gone: unconscious at zero hit points, making saves. */
  function isDying(creature) { return isDown(creature) && !creature.dead; }
  function canAct(creature) { return isAlive(creature) && !isDown(creature); }

  function hasCondition(creature, conditionType) {
    return (creature.conditions || []).some(function (condition) {
      return condition.type === conditionType;
    });
  }

  function modifier(creature, ability) {
    return DMAI.abilityModifier(creature.abilities[ability]);
  }

  // --- ability scores ------------------------------------------------------

  /** Six scores, 4d6 keep the best three, highest first. */
  function rollAbilityScores(dice) {
    var scores = [];
    for (var index = 0; index < 6; index += 1) {
      var result = dice.roll('4d6', { reason: 'ability score' });
      var values = result.dice.map(function (die) { return die.value; })
        .sort(function (left, right) { return right - left; });
      scores.push(values[0] + values[1] + values[2]);
    }
    return scores.sort(function (left, right) { return right - left; });
  }

  /** Total point-buy cost.  Throws if any score is outside the legal range. */
  function pointBuyCost(scores) {
    var total = 0;
    Object.keys(scores).forEach(function (ability) {
      var score = scores[ability];
      if (POINT_BUY_COST[score] === undefined) {
        throw new CharacterError(
          ability + ' is ' + score
          + '; point buy allows 8-15 before species bonuses'
        );
      }
      total += POINT_BUY_COST[score];
    });
    return total;
  }

  /** Which abilities this class wants first, from the rules pack. */
  function abilityPriority(rules, classKey) {
    var klass = rules.characterClass(classKey) || {};
    var order = [];
    (klass.primary || []).forEach(function (name) {
      if (DMAI.ABILITIES.indexOf(name) !== -1 && order.indexOf(name) === -1) {
        order.push(name);
      }
    });
    FALLBACK_PRIORITY.forEach(function (ability) {
      if (order.indexOf(ability) === -1) { order.push(ability); }
    });
    return order;
  }

  /** Hand out an ordered list of numbers to abilities in priority order. */
  function assignScores(values, priority) {
    if (values.length < 6) {
      throw new CharacterError('need six ability scores, got ' + values.length);
    }
    var sorted = values.slice().sort(function (left, right) { return right - left; });
    var chosen = {};
    priority.forEach(function (ability, index) {
      if (index < sorted.length) { chosen[ability] = sorted[index]; }
    });
    return chosen;
  }

  /** Produce final ability scores, species bonuses included. */
  function buildAbilities(rules, classKey, speciesKey, options) {
    var settings = options || {};
    var method = settings.method || ScoreMethod.STANDARD;
    var scores = settings.scores;
    var chosen;

    if (method === ScoreMethod.MANUAL || scores) {
      if (!scores) {
        throw new CharacterError('manual ability scores require a scores mapping');
      }
      chosen = {};
      Object.keys(scores).forEach(function (key) {
        chosen[key] = parseInt(scores[key], 10);
      });
      if (method === ScoreMethod.POINT_BUY) {
        var spent = pointBuyCost(chosen);
        if (spent > POINT_BUY_BUDGET) {
          throw new CharacterError(
            'point buy spends ' + spent + ' points; the budget is ' + POINT_BUY_BUDGET
          );
        }
      }
    } else {
      var priority = abilityPriority(rules, classKey);
      var values;
      if (method === ScoreMethod.ROLLED) {
        if (!settings.dice) {
          throw new CharacterError('rolled ability scores require a DiceEngine');
        }
        values = rollAbilityScores(settings.dice);
      } else {
        values = STANDARD_ARRAY.slice();
      }
      chosen = assignScores(values, priority);
    }

    var final = {};
    DMAI.ABILITIES.forEach(function (ability) {
      final[ability] = chosen[ability] === undefined ? 10 : chosen[ability];
    });

    var species = rules.species(speciesKey) || {};
    Object.keys(species.ability_bonuses || {}).forEach(function (name) {
      if (final[name] !== undefined) {
        final[name] += parseInt(species.ability_bonuses[name], 10);
      }
    });
    return newAbilities(final);
  }

  // --- gear and skills -----------------------------------------------------

  /**
   * Turn the class's starting-equipment keys into inventory items.
   *
   * Weapons and armour come back equipped, because a fighter who has to be
   * told to hold their own sword is a bad first impression.
   */
  function startingEquipment(rules, classKey) {
    var klass = rules.characterClass(classKey) || {};
    return (klass.starting_equipment || []).map(function (key) {
      var weapon = rules.weapon(key);
      if (weapon) {
        return newItem({
          name: weapon.name,
          kind: ItemKind.WEAPON,
          weight: weapon.weight || 0,
          value_gp: weapon.cost_gp || 0,
          equipped: true,
          properties: Object.assign({ key: key }, weapon)
        });
      }
      var armor = rules.armor(key);
      if (armor) {
        return newItem({
          name: armor.name,
          kind: armor.type === 'shield' ? ItemKind.SHIELD : ItemKind.ARMOR,
          weight: armor.weight || 0,
          value_gp: armor.cost_gp || 0,
          equipped: true,
          properties: Object.assign({ key: key }, armor)
        });
      }
      return newItem({ name: DMAI.titleCase(key), properties: { key: key } });
    });
  }

  /**
   * Validate skill proficiencies against the class list.
   *
   * A skill the class cannot take is an error rather than a silent drop: a
   * character sheet that quietly loses a proficiency is worse than a refusal.
   */
  function skillChoices(rules, classKey, requested) {
    var klass = rules.characterClass(classKey) || {};
    var allowed = klass.skills || [];
    var limit = parseInt(klass.skill_count, 10) || 2;
    var chosen = (requested || []).slice();

    if (!chosen.length) {
      chosen = allowed.slice().sort().slice(0, limit);
    }
    if (chosen.length > limit) {
      throw new CharacterError(
        classKey + ' may choose ' + limit + ' skills; ' + chosen.length + ' were given'
      );
    }
    var unavailable = chosen.filter(function (skill) {
      return allowed.length && allowed.indexOf(skill) === -1;
    });
    if (unavailable.length) {
      throw new CharacterError(
        classKey + ' cannot take ' + unavailable.join(', ')
        + '; available: ' + allowed.slice().sort().join(', ')
      );
    }

    var proficiencies = {};
    chosen.forEach(function (skill) { proficiencies[skill] = 1; });
    return proficiencies;
  }

  // --- creation ------------------------------------------------------------

  /** Build a complete, playable character sheet. */
  function createCharacter(rules, name, speciesKey, classKey, level, options) {
    var settings = options || {};
    speciesKey = speciesKey || 'human';
    classKey = classKey || 'fighter';
    level = level || 1;

    if (!String(name || '').trim()) {
      throw new CharacterError('a character needs a name');
    }
    if (level < 1) {
      throw new CharacterError('level must be at least 1, got ' + level);
    }
    if (!rules.characterClass(classKey)) {
      throw new CharacterError('unknown class for ' + rules.id + ': ' + classKey);
    }
    if (!rules.species(speciesKey)) {
      throw new CharacterError('unknown species for ' + rules.id + ': ' + speciesKey);
    }

    var abilities = buildAbilities(rules, classKey, speciesKey, {
      method: settings.method,
      scores: settings.scores,
      dice: settings.dice
    });
    var klass = rules.characterClass(classKey) || {};
    var speciesData = rules.species(speciesKey) || {};

    var proficiency = rules.proficiencyBonus(level);
    var conModifier = DMAI.abilityModifier(abilities.constitution);
    var maximumHp = rules.maxHitPoints(classKey, level, conModifier);

    var saves = (klass.saves || []).filter(function (save) {
      return DMAI.ABILITIES.indexOf(save) !== -1;
    });

    var trimmed = String(name).trim();
    var character = newCharacter({
      id: settings.character_id
        || DMAI.newId((trimmed.split(/\s+/)[0] || 'pc').toLowerCase()),
      name: trimmed,
      level: level,
      species: speciesData.name || speciesKey,
      'class': klass.name || classKey,
      class_key: classKey,
      species_key: speciesKey,
      abilities: abilities,
      hp: newHitPoints(maximumHp),
      speed: parseInt(speciesData.speed, 10) || 30,
      proficiency_bonus: proficiency,
      skill_proficiencies: skillChoices(rules, classKey, settings.skills),
      saving_throw_proficiencies: saves,
      resistances: (speciesData.resistances || []).slice(),
      traits: (speciesData.traits || []).slice(),
      inventory: startingEquipment(rules, classKey),
      pronouns: settings.pronouns || 'they/them',
      background: settings.background || '',
      personality: settings.personality || newPersonality(),
      player_id: settings.player_id || null
    });

    // Species skill proficiencies stack on top of the class choices.
    (speciesData.skill_proficiencies || []).forEach(function (skill) {
      character.skill_proficiencies[skill] = Math.max(
        1, character.skill_proficiencies[skill] || 0
      );
    });

    // Spell slots and class resources, so a wizard opens with something to spend.
    if (klass.spellcasting && klass.spellcasting.slots_level_1) {
      var slots = parseInt(klass.spellcasting.slots_level_1, 10);
      character.resources['spell slots (1st)'] = [slots, slots];
    }
    if (klass.features && klass.features.rage_uses_level_1) {
      var rages = parseInt(klass.features.rage_uses_level_1, 10);
      character.resources.rage = [rages, rages];
    }

    // Armour class is derived from what is equipped, so it is computed after
    // the gear is in place rather than guessed during construction.
    character.armor_class = rules.armorClass(character);
    return character;
  }

  /**
   * Advance one level.  Returns the LEVEL_UP event payload, not a mutation.
   * The engine records the new values as an event; the reducer applies them.
   */
  function levelUp(rules, character, classKey) {
    var newLevel = character.level + 1;
    var key = classKey || classKeyOf(rules, character);
    var conModifier = modifier(character, Ability.CON);
    var maximum = rules.maxHitPoints(key, newLevel, conModifier);
    var gained = maximum - character.hp.maximum;
    return {
      level: newLevel,
      proficiency_bonus: rules.proficiencyBonus(newLevel),
      hp_maximum: maximum,
      hp_current: character.hp.current + Math.max(0, gained),
      hp_gained: gained
    };
  }

  /** Map a display class name ("Fighter") back to its pack key ("fighter"). */
  function classKeyOf(rules, creature) {
    if (creature.class_key && rules.characterClass(creature.class_key)) {
      return creature.class_key;
    }
    var wanted = String(creature['class'] || '').trim().toLowerCase();
    if (rules.characterClass(wanted)) { return wanted; }

    var keys = rules.classKeys();
    for (var index = 0; index < keys.length; index += 1) {
      var klass = rules.characterClass(keys[index]);
      if (klass && String(klass.name || '').toLowerCase() === wanted) {
        return keys[index];
      }
    }
    throw new CharacterError(
      'cannot map class "' + creature['class'] + '" to ' + rules.id
    );
  }

  // --- the bestiary --------------------------------------------------------

  /**
   * Build one monster from its stat block.
   *
   * Pass a DiceEngine to roll hit points from the stat block's hit dice;
   * without one the block's average is used, which is what a DM does when
   * they want a predictable fight.
   */
  function spawnMonster(rules, key, options) {
    var settings = options || {};
    var block = rules.monster(key);
    if (!block) {
      throw new BestiaryError('no creature "' + key + '" in rules pack ' + rules.id);
    }

    var maximum;
    if (settings.dice && block.hp) {
      maximum = Math.max(1, settings.dice.roll(block.hp, {
        reason: key + ' hit points'
      }).total);
    } else {
      maximum = parseInt(block.hp_average, 10) || 1;
    }

    return newCreature({
      id: settings.creature_id || DMAI.newId(key.replace(/_/g, '-')),
      name: settings.name || block.name || DMAI.titleCase(key),
      kind: CreatureKind.MONSTER,
      species: block.name || key,
      monster_key: key,
      level: 1,
      abilities: newAbilities(block.abilities || {}),
      hp: newHitPoints(maximum),
      armor_class: parseInt(block.ac, 10) || 10,
      speed: parseInt(block.speed, 10) || 30,
      proficiency_bonus: parseInt(block.proficiency_bonus, 10) || 2,
      skill_proficiencies: Object.assign({}, block.skill_proficiencies || {}),
      saving_throw_proficiencies: (block.saves || []).filter(function (save) {
        return DMAI.ABILITIES.indexOf(save) !== -1;
      }),
      resistances: (block.resistances || []).slice(),
      vulnerabilities: (block.vulnerabilities || []).slice(),
      immunities: (block.immunities || []).slice(),
      traits: (block.traits || []).slice(),
      attacks: DMAI.deepCopy(block.attacks || []),
      xp: parseInt(block.xp, 10) || 0,
      cr: block.cr === undefined ? 0 : block.cr,
      morale: parseInt(block.morale, 10) || 50,
      goals: (block.goals || []).slice(),
      tactics: block.tactics || ''
    });
  }

  /**
   * Spawn `count` of a creature, numbered when there is more than one.
   *
   * Names matter here: "the wounded Goblin 3" is a sentence a DM can say, and
   * a bare list of identical goblins is not.
   */
  function spawnGroup(rules, key, count, options) {
    var settings = options || {};
    if (count < 1) {
      throw new BestiaryError('count must be at least 1, got ' + count);
    }
    var block = rules.monster(key);
    if (!block) {
      throw new BestiaryError('no creature "' + key + '" in rules pack ' + rules.id);
    }

    var base = block.name || DMAI.titleCase(key);
    var creatures = [];
    for (var index = 0; index < count; index += 1) {
      creatures.push(spawnMonster(rules, key, {
        name: count === 1 ? base : base + ' ' + (index + 1),
        dice: settings.dice
      }));
    }
    return creatures;
  }

  /**
   * A named NPC built on a stat-block template.
   *
   * The template supplies the numbers so a bartender who ends up in a brawl
   * has real statistics; everything above them is characterisation.
   */
  function spawnNpc(rules, name, options) {
    var settings = options || {};
    var base = spawnMonster(rules, settings.template || 'guard', {
      name: name, dice: settings.dice
    });
    return Object.assign(base, {
      id: settings.npc_id
        || DMAI.newId((String(name).split(/\s+/)[0] || 'npc').toLowerCase()),
      name: name,
      kind: CreatureKind.NPC,
      role: settings.role || '',
      personality: settings.personality || newPersonality(),
      fears: [],
      knowledge: (settings.knowledge || []).slice(),
      secrets: [],
      attitudes: {},
      relationships: {},
      emotional_state: 'calm',
      dialogue_style: settings.dialogue_style || '',
      location_id: settings.location_id || null,
      faction_id: settings.faction_id || null
    });
  }

  /** A portable sheet -- import/export as JSON. */
  function exportCharacter(character) {
    return DMAI.deepCopy(character);
  }

  /** Read a sheet back, filling in anything an older export is missing. */
  function importCharacter(payload) {
    return newCharacter(DMAI.deepCopy(payload));
  }

  DMAI.CharacterError = CharacterError;
  DMAI.BestiaryError = BestiaryError;
  DMAI.ScoreMethod = ScoreMethod;
  DMAI.STANDARD_ARRAY = STANDARD_ARRAY;
  DMAI.POINT_BUY_COST = POINT_BUY_COST;
  DMAI.POINT_BUY_BUDGET = POINT_BUY_BUDGET;
  DMAI.newAbilities = newAbilities;
  DMAI.newHitPoints = newHitPoints;
  DMAI.newPersonality = newPersonality;
  DMAI.newItem = newItem;
  DMAI.newCreature = newCreature;
  DMAI.newCharacter = newCharacter;
  DMAI.isAlive = isAlive;
  DMAI.isDown = isDown;
  DMAI.isBloodied = isBloodied;
  DMAI.isDying = isDying;
  DMAI.canAct = canAct;
  DMAI.hasCondition = hasCondition;
  DMAI.creatureModifier = modifier;
  DMAI.rollAbilityScores = rollAbilityScores;
  DMAI.pointBuyCost = pointBuyCost;
  DMAI.assignScores = assignScores;
  DMAI.buildAbilities = buildAbilities;
  DMAI.createCharacter = createCharacter;
  DMAI.levelUp = levelUp;
  DMAI.classKeyOf = classKeyOf;
  DMAI.spawnMonster = spawnMonster;
  DMAI.spawnGroup = spawnGroup;
  DMAI.spawnNpc = spawnNpc;
  DMAI.exportCharacter = exportCharacter;
  DMAI.importCharacter = importCharacter;
})(window.DMAI = window.DMAI || {});
