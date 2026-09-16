/* DungeonMaster AI -- standalone engine, layer A core.
 *
 * A faithful JavaScript port of dmai/engine/models/base.py.  Everything the
 * rest of the engine speaks lives here: identifiers, the ability and skill
 * tables, damage types, conditions, currency, and the seeded random source.
 *
 * The one deliberate divergence from the Python engine is the RNG.  Python
 * seeds a Mersenne Twister; this uses mulberry32.  Both are deterministic and
 * both replay a campaign exactly -- but the same seed does NOT produce the
 * same dice in both engines.  A save moves between them; a seed does not.
 */
(function (DMAI) {
  'use strict';

  // --- identity ------------------------------------------------------------

  var _counter = 0;

  /** Readable, sortable-enough identifier, e.g. "goblin-4f3a91". */
  function newId(prefix) {
    _counter += 1;
    var random = Math.random().toString(16).slice(2, 8);
    var tick = (_counter & 0xfff).toString(16);
    return prefix + '-' + (random + tick).slice(0, 6);
  }

  function utcnow() {
    return new Date().toISOString();
  }

  // --- enumerations --------------------------------------------------------

  var Ability = {
    STR: 'strength',
    DEX: 'dexterity',
    CON: 'constitution',
    INT: 'intelligence',
    WIS: 'wisdom',
    CHA: 'charisma'
  };

  var ABILITIES = [
    Ability.STR, Ability.DEX, Ability.CON,
    Ability.INT, Ability.WIS, Ability.CHA
  ];

  var ABILITY_ABBR = {
    strength: 'STR', dexterity: 'DEX', constitution: 'CON',
    intelligence: 'INT', wisdom: 'WIS', charisma: 'CHA'
  };

  /** Canonical SRD skill -> governing ability. */
  var SKILL_ABILITIES = {
    acrobatics: Ability.DEX,
    animal_handling: Ability.WIS,
    arcana: Ability.INT,
    athletics: Ability.STR,
    deception: Ability.CHA,
    history: Ability.INT,
    insight: Ability.WIS,
    intimidation: Ability.CHA,
    investigation: Ability.INT,
    medicine: Ability.WIS,
    nature: Ability.INT,
    perception: Ability.WIS,
    performance: Ability.CHA,
    persuasion: Ability.CHA,
    religion: Ability.WIS,
    sleight_of_hand: Ability.DEX,
    stealth: Ability.DEX,
    survival: Ability.WIS
  };

  var SKILLS = Object.keys(SKILL_ABILITIES);

  var DamageType = {
    ACID: 'acid', BLUDGEONING: 'bludgeoning', COLD: 'cold', FIRE: 'fire',
    FORCE: 'force', LIGHTNING: 'lightning', NECROTIC: 'necrotic',
    PIERCING: 'piercing', POISON: 'poison', PSYCHIC: 'psychic',
    RADIANT: 'radiant', SLASHING: 'slashing', THUNDER: 'thunder'
  };

  var ConditionType = {
    BLINDED: 'blinded', CHARMED: 'charmed', DEAFENED: 'deafened',
    FRIGHTENED: 'frightened', GRAPPLED: 'grappled',
    INCAPACITATED: 'incapacitated', INVISIBLE: 'invisible',
    PARALYZED: 'paralyzed', PETRIFIED: 'petrified', POISONED: 'poisoned',
    PRONE: 'prone', RESTRAINED: 'restrained', STUNNED: 'stunned',
    UNCONSCIOUS: 'unconscious', EXHAUSTION: 'exhaustion'
  };

  var CONDITION_TYPES = Object.keys(ConditionType).map(function (key) {
    return ConditionType[key];
  });

  var Difficulty = {
    EASY: 'easy', MODERATE: 'moderate', HARD: 'hard',
    DEADLY: 'deadly', CUSTOM: 'custom'
  };

  /** Who may see a piece of information.  Enforced in the multiplayer layer. */
  var Visibility = {
    PUBLIC: 'public',     // everyone at the table
    PRIVATE: 'private',   // a single player, whispered by the DM
    DM_ONLY: 'dm_only'    // never shown to players
  };

  var CreatureKind = { PLAYER: 'player', NPC: 'npc', MONSTER: 'monster' };

  var ItemKind = {
    WEAPON: 'weapon', ARMOR: 'armor', SHIELD: 'shield',
    CONSUMABLE: 'consumable', TOOL: 'tool', TREASURE: 'treasure',
    QUEST: 'quest', MISC: 'misc'
  };

  var Attitude = {
    HOSTILE: 'hostile', UNFRIENDLY: 'unfriendly', INDIFFERENT: 'indifferent',
    FRIENDLY: 'friendly', ALLIED: 'allied'
  };

  var QuestStatus = {
    RUMORED: 'rumored', ACTIVE: 'active', COMPLETED: 'completed',
    FAILED: 'failed', ABANDONED: 'abandoned'
  };

  var MemoryTier = {
    SHORT_TERM: 'short_term', SESSION: 'session',
    CAMPAIGN: 'campaign', WORLD: 'world'
  };

  var MemoryKind = {
    FACT: 'fact', RELATIONSHIP: 'relationship', PROMISE: 'promise',
    DISCOVERY: 'discovery', CONSEQUENCE: 'consequence',
    PREFERENCE: 'preference', LORE: 'lore'
  };

  var STORY_TONES = [
    'lighthearted', 'heroic', 'serious', 'dark', 'grim',
    'horror', 'comedic', 'political', 'mystery', 'epic'
  ];

  var NARRATIVE_STYLES = [
    'concise', 'conversational', 'descriptive', 'cinematic', 'literary'
  ];

  var DM_BEHAVIORS = [
    'strict', 'neutral', 'generous', 'chaotic', 'rules_focused', 'story_focused'
  ];

  // --- currency ------------------------------------------------------------

  /** Coin denominations in ascending value, with their worth in copper. */
  var DENOMINATIONS = {
    copper: 1, silver: 10, electrum: 50, gold: 100, platinum: 1000
  };

  function newCurrency(fields) {
    return Object.assign(
      { copper: 0, silver: 0, electrum: 0, gold: 0, platinum: 0 },
      fields || {}
    );
  }

  function totalInCopper(currency) {
    var total = 0;
    Object.keys(DENOMINATIONS).forEach(function (name) {
      total += (currency[name] || 0) * DENOMINATIONS[name];
    });
    return total;
  }

  // --- maths and text ------------------------------------------------------

  /** SRD modifier: floor((score - 10) / 2), correct for scores below 10. */
  function abilityModifier(score) {
    return Math.floor((score - 10) / 2);
  }

  function signed(value) {
    return (value >= 0 ? '+' : '') + value;
  }

  function titleCase(text) {
    return String(text || '')
      .replace(/[_-]+/g, ' ')
      .replace(/\b\w/g, function (character) { return character.toUpperCase(); });
  }

  function readable(text) {
    return String(text || '').replace(/_/g, ' ');
  }

  function clamp(value, low, high) {
    return Math.min(high, Math.max(low, value));
  }

  /** Deep copy of plain JSON data -- the shape everything in the engine uses. */
  function deepCopy(value) {
    return value === undefined ? value : JSON.parse(JSON.stringify(value));
  }

  // --- the random source ---------------------------------------------------

  /**
   * A seedable pseudo-random generator (mulberry32).
   *
   * Seeded explicitly it is fully deterministic, which is what makes a whole
   * campaign reproducible; seeded with null it takes a random seed and keeps
   * it, so even an "unseeded" campaign can be replayed afterwards.
   */
  function Rng(seed) {
    var chosen = (seed === null || seed === undefined)
      ? Math.floor(Math.random() * 0xffffffff)
      : seed;
    this.seed(chosen);
  }

  Rng.prototype.seed = function (value) {
    this._seed = value >>> 0;
    this._state = this._seed;
    return this._seed;
  };

  Rng.prototype.next = function () {
    this._state = (this._state + 0x6d2b79f5) >>> 0;
    var t = this._state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };

  /** Inclusive on both ends, like Python's random.randint. */
  Rng.prototype.randint = function (low, high) {
    return low + Math.floor(this.next() * (high - low + 1));
  };

  Rng.prototype.randrange = function (size) {
    return Math.floor(this.next() * size);
  };

  Rng.prototype.choice = function (items) {
    return items[this.randrange(items.length)];
  };

  // --- errors --------------------------------------------------------------

  /**
   * One error type per subsystem, so a client can tell a rules refusal
   * ("the rogue cannot take that skill") from a genuine bug.
   */
  function defineError(name) {
    function EngineError(message) {
      this.name = name;
      this.message = message;
      this.stack = (new Error(message)).stack;
    }
    EngineError.prototype = Object.create(Error.prototype);
    EngineError.prototype.constructor = EngineError;
    EngineError.prototype.toString = function () {
      return this.name + ': ' + this.message;
    };
    return EngineError;
  }

  DMAI.newId = newId;
  DMAI.utcnow = utcnow;
  DMAI.Ability = Ability;
  DMAI.ABILITIES = ABILITIES;
  DMAI.ABILITY_ABBR = ABILITY_ABBR;
  DMAI.SKILL_ABILITIES = SKILL_ABILITIES;
  DMAI.SKILLS = SKILLS;
  DMAI.DamageType = DamageType;
  DMAI.ConditionType = ConditionType;
  DMAI.CONDITION_TYPES = CONDITION_TYPES;
  DMAI.Difficulty = Difficulty;
  DMAI.Visibility = Visibility;
  DMAI.CreatureKind = CreatureKind;
  DMAI.ItemKind = ItemKind;
  DMAI.Attitude = Attitude;
  DMAI.QuestStatus = QuestStatus;
  DMAI.MemoryTier = MemoryTier;
  DMAI.MemoryKind = MemoryKind;
  DMAI.STORY_TONES = STORY_TONES;
  DMAI.NARRATIVE_STYLES = NARRATIVE_STYLES;
  DMAI.DM_BEHAVIORS = DM_BEHAVIORS;
  DMAI.DENOMINATIONS = DENOMINATIONS;
  DMAI.newCurrency = newCurrency;
  DMAI.totalInCopper = totalInCopper;
  DMAI.abilityModifier = abilityModifier;
  DMAI.signed = signed;
  DMAI.titleCase = titleCase;
  DMAI.readable = readable;
  DMAI.clamp = clamp;
  DMAI.deepCopy = deepCopy;
  DMAI.Rng = Rng;
  DMAI.defineError = defineError;
})(window.DMAI = window.DMAI || {});
