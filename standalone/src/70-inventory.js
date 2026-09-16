/* Inventory, equipment and money.
 *
 * A port of dmai/engine/inventory/manager.py.  All of it goes through the
 * journal, so "who has the amulet" is answerable at any point in the
 * campaign's history rather than only right now.
 */
(function (DMAI) {
  'use strict';

  var ItemKind = DMAI.ItemKind;
  var EventType = DMAI.EventType;

  var InventoryError = DMAI.defineError('InventoryError');

  //: Which item kinds occupy a body slot, and how many of each can be worn.
  var SLOT_LIMITS = {};
  SLOT_LIMITS[ItemKind.ARMOR] = 1;
  SLOT_LIMITS[ItemKind.SHIELD] = 1;

  /** Gives, takes, equips and pays.  Armour class is recomputed on equip. */
  function InventoryManager(rules) {
    this.rules = rules;
  }

  InventoryManager.prototype._require = function (journal, creatureId) {
    var creature = DMAI.findCreature(journal.state, creatureId);
    if (!creature) {
      throw new InventoryError('no such creature: ' + creatureId);
    }
    return creature;
  };

  function findItem(creature, itemId) {
    return (creature.inventory || []).filter(function (item) {
      return item.id === itemId;
    })[0] || null;
  }

  // --- building items from the rules pack ----------------------------------

  /** Look a key up in the active pack and build a real item from it. */
  InventoryManager.prototype.itemFromPack = function (key, quantity) {
    var weapon = this.rules.weapon(key);
    if (weapon) {
      return DMAI.newItem({
        name: weapon.name,
        kind: ItemKind.WEAPON,
        quantity: quantity || 1,
        weight: weapon.weight || 0,
        value_gp: weapon.cost_gp || 0,
        properties: Object.assign({ key: key }, weapon)
      });
    }
    var armor = this.rules.armor(key);
    if (armor) {
      return DMAI.newItem({
        name: armor.name,
        kind: armor.type === 'shield' ? ItemKind.SHIELD : ItemKind.ARMOR,
        quantity: quantity || 1,
        weight: armor.weight || 0,
        value_gp: armor.cost_gp || 0,
        properties: Object.assign({ key: key }, armor)
      });
    }
    throw new InventoryError(
      'no item "' + key + '" in rules pack ' + this.rules.id
    );
  };

  // --- moving items --------------------------------------------------------

  /** Put an item in someone's pack.  Accepts a pack key or a built item. */
  InventoryManager.prototype.give = function (journal, creatureId, item, options) {
    var settings = options || {};
    var quantity = settings.quantity === undefined ? 1 : settings.quantity;
    var reason = settings.reason || '';
    var creature = this._require(journal, creatureId);

    if (typeof item === 'string') {
      item = this.itemFromPack(item, quantity);
    } else {
      item = DMAI.deepCopy(item);
      item.id = item.id || DMAI.newId('item');
      if (quantity !== 1) { item.quantity = quantity; }
    }

    journal.record(EventType.ITEM_GAINED, {
      target_id: creatureId,
      creature_id: creatureId,
      summary: creature.name + ' gains ' + item.quantity + 'x ' + item.name
        + (reason ? ' (' + reason + ')' : '') + '.',
      item: item,
      reason: reason
    });
    return item;
  };

  /** Remove an item, or part of a stack.  quantity=0 removes it all. */
  InventoryManager.prototype.take = function (journal, creatureId, itemId, options) {
    var settings = options || {};
    var quantity = settings.quantity || 0;
    var creature = this._require(journal, creatureId);
    var item = findItem(creature, itemId);
    if (!item) {
      throw new InventoryError(creature.name + ' is not carrying ' + itemId);
    }
    if (quantity && quantity > item.quantity) {
      throw new InventoryError(
        creature.name + ' has ' + item.quantity + 'x ' + item.name
        + ', not ' + quantity
      );
    }

    journal.record(EventType.ITEM_LOST, {
      target_id: creatureId,
      creature_id: creatureId,
      summary: creature.name + ' loses ' + (quantity || item.quantity)
        + 'x ' + item.name + '.',
      item_id: itemId,
      item_name: item.name,
      quantity: quantity,
      reason: settings.reason || ''
    });
  };

  /** Hand something over.  Two events, so both sheets tell the story. */
  InventoryManager.prototype.transfer = function (journal, fromId, toId, itemId, options) {
    var settings = options || {};
    var giver = this._require(journal, fromId);
    var item = findItem(giver, itemId);
    if (!item) {
      throw new InventoryError(giver.name + ' is not carrying ' + itemId);
    }

    var moved = DMAI.deepCopy(item);
    moved.id = DMAI.newId('item');
    moved.quantity = settings.quantity || item.quantity;
    moved.equipped = false;

    this.take(journal, fromId, itemId, {
      quantity: settings.quantity || 0,
      reason: settings.reason || 'given away'
    });
    return this.give(journal, toId, moved, { reason: settings.reason || 'received' });
  };

  // --- equipping -----------------------------------------------------------

  /**
   * Wear or wield an item; returns the resulting armour class.
   *
   * Armour and shields occupy a slot, so equipping a breastplate takes the
   * chain mail off rather than stacking both.
   */
  InventoryManager.prototype.equip = function (journal, creatureId, itemId) {
    var creature = this._require(journal, creatureId);
    var item = findItem(creature, itemId);
    if (!item) {
      throw new InventoryError(creature.name + ' is not carrying ' + itemId);
    }

    var limit = SLOT_LIMITS[item.kind];
    if (limit !== undefined) {
      var worn = creature.inventory.filter(function (candidate) {
        return candidate.equipped && candidate.kind === item.kind;
      });
      var self = this;
      worn.slice(0, Math.max(0, worn.length - limit + 1)).forEach(function (displaced) {
        if (displaced.id !== itemId) {
          self.unequip(journal, creatureId, displaced.id);
        }
      });
    }

    var probe = DMAI.deepCopy(this._require(journal, creatureId));
    probe.inventory.forEach(function (candidate) {
      if (candidate.id === itemId) { candidate.equipped = true; }
    });
    var armorClass = this.rules.armorClass(probe);

    journal.record(EventType.ITEM_EQUIPPED, {
      target_id: creatureId,
      creature_id: creatureId,
      summary: creature.name + ' equips ' + item.name + '.',
      item_id: itemId,
      item_name: item.name,
      equipped: true,
      armor_class: armorClass
    });
    return armorClass;
  };

  InventoryManager.prototype.unequip = function (journal, creatureId, itemId) {
    var creature = this._require(journal, creatureId);
    var item = findItem(creature, itemId);
    if (!item) {
      throw new InventoryError(creature.name + ' is not carrying ' + itemId);
    }

    var probe = DMAI.deepCopy(creature);
    probe.inventory.forEach(function (candidate) {
      if (candidate.id === itemId) { candidate.equipped = false; }
    });
    var armorClass = this.rules.armorClass(probe);

    journal.record(EventType.ITEM_EQUIPPED, {
      target_id: creatureId,
      creature_id: creatureId,
      summary: creature.name + ' stows ' + item.name + '.',
      item_id: itemId,
      item_name: item.name,
      equipped: false,
      armor_class: armorClass
    });
    return armorClass;
  };

  // --- money ---------------------------------------------------------------

  /** Add or subtract coins by denomination.  Refuses to go negative. */
  InventoryManager.prototype.adjustCurrency = function (journal, creatureId, deltas, reason) {
    var creature = this._require(journal, creatureId);
    var purse = DMAI.deepCopy(creature.currency);

    Object.keys(deltas).forEach(function (denomination) {
      if (DMAI.DENOMINATIONS[denomination] === undefined) {
        throw new InventoryError('unknown denomination: ' + denomination);
      }
      var value = purse[denomination] + parseInt(deltas[denomination], 10);
      if (value < 0) {
        throw new InventoryError(
          creature.name + ' has ' + purse[denomination] + ' ' + denomination
          + ', cannot spend ' + Math.abs(parseInt(deltas[denomination], 10))
        );
      }
      purse[denomination] = value;
    });

    journal.record(EventType.CURRENCY_CHANGED, {
      target_id: creatureId,
      creature_id: creatureId,
      summary: creature.name + "'s purse changes"
        + (reason ? ' (' + reason + ')' : '') + '.',
      currency: purse,
      deltas: deltas,
      reason: reason || ''
    });
    return purse;
  };

  /**
   * Spend a price quoted in gold, making change from smaller coins.
   *
   * Prices are quoted in gold pieces and purses are not, so the cost is
   * converted to copper and taken from the smallest coins first -- which is
   * both correct and what a shopkeeper would do.
   */
  InventoryManager.prototype.pay = function (journal, creatureId, gold, reason) {
    var creature = this._require(journal, creatureId);
    var owed = Math.round(gold * 100);
    var purse = creature.currency;
    var available = DMAI.totalInCopper(purse);

    if (available < owed) {
      throw new InventoryError(
        creature.name + ' cannot afford ' + gold + ' gp (has '
        + (available / 100).toFixed(2) + ' gp)'
      );
    }

    var remaining = owed;
    var deltas = {};
    var names = Object.keys(DMAI.DENOMINATIONS);  // smallest first

    names.forEach(function (denomination) {
      if (remaining <= 0) { return; }
      var worth = DMAI.DENOMINATIONS[denomination];
      var spend = Math.min(purse[denomination], Math.floor(remaining / worth));
      if (spend) {
        deltas[denomination] = -spend;
        remaining -= spend * worth;
      }
    });

    // Anything left needs a coin broken.  Break the *smallest* one that covers
    // the shortfall and hand back the change, the way a shopkeeper would --
    // breaking a platinum piece to pay three copper is not it.
    if (remaining > 0) {
      for (var index = 0; index < names.length; index += 1) {
        var denomination = names[index];
        var worth = DMAI.DENOMINATIONS[denomination];
        var held = purse[denomination] + (deltas[denomination] || 0);
        if (held > 0 && worth >= remaining) {
          deltas[denomination] = (deltas[denomination] || 0) - 1;
          var change = worth - remaining;
          remaining = 0;
          if (change) { deltas.copper = (deltas.copper || 0) + change; }
          break;
        }
      }
    }

    if (remaining > 0) {
      throw new InventoryError(
        creature.name + ' cannot make ' + gold + ' gp in coin'
      );
    }
    return this.adjustCurrency(
      journal, creatureId, deltas, reason || ('paid ' + gold + ' gp')
    );
  };

  /** Buy from the rules pack: pay the price, then take the goods. */
  InventoryManager.prototype.buy = function (journal, creatureId, key, quantity) {
    var count = quantity || 1;
    var item = this.itemFromPack(key, count);
    this.pay(journal, creatureId, item.value_gp * count, 'bought ' + item.name);
    return this.give(journal, creatureId, item, { quantity: count, reason: 'purchased' });
  };

  // --- reading -------------------------------------------------------------

  function carriedWeight(creature) {
    return (creature.inventory || []).reduce(function (total, item) {
      return total + item.weight * item.quantity;
    }, 0);
  }

  function equippedItems(creature) {
    return (creature.inventory || []).filter(function (item) {
      return item.equipped;
    });
  }

  /**
   * What this creature is actually wearing and holding, by slot.
   *
   * The character sheet needs this shape rather than a flat list: "plate
   * armour, a shield, and a longsword in hand" is a picture; a list of three
   * items with a boolean is not.
   */
  function equipmentSlots(creature) {
    var worn = equippedItems(creature);
    var body = null;
    var shield = null;
    var weapons = [];
    var other = [];

    worn.forEach(function (item) {
      if (item.kind === ItemKind.ARMOR) {
        if (!body) { body = item; } else { other.push(item); }
      } else if (item.kind === ItemKind.SHIELD) {
        if (!shield) { shield = item; } else { other.push(item); }
      } else if (item.kind === ItemKind.WEAPON) {
        weapons.push(item);
      } else {
        other.push(item);
      }
    });

    return { armor: body, shield: shield, weapons: weapons, other: other };
  }

  /** Carrying capacity, and whether this creature is over it. */
  function encumbrance(creature) {
    var capacity = creature.abilities.strength * 15;
    var carried = carriedWeight(creature);
    return {
      carried: carried,
      capacity: capacity,
      //: The SRD's variant thresholds: encumbered at 5x STR, heavily at 10x.
      encumbered: carried > creature.abilities.strength * 5,
      heavily_encumbered: carried > creature.abilities.strength * 10,
      overloaded: carried > capacity
    };
  }

  DMAI.InventoryError = InventoryError;
  DMAI.InventoryManager = InventoryManager;
  DMAI.SLOT_LIMITS = SLOT_LIMITS;
  DMAI.carriedWeight = carriedWeight;
  DMAI.equippedItems = equippedItems;
  DMAI.equipmentSlots = equipmentSlots;
  DMAI.encumbrance = encumbrance;
})(window.DMAI = window.DMAI || {});
