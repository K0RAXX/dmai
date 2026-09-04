"""Dice correctness.  Everything downstream trusts these numbers."""

from __future__ import annotations

import pytest

from dmai.engine.dice import DiceEngine, DiceError, average, parse
from dmai.engine.models.dice import Outcome, RollMode


@pytest.fixture
def dice() -> DiceEngine:
    return DiceEngine(seed=20260902)


# --- parsing ---------------------------------------------------------------

@pytest.mark.parametrize(
    "expression,expected_dice,expected_flat",
    [
        ("1d20", [(1, 20)], 0),
        ("d20", [(1, 20)], 0),
        ("2d6+3", [(2, 6)], 3),
        ("4d8+2", [(4, 8)], 2),
        ("1d20+5", [(1, 20)], 5),
        ("1d20-1", [(1, 20)], -1),
        ("d%", [(1, 100)], 0),
        ("10", [], 10),
        ("2d6+1d4+1", [(2, 6), (1, 4)], 1),
        (" 1d20 + 3 ", [(1, 20)], 3),
    ],
)
def test_parse(expression, expected_dice, expected_flat):
    terms = parse(expression)
    dice_terms = [(t.count, t.sides) for t in terms if t.is_dice]
    flat = sum(t.sign * t.flat for t in terms if not t.is_dice)
    assert dice_terms == expected_dice
    assert flat == expected_flat


@pytest.mark.parametrize("bad", ["", "   ", "d", "1d", "2d0", "1d1", "0d6", "abc", "1d20++3"])
def test_parse_rejects_nonsense(bad):
    with pytest.raises(DiceError):
        parse(bad)


def test_parse_rejects_absurd_quantities():
    with pytest.raises(DiceError):
        parse("99999d6")
    with pytest.raises(DiceError):
        parse("1d99999")


# --- rolling ---------------------------------------------------------------

def test_die_values_are_in_range(dice):
    for _ in range(200):
        result = dice.roll("1d20")
        assert 1 <= result.dice[0].value <= 20
        assert result.total == result.dice[0].value


def test_modifier_is_added_and_reported_separately(dice):
    result = dice.roll("1d20+7")
    assert result.modifier == 7
    assert result.total == result.natural + 7


def test_negative_modifier(dice):
    result = dice.roll("1d20-3")
    assert result.modifier == -3
    assert result.total == result.natural - 3


def test_multiple_dice_are_all_recorded(dice):
    result = dice.roll("4d8+2")
    assert len(result.dice) == 4
    assert all(d.sides == 8 for d in result.dice)
    assert result.total == sum(d.value for d in result.dice) + 2


def test_percentile(dice):
    result = dice.roll("d%")
    assert result.dice[0].sides == 100
    assert 1 <= result.total <= 100


def test_mixed_dice_expression(dice):
    result = dice.roll("2d6+1d4+1")
    assert sorted(d.sides for d in result.dice) == [4, 6, 6]
    assert result.total == sum(d.value for d in result.dice) + 1


# --- determinism -----------------------------------------------------------

def test_same_seed_gives_same_sequence():
    a = DiceEngine(seed=99)
    b = DiceEngine(seed=99)
    assert [a.roll("1d20").total for _ in range(50)] == [
        b.roll("1d20").total for _ in range(50)
    ]


def test_different_seeds_diverge():
    a = [DiceEngine(seed=1).roll("1d20").total for _ in range(30)]
    b = [DiceEngine(seed=2).roll("1d20").total for _ in range(30)]
    assert a != b


# --- advantage / disadvantage ---------------------------------------------

def test_advantage_rolls_two_dice_and_keeps_the_higher(dice):
    for _ in range(100):
        result = dice.roll("1d20+2", mode=RollMode.ADVANTAGE)
        assert len(result.dice) == 2
        kept = result.kept_dice
        assert len(kept) == 1
        assert kept[0].value == max(d.value for d in result.dice)
        assert result.total == kept[0].value + 2


def test_disadvantage_keeps_the_lower(dice):
    for _ in range(100):
        result = dice.roll("1d20", mode=RollMode.DISADVANTAGE)
        assert len(result.dice) == 2
        assert result.kept_dice[0].value == min(d.value for d in result.dice)


def test_advantage_does_not_apply_to_damage_dice(dice):
    """Advantage is a d20 mechanic; 2d6 damage stays 2d6."""
    result = dice.roll("2d6+3", mode=RollMode.ADVANTAGE)
    assert len(result.dice) == 2
    assert not any(d.dropped for d in result.dice)


# --- outcomes --------------------------------------------------------------

def test_success_and_failure_against_dc():
    """Stub the die to a middling 10 so criticals do not overrule the DC."""
    engine = DiceEngine(seed=0)
    engine._roll_die = lambda sides: 10  # type: ignore[method-assign]
    assert engine.roll("1d20+8", dc=15).outcome is Outcome.SUCCESS   # 18 vs 15
    assert engine.roll("1d20+1", dc=15).outcome is Outcome.FAILURE   # 11 vs 15
    assert engine.roll("1d20+5", dc=15).outcome is Outcome.SUCCESS   # 15 ties, succeeds


def test_no_dc_means_no_verdict(dice):
    result = dice.roll("2d6+3", reason="longsword damage")
    assert result.outcome is Outcome.NOT_APPLICABLE


def test_natural_twenty_and_one_are_critical():
    """Force the extremes with a stubbed source rather than rolling forever."""
    engine = DiceEngine(seed=0)
    engine._roll_die = lambda sides: 20  # type: ignore[method-assign]
    assert engine.roll("1d20+0", dc=99).outcome is Outcome.CRITICAL_SUCCESS

    engine._roll_die = lambda sides: 1  # type: ignore[method-assign]
    assert engine.roll("1d20+0", dc=1).outcome is Outcome.CRITICAL_FAILURE


# --- audit trail -----------------------------------------------------------

def test_result_describes_itself(dice):
    result = dice.roll("1d20+4", reason="Perception check", dc=15)
    text = result.describe()
    assert "PERCEPTION CHECK" in text
    assert "Modifier: +4" in text
    assert f"Total: {result.total}" in text
    assert "DC: 15" in text


def test_result_never_hides_its_dice(dice):
    """A player must always be able to see the physical dice."""
    for expression in ("1d20+5", "2d6+3", "4d8+2", "d%"):
        result = dice.roll(expression)
        assert result.dice, f"{expression} produced a bare number"
        assert result.total == result.natural + result.modifier


# --- statistics ------------------------------------------------------------

def test_average_matches_expectation():
    assert average("1d20") == 10.5
    assert average("2d6+3") == 10.0
    assert average("4d8") == 18.0
    assert average("1d6-1") == 2.5


def test_empirical_mean_is_near_the_average():
    engine = DiceEngine(seed=7)
    rolls = [engine.roll("2d6+3").total for _ in range(4000)]
    assert abs(sum(rolls) / len(rolls) - average("2d6+3")) < 0.25
