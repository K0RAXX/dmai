/* Headless tests for the standalone engine.
 *
 * Run with:  node standalone/test/engine.test.js
 *
 * The engine is written for the browser, so this harness supplies the two
 * globals it expects and then loads the same source files the bundle does, in
 * the same order.  Nothing is stubbed: these tests drive the real engine.
 *
 * What is checked here is what the Python suite checks, and for the same
 * reasons: that dice are auditable, that a sheet derives correctly from the
 * pack, that a fight resolves and ends, that inventory recomputes armour
 * class, and -- the load-bearing one -- that replaying a log rebuilds the
 * state exactly.
 */

'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

// --- the harness ---------------------------------------------------------

const SRC = path.join(__dirname, '..', 'src');
const ORDER = fs.readdirSync(SRC)
  .filter((name) => name.endsWith('.js'))
  .sort();

const sandbox = { console, Math, Date, JSON, setTimeout };
sandbox.window = sandbox;
vm.createContext(sandbox);

ORDER.forEach((name) => {
  const code = fs.readFileSync(path.join(SRC, name), 'utf8');
  vm.runInContext(code, sandbox, { filename: name });
});

const DMAI = sandbox.DMAI;

let passed = 0;
let failed = 0;
const failures = [];

function test(name, body) {
  try {
    body();
    passed += 1;
    console.log('  ok   ' + name);
  } catch (error) {
    failed += 1;
    failures.push({ name, error });
    console.log('  FAIL ' + name + '\n         ' + error.message);
  }
}

function assert(condition, message) {
  if (!condition) { throw new Error(message || 'assertion failed'); }
}

function equal(actual, expected, message) {
  if (actual !== expected) {
    throw new Error(
      (message || 'values differ') + ': expected ' + JSON.stringify(expected)
      + ', got ' + JSON.stringify(actual)
    );
  }
}

function section(name) { console.log('\n' + name); }

/** A fresh campaign, seeded so every test below is reproducible. */
function newSession(overrides) {
  const campaign = DMAI.newCampaign(Object.assign({
    name: 'Ashes of Emberfall',
    premise: 'A caravan vanished on the Cinder Road.'
  }, overrides || {}));
  campaign.settings.rng_seed = 1234;
  return DMAI.GameSession.create(campaign);
}

// --- dice ----------------------------------------------------------------

section('dice');

test('parses the expressions the game actually uses', () => {
  equal(DMAI.parseDice('2d6+3').length, 2);
  equal(DMAI.parseDice('d20').length, 1);
  equal(DMAI.parseDice('1d12+3').length, 2);
  equal(DMAI.parseDice('d%')[0].sides, 100);
  equal(DMAI.parseDice('4d8-2')[1].sign, -1);
});

test('refuses expressions it cannot honour', () => {
  ['', 'fireball', '1d1', '2000d6', 'd0'].forEach((bad) => {
    let threw = false;
    try { DMAI.parseDice(bad); } catch (error) { threw = true; }
    assert(threw, 'expected "' + bad + '" to be refused');
  });
});

test('a seeded engine replays exactly', () => {
  const left = new DMAI.DiceEngine(99);
  const right = new DMAI.DiceEngine(99);
  for (let index = 0; index < 50; index += 1) {
    equal(left.roll('1d20+3').total, right.roll('1d20+3').total);
  }
});

test('every roll carries its own audit trail', () => {
  const dice = new DMAI.DiceEngine(7);
  const result = dice.roll('3d6+2', { reason: 'damage' });
  equal(result.dice.length, 3);
  equal(result.modifier, 2);
  const sum = result.dice.reduce((total, die) => total + die.value, 0);
  equal(result.total, sum + 2, 'total must equal the dice it shows');
  result.dice.forEach((die) => {
    assert(die.value >= 1 && die.value <= 6, 'a d6 rolled ' + die.value);
  });
});

test('advantage keeps the better die and shows the discarded one', () => {
  const dice = new DMAI.DiceEngine(3);
  const result = dice.roll('1d20+5', { mode: DMAI.RollMode.ADVANTAGE });
  equal(result.dice.length, 2, 'both dice must be visible');
  equal(result.dice.filter((die) => die.dropped).length, 1);
  const kept = DMAI.keptDice(result)[0];
  const dropped = result.dice.filter((die) => die.dropped)[0];
  assert(kept.value >= dropped.value, 'advantage kept the worse die');
});

test('disadvantage keeps the worse die', () => {
  const dice = new DMAI.DiceEngine(3);
  const result = dice.roll('1d20', { mode: DMAI.RollMode.DISADVANTAGE });
  const kept = DMAI.keptDice(result)[0];
  const dropped = result.dice.filter((die) => die.dropped)[0];
  assert(kept.value <= dropped.value, 'disadvantage kept the better die');
});

test('naturals are critical regardless of the DC', () => {
  const dice = new DMAI.DiceEngine(1);
  let sawTwenty = false;
  let sawOne = false;
  for (let index = 0; index < 400 && !(sawTwenty && sawOne); index += 1) {
    const result = dice.roll('1d20', { dc: 30 });
    if (result.total === 20) {
      equal(result.outcome, DMAI.Outcome.CRITICAL_SUCCESS, 'nat 20 vs DC 30');
      sawTwenty = true;
    }
    if (result.total === 1) {
      equal(result.outcome, DMAI.Outcome.CRITICAL_FAILURE, 'nat 1');
      sawOne = true;
    }
  }
  assert(sawTwenty && sawOne, 'never rolled a natural 20 and a natural 1');
});

test('averages are right, for encounter budgeting', () => {
  equal(DMAI.averageDice('1d6'), 3.5);
  equal(DMAI.averageDice('2d6+3'), 10);
  equal(DMAI.averageDice('1d8-1'), 3.5);
});

// --- rules ---------------------------------------------------------------

section('rules');

test('the SRD pack loads with its content intact', () => {
  const rules = DMAI.loadRules('srd51');
  equal(rules.id, 'srd51');
  assert(rules.classKeys().length >= 6, 'expected the six classes');
  assert(rules.monsterKeys().length >= 10, 'expected a bestiary');
  assert(rules.attribution.indexOf('System Reference Document') !== -1,
    'the SRD attribution must survive into the bundle');
});

test('ability modifiers round the way the SRD says, below 10 included', () => {
  equal(DMAI.abilityModifier(10), 0);
  equal(DMAI.abilityModifier(11), 0);
  equal(DMAI.abilityModifier(18), 4);
  equal(DMAI.abilityModifier(9), -1);
  equal(DMAI.abilityModifier(7), -2);
  equal(DMAI.abilityModifier(1), -5);
});

test('proficiency follows the level table and clamps at both ends', () => {
  const rules = DMAI.loadRules('srd51');
  equal(rules.proficiencyBonus(1), 2);
  equal(rules.proficiencyBonus(5), 3);
  equal(rules.proficiencyBonus(17), 6);
  equal(rules.proficiencyBonus(40), 6, 'beyond 20 stays at the top of the table');
});

test('a critical doubles the dice and never the modifier', () => {
  equal(DMAI.criticalDice('1d8+3'), '2d8+3');
  equal(DMAI.criticalDice('2d6'), '4d6');
  equal(DMAI.criticalDice('1d12+4'), '2d12+4');
});

test('resistance halves, vulnerability doubles, immunity zeroes', () => {
  const rules = DMAI.loadRules('srd51');
  const make = (fields) => DMAI.newCreature(Object.assign({
    hp: DMAI.newHitPoints(40)
  }, fields));

  const plain = make({});
  equal(rules.applyDamage(plain, 10, 'fire'), 10);
  equal(plain.hp.current, 30);

  const resistant = make({ resistances: ['fire'] });
  equal(rules.applyDamage(resistant, 11, 'fire'), 5, 'resistance rounds down');

  const vulnerable = make({ vulnerabilities: ['fire'] });
  equal(rules.applyDamage(vulnerable, 10, 'fire'), 20);

  const immune = make({ immunities: ['fire'] });
  equal(rules.applyDamage(immune, 999, 'fire'), 0);
  equal(immune.hp.current, 40);
});

test('temporary hit points absorb damage before real ones', () => {
  const rules = DMAI.loadRules('srd51');
  const creature = DMAI.newCreature({ hp: { current: 20, maximum: 20, temporary: 5 } });
  rules.applyDamage(creature, 8, 'slashing');
  equal(creature.hp.temporary, 0);
  equal(creature.hp.current, 17, 'only the overflow reaches real hit points');
});

// --- characters ----------------------------------------------------------

section('characters');

test('a fighter derives a complete, correct sheet from the pack', () => {
  const session = newSession();
  const vale = session.addCharacter('Vale', {
    species: 'human', character_class: 'fighter', level: 2
  });

  equal(vale.name, 'Vale');
  equal(vale['class'], 'Fighter');
  equal(vale.species, 'Human');
  equal(vale.level, 2);
  equal(vale.proficiency_bonus, 2);

  // Standard array 15 to the fighter's primary, +1 human across the board.
  equal(vale.abilities.strength, 16);
  // d10 hit die: 10 + CON at level 1, then the average (6) + CON.
  const con = DMAI.abilityModifier(vale.abilities.constitution);
  equal(vale.hp.maximum, 10 + con + (6 + con));
  equal(vale.hp.current, vale.hp.maximum, 'a new character starts whole');

  assert(vale.saving_throw_proficiencies.indexOf('strength') !== -1);
  assert(vale.saving_throw_proficiencies.indexOf('constitution') !== -1);

  // Chain mail (16, no DEX) plus a shield (+2).
  equal(vale.armor_class, 18);
  const worn = DMAI.equipmentSlots(vale);
  equal(worn.armor.name, 'Chain Mail');
  equal(worn.shield.name, 'Shield');
  equal(worn.weapons.length, 1);
  equal(worn.weapons[0].name, 'Longsword');
});

test('species bonuses, traits and resistances land on the sheet', () => {
  const session = newSession();
  const elf = session.addCharacter('Faen', {
    species: 'elf', character_class: 'rogue', level: 1
  });
  const dwarf = session.addCharacter('Bruni', {
    species: 'dwarf', character_class: 'cleric', level: 1
  });

  // Rogue wants DEX first: 15 + 2 elf.
  equal(elf.abilities.dexterity, 17);
  assert(elf.skill_proficiencies.perception >= 1, 'elves have Keen Senses');
  assert(dwarf.resistances.indexOf('poison') !== -1, 'Dwarven Resilience');
  equal(dwarf.speed, 25);
});

test('a class cannot take a skill it has no business with', () => {
  const session = newSession();
  let threw = false;
  try {
    session.addCharacter('Wrong', {
      character_class: 'wizard', skills: ['athletics']
    });
  } catch (error) {
    threw = true;
    assert(error.message.indexOf('athletics') !== -1, 'the refusal must name the skill');
  }
  assert(threw, 'a wizard was allowed athletics');
});

test('point buy refuses a sheet over budget', () => {
  const rules = DMAI.loadRules('srd51');
  let threw = false;
  try {
    DMAI.buildAbilities(rules, 'fighter', 'human', {
      method: DMAI.ScoreMethod.POINT_BUY,
      scores: {
        strength: 15, dexterity: 15, constitution: 15,
        intelligence: 15, wisdom: 15, charisma: 15
      }
    });
  } catch (error) {
    threw = true;
  }
  assert(threw, '90 points of point buy was accepted');
});

test('levelling up raises the ceiling and the floor together', () => {
  const session = newSession();
  const vale = session.addCharacter('Vale', { character_class: 'fighter', level: 1 });
  const before = vale.hp.maximum;

  session.combat.dealDamage(session.journal, vale.id, 4, 'slashing');
  const wounded = session.state.party[vale.id].hp.current;

  const levelled = session.awardLevel(vale.id);
  equal(levelled.level, 2);
  assert(levelled.hp.maximum > before, 'maximum hit points did not rise');
  equal(levelled.hp.current, wounded + (levelled.hp.maximum - before),
    'the gain applies to current hit points too');
});

test('monsters spawn from their stat block, numbered when there are several', () => {
  const session = newSession();
  const goblins = session.spawn('goblin', 3);
  equal(goblins.length, 3);
  equal(goblins[0].name, 'Goblin 1');
  equal(goblins[2].name, 'Goblin 3');
  goblins.forEach((goblin) => {
    equal(goblin.armor_class, 15);
    assert(goblin.hp.maximum >= 2 && goblin.hp.maximum <= 14, '2d6+2 hit points');
    equal(goblin.attacks.length, 2);
    assert(goblin.id !== goblins[0].id || goblin === goblins[0],
      'each goblin needs its own identity');
  });

  const lone = session.spawn('ogre', 1)[0];
  equal(lone.name, 'Ogre', 'a single monster is not numbered');
});

// --- inventory -----------------------------------------------------------

section('inventory');

test('equipping armour recomputes armour class and frees the slot', () => {
  const session = newSession();
  const vale = session.addCharacter('Vale', { character_class: 'fighter', level: 1 });
  equal(vale.armor_class, 18, 'chain mail and a shield');

  const leather = session.inventory.give(session.journal, vale.id, 'leather_armor');
  const armorClass = session.inventory.equip(session.journal, vale.id, leather.id);

  const after = session.state.party[vale.id];
  equal(after.armor_class, armorClass);
  const worn = DMAI.equipmentSlots(after);
  equal(worn.armor.name, 'Leather Armor', 'the chain mail should have come off');
  equal(worn.shield.name, 'Shield', 'the shield stays: a different slot');

  const dex = DMAI.abilityModifier(after.abilities.dexterity);
  equal(after.armor_class, 11 + dex + 2);
});

test('unequipping everything falls back to unarmoured defence', () => {
  const session = newSession();
  const vale = session.addCharacter('Vale', { character_class: 'fighter', level: 1 });
  DMAI.equipmentSlots(vale);
  session.state.party[vale.id].inventory.slice().forEach((item) => {
    session.inventory.unequip(session.journal, vale.id, item.id);
  });
  const after = session.state.party[vale.id];
  equal(after.armor_class, 10 + DMAI.abilityModifier(after.abilities.dexterity));
});

test('a stack splits when part of it is taken', () => {
  const session = newSession();
  const vale = session.addCharacter('Vale');
  const potions = DMAI.newItem({
    name: 'Potion of Healing', kind: DMAI.ItemKind.CONSUMABLE, quantity: 5
  });
  session.inventory.give(session.journal, vale.id, potions, { quantity: 5 });

  let held = session.state.party[vale.id].inventory
    .filter((item) => item.name === 'Potion of Healing')[0];
  equal(held.quantity, 5);

  session.inventory.take(session.journal, vale.id, held.id, { quantity: 2 });
  held = session.state.party[vale.id].inventory
    .filter((item) => item.name === 'Potion of Healing')[0];
  equal(held.quantity, 3, 'three should be left');
});

test('a purse makes change rather than refusing a small price', () => {
  const session = newSession();
  const vale = session.addCharacter('Vale');
  session.inventory.adjustCurrency(session.journal, vale.id, { gold: 5 }, 'starting funds');

  const purse = session.inventory.pay(session.journal, vale.id, 0.03, 'a candle');
  equal(DMAI.totalInCopper(purse), 497, 'five gold less three copper');
  assert(purse.copper > 0, 'a gold piece should have been broken into change');
});

test('you cannot spend what you do not have', () => {
  const session = newSession();
  const vale = session.addCharacter('Vale');
  session.inventory.adjustCurrency(session.journal, vale.id, { gold: 1 });
  let threw = false;
  try {
    session.inventory.pay(session.journal, vale.id, 50);
  } catch (error) {
    threw = true;
  }
  assert(threw, 'the party bought plate armour with one gold piece');
});

test('items move between characters without being duplicated', () => {
  const session = newSession();
  const vale = session.addCharacter('Vale', { character_class: 'fighter' });
  const faen = session.addCharacter('Faen', { character_class: 'rogue' });

  const sword = session.state.party[vale.id].inventory
    .filter((item) => item.name === 'Longsword')[0];
  session.inventory.transfer(session.journal, vale.id, faen.id, sword.id);

  const giver = session.state.party[vale.id];
  const taker = session.state.party[faen.id];
  equal(giver.inventory.filter((item) => item.name === 'Longsword').length, 0);
  equal(taker.inventory.filter((item) => item.name === 'Longsword').length, 1);
  assert(!taker.inventory.filter((item) => item.name === 'Longsword')[0].equipped,
    'a handed-over weapon arrives stowed, not already drawn');
});

// --- combat --------------------------------------------------------------

section('combat');

test('initiative orders the fight and opens the first turn', () => {
  const session = newSession();
  const vale = session.addCharacter('Vale', { character_class: 'fighter', level: 3 });
  const goblins = session.spawn('goblin', 2);

  const combat = session.combat.start(
    session.journal, [vale.id].concat(goblins.map((g) => g.id))
  );
  equal(combat.active, true);
  equal(combat.round, 1);
  equal(combat.order.length, 3);

  for (let index = 1; index < combat.order.length; index += 1) {
    assert(combat.order[index - 1].initiative >= combat.order[index].initiative,
      'the order must run highest first');
  }
  assert(DMAI.currentCombatant(combat), 'somebody must be on turn');
});

test('an attack rolls against real armour class and logs what it did', () => {
  const session = newSession();
  const vale = session.addCharacter('Vale', { character_class: 'fighter', level: 5 });
  const goblin = session.spawn('goblin', 1)[0];

  const outcome = session.combat.attack(session.journal, vale.id, goblin.id);
  equal(outcome.attack_roll.dc, 15, 'a goblin wears 15 armour class');
  if (outcome.hit) {
    assert(outcome.damage_roll, 'a hit must roll damage');
    assert(outcome.damage_dealt > 0, 'a hit must deal damage');
    equal(session.state.bestiary[goblin.id].hp.current,
      Math.max(0, goblin.hp.maximum - outcome.damage_dealt));
  } else {
    equal(outcome.damage_dealt, 0, 'a miss must deal nothing');
  }
});

test('a monster at zero hit points dies; a player falls and makes saves', () => {
  const session = newSession();
  const vale = session.addCharacter('Vale', { character_class: 'fighter', level: 1 });
  const goblin = session.spawn('goblin', 1)[0];

  session.combat.dealDamage(session.journal, goblin.id, 500, 'slashing');
  equal(session.state.bestiary[goblin.id].dead, true, 'the goblin should be dead');

  // Exactly to zero: down, not dead -- overkill has to exceed the maximum.
  session.combat.dealDamage(session.journal, vale.id, vale.hp.maximum, 'slashing');
  const downed = session.state.party[vale.id];
  equal(downed.hp.current, 0);
  equal(downed.dead, false, 'a player character does not simply die at zero');
  assert(DMAI.isDying(downed), 'the character should be dying');
  assert(DMAI.hasCondition(downed, DMAI.ConditionType.UNCONSCIOUS));
});

test('massive damage kills a player outright', () => {
  const session = newSession();
  const vale = session.addCharacter('Vale', { character_class: 'fighter', level: 1 });
  session.combat.dealDamage(session.journal, vale.id, vale.hp.maximum * 3, 'force');
  equal(session.state.party[vale.id].dead, true);
});

test('three failed death saves end a character; healing brings them back', () => {
  const session = newSession();
  const vale = session.addCharacter('Vale', { character_class: 'fighter', level: 1 });
  session.combat.dealDamage(session.journal, vale.id, vale.hp.maximum, 'slashing');

  let guard = 0;
  while (DMAI.isDying(session.state.party[vale.id]) && guard < 40) {
    session.combat.deathSave(session.journal, vale.id);
    guard += 1;
    const now = session.state.party[vale.id];
    if (now.death_saves.stable || now.dead || now.hp.current > 0) { break; }
  }
  const settled = session.state.party[vale.id];
  assert(settled.dead || settled.death_saves.stable || settled.hp.current > 0,
    'death saves must resolve one way or another');

  if (!settled.dead) {
    session.combat.heal(session.journal, vale.id, 6);
    const healed = session.state.party[vale.id];
    assert(healed.hp.current > 0, 'healing must bring them round');
    equal(healed.death_saves.failures, 0, 'death saves reset on healing');
    assert(!DMAI.hasCondition(healed, DMAI.ConditionType.UNCONSCIOUS));
  }
});

test('a whole fight runs to a conclusion on tactics alone', () => {
  const session = newSession();
  const vale = session.addCharacter('Vale', { character_class: 'fighter', level: 5 });
  const bruni = session.addCharacter('Bruni', { character_class: 'cleric', level: 5 });
  const enemies = session.spawn('goblin', 3);

  session.combat.start(
    session.journal,
    [vale.id, bruni.id].concat(enemies.map((enemy) => enemy.id))
  );

  let rounds = 0;
  while (session.state.combat.active && rounds < 200) {
    const onTurn = session.combat.current(session.journal);
    if (!onTurn) { break; }
    if (onTurn.kind === DMAI.CreatureKind.PLAYER) {
      const target = DMAI.chooseTarget(session.combat, session.journal, onTurn.id);
      if (target) { session.combat.attack(session.journal, onTurn.id, target); }
      if (session.state.combat.active) { session.combat.advanceTurn(session.journal); }
    } else {
      DMAI.takeMonsterTurn(session.combat, session.journal, onTurn.id);
      if (session.state.combat.active) { session.combat.advanceTurn(session.journal); }
    }
    rounds += 1;
  }

  equal(session.state.combat.active, false, 'the fight never ended');
  assert(rounds < 200, 'the fight ran away with itself');
});

test('a wounded bandit would rather leave than die', () => {
  const session = newSession();
  const vale = session.addCharacter('Vale', { character_class: 'fighter', level: 5 });
  const faen = session.addCharacter('Faen', { character_class: 'rogue', level: 5 });
  const bandit = session.spawn('bandit', 1)[0];

  session.combat.start(session.journal, [vale.id, faen.id, bandit.id]);
  // Leave the bandit on its last legs, which is when morale is tested.
  session.combat.dealDamage(
    session.journal, bandit.id,
    session.state.bestiary[bandit.id].hp.current - 1, 'slashing'
  );

  const seen = {};
  for (let index = 0; index < 40; index += 1) {
    const decision = DMAI.decideTactic(session.combat, session.journal, bandit.id);
    seen[decision.tactic] = true;
  }
  assert(seen.flee || seen.surrender || seen.negotiate,
    'a bandit on one hit point only ever attacked');
});

test('conditions steer attack rolls the way the rules say', () => {
  const session = newSession();
  const vale = session.addCharacter('Vale', { character_class: 'fighter', level: 3 });
  const goblin = session.spawn('goblin', 1)[0];

  session.combat.addCondition(session.journal, goblin.id, DMAI.ConditionType.PRONE);
  equal(
    session.combat.attackMode(
      session.state.party[vale.id], session.state.bestiary[goblin.id]
    ),
    DMAI.RollMode.ADVANTAGE,
    'a prone target should be easier to hit'
  );

  session.combat.addCondition(session.journal, vale.id, DMAI.ConditionType.POISONED);
  equal(
    session.combat.attackMode(
      session.state.party[vale.id], session.state.bestiary[goblin.id]
    ),
    DMAI.RollMode.NORMAL,
    'advantage and disadvantage should cancel'
  );
});

// --- checks --------------------------------------------------------------

section('checks');

test('a skill check adds proficiency only where it is held', () => {
  const session = newSession();
  const faen = session.addCharacter('Faen', {
    character_class: 'rogue', level: 3, skills: ['stealth', 'perception', 'deception', 'acrobatics']
  });
  const sheet = session.state.party[faen.id];

  const dex = DMAI.abilityModifier(sheet.abilities.dexterity);
  equal(session.rules.skillModifier(sheet, 'stealth'), dex + sheet.proficiency_bonus);
  equal(session.rules.skillModifier(sheet, 'arcana'),
    DMAI.abilityModifier(sheet.abilities.intelligence),
    'no proficiency, no bonus');
});

test('a blinded character rolls perception at disadvantage', () => {
  const session = newSession();
  const vale = session.addCharacter('Vale', { character_class: 'fighter' });
  session.combat.addCondition(session.journal, vale.id, DMAI.ConditionType.BLINDED);
  const result = session.checks.check(session.journal, vale.id, {
    skill: 'perception', dc: 15
  });
  equal(result.mode, DMAI.RollMode.DISADVANTAGE);
  equal(result.dice.length, 2);
});

test('passive scores need no roll', () => {
  const session = newSession();
  const elf = session.addCharacter('Faen', { species: 'elf', character_class: 'ranger' });
  const sheet = session.state.party[elf.id];
  equal(session.checks.passive(sheet, 'perception'),
    10 + session.rules.skillModifier(sheet, 'perception'));
});

// --- quests and the world ------------------------------------------------

section('quests and the world');

test('a quest completes itself when its last objective falls', () => {
  const session = newSession();
  const quest = session.quests.add(session.journal, 'Find the caravan', {
    objectives: ['Search the Cinder Road', 'Learn what took it'],
    status: DMAI.QuestStatus.ACTIVE
  });

  session.quests.completeObjective(session.journal, quest.id, quest.objectives[0].id);
  equal(session.state.story.quests[quest.id].status, DMAI.QuestStatus.ACTIVE);

  session.quests.completeObjective(session.journal, quest.id, quest.objectives[1].id);
  equal(session.state.story.quests[quest.id].status, DMAI.QuestStatus.COMPLETED);
});

test('a completed quest does not quietly reopen', () => {
  const session = newSession();
  const quest = session.quests.add(session.journal, 'Done', {
    objectives: ['Finish it'], status: DMAI.QuestStatus.ACTIVE
  });
  session.quests.complete(session.journal, quest.id);

  let threw = false;
  try {
    session.quests.setStatus(session.journal, quest.id, DMAI.QuestStatus.ACTIVE);
  } catch (error) {
    threw = true;
  }
  assert(threw, 'a finished quest was reopened');
});

test('a promise the world made comes due on its day', () => {
  const session = newSession();
  session.quests.decide(session.journal, 'The party spared the captain', {
    effect: 'The captain returns with friends', due_day: 3
  });
  equal(session.quests.pending(session.journal).length, 1);

  session.world.tick(session.journal, 60);
  equal(session.quests.pending(session.journal).length, 1, 'not due yet');

  session.world.tick(session.journal, 60 * 24 * 3);
  equal(session.quests.pending(session.journal).length, 0, 'the debt should have been paid');
  assert(session.state.world.world_events.some(
    (line) => line.indexOf('captain returns') !== -1
  ), 'the consequence should appear in the world log');
});

test('a long rest restores hit points, resources and the clock', () => {
  const session = newSession();
  const bruni = session.addCharacter('Bruni', { character_class: 'cleric', level: 3 });
  session.combat.dealDamage(session.journal, bruni.id, 8, 'slashing');
  const slots = session.state.party[bruni.id].resources['spell slots (1st)'];
  session.state.party[bruni.id].resources['spell slots (1st)'] = [0, slots[1]];

  const dayBefore = session.state.world.time.day;
  session.world.longRest(session.journal, [bruni.id]);

  const rested = session.state.party[bruni.id];
  equal(rested.hp.current, rested.hp.maximum);
  equal(rested.resources['spell slots (1st)'][0], slots[1], 'slots come back');
  assert(session.state.world.time.day > dayBefore
    || session.state.world.time.hour >= 16, 'eight hours must pass');
});

test('the atlas wires places together and moves the party between them', () => {
  const session = newSession();
  const inn = session.atlas.addLocation(session.journal, 'The Ashen Hearth', {
    kind: 'building', description: 'Smoke-blacked beams.', discovered: true
  });
  const road = session.atlas.addLocation(session.journal, 'The Cinder Road', {
    kind: 'wilderness', connect_to: [inn.id]
  });

  session.atlas.enter(session.journal, inn.id);
  equal(session.state.current_location_id, inn.id);
  equal(DMAI.currentLocation(session.state).name, 'The Ashen Hearth');

  const exits = session.atlas.neighbours(session.journal, inn.id);
  equal(exits.length, 1);
  equal(exits[0].id, road.id, 'connections must be two-way by default');
});

test('attitudes move a rung at a time, never in a leap', () => {
  const session = newSession();
  const vale = session.addCharacter('Vale');
  const npc = session.atlas.addNpc(
    session.journal, DMAI.spawnNpc(session.rules, 'Maerin', { role: 'innkeeper' })
  );

  equal(session.atlas.attitudeToward(npc, vale.id), DMAI.Attitude.INDIFFERENT);
  session.atlas.shiftAttitude(session.journal, npc.id, vale.id, 1, 'paid in advance');
  equal(session.atlas.attitudeToward(
    session.state.world.npcs[npc.id], vale.id
  ), DMAI.Attitude.FRIENDLY);

  session.atlas.shiftAttitude(session.journal, npc.id, vale.id, -9, 'drew a blade');
  equal(session.atlas.attitudeToward(
    session.state.world.npcs[npc.id], vale.id
  ), DMAI.Attitude.HOSTILE, 'the ladder bottoms out rather than overflowing');
});

// --- encounters ----------------------------------------------------------

section('encounters');

test('an encounter is budgeted against this party, not an imaginary one', () => {
  const session = newSession();
  const party = [
    session.addCharacter('Vale', { character_class: 'fighter', level: 3 }),
    session.addCharacter('Faen', { character_class: 'rogue', level: 3 })
  ];

  const built = session.encounters.combatEncounter(party, DMAI.Difficulty.MODERATE);
  const load = DMAI.budgetLoad(built.budget);
  assert(built.budget.threshold > 0, 'the party must have a budget');
  assert(load > 0 && load <= 1 + DMAI.BUDGET_TOLERANCE,
    'the fight landed at ' + load.toFixed(2) + 'x budget');
  assert(built.encounter.roster && Object.keys(built.encounter.roster).length,
    'a combat encounter needs monsters in it');
});

test('a deadly fight is bigger than an easy one for the same party', () => {
  const session = newSession();
  const party = [session.addCharacter('Vale', { character_class: 'fighter', level: 5 })];
  equal(
    DMAI.partyThreshold(session.rules, party, 'deadly') >
    DMAI.partyThreshold(session.rules, party, 'easy'),
    true
  );
});

test('starting an encounter spawns it and rolls initiative in one step', () => {
  const session = newSession();
  session.addCharacter('Vale', { character_class: 'fighter', level: 3 });
  session.addCharacter('Bruni', { character_class: 'cleric', level: 3 });

  const started = session.startEncounter(DMAI.Difficulty.EASY);
  assert(started.creatures.length > 0, 'nothing was spawned');
  equal(session.state.combat.active, true);
  equal(
    session.state.combat.order.length,
    2 + started.creatures.length,
    'everyone should be in the initiative order'
  );
});

// --- the log -------------------------------------------------------------

section('the event log');

test('sequence numbers are assigned by the store and never collide', () => {
  const session = newSession();
  session.addCharacter('Vale');
  session.roll('1d20', { reason: 'a test' });
  const events = session.store.all();
  events.forEach((event, index) => {
    equal(event.seq, index + 1, 'sequence numbers must be dense and ordered');
  });
});

test('replaying a log rebuilds the state exactly', () => {
  const session = newSession();
  const vale = session.addCharacter('Vale', { character_class: 'fighter', level: 3 });
  const faen = session.addCharacter('Faen', { character_class: 'rogue', level: 3 });
  const goblins = session.spawn('goblin', 2);

  session.inventory.give(session.journal, vale.id, 'greatsword');
  session.inventory.adjustCurrency(session.journal, vale.id, { gold: 12 });
  session.quests.add(session.journal, 'Find the caravan', {
    objectives: ['Search the road'], status: DMAI.QuestStatus.ACTIVE
  });
  session.atlas.addLocation(session.journal, 'The Ashen Hearth', { discovered: true });
  session.combat.start(
    session.journal, [vale.id, faen.id].concat(goblins.map((g) => g.id))
  );
  session.combat.attack(session.journal, vale.id, goblins[0].id);
  session.world.tick(session.journal, 120);

  const replayed = DMAI.rebuild(session.campaign, session.store.all());
  equal(
    JSON.stringify(replayed),
    JSON.stringify(session.state),
    'a replayed campaign must be identical to the one that was played'
  );
});

test('rolling back to a checkpoint undoes exactly what came after it', () => {
  const session = newSession();
  const vale = session.addCharacter('Vale', { character_class: 'fighter', level: 3 });
  const before = DMAI.deepCopy(session.state.party[vale.id]);

  const mark = session.checkpoint('before the fight');
  const goblin = session.spawn('goblin', 1)[0];
  session.combat.dealDamage(session.journal, vale.id, 5, 'slashing');
  assert(session.state.party[vale.id].hp.current < before.hp.current,
    'the character should be wounded before the rollback');

  session.rollback(mark.event_seq);
  equal(
    JSON.stringify(session.state.party[vale.id]),
    JSON.stringify(before),
    'the sheet should be exactly as it was'
  );
  equal(session.state.bestiary[goblin.id], undefined, 'the goblin was never spawned');
  assert(session.checkpoints().some((point) => point.id === mark.id),
    'rolling back to a checkpoint must keep the checkpoint');
});

test('private information never reaches the wrong seat', () => {
  const session = newSession();
  const james = session.addPlayer('James');
  const other = session.addPlayer('Sam');

  session.whisper(james.id, 'You notice the innkeeper palm a key.');
  session.narrate('The fire crackles.');
  session.journal.record(DMAI.EventType.DM_OVERRIDE, {
    summary: 'The trap is on the third stair.',
    visibility: DMAI.Visibility.DM_ONLY
  });

  const mine = session.transcript(james.id);
  const theirs = session.transcript(other.id);
  const dm = session.transcript(null, { is_dm: true });

  assert(mine.some((line) => line.indexOf('palm a key') !== -1),
    'the whisper must reach the player it was for');
  assert(!theirs.some((line) => line.indexOf('palm a key') !== -1),
    'another player read a private whisper');
  assert(!mine.some((line) => line.indexOf('third stair') !== -1),
    'a player read DM-only information');
  assert(dm.some((line) => line.indexOf('third stair') !== -1),
    'the DM seat must see everything');
});

// --- the DM --------------------------------------------------------------

section('the offline DM');

test('interpretation reads intent and the skill it implies', () => {
  equal(DMAI.interpretAction('I attack the goblin').intent, DMAI.IntentKind.ATTACK);
  equal(DMAI.interpretAction('I sneak past the guard').checks[0].skill, 'stealth');
  equal(DMAI.interpretAction('I try to persuade her').checks[0].skill, 'persuasion');
  equal(DMAI.interpretAction('I climb the wall').checks[0].skill, 'athletics');
  equal(DMAI.interpretAction('I take a long rest').intent, DMAI.IntentKind.REST);
  equal(DMAI.interpretAction('Hello there, friend').intent, DMAI.IntentKind.FREEFORM);
});

test('a turn resolves mechanics and narrates only what the log says', () => {
  const session = newSession();
  const vale = session.addCharacter('Vale', { character_class: 'fighter', level: 3 });
  const dm = new DMAI.DungeonMaster(session);

  const action = { id: DMAI.newId('act'), campaign_id: session.campaign.id,
    player_id: null, character_id: vale.id, text: 'I search the wreckage' };
  const result = dm.takeTurn(action);

  equal(result.rolls.length, 1, 'a search should have rolled investigation');
  equal(result.interpretation.checks[0].skill, 'investigation');
  assert(result.narration.length > 0, 'the DM said nothing at all');
  assert(result.narration.indexOf(String(result.rolls[0].total)) !== -1,
    'narration must restate the number the engine actually rolled');
});

test('the DM cannot be talked into attacking something that is not there', () => {
  const session = newSession();
  const vale = session.addCharacter('Vale', { character_class: 'fighter', level: 3 });
  const dm = new DMAI.DungeonMaster(session);

  const before = session.store.head();
  dm.takeTurn({
    id: DMAI.newId('act'), campaign_id: session.campaign.id,
    player_id: null, character_id: vale.id, text: 'I attack the ancient red dragon'
  });

  const attacks = session.store.since(before).filter(
    (event) => event.type === DMAI.EventType.ATTACK
  );
  equal(attacks.length, 0, 'an imaginary dragon was attacked');
});

test('an attack on something real goes through the combat engine', () => {
  const session = newSession();
  const vale = session.addCharacter('Vale', { character_class: 'fighter', level: 3 });
  const goblin = session.spawn('goblin', 1)[0];
  const dm = new DMAI.DungeonMaster(session);

  const before = session.store.head();
  dm.takeTurn({
    id: DMAI.newId('act'), campaign_id: session.campaign.id,
    player_id: null, character_id: vale.id, text: 'I attack the Goblin'
  });

  const attacks = session.store.since(before).filter(
    (event) => event.type === DMAI.EventType.ATTACK
  );
  equal(attacks.length, 1, 'the attack never reached the combat engine');
  equal(attacks[0].target_id, goblin.id);
});

test('memories are extracted from the log, and noise is left out', () => {
  const session = newSession();
  const vale = session.addCharacter('Vale', { character_class: 'fighter', level: 3 });
  const dm = new DMAI.DungeonMaster(session);

  session.quests.discover(session.journal, 'The caravan was taken, not lost.');
  dm._remember(session.store.all());

  const memories = session.state.memories;
  assert(memories.some((memory) => memory.content.indexOf('taken, not lost') !== -1),
    'a discovery should be remembered');
  assert(!memories.some((memory) => memory.content.indexOf('Modifier:') !== -1),
    'dice rolls should not be remembered');
});

// --- the library ---------------------------------------------------------

section('the library');

test('a bundle round-trips through export and back', () => {
  const session = newSession();
  const vale = session.addCharacter('Vale', { character_class: 'fighter', level: 3 });
  session.spawn('goblin', 2);
  session.quests.add(session.journal, 'Find the caravan', { objectives: ['Look'] });
  session.combat.dealDamage(session.journal, vale.id, 4, 'slashing');

  const bundle = DMAI.exportBundle(session);
  const reopened = new DMAI.Library({ available: true }).openBundle(
    JSON.parse(JSON.stringify(bundle))
  );

  equal(reopened.campaign.name, session.campaign.name);
  equal(reopened.store.length(), session.store.length());
  equal(
    JSON.stringify(reopened.state),
    JSON.stringify(session.state),
    'a reopened campaign must equal the one that was saved'
  );
});

test('the Python engine\'s save shape is understood, and produced', () => {
  const session = newSession();
  session.addCharacter('Vale', { character_class: 'fighter', level: 2 });

  const files = DMAI.exportForPython(session);
  assert(files['campaign.json'], 'campaign.json missing');
  assert(files['events.jsonl'], 'events.jsonl missing');

  const lines = files['events.jsonl'].trim().split('\n');
  equal(lines.length, session.store.length(), 'one JSON object per line');
  lines.forEach((line) => { JSON.parse(line); });

  // And back the other way: the two files handed over as one object.
  const reopened = new DMAI.Library({ available: true }).openBundle({
    campaign: JSON.parse(files['campaign.json']),
    events: files['events.jsonl']
  });
  equal(reopened.store.length(), session.store.length());
  equal(reopened.partyList()[0].name, 'Vale');
});

test('an unreadable bundle is refused with something a person can act on', () => {
  const library = new DMAI.Library({ available: true });
  [null, {}, { events: [] }, 'not json at all'].forEach((bad) => {
    let threw = false;
    try { library.openBundle(bad); } catch (error) {
      threw = true;
      assert(error.message.length > 10, 'the refusal should explain itself');
    }
    assert(threw, 'a malformed bundle was accepted: ' + JSON.stringify(bad));
  });
});

// --- results -------------------------------------------------------------

console.log('\n' + '-'.repeat(60));
console.log(passed + ' passed, ' + failed + ' failed');
if (failed) {
  console.log('\nfailures:');
  failures.forEach((entry) => {
    console.log('  ' + entry.name);
    console.log('    ' + entry.error.stack.split('\n').slice(0, 3).join('\n    '));
  });
  process.exit(1);
}
