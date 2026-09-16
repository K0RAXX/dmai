/* Deterministic, auditable dice -- a port of dmai/engine/dice.py.
 *
 * Two rules govern this module:
 *
 * 1. Every roll returns a result carrying the individual dice, so any number
 *    that reaches a player can be traced back to physical d20s.
 * 2. The random source is injectable and seedable, so the entire game engine
 *    is reproducible.
 *
 * The DM never produces a number.  It produces a request; this module
 * produces the result.
 */
(function (DMAI) {
  'use strict';

  var RollMode = { NORMAL: 'normal', ADVANTAGE: 'advantage', DISADVANTAGE: 'disadvantage' };

  var Outcome = {
    SUCCESS: 'success',
    FAILURE: 'failure',
    CRITICAL_SUCCESS: 'critical_success',
    CRITICAL_FAILURE: 'critical_failure',
    //: Rolls with no target number (damage, healing, a wandering-monster roll).
    NOT_APPLICABLE: 'n/a'
  };

  var MAX_DICE = 1000;
  var MAX_SIDES = 1000;

  var DiceError = DMAI.defineError('DiceError');

  //: A term in a dice expression: '2d6', 'd20', '-1d4', '+3', '-2', 'd%'.
  var TERM = /([+-])?\s*(?:(\d+)?\s*[dD]\s*(\d+|%)|(\d+))/g;

  /** Parse '2d6+3' into its terms.  Throws DiceError on anything unsupported. */
  function parse(expression) {
    var text = String(expression == null ? '' : expression).replace(/\s+/g, '');
    if (!text) {
      throw new DiceError('empty dice expression');
    }

    var terms = [];
    var position = 0;
    TERM.lastIndex = 0;

    while (position < text.length) {
      TERM.lastIndex = position;
      var match = TERM.exec(text);
      if (!match || match.index !== position || TERM.lastIndex === position) {
        throw new DiceError(
          'could not parse "' + expression + '" at position ' + position
        );
      }
      position = TERM.lastIndex;

      var sign = match[1] === '-' ? -1 : 1;

      if (match[4] !== undefined) {
        terms.push({ sign: sign, count: 0, sides: 0, flat: parseInt(match[4], 10) });
        continue;
      }

      var sides = match[3] === '%' ? 100 : parseInt(match[3], 10);
      var count = match[2] === undefined ? 1 : parseInt(match[2], 10);

      if (count < 1 || count > MAX_DICE) {
        throw new DiceError(
          'dice count out of range in "' + expression + '": ' + count
        );
      }
      if (sides < 2 || sides > MAX_SIDES) {
        throw new DiceError(
          'die size out of range in "' + expression + '": d' + sides
        );
      }
      terms.push({ sign: sign, count: count, sides: sides, flat: 0 });
    }

    if (!terms.length) {
      throw new DiceError('no terms found in "' + expression + '"');
    }
    return terms;
  }

  /** Expected value of an expression, used for encounter budgeting. */
  function average(expression) {
    return parse(expression).reduce(function (total, term) {
      return total + (term.count > 0
        ? term.sign * term.count * (term.sides + 1) / 2
        : term.sign * term.flat);
    }, 0);
  }

  // --- results -------------------------------------------------------------

  function keptDice(result) {
    return result.dice.filter(function (die) { return !die.dropped; });
  }

  /** Sum of the dice before modifiers. */
  function naturalTotal(result) {
    return keptDice(result).reduce(function (sum, die) { return sum + die.value; }, 0);
  }

  /** The block shown in the narrative log. */
  function describeRoll(result) {
    var faces = result.dice.map(function (die) {
      return die.value + (die.dropped ? '*' : '');
    }).join(', ');

    var lines = [
      String(result.reason || 'roll').toUpperCase(),
      result.expression + ': ' + faces,
      'Modifier: ' + DMAI.signed(result.modifier),
      'Total: ' + result.total
    ];
    if (result.dc !== null && result.dc !== undefined) {
      lines.push('DC: ' + result.dc);
    }
    if (result.outcome !== Outcome.NOT_APPLICABLE) {
      lines.push(result.outcome.replace(/_/g, ' ').toUpperCase());
    }
    return lines.join('\n');
  }

  /** One line rather than a block -- what a narrator wants. */
  function summariseRoll(result) {
    var parts = [(result.reason || 'roll') + ': ' + result.total];
    if (result.dc !== null && result.dc !== undefined) {
      parts.push('against DC ' + result.dc);
    }
    if (result.outcome && result.outcome !== Outcome.NOT_APPLICABLE) {
      parts.push(result.outcome.replace(/_/g, ' '));
    }
    return parts.join(' -- ');
  }

  // --- the engine ----------------------------------------------------------

  /** Rolls dice.  Seed it to make a whole campaign reproducible. */
  function DiceEngine(seed, rng) {
    this.rng = rng || new DMAI.Rng(seed === undefined ? null : seed);
    this._seed = this.rng._seed;
  }

  DiceEngine.prototype.reseed = function (seed) {
    this._seed = this.rng.seed(seed);
    return this._seed;
  };

  DiceEngine.prototype._rollDie = function (sides) {
    return this.rng.randint(1, sides);
  };

  /** Roll an expression and return the full audit trail. */
  DiceEngine.prototype.roll = function (expression, options) {
    var settings = options || {};
    var mode = settings.mode || RollMode.NORMAL;
    var terms = parse(expression);
    var dice = [];
    var modifier = 0;
    var total = 0;
    var self = this;

    // Advantage applies to the first single d20 in the expression.
    var advantageApplied = mode === RollMode.NORMAL;

    terms.forEach(function (term) {
      if (term.count === 0) {
        modifier += term.sign * term.flat;
        total += term.sign * term.flat;
        return;
      }

      var rolled = [];

      if (!advantageApplied && term.sides === 20 && term.count === 1) {
        var pair = [self._rollDie(20), self._rollDie(20)];
        var keep = mode === RollMode.ADVANTAGE
          ? Math.max(pair[0], pair[1])
          : Math.min(pair[0], pair[1]);
        var keptOnce = false;
        pair.forEach(function (value) {
          // Keep exactly one die matching the target value.
          if (value === keep && !keptOnce) {
            rolled.push({ sides: 20, value: value, dropped: false });
            keptOnce = true;
          } else {
            rolled.push({ sides: 20, value: value, dropped: true });
          }
        });
        advantageApplied = true;
      } else {
        for (var index = 0; index < term.count; index += 1) {
          rolled.push({ sides: term.sides, value: self._rollDie(term.sides), dropped: false });
        }
      }

      dice = dice.concat(rolled);
      total += term.sign * rolled.reduce(function (sum, die) {
        return sum + (die.dropped ? 0 : die.value);
      }, 0);
    });

    var result = {
      expression: expression,
      reason: settings.reason || 'roll',
      mode: mode,
      dice: dice,
      modifier: modifier,
      total: total,
      dc: settings.dc === undefined ? null : settings.dc,
      outcome: Outcome.NOT_APPLICABLE,
      actor_id: settings.actor_id || null,
      target_id: settings.target_id || null
    };
    result.outcome = judge(result);
    return result;
  };

  /** Roll a request produced by the DM layer. */
  DiceEngine.prototype.resolve = function (request) {
    return this.roll(request.expression, {
      reason: request.reason,
      mode: request.mode,
      dc: request.dc,
      actor_id: request.actor_id,
      target_id: request.target_id
    });
  };

  /** Natural 20/1 on a single d20 are critical regardless of the DC. */
  function judge(result) {
    var twenties = keptDice(result).filter(function (die) { return die.sides === 20; });
    if (twenties.length === 1) {
      if (twenties[0].value === 20) { return Outcome.CRITICAL_SUCCESS; }
      if (twenties[0].value === 1) { return Outcome.CRITICAL_FAILURE; }
    }
    if (result.dc === null || result.dc === undefined) {
      return Outcome.NOT_APPLICABLE;
    }
    return result.total >= result.dc ? Outcome.SUCCESS : Outcome.FAILURE;
  }

  /** One place to ask "did that work", so criticals are never mishandled. */
  function succeeded(result) {
    return result.outcome === Outcome.SUCCESS
      || result.outcome === Outcome.CRITICAL_SUCCESS;
  }

  DMAI.RollMode = RollMode;
  DMAI.Outcome = Outcome;
  DMAI.DiceError = DiceError;
  DMAI.DiceEngine = DiceEngine;
  DMAI.parseDice = parse;
  DMAI.averageDice = average;
  DMAI.describeRoll = describeRoll;
  DMAI.summariseRoll = summariseRoll;
  DMAI.keptDice = keptDice;
  DMAI.naturalTotal = naturalTotal;
  DMAI.succeeded = succeeded;
})(window.DMAI = window.DMAI || {});
