"""Inventory, equipment and money.

All of it goes through the journal, so "who has the amulet" is answerable at
any point in the campaign's history rather than only right now.
"""

from __future__ import annotations

from ..models.base import Currency
from ..models.character import Creature, Item, ItemKind
from ..models.events import EventType
from ..rules.base import RulesEngine
from ..state import Journal

#: Which item kinds occupy a body slot, and how many of each can be worn.
SLOT_LIMITS = {ItemKind.ARMOR: 1, ItemKind.SHIELD: 1}

#: Coin denominations in ascending value, with their worth in copper.
DENOMINATIONS = {
    "copper": 1,
    "silver": 10,
    "electrum": 50,
    "gold": 100,
    "platinum": 1000,
}


class InventoryError(ValueError):
    """The item cannot be moved, equipped or afforded as requested."""


class InventoryManager:
    """Gives, takes, equips and pays.  Armour class is recomputed on equip."""

    def __init__(self, rules: RulesEngine):
        self.rules = rules

    def _require(self, journal: Journal, creature_id: str) -> Creature:
        creature = journal.state.creature(creature_id)
        if creature is None:
            raise InventoryError(f"no such creature: {creature_id}")
        return creature

    # --- building items from the rules pack --------------------------------

    def item_from_pack(self, key: str, quantity: int = 1) -> Item:
        """Look a key up in the active pack and build a real item from it."""
        weapon = self.rules.weapon(key)
        if weapon is not None:
            return Item(
                name=weapon["name"],
                kind=ItemKind.WEAPON,
                quantity=quantity,
                weight=float(weapon.get("weight", 0)),
                value_gp=float(weapon.get("cost_gp", 0)),
                properties={"key": key, **weapon},
            )
        armor = self.rules.armor(key)
        if armor is not None:
            return Item(
                name=armor["name"],
                kind=ItemKind.SHIELD if armor.get("type") == "shield" else ItemKind.ARMOR,
                quantity=quantity,
                weight=float(armor.get("weight", 0)),
                value_gp=float(armor.get("cost_gp", 0)),
                properties={"key": key, **armor},
            )
        raise InventoryError(f"no item {key!r} in rules pack {self.rules.id}")

    # --- moving items ------------------------------------------------------

    def give(
        self,
        journal: Journal,
        creature_id: str,
        item: Item | str,
        *,
        quantity: int = 1,
        reason: str = "",
    ) -> Item:
        """Put an item in someone's pack.  Accepts a pack key or a built item."""
        creature = self._require(journal, creature_id)
        if isinstance(item, str):
            item = self.item_from_pack(item, quantity)
        elif quantity != 1:
            item = item.model_copy(update={"quantity": quantity})

        journal.record(
            EventType.ITEM_GAINED,
            target_id=creature_id,
            creature_id=creature_id,
            summary=(
                f"{creature.name} gains {item.quantity}x {item.name}"
                f"{f' ({reason})' if reason else ''}."
            ),
            item=item.model_dump(mode="json"),
            reason=reason,
        )
        return item

    def take(
        self,
        journal: Journal,
        creature_id: str,
        item_id: str,
        *,
        quantity: int = 0,
        reason: str = "",
    ) -> None:
        """Remove an item, or part of a stack.  ``quantity=0`` removes it all."""
        creature = self._require(journal, creature_id)
        item = next((i for i in creature.inventory if i.id == item_id), None)
        if item is None:
            raise InventoryError(f"{creature.name} is not carrying {item_id}")
        if quantity and quantity > item.quantity:
            raise InventoryError(
                f"{creature.name} has {item.quantity}x {item.name}, not {quantity}"
            )

        journal.record(
            EventType.ITEM_LOST,
            target_id=creature_id,
            creature_id=creature_id,
            summary=f"{creature.name} loses {quantity or item.quantity}x {item.name}.",
            item_id=item_id,
            item_name=item.name,
            quantity=quantity,
            reason=reason,
        )

    def transfer(
        self,
        journal: Journal,
        from_id: str,
        to_id: str,
        item_id: str,
        *,
        quantity: int = 0,
        reason: str = "",
    ) -> Item:
        """Hand something over.  Two events, so both sheets tell the story."""
        giver = self._require(journal, from_id)
        item = next((i for i in giver.inventory if i.id == item_id), None)
        if item is None:
            raise InventoryError(f"{giver.name} is not carrying {item_id}")

        moved = item.model_copy(deep=True)
        moved.quantity = quantity or item.quantity
        moved.equipped = False
        self.take(journal, from_id, item_id, quantity=quantity, reason=reason or "given away")
        return self.give(journal, to_id, moved, reason=reason or "received")

    # --- equipping ---------------------------------------------------------

    def equip(self, journal: Journal, creature_id: str, item_id: str) -> int:
        """Wear or wield an item; returns the resulting armour class.

        Armour and shields occupy a slot, so equipping a breastplate takes the
        chain mail off rather than stacking both.
        """
        creature = self._require(journal, creature_id)
        item = next((i for i in creature.inventory if i.id == item_id), None)
        if item is None:
            raise InventoryError(f"{creature.name} is not carrying {item_id}")

        limit = SLOT_LIMITS.get(item.kind)
        if limit is not None:
            worn = [i for i in creature.inventory if i.equipped and i.kind == item.kind]
            for displaced in worn[: max(0, len(worn) - limit + 1)]:
                if displaced.id != item_id:
                    self.unequip(journal, creature_id, displaced.id)

        probe = self._require(journal, creature_id).model_copy(deep=True)
        for candidate in probe.inventory:
            if candidate.id == item_id:
                candidate.equipped = True
        armor_class = self.rules.armor_class(probe)

        journal.record(
            EventType.ITEM_EQUIPPED,
            target_id=creature_id,
            creature_id=creature_id,
            summary=f"{creature.name} equips {item.name}.",
            item_id=item_id,
            item_name=item.name,
            equipped=True,
            armor_class=armor_class,
        )
        return armor_class

    def unequip(self, journal: Journal, creature_id: str, item_id: str) -> int:
        creature = self._require(journal, creature_id)
        item = next((i for i in creature.inventory if i.id == item_id), None)
        if item is None:
            raise InventoryError(f"{creature.name} is not carrying {item_id}")

        probe = creature.model_copy(deep=True)
        for candidate in probe.inventory:
            if candidate.id == item_id:
                candidate.equipped = False
        armor_class = self.rules.armor_class(probe)

        journal.record(
            EventType.ITEM_EQUIPPED,
            target_id=creature_id,
            creature_id=creature_id,
            summary=f"{creature.name} stows {item.name}.",
            item_id=item_id,
            item_name=item.name,
            equipped=False,
            armor_class=armor_class,
        )
        return armor_class

    # --- money -------------------------------------------------------------

    def adjust_currency(
        self,
        journal: Journal,
        creature_id: str,
        *,
        reason: str = "",
        **deltas: int,
    ) -> Currency:
        """Add or subtract coins by denomination.  Refuses to go negative."""
        creature = self._require(journal, creature_id)
        purse = creature.currency.model_copy(deep=True)

        for denomination, delta in deltas.items():
            if denomination not in DENOMINATIONS:
                raise InventoryError(f"unknown denomination: {denomination}")
            new_value = getattr(purse, denomination) + int(delta)
            if new_value < 0:
                raise InventoryError(
                    f"{creature.name} has {getattr(purse, denomination)} {denomination}, "
                    f"cannot spend {abs(int(delta))}"
                )
            setattr(purse, denomination, new_value)

        journal.record(
            EventType.CURRENCY_CHANGED,
            target_id=creature_id,
            creature_id=creature_id,
            summary=f"{creature.name}'s purse changes{f' ({reason})' if reason else ''}.",
            currency=purse.model_dump(mode="json"),
            deltas=dict(deltas),
            reason=reason,
        )
        return purse

    def pay(
        self, journal: Journal, creature_id: str, gold: float, *, reason: str = ""
    ) -> Currency:
        """Spend a price quoted in gold, making change from smaller coins.

        Prices are quoted in gold pieces and purses are not, so the cost is
        converted to copper and taken from the smallest coins first -- which
        is both correct and what a shopkeeper would do.
        """
        creature = self._require(journal, creature_id)
        owed = int(round(gold * 100))
        purse = creature.currency
        if purse.total_in_copper < owed:
            raise InventoryError(
                f"{creature.name} cannot afford {gold} gp "
                f"(has {purse.total_in_copper / 100:.2f} gp)"
            )

        remaining = owed
        deltas: dict[str, int] = {}
        for denomination, worth in DENOMINATIONS.items():  # smallest first
            if remaining <= 0:
                break
            available = getattr(purse, denomination)
            spend = min(available, remaining // worth)
            if spend:
                deltas[denomination] = -spend
                remaining -= spend * worth

        # Anything left needs a coin broken.  Break the *smallest* one that
        # covers the shortfall and hand back the change, the way a shopkeeper
        # would -- breaking a platinum piece to pay three copper is not it.
        if remaining > 0:
            for denomination, worth in DENOMINATIONS.items():  # ascending
                available = getattr(purse, denomination) + deltas.get(denomination, 0)
                if available > 0 and worth >= remaining:
                    deltas[denomination] = deltas.get(denomination, 0) - 1
                    change = worth - remaining
                    remaining = 0
                    if change:
                        deltas["copper"] = deltas.get("copper", 0) + change
                    break

        if remaining > 0:  # pragma: no cover - guarded by the affordability check
            raise InventoryError(f"{creature.name} cannot make {gold} gp in coin")

        return self.adjust_currency(
            journal, creature_id, reason=reason or f"paid {gold} gp", **deltas
        )

    # --- reading -----------------------------------------------------------

    @staticmethod
    def carried_weight(creature: Creature) -> float:
        return sum(item.weight * item.quantity for item in creature.inventory)

    @staticmethod
    def equipped(creature: Creature) -> list[Item]:
        return [item for item in creature.inventory if item.equipped]


__all__ = ["DENOMINATIONS", "SLOT_LIMITS", "InventoryError", "InventoryManager"]
