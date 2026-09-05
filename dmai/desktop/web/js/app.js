/* The desktop client.
 *
 * A client in the sense the engine means: it never holds game state of its
 * own.  It calls the bridge, then redraws every panel from one `view()` --
 * which is a fold of the event log -- so the party sheet, the initiative order
 * and the chronicle cannot drift out of agreement with each other.
 *
 * Layout of this file: state, then one render function per panel, then the
 * actions those panels call, then the slash commands, then wiring.
 */
(function (DMAI, UI) {
  'use strict';

  var el = UI.el, node = UI.node, fill = UI.fill, title = UI.title;

  /** The last view the bridge gave us.  Read-only as far as this file cares. */
  var view = null;
  /** Options for the creation forms, fetched once. */
  var options = null;
  /** True while a DM turn is in flight, so a second Enter cannot double-act. */
  var busy = false;
  /** How far the chronicle had been drawn, so we only append what is new. */
  var drawnSeq = -1;
  /** Recent inputs, walked with the arrow keys. */
  var history = [];
  var historyIndex = -1;

  // ======================================================================
  // The library
  // ======================================================================

  function showLibrary() {
    el('screen-table').hidden = true;
    el('screen-library').hidden = false;
    return refreshLibrary();
  }

  function refreshLibrary() {
    return DMAI.call('list_campaigns').then(function (result) {
      var shelf = el('campaign-shelf');
      el('campaign-count').textContent = result.campaigns.length
        ? '(' + result.campaigns.length + ')' : '';
      el('library-root').textContent = 'Campaigns are kept in ' + result.root;

      if (!result.campaigns.length) {
        fill(shelf, [node('div.empty', {
          text: 'No chronicles yet. Begin one, and the Dungeon Master will meet you there.'
        })]);
        return;
      }
      fill(shelf, result.campaigns.map(campaignCard));
    }).catch(fail);
  }

  function campaignCard(campaign) {
    var initial = (campaign.name || '?').trim().charAt(0).toUpperCase();

    return node('div.card.material-vellum.nailed', {
      onclick: function () { openCampaign(campaign.id); }
    }, [
      node('div.card-top', {}, [
        node('div.seal', { text: initial }),
        node('div', {}, [
          node('h3', { text: campaign.name }),
          node('p.card-premise', {
            text: campaign.premise || 'No premise recorded.'
          })
        ])
      ]),
      node('div.card-foot', {}, [
        node('span', { text: campaign.events + ' events · ' + campaign.updated_human }),
        node('div.card-actions', {}, [
          node('button.card-btn', {
            text: 'Export',
            onclick: function (event) {
              event.stopPropagation();
              exportCampaign(campaign);
            }
          }),
          node('button.card-btn', {
            text: 'Delete',
            onclick: function (event) {
              event.stopPropagation();
              confirmDelete(campaign);
            }
          })
        ])
      ])
    ]);
  }

  // ======================================================================
  // The table
  // ======================================================================

  function openCampaign(campaignId) {
    return DMAI.call('open_campaign', campaignId).then(function () {
      el('screen-library').hidden = true;
      el('screen-table').hidden = false;
      drawnSeq = -1;
      return refresh();
    }).then(function () {
      // A campaign with nobody in it cannot act, so ask for a hero first.
      if (!view.party.length) { promptCharacter(true); }
      else { el('action-input').focus(); }
    }).catch(fail);
  }

  function leaveTable() {
    return DMAI.call('close_campaign')
      .then(showLibrary)
      .catch(fail);
  }

  /** Re-read everything and redraw.  The one way any panel changes. */
  function refresh() {
    return DMAI.call('view').then(function (result) {
      view = result;
      renderBanner();
      renderParty();
      renderQuests();
      renderChronicle();
      renderInitiative();
      renderDM();
    });
  }

  // --- banner --------------------------------------------------------------

  function renderBanner() {
    el('table-name').textContent = view.campaign.name;
    el('table-where').textContent = view.where.location;
    el('table-when').textContent = view.when.time
      + (view.when.weather ? ', ' + view.when.weather : '');
  }

  // --- party ---------------------------------------------------------------

  function renderParty() {
    var host = el('party-list');
    if (!view.party.length) {
      fill(host, [node('div.empty', { text: 'Nobody has been mustered yet.' })]);
      return;
    }
    fill(host, view.party.map(heroCard));
  }

  function heroCard(hero) {
    var classes = '.hero';
    if (hero.id === view.active_character_id) { classes += '.is-active'; }
    if (hero.dead) { classes += '.is-dead'; }

    var fraction = hero.hp.fraction;
    var fillClass = '.wound-fill';
    if (fraction <= 0.25) { fillClass += '.is-bloodied'; }
    else if (fraction <= 0.6) { fillClass += '.is-hurt'; }

    var bar = node(fillClass);
    bar.style.width = Math.round(fraction * 100) + '%';

    var descriptor = [hero.species, hero.class].filter(Boolean).map(title).join(' ');

    return node('div' + classes, {
      title: 'Open ' + hero.name + "'s sheet. Double-click to play as them.",
      onclick: function () { openSheet(hero.id); },
      ondblclick: function () { playAs(hero.id); }
    }, [
      node('div.hero-top', {}, [
        node('span.hero-name', { text: hero.name }),
        node('span.hero-class', { text: 'Lv ' + hero.level })
      ]),
      node('div.hero-class', { text: descriptor || 'Adventurer' }),
      node('div.hero-stats', {}, [
        node('span.stat.hp', { svg: 'i-heart' }, [
          document.createTextNode(hero.hp.current + '/' + hero.hp.maximum)
        ]),
        node('span.stat.ac', { svg: 'i-shield' }, [
          document.createTextNode(String(hero.armor_class))
        ]),
        hero.player ? node('span.stat', { text: hero.player }) : null
      ]),
      node('div.wound-track', {}, [bar]),
      hero.conditions.length
        ? node('div.conditions', {}, hero.conditions.map(function (condition) {
            return node('span.condition', { text: condition });
          }))
        : null
    ]);
  }

  // --- quests --------------------------------------------------------------

  function renderQuests() {
    var host = el('quest-list');
    if (!view.quests.length) {
      fill(host, [node('div.empty', { text: 'No quests recorded.' })]);
      return;
    }
    fill(host, view.quests.map(function (quest) {
      return node('div.quest.is-' + quest.status, {}, [
        node('div.quest-title', {}, [
          node('span.mark', { text: '❖' }),
          document.createTextNode(quest.title)
        ]),
        quest.summary ? node('div.quest-summary', { text: quest.summary }) : null
      ].concat(quest.objectives.map(function (objective) {
        return node('div.objective' + (objective.completed ? '.is-done' : ''), {
          text: objective.description
        });
      })));
    }));
  }

  // --- the chronicle -------------------------------------------------------

  function renderChronicle() {
    var host = el('chronicle');
    var entries = view.chronicle;

    // A rollback moves the head backwards; anything but a clean append is
    // redrawn from scratch rather than reconciled.
    var appending = drawnSeq >= 0
      && entries.length
      && entries[entries.length - 1].seq >= drawnSeq
      && entries.some(function (entry) { return entry.seq === drawnSeq; });

    if (!appending) { UI.clear(host); }
    var pending = appending
      ? entries.filter(function (entry) { return entry.seq > drawnSeq; })
      : entries;

    pending.forEach(function (entry) { host.appendChild(chronicleEntry(entry)); });
    if (entries.length) { drawnSeq = entries[entries.length - 1].seq; }
    host.scrollTop = host.scrollHeight;
  }

  function chronicleEntry(entry) {
    if (entry.kind === 'combat-edge') {
      return node('div.entry.entry-combat-edge', { text: entry.text });
    }
    if (entry.kind === 'narration') {
      // The drop cap goes on a narration that opens a scene, not on every
      // paragraph -- a page of drop caps reads as a ransom note.
      var opening = /^(the|a|an|you|it|dawn|night|smoke|silence)\b/i.test(entry.text);
      return node('p.entry.entry-narration' + (opening ? '.is-opening' : ''), {
        text: entry.text
      });
    }
    if (entry.kind === 'dialogue') {
      var split = entry.text.indexOf(':');
      if (split > 0 && split < 40) {
        return node('p.entry.entry-dialogue', {}, [
          node('span.speaker', { text: entry.text.slice(0, split + 1) }),
          document.createTextNode(entry.text.slice(split + 1))
        ]);
      }
      return node('p.entry.entry-dialogue', { text: entry.text });
    }
    return node('div.entry.entry-' + entry.kind, { text: entry.text });
  }

  // --- initiative ----------------------------------------------------------

  function renderInitiative() {
    var panel = el('initiative-panel');
    if (!view.combat.active) { panel.hidden = true; return; }

    panel.hidden = false;
    el('combat-round').textContent = 'Round ' + view.combat.round;

    fill(el('initiative-list'), view.combat.order.map(function (combatant) {
      var classes = '.combatant';
      if (combatant.current) { classes += '.is-current'; }
      if (combatant.is_player) { classes += '.is-player'; }
      if (combatant.dead) { classes += '.is-dead'; }

      return node('div' + classes, {}, [
        node('span.init-roll', { text: String(combatant.initiative) }),
        node('span.combatant-name', { text: combatant.name }),
        node('span.combatant-hp', {
          text: combatant.hp.current + '/' + combatant.hp.maximum
        })
      ]);
    }));
  }

  // --- the DM panel --------------------------------------------------------

  function renderDM() {
    var dm = view.dm;
    var rows = [node('div.dm-provider', { text: dm.provider })];

    if (dm.served_by && dm.served_by !== dm.provider_id) {
      rows.push(dmRow('Served by', dm.served_by));
    }
    if (dm.checks_requested) { rows.push(dmRow('Checks', String(dm.checks_requested))); }
    if (dm.events_appended) { rows.push(dmRow('Events', String(dm.events_appended))); }
    if (dm.cost_usd) { rows.push(dmRow('Cost', '$' + dm.cost_usd.toFixed(4))); }
    rows.push(dmRow('Rules', view.campaign.rules));

    if (dm.rationale) {
      rows.push(node('div.dm-rationale', { text: dm.rationale }));
    }
    (dm.degraded || []).forEach(function (note) {
      rows.push(node('div.dm-note', { text: note }));
    });

    fill(el('dm-panel'), rows);
  }

  function dmRow(label, value) {
    return node('div.dm-row', {}, [
      node('span.label', { text: label }),
      node('span.value', { text: value })
    ]);
  }

  // ======================================================================
  // Actions
  // ======================================================================

  function setBusy(state, message) {
    busy = state;
    el('thinking').hidden = !state;
    el('thinking-text').textContent = message || 'The Dungeon Master considers…';
    el('action-send').disabled = state;
    el('action-input').disabled = state;
    if (!state) { el('action-input').focus(); }
  }

  function submitAction(event) {
    if (event) { event.preventDefault(); }
    if (busy) { return; }

    var input = el('action-input');
    var text = input.value.trim();
    if (!text) { return; }

    input.value = '';
    history.push(text);
    historyIndex = history.length;

    if (text.charAt(0) === '/') { runCommand(text); return; }

    setBusy(true);
    DMAI.call('act', text)
      .then(refresh)
      .catch(fail)
      .then(function () { setBusy(false); });
  }

  function playAs(characterId) {
    DMAI.call('play_as', characterId).then(function () {
      return refresh();
    }).then(function () {
      var hero = view.party.filter(function (c) { return c.id === characterId; })[0];
      if (hero) { UI.toast('You now act as ' + hero.name + '.'); }
    }).catch(fail);
  }

  function rollDice(expression) {
    DMAI.call('roll', expression).then(function (result) {
      fill(el('dice-result'), [
        node('span.total', { text: String(result.total) }),
        document.createTextNode(result.text.trim())
      ]);
      return refresh();
    }).catch(fail);
  }

  // ======================================================================
  // Slash commands -- the DM's own controls
  // ======================================================================

  var COMMANDS = {
    roll: function (argument) { rollDice(argument || 'd20'); },

    narrate: function (argument) {
      if (!argument) { UI.toast('Say what to narrate.', 'bad'); return; }
      DMAI.call('narrate', argument).then(refresh).catch(fail);
    },

    scene: function (argument) {
      setBusy(true, 'The Dungeon Master sets the scene…');
      DMAI.call('open_scene', argument || '')
        .then(refresh).catch(fail)
        .then(function () { setBusy(false); });
    },

    resume: function () {
      setBusy(true, 'The Dungeon Master remembers…');
      DMAI.call('resume')
        .then(refresh).catch(fail)
        .then(function () { setBusy(false); });
    },

    recap: function () {
      DMAI.call('recap').then(function (result) {
        showRecap(result.recap);
      }).catch(fail);
    },

    mark: function (argument) {
      DMAI.call('checkpoint', argument || '').then(function (result) {
        UI.toast('Marked at event ' + result.seq + '.');
        return refresh();
      }).catch(fail);
    },

    marks: function () { showMarks(); },

    party: function () { UI.toast(view.party.length + ' in the party.'); },

    muster: function () { promptCharacter(false); },

    help: function () { showHelp(); },

    quit: function () { leaveTable(); }
  };

  //: Aliases, so the CLI's vocabulary works here too.
  COMMANDS.checkpoint = COMMANDS.mark;
  COMMANDS.character = COMMANDS.muster;

  function runCommand(text) {
    var space = text.indexOf(' ');
    var name = (space < 0 ? text.slice(1) : text.slice(1, space)).toLowerCase();
    var argument = space < 0 ? '' : text.slice(space + 1).trim();

    var command = COMMANDS[name];
    if (!command) {
      UI.toast('No such command: /' + name + '. Try /help.', 'bad');
      return;
    }
    command(argument);
  }

  // ======================================================================
  // Overlays
  // ======================================================================

  function promptCampaign() {
    loadOptions().then(function () {
      var name = UI.input('f-name', { placeholder: 'Ashes of Emberfall' });
      var premise = UI.input('f-premise', { placeholder: 'A caravan vanished on the Cinder Road.' });
      var setting = UI.input('f-setting', { placeholder: 'The Emberlands' });
      var player = UI.input('f-player', { placeholder: 'Your name' });
      var tone = UI.select('f-tone', options.tones.map(labelled), 'heroic');
      var rules = UI.select('f-rules', options.rules.map(labelled), 'srd51');
      var provider = UI.select('f-provider', options.providers.map(labelled), 'offline');
      var seed = UI.input('f-seed', { placeholder: 'blank for random', inputmode: 'numeric' });

      UI.openOverlay([
        node('h2', { text: 'A new chronicle' }),
        node('p.lede', { text: 'Name the tale, and the Dungeon Master will keep it.' }),
        UI.field('Name', name),
        UI.field('Premise', premise),
        node('div.form-grid', {}, [UI.field('Setting', setting), UI.field('Your name', player)]),
        node('div.form-grid', {}, [UI.field('Tone', tone), UI.field('Rules', rules)]),
        node('div.form-grid', {}, [UI.field('Dungeon Master', provider), UI.field('Dice seed', seed)]),
        node('div.form-actions', {}, [
          node('button.btn', { text: 'Cancel', onclick: UI.closeOverlay }),
          node('button.btn.btn-gilt', { text: 'Begin', onclick: function () {
            var form = {
              name: name.value.trim(),
              premise: premise.value.trim(),
              setting: setting.value.trim(),
              player: player.value.trim(),
              tone: tone.value,
              rules: rules.value,
              provider: provider.value,
              seed: seed.value.trim()
            };
            if (!form.name) { UI.toast('A chronicle needs a name.', 'bad'); return; }

            DMAI.call('create_campaign', form).then(function () {
              UI.closeOverlay();
              el('screen-library').hidden = true;
              el('screen-table').hidden = false;
              drawnSeq = -1;
              return refresh();
            }).then(function () {
              promptCharacter(true);
            }).catch(fail);
          } })
        ])
      ]);
    }).catch(fail);
  }

  function promptCharacter(first) {
    loadOptions().then(function () {
      var name = UI.input('c-name', { placeholder: 'Vale' });
      var species = UI.select('c-species', options.species.map(function (s) {
        return { value: s.key, label: s.name };
      }), 'human');
      var klass = UI.select('c-class', options.classes.map(function (c) {
        return { value: c.key, label: c.name };
      }), 'fighter');
      var level = UI.input('c-level', { type: 'number', value: '1', min: '1', max: '20' });

      UI.openOverlay([
        node('h2', { text: first ? 'Your first hero' : 'Muster a hero' }),
        node('p.lede', {
          text: first
            ? 'Every chronicle needs someone to walk it. Roll one up.'
            : 'Another blade for the party.'
        }),
        UI.field('Name', name),
        node('div.form-grid', {}, [UI.field('Species', species), UI.field('Class', klass)]),
        UI.field('Level', level),
        node('div.form-actions', {}, [
          node('button.btn', { text: first ? 'Later' : 'Cancel', onclick: UI.closeOverlay }),
          node('button.btn.btn-gilt', { text: 'Roll them up', onclick: function () {
            var form = {
              name: name.value.trim(),
              species: species.value,
              character_class: klass.value,
              level: parseInt(level.value, 10) || 1
            };
            if (!form.name) { UI.toast('A hero needs a name.', 'bad'); return; }

            DMAI.call('add_character', form).then(function () {
              UI.closeOverlay();
              return refresh();
            }).then(function () {
              UI.toast(form.name + ' joins the party.');
              el('action-input').focus();
            }).catch(fail);
          } })
        ])
      ]);
    }).catch(fail);
  }

  function openSheet(characterId) {
    DMAI.call('sheet', characterId).then(function (result) {
      var sheet = result.sheet;
      var descriptor = [sheet.species, sheet.class].filter(Boolean).map(title).join(' ');

      var abilities = node('div.sheet-abilities', {},
        Object.keys(sheet.abilities).map(function (key) {
          var ability = sheet.abilities[key];
          var modifier = (ability.modifier >= 0 ? '+' : '') + ability.modifier;
          return node('div.ability', {}, [
            node('div.name', { text: key.slice(0, 3) }),
            node('div.mod', { text: modifier }),
            node('div.score', { text: String(ability.score) })
          ]);
        }));

      var blocks = [];
      blocks.push(sheetBlock('Defence', [
        'Armour class ' + sheet.armor_class,
        'Hit points ' + sheet.hp.current + ' / ' + sheet.hp.maximum,
        'Speed ' + sheet.speed + ' ft',
        'Proficiency +' + sheet.proficiency_bonus
      ]));
      blocks.push(sheetBlock('Skills', sheet.skills.length
        ? sheet.skills.map(title) : ['None recorded']));
      blocks.push(sheetBlock('Carried', sheet.inventory.length
        ? sheet.inventory.map(function (item) {
            return item.name + (item.quantity > 1 ? ' ×' + item.quantity : '')
              + (item.equipped ? ' (worn)' : '');
          })
        : ['Nothing']));
      blocks.push(sheetBlock('Purse', [
        Object.keys(sheet.currency).map(function (coin) {
          return sheet.currency[coin] + ' ' + coin;
        }).join(', ') || 'Empty'
      ]));

      UI.openOverlay([
        node('div.sheet-head', {}, [
          node('div.seal', { text: sheet.name.charAt(0).toUpperCase() }),
          node('div', {}, [
            node('h2', { text: sheet.name }),
            node('p.lede', {
              text: 'Level ' + sheet.level + ' ' + descriptor
                + (sheet.player ? ' · played by ' + sheet.player : '')
            })
          ])
        ]),
        abilities,
        node('div.sheet-grid', {}, blocks),
        node('div.form-actions', {}, [
          node('button.btn', { text: 'Close', onclick: UI.closeOverlay }),
          node('button.btn.btn-gilt', { text: 'Play as ' + sheet.name, onclick: function () {
            UI.closeOverlay();
            playAs(characterId);
          } })
        ])
      ]);
    }).catch(fail);
  }

  function sheetBlock(heading, lines) {
    return node('div.sheet-block', {}, [
      node('h4', { text: heading }),
      node('ul', {}, lines.map(function (line) { return node('li', { text: line }); }))
    ]);
  }

  function showMarks() {
    var marks = (view && view.checkpoints) || [];
    var rows = marks.length
      ? marks.slice().reverse().map(function (mark) {
          return node('div.mark-row', {}, [
            node('div', {}, [
              node('div', { text: mark.label }),
              node('span.mark-seq', { text: 'event ' + mark.seq + ' · ' + mark.created })
            ]),
            node('button.btn.btn-tiny', { text: 'Rewind here', onclick: function () {
              rollbackTo(mark);
            } })
          ]);
        })
      : [node('div.empty', { text: 'No marks yet. /mark sets one.' })];

    UI.openOverlay([
      node('h2', { text: 'Marks' }),
      node('p.lede', {
        text: 'A mark is a place in the record. Rewinding truncates the log to it '
            + '— everything after is undone.'
      })
    ].concat(rows).concat([
      node('div.form-actions', {}, [
        node('button.btn', { text: 'Close', onclick: UI.closeOverlay }),
        node('button.btn.btn-gilt', { text: 'Mark here', onclick: function () {
          UI.closeOverlay();
          COMMANDS.mark('');
        } })
      ])
    ]));
  }

  function rollbackTo(mark) {
    UI.openOverlay([
      node('h2', { text: 'Rewind the chronicle?' }),
      node('p.lede', {
        text: 'Everything after "' + mark.label + '" (event ' + mark.seq
            + ') will be undone. This cannot be reversed.'
      }),
      node('div.form-actions', {}, [
        node('button.btn', { text: 'Keep it', onclick: UI.closeOverlay }),
        node('button.btn.btn-gilt', { text: 'Rewind', onclick: function () {
          DMAI.call('rollback', mark.seq).then(function () {
            UI.closeOverlay();
            drawnSeq = -1;
            return refresh();
          }).then(function () {
            UI.toast('Rewound to ' + mark.label + '.');
          }).catch(fail);
        } })
      ])
    ]);
  }

  function showRecap(recap) {
    var lines = String(recap.narrative || '').split('\n').filter(Boolean);
    UI.openOverlay([
      node('h2', { text: 'The tale so far' }),
      node('p.lede', { text: recap.campaign + ' · ' + recap.events_covered + ' events' })
    ].concat(lines.map(function (line) {
      return node('p.entry.entry-narration', { text: line });
    })).concat([
      recap.open_objectives && recap.open_objectives.length
        ? sheetBlock('Still open', recap.open_objectives)
        : null,
      node('div.form-actions', {}, [
        node('button.btn.btn-gilt', { text: 'Close', onclick: UI.closeOverlay })
      ])
    ]));
  }

  function showHelp() {
    UI.openOverlay([
      node('h2', { text: "The DM's commands" }),
      node('p.lede', {
        text: 'Anything that is not a command is an action your character takes.'
      }),
      sheetBlock('Commands', [
        '/roll 2d6+3 — roll where the table can see it',
        '/narrate <text> — put a line in the record yourself',
        '/scene [direction] — have the DM set the scene',
        '/resume — have the DM recap where you left off',
        '/recap — the mechanical summary of this sitting',
        '/mark [label] — set a checkpoint',
        '/marks — list marks and rewind to one',
        '/muster — add a character to the party',
        '/quit — save and return to the library'
      ]),
      sheetBlock('At the table', [
        'Click a hero to open their sheet',
        'Double-click a hero to act as them',
        'Up and Down walk back through what you have typed',
        'Escape closes any overlay'
      ]),
      node('div.form-actions', {}, [
        node('button.btn.btn-gilt', { text: 'Close', onclick: UI.closeOverlay })
      ])
    ]);
  }

  function confirmDelete(campaign) {
    UI.openOverlay([
      node('h2', { text: 'Burn this chronicle?' }),
      node('p.lede', {
        text: '"' + campaign.name + '" and all ' + campaign.events
            + ' of its events will be destroyed. Export it first if you would be sorry to lose it.'
      }),
      node('div.form-actions', {}, [
        node('button.btn', { text: 'Keep it', onclick: UI.closeOverlay }),
        node('button.btn.btn-gilt', { text: 'Burn it', onclick: function () {
          DMAI.call('delete_campaign', campaign.id).then(function () {
            UI.closeOverlay();
            UI.toast('"' + campaign.name + '" is gone.');
            return refreshLibrary();
          }).catch(fail);
        } })
      ])
    ]);
  }

  function exportCampaign(campaign) {
    DMAI.call('choose_export', campaign.id, campaign.name).then(function (result) {
      UI.toast('Written to ' + result.path + '.');
    }).catch(function (error) {
      if (!error.cancelled) { fail(error); }
    });
  }

  function importCampaign() {
    DMAI.call('choose_import').then(function (result) {
      UI.toast('"' + result.name + '" imported.');
      return refreshLibrary();
    }).catch(function (error) {
      if (!error.cancelled) { fail(error); }
    });
  }

  // ======================================================================
  // Wiring
  // ======================================================================

  function loadOptions() {
    if (options) { return Promise.resolve(options); }
    return DMAI.call('options').then(function (result) {
      options = result;
      return options;
    });
  }

  function labelled(value) { return { value: value, label: title(value) }; }

  function fail(error) {
    UI.toast(error && error.message ? error.message : String(error), 'bad');
  }

  function recallHistory(direction) {
    if (!history.length) { return; }
    historyIndex = Math.max(0, Math.min(history.length, historyIndex + direction));
    el('action-input').value = historyIndex < history.length ? history[historyIndex] : '';
  }

  document.addEventListener('DOMContentLoaded', function () {
    el('new-campaign').addEventListener('click', promptCampaign);
    el('import-campaign').addEventListener('click', importCampaign);
    el('leave-table').addEventListener('click', leaveTable);
    el('add-character').addEventListener('click', function () { promptCharacter(false); });
    el('open-marks').addEventListener('click', showMarks);

    el('action-form').addEventListener('submit', submitAction);
    el('action-input').addEventListener('keydown', function (event) {
      if (event.key === 'ArrowUp') { event.preventDefault(); recallHistory(-1); }
      else if (event.key === 'ArrowDown') { event.preventDefault(); recallHistory(1); }
    });

    el('dice-form').addEventListener('submit', function (event) {
      event.preventDefault();
      var input = el('dice-input');
      rollDice(input.value.trim() || 'd20');
      input.value = '';
    });
    el('dice-tray').addEventListener('click', function (event) {
      var die = event.target.closest('.die');
      if (die) { rollDice(die.getAttribute('data-roll')); }
    });

    // A window-level shortcut: Ctrl+Enter acts even from the dice field.
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
        el('action-input').focus();
      }
    });

    DMAI.ready().then(showLibrary).catch(fail);
  });
}(window.DMAI, window.UI));
