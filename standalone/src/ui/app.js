/* The standalone client.
 *
 * This file is a *client* in the sense the engine means: it never edits game
 * state, it calls the session and re-renders from what the session says
 * afterwards.  Everything it displays is derived from the event log, which is
 * why the sheet, the initiative tracker and the transcript can never disagree
 * with each other -- they are three views of one fold.
 *
 * Layout of the file: element helpers, then app state, then one render
 * function per panel, then the actions those panels call, then wiring.
 */
(function (DMAI) {
  'use strict';

  // --- tiny DOM helpers ----------------------------------------------------

  function el(id) { return document.getElementById(id); }
  function all(selector, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(selector));
  }

  /** Build an element: node('div.card', {title: 'x'}, [children...]) */
  function node(spec, attributes, children) {
    var parts = spec.split('.');
    var element = document.createElement(parts[0] || 'div');
    if (parts.length > 1) { element.className = parts.slice(1).join(' '); }

    Object.keys(attributes || {}).forEach(function (key) {
      var value = attributes[key];
      if (value === null || value === undefined || value === false) { return; }
      if (key === 'text') { element.textContent = value; }
      else if (key === 'html') { element.innerHTML = value; }
      else if (key.indexOf('on') === 0) { element.addEventListener(key.slice(2), value); }
      else if (key === 'data') {
        Object.keys(value).forEach(function (name) {
          element.setAttribute('data-' + name, value[name]);
        });
      } else { element.setAttribute(key, value); }
    });

    (children || []).forEach(function (child) {
      if (child === null || child === undefined || child === false) { return; }
      element.appendChild(
        typeof child === 'string' ? document.createTextNode(child) : child
      );
    });
    return element;
  }

  function clear(element) {
    while (element.firstChild) { element.removeChild(element.firstChild); }
    return element;
  }

  function fill(element, children) {
    clear(element);
    (Array.isArray(children) ? children : [children]).forEach(function (child) {
      if (child) { element.appendChild(child); }
    });
    return element;
  }

  // --- app state -----------------------------------------------------------

  var library = new DMAI.Library();

  var app = {
    session: null,
    dm: null,
    //: Which character the player is speaking and acting as.
    activeCharacterId: null,
    //: Which character's sheet and gear are on screen.
    viewCharacterId: null,
    //: The seat the transcript is filtered for.  null with dmSeat means the
    //: solo player, who is also the DM -- the common case for this build.
    playerId: null,
    dmSeat: true,
    tab: 'play',
    dirty: false,
    autosave: true
  };

  // --- toasts and modals ---------------------------------------------------

  function toast(message, kind) {
    var box = node('div.toast' + (kind ? '.' + kind : ''), { text: message });
    el('toasts').appendChild(box);
    setTimeout(function () {
      box.style.opacity = '0';
      box.style.transition = 'opacity 0.4s';
      setTimeout(function () {
        if (box.parentNode) { box.parentNode.removeChild(box); }
      }, 400);
    }, kind === 'error' ? 5200 : 2600);
  }

  /** Report a rules refusal as guidance, and a real fault as a fault. */
  function report(error) {
    var known = [
      'CharacterError', 'InventoryError', 'CombatError', 'CheckError',
      'QuestError', 'AtlasError', 'DiceError', 'EncounterError',
      'LibraryError', 'BestiaryError', 'EventStoreError', 'RulesPackError'
    ];
    if (known.indexOf(error.name) !== -1) {
      toast(error.message, 'error');
    } else {
      toast('Something went wrong: ' + error.message, 'error');
      if (window.console) { console.error(error); }
    }
  }

  /** Run an engine call, catching refusals so a bad click cannot end the game. */
  function attempt(body, after) {
    try {
      var result = body();
      touch();
      if (after) { after(result); }
      return result;
    } catch (error) {
      report(error);
      return null;
    }
  }

  function openModal(title, bodyNodes, footNodes, wide) {
    var backdrop = el('modal-backdrop');
    el('modal-title').textContent = title;
    fill(el('modal-body'), bodyNodes);
    fill(el('modal-foot'), footNodes || [
      node('button.btn', { text: 'Close', onclick: closeModal })
    ]);
    el('modal').className = 'modal' + (wide ? ' wide' : '');
    backdrop.classList.add('open');

    var focusable = el('modal-body').querySelector('input, select, textarea');
    if (focusable) { focusable.focus(); }
  }

  function closeModal() {
    el('modal-backdrop').classList.remove('open');
  }

  function confirmAction(message, label, onConfirm) {
    openModal('Are you sure?', [node('p', { text: message })], [
      node('button.btn', { text: 'Cancel', onclick: closeModal }),
      node('button.btn.btn-danger', {
        text: label,
        onclick: function () { closeModal(); onConfirm(); }
      })
    ]);
  }

  // --- persistence ---------------------------------------------------------

  /** Mark the campaign changed, and save it if autosave is on. */
  function touch() {
    app.dirty = true;
    if (app.autosave && app.session && library.available()) {
      try {
        library.saveCampaign(app.session);
        app.dirty = false;
        setSaveState('saved', 'saved');
      } catch (error) {
        setSaveState('dirty', 'not saved');
        report(error);
      }
    } else {
      setSaveState('dirty', 'unsaved');
    }
  }

  function setSaveState(className, text) {
    var indicator = el('save-state');
    indicator.className = 'save-state ' + className;
    indicator.textContent = text;
  }

  // --- formatting ----------------------------------------------------------

  function signed(value) { return DMAI.signed(value); }

  function hpBar(creature) {
    var fraction = creature.hp.maximum
      ? creature.hp.current / creature.hp.maximum : 0;
    var tone = fraction <= 0.25 ? 'critical' : (fraction <= 0.5 ? 'bloodied' : '');
    var temporary = creature.hp.maximum
      ? Math.min(1, creature.hp.temporary / creature.hp.maximum) : 0;

    return node('div.hp-bar', {}, [
      node('div.hp-fill' + (tone ? '.' + tone : ''), {
        style: 'width:' + Math.max(0, Math.min(1, fraction)) * 100 + '%'
      }),
      temporary > 0 ? node('div.hp-temp', {
        style: 'width:' + temporary * 100 + '%'
      }) : null
    ]);
  }

  function conditionTags(creature) {
    return (creature.conditions || []).map(function (condition) {
      return node('span.tag.blood', {
        text: condition.type + (condition.level > 1 ? ' ' + condition.level : '')
      });
    });
  }

  function creatureStatus(creature) {
    if (creature.dead) { return node('span.tag.blood', { text: 'dead' }); }
    if (DMAI.isDying(creature)) {
      var saves = creature.death_saves;
      return node('span.tag.blood', {
        text: saves.stable
          ? 'stable'
          : 'dying ' + saves.successes + 'S/' + saves.failures + 'F'
      });
    }
    if (DMAI.isBloodied(creature)) {
      return node('span.tag.gold', { text: 'bloodied' });
    }
    return null;
  }

  // ========================================================================
  // THE LIBRARY SCREEN
  // ========================================================================

  function renderLibrary() {
    renderCampaignShelf();
    renderCharacterVault();

    var warning = el('storage-warning');
    if (library.available()) {
      warning.style.display = 'none';
    } else {
      warning.style.display = 'block';
      warning.textContent =
        'This browser is not allowing local storage, so campaigns cannot be '
        + 'kept here between visits. Everything still plays -- use Export to '
        + 'save a campaign to a file, and Import to bring it back.';
    }
  }

  function renderCampaignShelf() {
    var entries = library.available() ? library.campaigns() : [];
    el('campaign-count').textContent = entries.length
      ? entries.length + (entries.length === 1 ? ' campaign' : ' campaigns')
      : '';

    if (!entries.length) {
      fill(el('campaign-shelf'), node('div.empty', {
        text: 'No campaigns yet. Start one, or import a saved bundle.'
      }));
      return;
    }

    fill(el('campaign-shelf'), entries.map(function (entry) {
      return node('div.card', {}, [
        node('h3', { text: entry.name }),
        node('div.premise', {
          text: entry.premise || 'No premise written.'
        }),
        node('div.meta', {}, [
          node('span', { text: entry.events + ' events' }),
          node('span', { text: entry.rules_pack || 'srd51' }),
          node('span', { text: entry.tone || 'heroic' }),
          node('span', {
            text: 'updated ' + String(entry.updated_at).slice(0, 10)
          })
        ]),
        node('div.card-actions', {}, [
          node('button.btn.btn-primary.btn-sm', {
            text: 'Play',
            onclick: function () { openCampaign(entry.id); }
          }),
          node('button.btn.btn-sm', {
            text: 'Export',
            onclick: function () { exportCampaign(entry.id); }
          }),
          node('button.btn.btn-sm', {
            text: 'Duplicate',
            onclick: function () { duplicateCampaign(entry.id, entry.name); }
          }),
          node('button.btn.btn-sm.btn-danger', {
            text: 'Delete',
            onclick: function () {
              confirmAction(
                'Delete "' + entry.name + '"? Its whole event log goes with it. '
                + 'Export it first if you might want it back.',
                'Delete campaign',
                function () {
                  library.deleteCampaign(entry.id);
                  renderLibrary();
                  toast('Deleted "' + entry.name + '".');
                }
              );
            }
          })
        ])
      ]);
    }));
  }

  function renderCharacterVault() {
    var records = library.available() ? library.characters() : [];
    el('vault-count').textContent = records.length
      ? records.length + (records.length === 1 ? ' sheet' : ' sheets')
      : '';

    if (!records.length) {
      fill(el('vault-shelf'), node('div.empty', {
        text: 'The vault is empty. Save a character from a campaign and they '
          + 'will keep, ready to be brought into the next one.'
      }));
      return;
    }

    fill(el('vault-shelf'), records.map(function (record) {
      var sheet = record.character;
      return node('div.card', {}, [
        node('h3', { text: sheet.name }),
        node('div.premise', {
          text: 'Level ' + sheet.level + ' ' + sheet.species + ' ' + sheet['class']
            + (record.origin_campaign ? ' — from ' + record.origin_campaign : '')
        }),
        node('div.meta', {}, [
          node('span', { text: sheet.hp.maximum + ' hp' }),
          node('span', { text: 'AC ' + sheet.armor_class }),
          node('span', { text: sheet.experience + ' xp' }),
          node('span', { text: 'saved ' + String(record.saved_at).slice(0, 10) })
        ]),
        node('div.card-actions', {}, [
          node('button.btn.btn-sm', {
            text: 'View sheet',
            onclick: function () { showVaultSheet(sheet); }
          }),
          node('button.btn.btn-sm', {
            text: 'Export',
            onclick: function () {
              DMAI.download(
                DMAI.safeFilename(sheet.name, '.character.json'),
                JSON.stringify(sheet, null, 2)
              );
            }
          }),
          node('button.btn.btn-sm.btn-danger', {
            text: 'Delete',
            onclick: function () {
              confirmAction(
                'Remove ' + sheet.name + ' from the vault?',
                'Delete sheet',
                function () {
                  library.deleteCharacter(sheet.id);
                  renderLibrary();
                }
              );
            }
          })
        ])
      ]);
    }));
  }

  /** A read-only sheet, for a character who is not in any open campaign. */
  function showVaultSheet(sheet) {
    var rules = DMAI.loadRules('srd51');
    openModal(sheet.name, [
      node('div.muted.mb', {
        text: 'Level ' + sheet.level + ' ' + sheet.species + ' ' + sheet['class']
      }),
      abilityGrid(sheet),
      node('div.mt', {}, [equipmentSlotsView(sheet)]),
      node('h3.mt', { text: 'Skills' }),
      skillList(sheet, rules)
    ], [
      node('button.btn', { text: 'Close', onclick: closeModal })
    ], true);
  }

  // ========================================================================
  // CAMPAIGN LIFECYCLE
  // ========================================================================

  function newCampaignDialog() {
    var packs = DMAI.RulesEngine.available();

    var name = node('input', { type: 'text', value: '', placeholder: 'Ashes of Emberfall' });
    var premise = node('textarea', {
      placeholder: 'A caravan vanished on the Cinder Road.'
    });
    var tone = node('select', {}, DMAI.STORY_TONES.map(function (value) {
      return node('option', { value: value, text: DMAI.titleCase(value) });
    }));
    tone.value = 'heroic';

    var difficulty = node('select', {}, ['easy', 'moderate', 'hard', 'deadly']
      .map(function (value) {
        return node('option', { value: value, text: DMAI.titleCase(value) });
      }));
    difficulty.value = 'moderate';

    var pack = node('select', {}, packs.map(function (value) {
      return node('option', { value: value, text: value });
    }));

    var seed = node('input', { type: 'text', placeholder: 'leave blank for random' });
    var sandbox = node('input', { type: 'checkbox' });

    openModal('New campaign', [
      node('label.field', {}, [node('span', { text: 'Name' }), name]),
      node('label.field', {}, [node('span', { text: 'Premise' }), premise]),
      node('div.field-row', {}, [
        node('label.field', {}, [node('span', { text: 'Tone' }), tone]),
        node('label.field', {}, [node('span', { text: 'Difficulty' }), difficulty])
      ]),
      node('div.field-row', {}, [
        node('label.field', {}, [node('span', { text: 'Rules pack' }), pack]),
        node('label.field', {}, [node('span', { text: 'Dice seed' }), seed])
      ]),
      node('label.checkbox', {}, [
        sandbox,
        node('span', { text: 'Sandbox mode — the world moves whether or not you do' })
      ]),
      node('p.hint', {
        text: 'A seed makes every roll in the campaign reproducible. Leave it '
          + 'blank and one is chosen for you and recorded, so a campaign can '
          + 'still be replayed exactly afterwards.'
      })
    ], [
      node('button.btn', { text: 'Cancel', onclick: closeModal }),
      node('button.btn.btn-primary', {
        text: 'Begin',
        onclick: function () {
          if (!name.value.trim()) { toast('The campaign needs a name.', 'error'); return; }
          createCampaign({
            name: name.value.trim(),
            premise: premise.value.trim(),
            tone: tone.value,
            difficulty: difficulty.value,
            rules_pack: pack.value,
            rng_seed: seed.value.trim() ? parseInt(seed.value.trim(), 10) : null,
            sandbox_mode: sandbox.checked
          });
        }
      })
    ]);
  }

  function createCampaign(options) {
    var campaign = DMAI.newCampaign({
      name: options.name,
      premise: options.premise
    });
    campaign.settings = DMAI.newCampaignSettings({
      tone: options.tone,
      difficulty: options.difficulty,
      rules_pack: options.rules_pack,
      rng_seed: options.rng_seed,
      sandbox_mode: options.sandbox_mode
    });

    attempt(function () {
      var session = DMAI.GameSession.create(campaign);
      // An unseeded campaign still records the seed it was given, so it can
      // be replayed exactly later.
      campaign.settings.rng_seed = session.dice._seed;
      library.saveCampaign(session);
      return session;
    }, function (session) {
      closeModal();
      enterCampaign(session);
      toast('"' + campaign.name + '" begins.', 'good');
      addCharacterDialog();
    });
  }

  function openCampaign(campaignId) {
    attempt(function () {
      return library.loadCampaign(campaignId);
    }, function (session) { enterCampaign(session); });
  }

  function enterCampaign(session) {
    app.session = session;
    app.dm = new DMAI.DungeonMaster(session);
    app.activeCharacterId = null;
    app.viewCharacterId = null;
    app.dirty = false;

    var party = session.partyList();
    if (party.length) {
      app.activeCharacterId = party[0].id;
      app.viewCharacterId = party[0].id;
    }

    el('screen-library').classList.remove('active');
    el('screen-table').classList.add('active');
    setSaveState('saved', 'saved');
    switchTab('play');
    renderAll();
  }

  function leaveCampaign() {
    if (app.session && library.available()) {
      try { library.saveCampaign(app.session); } catch (error) { report(error); }
    }
    app.session = null;
    app.dm = null;
    el('screen-table').classList.remove('active');
    el('screen-library').classList.add('active');
    renderLibrary();
  }

  function exportCampaign(campaignId) {
    attempt(function () {
      var session = campaignId
        ? library.loadCampaign(campaignId)
        : app.session;
      var bundle = DMAI.exportBundle(session);
      DMAI.download(
        DMAI.safeFilename(session.campaign.name, '.dmai.json'),
        JSON.stringify(bundle, null, 2)
      );
      return session;
    }, function () { toast('Exported.', 'good'); });
  }

  function duplicateCampaign(campaignId, name) {
    var field = node('input', { type: 'text', value: name + ' (copy)' });
    openModal('Duplicate campaign', [
      node('label.field', {}, [node('span', { text: 'New name' }), field]),
      node('p.hint', {
        text: 'The copy carries the whole event log, so it starts exactly '
          + 'where the original stands. This is the safe way to try a '
          + 'different road from the same point.'
      })
    ], [
      node('button.btn', { text: 'Cancel', onclick: closeModal }),
      node('button.btn.btn-primary', {
        text: 'Duplicate',
        onclick: function () {
          attempt(function () {
            return library.duplicateCampaign(campaignId, field.value.trim());
          }, function () {
            closeModal();
            renderLibrary();
            toast('Duplicated.', 'good');
          });
        }
      })
    ]);
  }

  function importBundle(file) {
    var reader = new FileReader();
    reader.onload = function () {
      attempt(function () {
        var parsed = JSON.parse(reader.result);
        // A bare character sheet is a vault import, not a campaign.
        if (!parsed.campaign && !parsed.events && parsed.abilities) {
          library.saveCharacter(DMAI.importCharacter(parsed), {});
          return null;
        }
        var session = library.openBundle(parsed);
        library.saveCampaign(session);
        return session;
      }, function (session) {
        renderLibrary();
        toast(session ? 'Imported "' + session.campaign.name + '".' : 'Sheet added to the vault.', 'good');
      });
    };
    reader.onerror = function () { toast('That file could not be read.', 'error'); };
    reader.readAsText(file);
  }

  // ========================================================================
  // PLAY
  // ========================================================================

  /** How one event should look in the story column. */
  function entryFor(event) {
    var Type = DMAI.EventType;

    if (event.type === Type.DM_NARRATION) {
      return { kind: 'narration', who: 'Dungeon Master', text: event.data.text || event.summary };
    }
    if (event.type === Type.PLAYER_ACTION) {
      return { kind: 'player', who: '', text: event.summary };
    }
    if (event.type === Type.NPC_DIALOGUE) {
      return { kind: 'dialogue', who: event.data.speaker || '', text: event.data.text };
    }
    if (event.type === Type.PRIVATE_MESSAGE) {
      return { kind: 'whisper', who: 'Whispered to you', text: event.data.text };
    }
    if (event.type === Type.CHECK_RESOLVED || event.type === Type.SAVE_RESOLVED
      || event.type === Type.DICE_ROLLED) {
      var roll = event.data.roll || {};
      var tone = '';
      if (roll.outcome === DMAI.Outcome.CRITICAL_SUCCESS
        || roll.outcome === DMAI.Outcome.CRITICAL_FAILURE) { tone = 'critical'; }
      else if (roll.outcome === DMAI.Outcome.SUCCESS) { tone = 'success'; }
      else if (roll.outcome === DMAI.Outcome.FAILURE) { tone = 'failure'; }
      return { kind: 'roll ' + tone, who: '', text: event.summary };
    }
    if ([Type.ATTACK, Type.DAMAGE_DEALT, Type.HEALING, Type.CREATURE_DIED,
      Type.COMBAT_STARTED, Type.COMBAT_ENDED, Type.TURN_STARTED,
      Type.DEATH_SAVE, Type.ENEMY_FLED, Type.ENEMY_SURRENDERED,
      Type.CONDITION_ADDED, Type.CONDITION_REMOVED].indexOf(event.type) !== -1) {
      return { kind: 'combat', who: '', text: event.summary };
    }
    if (!event.summary) { return null; }
    return { kind: 'system', who: '', text: event.summary };
  }

  function renderStory() {
    var session = app.session;
    if (!session) { return; }

    var events = session.logFor(app.playerId, { is_dm: app.dmSeat });
    var story = el('story-inner');
    var atBottom = isScrolledToBottom(el('story'));

    fill(story, events.map(function (event) {
      var entry = entryFor(event);
      if (!entry) { return null; }
      return node('div.entry.' + entry.kind, {}, [
        entry.who ? node('div.who', { text: entry.who }) : null,
        node('div.said', { text: entry.text })
      ]);
    }).filter(Boolean));

    if (!story.children.length) {
      fill(story, node('div.empty', {
        text: 'Nothing has happened yet. Say what your character does, or '
          + 'press "Open the scene" to have the DM set it.'
      }));
    }
    if (atBottom) { scrollStoryToBottom(); }
  }

  function isScrolledToBottom(element) {
    return element.scrollHeight - element.scrollTop - element.clientHeight < 90;
  }

  function scrollStoryToBottom() {
    var story = el('story');
    story.scrollTop = story.scrollHeight;
  }

  function renderComposer() {
    var session = app.session;
    var party = session.partyList();
    var acting = session.state.party[app.activeCharacterId];

    var picker = node('select', {
      style: 'width:auto;display:inline-block;padding:2px 6px;font-size:12px',
      onchange: function (event) {
        app.activeCharacterId = event.target.value;
        renderComposer();
      }
    }, party.map(function (character) {
      return node('option', {
        value: character.id,
        text: character.name,
        selected: character.id === app.activeCharacterId
      });
    }));

    fill(el('acting-as'), party.length
      ? [node('span', { text: 'Acting as ' }), picker]
      : [node('span', {
        text: 'No characters yet — add one from the Party tab before you play.'
      })]);

    el('say-input').disabled = !party.length;
    el('say-button').disabled = !party.length;

    if (acting && DMAI.isDying(acting)) {
      el('acting-as').appendChild(node('span.tag.blood', {
        style: 'margin-left:8px', text: 'dying'
      }));
    }
  }

  /** Send what the player typed through the DM's turn loop. */
  function say() {
    var input = el('say-input');
    var text = input.value.trim();
    if (!text) { return; }

    if (text.charAt(0) === '/') {
      input.value = '';
      runCommand(text);
      return;
    }
    if (!app.activeCharacterId) {
      toast('Add a character before you act.', 'error');
      return;
    }

    input.value = '';
    input.style.height = 'auto';

    attempt(function () {
      var action = {
        id: DMAI.newId('act'),
        campaign_id: app.session.campaign.id,
        player_id: app.playerId,
        character_id: app.activeCharacterId,
        text: text
      };
      return app.dm.takeTurn(action);
    }, function (result) {
      renderAll();
      scrollStoryToBottom();
      showDiagnostics(result.diagnostics);
    });
  }

  function showDiagnostics(diagnostics) {
    if (!diagnostics) { return; }
    var parts = [
      'DM: ' + diagnostics.narrated_by,
      diagnostics.checks_requested + ' check(s)',
      diagnostics.events_appended + ' events'
    ];
    if (diagnostics.rationale) { parts.push(diagnostics.rationale); }
    if (diagnostics.degraded.length) { parts.push(diagnostics.degraded.join('; ')); }
    el('diagnostics').textContent = parts.join('  ·  ');
  }

  /**
   * The slash commands, mirroring the CLI so the two clients feel like one
   * program.  Anything not a command is play.
   */
  var COMMANDS = {
    '/help': 'list these commands',
    '/roll': 'roll dice, e.g. /roll 2d6+3',
    '/narrate': 'add narration in your own words',
    '/whisper': 'a private note to yourself',
    '/scene': 'have the DM open or set the scene',
    '/recap': 'a factual summary of what has happened',
    '/party': 'the party, at a glance',
    '/quests': 'the quest board',
    '/rest': 'take a long rest (/rest short for an hour)',
    '/checkpoint': 'mark this point so you can come back to it',
    '/save': 'save the campaign now'
  };

  function runCommand(line) {
    var parts = line.trim().split(/\s+/);
    var command = parts[0].toLowerCase();
    var rest = line.slice(parts[0].length).trim();
    var session = app.session;

    if (command === '/help') {
      openModal('Commands', [
        node('p.muted.mb', {
          text: 'Anything that is not a command is what your character does, '
            + 'and goes through the DM.'
        }),
        node('ul.list-plain', {}, Object.keys(COMMANDS).map(function (key) {
          return node('li', {}, [
            node('span.mono', { text: key }),
            node('span.muted', { text: '  —  ' + COMMANDS[key] })
          ]);
        }))
      ]);
      return;
    }

    if (command === '/roll') {
      attempt(function () {
        return session.roll(rest || '1d20', {
          reason: 'a roll', actor_id: app.activeCharacterId
        });
      }, function () { renderAll(); scrollStoryToBottom(); });
      return;
    }

    if (command === '/narrate') {
      if (!rest) { toast('Say what happens: /narrate ...', 'error'); return; }
      attempt(function () { return session.narrate(rest); },
        function () { renderAll(); scrollStoryToBottom(); });
      return;
    }

    if (command === '/whisper') {
      if (!rest) { toast('Whisper what? /whisper ...', 'error'); return; }
      attempt(function () {
        var seat = app.playerId || session.addPlayer('You', { is_host: true }).id;
        app.playerId = seat;
        return session.whisper(seat, rest);
      }, function () { renderAll(); scrollStoryToBottom(); });
      return;
    }

    if (command === '/scene') {
      attempt(function () { return app.dm.openScene(rest); },
        function () { renderAll(); scrollStoryToBottom(); });
      return;
    }

    if (command === '/recap') {
      openModal('Recap', [
        node('div', { style: 'white-space:pre-wrap', text: session.recap().narrative })
      ]);
      return;
    }

    if (command === '/party') { switchTab('party'); return; }
    if (command === '/quests') { switchTab('world'); return; }

    if (command === '/rest') {
      takeRest(rest === 'short' ? 'short' : 'long');
      return;
    }

    if (command === '/checkpoint') {
      attempt(function () { return session.checkpoint(rest || 'manual'); },
        function () { renderAll(); toast('Checkpoint marked.', 'good'); });
      return;
    }

    if (command === '/save') {
      attempt(function () { return library.saveCampaign(session); }, function () {
        app.dirty = false;
        setSaveState('saved', 'saved');
        toast('Saved.', 'good');
      });
      return;
    }

    toast('Unknown command: ' + command + '. Try /help.', 'error');
  }

  // ========================================================================
  // PARTY
  // ========================================================================

  function renderParty() {
    var session = app.session;
    var party = session.partyList();

    if (!party.length) {
      fill(el('party-list'), node('div.empty', {
        text: 'Nobody at the table yet. Roll up a character to begin.'
      }));
      return;
    }

    fill(el('party-list'), party.map(function (character) {
      return node('div.card', {}, [
        node('h3', { text: character.name }),
        node('div.muted', {
          text: 'Level ' + character.level + ' ' + character.species + ' '
            + character['class']
        }),
        node('div.mt', {}, [
          node('div', {
            style: 'display:flex;justify-content:space-between;font-size:13px'
          }, [
            node('span', { text: character.hp.current + ' / ' + character.hp.maximum + ' hp' }),
            node('span.muted', { text: 'AC ' + character.armor_class })
          ]),
          hpBar(character)
        ]),
        node('div.meta.mt', {}, [
          node('span', { text: character.experience + ' xp' }),
          node('span', { text: DMAI.carriedWeight(character).toFixed(0) + ' lb' })
        ]),
        node('div', { style: 'display:flex;flex-wrap:wrap;gap:5px;margin:6px 0' },
          [creatureStatus(character)].concat(conditionTags(character)).filter(Boolean)),
        node('div.card-actions', {}, [
          node('button.btn.btn-sm.btn-primary', {
            text: 'Sheet',
            onclick: function () {
              app.viewCharacterId = character.id;
              switchTab('sheet');
            }
          }),
          node('button.btn.btn-sm', {
            text: 'Gear',
            onclick: function () {
              app.viewCharacterId = character.id;
              switchTab('gear');
            }
          }),
          node('button.btn.btn-sm', {
            text: 'Play as',
            onclick: function () {
              app.activeCharacterId = character.id;
              renderComposer();
              toast('Acting as ' + character.name + '.');
            }
          }),
          node('button.btn.btn-sm', {
            text: 'To vault',
            onclick: function () {
              attempt(function () {
                return library.saveCharacter(character, {
                  campaign_id: session.campaign.id,
                  campaign_name: session.campaign.name
                });
              }, function () {
                toast(character.name + ' saved to the vault.', 'good');
              });
            }
          })
        ])
      ]);
    }));
  }

  function addCharacterDialog() {
    var session = app.session;
    var rules = session.rules;

    var name = node('input', { type: 'text', placeholder: 'Vale' });
    var species = node('select', {}, rules.speciesKeys().map(function (key) {
      return node('option', { value: key, text: rules.species(key).name });
    }));
    var klass = node('select', {}, rules.classKeys().map(function (key) {
      return node('option', { value: key, text: rules.characterClass(key).name });
    }));
    var level = node('input', { type: 'number', value: '1', min: '1', max: '20' });
    var pronouns = node('input', { type: 'text', value: 'they/them' });

    var method = node('select', {}, [
      node('option', { value: 'standard', text: 'Standard array (15 14 13 12 10 8)' }),
      node('option', { value: 'rolled', text: 'Rolled (4d6 drop lowest)' })
    ]);

    var skillBox = node('div');

    /** The skill list is per class, so it is rebuilt when the class changes. */
    function refreshSkills() {
      var data = rules.characterClass(klass.value) || {};
      var limit = data.skill_count || 2;
      fill(skillBox, [
        node('span', {
          text: 'Skills — choose up to ' + limit,
          style: 'color:var(--text-dim);font-size:12px;letter-spacing:0.06em;'
            + 'text-transform:uppercase;display:block;margin-bottom:6px'
        }),
        node('div', {
          style: 'display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:4px'
        }, (data.skills || []).map(function (skill) {
          return node('label.checkbox', { style: 'font-size:13.5px' }, [
            node('input', { type: 'checkbox', value: skill, 'data-skill': '1' }),
            node('span', { text: DMAI.titleCase(skill) })
          ]);
        }))
      ]);
    }

    klass.addEventListener('change', refreshSkills);
    refreshSkills();

    openModal('New character', [
      node('label.field', {}, [node('span', { text: 'Name' }), name]),
      node('div.field-row', {}, [
        node('label.field', {}, [node('span', { text: 'Species' }), species]),
        node('label.field', {}, [node('span', { text: 'Class' }), klass])
      ]),
      node('div.field-row', {}, [
        node('label.field', {}, [node('span', { text: 'Level' }), level]),
        node('label.field', {}, [node('span', { text: 'Pronouns' }), pronouns])
      ]),
      node('label.field', {}, [node('span', { text: 'Ability scores' }), method]),
      skillBox,
      node('p.hint', {
        text: 'Hit points, armour class, saving throws and starting gear are '
          + 'all derived from the rules pack. Nothing here is typed in twice.'
      })
    ], [
      node('button.btn', { text: 'Cancel', onclick: closeModal }),
      node('button.btn.btn-primary', {
        text: 'Roll them up',
        onclick: function () {
          var chosen = all('input[data-skill]', skillBox)
            .filter(function (box) { return box.checked; })
            .map(function (box) { return box.value; });

          attempt(function () {
            return session.addCharacter(name.value.trim(), {
              species: species.value,
              character_class: klass.value,
              level: parseInt(level.value, 10) || 1,
              method: method.value,
              skills: chosen,
              pronouns: pronouns.value.trim() || 'they/them'
            });
          }, function (character) {
            closeModal();
            if (!app.activeCharacterId) { app.activeCharacterId = character.id; }
            app.viewCharacterId = character.id;
            renderAll();
            toast(character.name + ' joins the party.', 'good');
          });
        }
      })
    ]);
  }

  /** Bring a vaulted sheet into this campaign, as a copy. */
  function adoptFromVaultDialog() {
    var records = library.available() ? library.characters() : [];
    if (!records.length) {
      toast('The vault is empty.', 'error');
      return;
    }
    openModal('Bring a character in from the vault', [
      node('p.muted.mb', {
        text: 'The sheet is copied in under a fresh identity, so levelling '
          + 'them here will not edit the one in the vault.'
      }),
      node('div.cards', {}, records.map(function (record) {
        var sheet = record.character;
        return node('div.card', {}, [
          node('h3', { text: sheet.name }),
          node('div.premise', {
            text: 'Level ' + sheet.level + ' ' + sheet.species + ' ' + sheet['class']
          }),
          node('div.card-actions', {}, [
            node('button.btn.btn-sm.btn-primary', {
              text: 'Bring in',
              onclick: function () {
                attempt(function () {
                  return app.session.adoptCharacter(sheet, app.playerId);
                }, function (character) {
                  closeModal();
                  if (!app.activeCharacterId) { app.activeCharacterId = character.id; }
                  renderAll();
                  toast(character.name + ' joins the party.', 'good');
                });
              }
            })
          ])
        ]);
      }))
    ], null, true);
  }

  // ========================================================================
  // CHARACTER SHEET
  // ========================================================================

  function abilityGrid(character) {
    return node('div.ability-grid', {}, DMAI.ABILITIES.map(function (ability) {
      var score = character.abilities[ability];
      var proficient = (character.saving_throw_proficiencies || [])
        .indexOf(ability) !== -1;
      return node('div.ability' + (proficient ? '.proficient' : ''), {
        title: proficient ? 'Proficient in ' + ability + ' saving throws' : ability
      }, [
        node('div.name', { text: DMAI.ABILITY_ABBR[ability] }),
        node('div.mod', { text: signed(DMAI.abilityModifier(score)) }),
        node('div.score', { text: String(score) })
      ]);
    }));
  }

  function skillList(character, rules) {
    return node('div.skill-list', {}, DMAI.SKILLS.map(function (skill) {
      var proficient = (character.skill_proficiencies || {})[skill] > 0;
      var modifier = rules.skillModifier(character, skill);
      return node('div.skill' + (proficient ? '.proficient' : ''), {}, [
        node('span', {}, [
          node('span.pip', { text: proficient ? '◆' : '◇' }),
          node('span.skill-name', {
            text: DMAI.titleCase(skill) + ' ('
              + DMAI.ABILITY_ABBR[DMAI.SKILL_ABILITIES[skill]] + ')'
          })
        ]),
        node('span.skill-mod', { text: signed(modifier) })
      ]);
    }));
  }

  function renderSheet() {
    var session = app.session;
    var character = session.state.party[app.viewCharacterId]
      || session.partyList()[0];

    if (!character) {
      fill(el('sheet-body'), node('div.empty', {
        text: 'No character selected. Add one from the Party tab.'
      }));
      return;
    }
    app.viewCharacterId = character.id;

    var rules = session.rules;
    var slots = DMAI.equipmentSlots(character);
    var load = DMAI.encumbrance(character);

    fill(el('sheet-body'), [
      node('div.sheet-head', {}, [
        node('div', {}, [
          node('h2', { text: character.name }),
          node('div.subtitle', {
            text: 'Level ' + character.level + ' ' + character.species + ' '
              + character['class'] + '  ·  ' + character.pronouns
          }),
          node('div', { style: 'display:flex;flex-wrap:wrap;gap:5px;margin-top:8px' },
            [creatureStatus(character)].concat(conditionTags(character)).filter(Boolean))
        ]),
        node('div', { style: 'display:flex;gap:6px;flex-wrap:wrap' }, [
          node('button.btn.btn-sm', {
            text: 'Level up',
            onclick: function () {
              attempt(function () { return session.awardLevel(character.id); },
                function (levelled) {
                  renderAll();
                  toast(levelled.name + ' reaches level ' + levelled.level + '.', 'good');
                });
            }
          }),
          node('button.btn.btn-sm', {
            text: 'Damage / heal',
            onclick: function () { adjustHitPointsDialog(character); }
          }),
          node('button.btn.btn-sm', {
            text: 'Conditions',
            onclick: function () { conditionsDialog(character); }
          }),
          node('button.btn.btn-sm', {
            text: 'Save to vault',
            onclick: function () {
              attempt(function () {
                return library.saveCharacter(character, {
                  campaign_id: session.campaign.id,
                  campaign_name: session.campaign.name
                });
              }, function () { toast('Saved to the vault.', 'good'); });
            }
          })
        ])
      ]),

      node('div.stat-strip', {}, [
        node('div.stat', {}, [
          node('div.stat-label', { text: 'Hit points' }),
          node('div.stat-value', { text: String(character.hp.current) }),
          node('div.stat-sub', {
            text: 'of ' + character.hp.maximum
              + (character.hp.temporary ? ' (+' + character.hp.temporary + ' temp)' : '')
          }),
          hpBar(character)
        ]),
        node('div.stat.ember', {}, [
          node('div.stat-label', { text: 'Armour class' }),
          node('div.stat-value', { text: String(character.armor_class) }),
          node('div.stat-sub', {
            text: slots.armor ? slots.armor.name : 'unarmoured'
          })
        ]),
        node('div.stat', {}, [
          node('div.stat-label', { text: 'Proficiency' }),
          node('div.stat-value', { text: signed(character.proficiency_bonus) })
        ]),
        node('div.stat', {}, [
          node('div.stat-label', { text: 'Initiative' }),
          node('div.stat-value', {
            text: signed(DMAI.creatureModifier(character, DMAI.Ability.DEX))
          })
        ]),
        node('div.stat', {}, [
          node('div.stat-label', { text: 'Speed' }),
          node('div.stat-value', { text: String(character.speed) }),
          node('div.stat-sub', { text: 'feet' })
        ]),
        node('div.stat', {}, [
          node('div.stat-label', { text: 'Passive perception' }),
          node('div.stat-value', {
            text: String(rules.passiveScore(character, 'perception'))
          })
        ]),
        node('div.stat', {}, [
          node('div.stat-label', { text: 'Experience' }),
          node('div.stat-value', { text: String(character.experience) })
        ])
      ]),

      node('div.section', {}, [
        node('h3', { text: 'Abilities — a lit border marks a saving-throw proficiency' }),
        abilityGrid(character),
        node('div.mt', {}, [
          node('table', {}, [
            node('tbody', {}, DMAI.ABILITIES.map(function (ability) {
              return node('tr', {}, [
                node('td', { text: DMAI.titleCase(ability) + ' save' }),
                node('td.numeric.mono', {
                  text: signed(rules.saveModifier(character, ability))
                }),
                node('td.right', {}, [
                  node('button.btn.btn-sm', {
                    text: 'Roll',
                    onclick: function () {
                      attempt(function () {
                        return session.checks.save(
                          session.journal, character.id, ability, { dc: 15 }
                        );
                      }, function () { renderAll(); });
                    }
                  })
                ])
              ]);
            }))
          ])
        ])
      ]),

      node('div.two-col', {}, [
        node('div.section', {}, [
          node('h3', { text: 'Skills' }),
          skillList(character, rules),
          node('div.mt', {}, [
            node('button.btn.btn-sm', {
              text: 'Roll a check…',
              onclick: function () { rollCheckDialog(character); }
            })
          ])
        ]),
        node('div', {}, [
          node('div.section', {}, [
            node('h3', { text: 'Worn and wielded' }),
            equipmentSlotsView(character),
            node('div.hint', {
              text: 'Carrying ' + load.carried.toFixed(0) + ' of '
                + load.capacity + ' lb'
                + (load.encumbered ? ' — encumbered' : '')
            })
          ]),
          Object.keys(character.resources).length
            ? node('div.section', {}, [
              node('h3', { text: 'Resources' }),
              node('table', {}, [
                node('tbody', {}, Object.keys(character.resources).map(function (name) {
                  var pair = character.resources[name];
                  return node('tr', {}, [
                    node('td', { text: DMAI.titleCase(name) }),
                    node('td.numeric.mono', { text: pair[0] + ' / ' + pair[1] })
                  ]);
                }))
              ])
            ])
            : null,
          DMAI.isDying(character)
            ? node('div.section', {}, [
              node('h3', { text: 'Death saves' }),
              node('div', {
                text: character.death_saves.successes + ' successes, '
                  + character.death_saves.failures + ' failures'
                  + (character.death_saves.stable ? ' — stable' : '')
              }),
              node('button.btn.btn-sm.mt', {
                text: 'Roll a death save',
                onclick: function () {
                  attempt(function () {
                    return session.combat.deathSave(session.journal, character.id);
                  }, function () { renderAll(); });
                }
              })
            ])
            : null
        ])
      ]),

      node('div.section', {}, [
        node('h3', { text: 'Character' }),
        node('label.field', {}, [
          node('span', { text: 'Background' }),
          node('input', {
            type: 'text', value: character.background || '',
            onchange: function (event) {
              var updated = DMAI.deepCopy(character);
              updated.background = event.target.value;
              attempt(function () {
                return session.updateCharacter(updated, '');
              });
            }
          })
        ]),
        node('label.field', {}, [
          node('span', { text: 'Appearance and backstory' }),
          node('textarea', {
            value: character.personality.backstory || '',
            onchange: function (event) {
              var updated = DMAI.deepCopy(character);
              updated.personality.backstory = event.target.value;
              attempt(function () {
                return session.updateCharacter(updated, '');
              });
            }
          })
        ])
      ])
    ]);
  }

  function equipmentSlotsView(character) {
    var slots = DMAI.equipmentSlots(character);

    function slot(label, item, detail) {
      return node('div.slot' + (item ? '.filled' : '.empty-slot'), {}, [
        node('div.slot-label', { text: label }),
        node('div.slot-item', { text: item ? item.name : 'nothing' }),
        item && detail ? node('div.slot-detail', { text: detail }) : null
      ]);
    }

    var armorDetail = '';
    if (slots.armor) {
      var properties = slots.armor.properties || {};
      armorDetail = DMAI.titleCase(properties.type || '') + ' armour, base AC '
        + properties.base_ac
        + (properties.stealth_disadvantage ? ' · stealth disadvantage' : '');
    }

    return node('div.slots', {}, [
      slot('Armour', slots.armor, armorDetail),
      slot('Shield', slots.shield, slots.shield
        ? '+' + (slots.shield.properties.ac_bonus || 0) + ' AC' : ''),
      slots.weapons.length
        ? node('div.slot.filled', {}, [
          node('div.slot-label', { text: 'In hand' }),
          node('div.slot-item', {
            text: slots.weapons.map(function (item) { return item.name; }).join(', ')
          }),
          node('div.slot-detail', {
            text: slots.weapons.map(function (item) {
              var properties = item.properties || {};
              return properties.damage
                ? properties.damage + ' ' + properties.damage_type : '';
            }).filter(Boolean).join(' · ')
          })
        ])
        : slot('In hand', null),
      slots.other.length
        ? node('div.slot.filled', {}, [
          node('div.slot-label', { text: 'Also worn' }),
          node('div.slot-item', {
            text: slots.other.map(function (item) { return item.name; }).join(', ')
          })
        ])
        : null
    ]);
  }

  function adjustHitPointsDialog(character) {
    var amount = node('input', { type: 'number', value: '5', min: '0' });
    var damageType = node('select', {}, Object.keys(DMAI.DamageType)
      .map(function (key) {
        return node('option', {
          value: DMAI.DamageType[key], text: DMAI.titleCase(DMAI.DamageType[key])
        });
      }));
    damageType.value = DMAI.DamageType.SLASHING;

    openModal('Damage or heal ' + character.name, [
      node('label.field', {}, [node('span', { text: 'Amount' }), amount]),
      node('label.field', {}, [node('span', { text: 'Damage type' }), damageType]),
      node('p.hint', {
        text: 'Resistance, vulnerability and temporary hit points are applied '
          + 'by the rules pack, and the result is written to the log as an '
          + 'absolute total — which is what makes it safe to replay.'
      })
    ], [
      node('button.btn', { text: 'Cancel', onclick: closeModal }),
      node('button.btn', {
        text: 'Heal',
        onclick: function () {
          attempt(function () {
            return app.session.combat.heal(
              app.session.journal, character.id,
              parseInt(amount.value, 10) || 0
            );
          }, function () { closeModal(); renderAll(); });
        }
      }),
      node('button.btn.btn-danger', {
        text: 'Damage',
        onclick: function () {
          attempt(function () {
            return app.session.combat.dealDamage(
              app.session.journal, character.id,
              parseInt(amount.value, 10) || 0, damageType.value
            );
          }, function () { closeModal(); renderAll(); });
        }
      })
    ]);
  }

  function conditionsDialog(creature) {
    var session = app.session;
    var picker = node('select', {}, DMAI.CONDITION_TYPES.map(function (type) {
      return node('option', { value: type, text: DMAI.titleCase(type) });
    }));

    function body() {
      return [
        node('div.mb', {}, (creature.conditions || []).length
          ? creature.conditions.map(function (condition) {
            return node('div', {
              style: 'display:flex;justify-content:space-between;align-items:center;'
                + 'padding:6px 0;border-bottom:1px solid var(--line-soft)'
            }, [
              node('span', { text: DMAI.titleCase(condition.type) }),
              node('button.btn.btn-sm', {
                text: 'Remove',
                onclick: function () {
                  attempt(function () {
                    return session.combat.removeCondition(
                      session.journal, creature.id, condition.type
                    );
                  }, function () {
                    renderAll();
                    conditionsDialog(session.state.party[creature.id]
                      || session.state.bestiary[creature.id]);
                  });
                }
              })
            ]);
          })
          : [node('div.faint', { text: 'No conditions.' })]),
        node('label.field', {}, [node('span', { text: 'Add a condition' }), picker])
      ];
    }

    openModal('Conditions on ' + creature.name, body(), [
      node('button.btn', { text: 'Close', onclick: closeModal }),
      node('button.btn.btn-primary', {
        text: 'Add',
        onclick: function () {
          attempt(function () {
            return session.combat.addCondition(
              session.journal, creature.id, picker.value
            );
          }, function () {
            renderAll();
            conditionsDialog(session.state.party[creature.id]
              || session.state.bestiary[creature.id]);
          });
        }
      })
    ]);
  }

  function rollCheckDialog(character) {
    var session = app.session;
    var skill = node('select', {}, DMAI.SKILLS.map(function (name) {
      return node('option', { value: name, text: DMAI.titleCase(name) });
    }));
    var dc = node('input', { type: 'number', value: '15', min: '1', max: '30' });
    var mode = node('select', {}, [
      node('option', { value: '', text: 'Normal (or whatever conditions say)' }),
      node('option', { value: DMAI.RollMode.ADVANTAGE, text: 'Advantage' }),
      node('option', { value: DMAI.RollMode.DISADVANTAGE, text: 'Disadvantage' })
    ]);

    openModal('Roll a check for ' + character.name, [
      node('label.field', {}, [node('span', { text: 'Skill' }), skill]),
      node('div.field-row', {}, [
        node('label.field', {}, [node('span', { text: 'Difficulty class' }), dc]),
        node('label.field', {}, [node('span', { text: 'Roll mode' }), mode])
      ])
    ], [
      node('button.btn', { text: 'Cancel', onclick: closeModal }),
      node('button.btn.btn-primary', {
        text: 'Roll',
        onclick: function () {
          attempt(function () {
            return session.checks.check(session.journal, character.id, {
              skill: skill.value,
              dc: parseInt(dc.value, 10) || 15,
              mode: mode.value || null
            });
          }, function () {
            closeModal();
            switchTab('play');
            renderAll();
            scrollStoryToBottom();
          });
        }
      })
    ]);
  }

  // ========================================================================
  // GEAR
  // ========================================================================

  function renderGear() {
    var session = app.session;
    var character = session.state.party[app.viewCharacterId]
      || session.partyList()[0];

    if (!character) {
      fill(el('gear-body'), node('div.empty', { text: 'No character selected.' }));
      return;
    }
    app.viewCharacterId = character.id;

    var load = DMAI.encumbrance(character);
    var party = session.partyList();

    fill(el('gear-body'), [
      node('div.sheet-head', {}, [
        node('div', {}, [
          node('h2', { text: character.name + '’s gear' }),
          node('div.subtitle', {
            text: 'Armour class ' + character.armor_class
              + '  ·  ' + load.carried.toFixed(0) + ' of ' + load.capacity + ' lb'
              + (load.overloaded ? '  ·  over capacity'
                : load.encumbered ? '  ·  encumbered' : '')
          })
        ]),
        node('div', { style: 'display:flex;gap:6px;flex-wrap:wrap' }, [
          node('button.btn.btn-sm', {
            text: 'Add from pack',
            onclick: function () { shopDialog(character); }
          }),
          node('button.btn.btn-sm', {
            text: 'Custom item',
            onclick: function () { customItemDialog(character); }
          }),
          node('button.btn.btn-sm', {
            text: 'Coins',
            onclick: function () { coinsDialog(character); }
          })
        ])
      ]),

      node('div.section', {}, [
        node('h3', { text: 'Worn and wielded' }),
        equipmentSlotsView(character)
      ]),

      node('div.section', {}, [
        node('h3', { text: 'Purse' }),
        node('div.purse', {}, ['platinum', 'gold', 'electrum', 'silver', 'copper']
          .map(function (denomination) {
            return node('div.coin' + (denomination === 'gold' ? '.gold' : ''), {}, [
              node('div.coin-value', { text: String(character.currency[denomination]) }),
              node('div.coin-name', { text: denomination.slice(0, 2) })
            ]);
          })),
        node('div.hint', {
          text: 'Worth ' + (DMAI.totalInCopper(character.currency) / 100).toFixed(2)
            + ' gp in total.'
        })
      ]),

      node('div.section', {}, [
        node('h3', { text: 'Carried' }),
        character.inventory.length
          ? node('table', {}, [
            node('thead', {}, [node('tr', {}, [
              node('th', { text: 'Item' }),
              node('th', { text: 'Kind' }),
              node('th.numeric', { text: 'Qty' }),
              node('th.numeric', { text: 'lb' }),
              node('th.numeric', { text: 'gp' }),
              node('th', { text: '' })
            ])]),
            node('tbody', {}, character.inventory.map(function (item) {
              return node('tr' + (item.equipped ? '.equipped' : ''), {}, [
                node('td', {}, [
                  node('div', { text: item.name }),
                  item.properties && item.properties.damage
                    ? node('div.faint', {
                      style: 'font-size:12px',
                      text: item.properties.damage + ' ' + item.properties.damage_type
                    })
                    : null
                ]),
                node('td', {}, [node('span.tag', { text: item.kind })]),
                node('td.numeric', { text: String(item.quantity) }),
                node('td.numeric', { text: (item.weight * item.quantity).toFixed(1) }),
                node('td.numeric', { text: String(item.value_gp) }),
                node('td.right.nowrap', {}, [
                  isEquippable(item)
                    ? node('button.btn.btn-sm', {
                      text: item.equipped ? 'Stow' : 'Equip',
                      onclick: function () {
                        attempt(function () {
                          return item.equipped
                            ? session.inventory.unequip(session.journal, character.id, item.id)
                            : session.inventory.equip(session.journal, character.id, item.id);
                        }, function () { renderAll(); });
                      }
                    })
                    : null,
                  party.length > 1
                    ? node('button.btn.btn-sm', {
                      text: 'Give',
                      onclick: function () { giveDialog(character, item); }
                    })
                    : null,
                  node('button.btn.btn-sm.btn-danger', {
                    text: 'Drop',
                    onclick: function () {
                      attempt(function () {
                        return session.inventory.take(
                          session.journal, character.id, item.id, { reason: 'dropped' }
                        );
                      }, function () { renderAll(); });
                    }
                  })
                ])
              ]);
            }))
          ])
          : node('div.empty', { text: 'Carrying nothing at all.' })
      ])
    ]);
  }

  function isEquippable(item) {
    return [DMAI.ItemKind.WEAPON, DMAI.ItemKind.ARMOR, DMAI.ItemKind.SHIELD]
      .indexOf(item.kind) !== -1;
  }

  function shopDialog(character) {
    var session = app.session;
    var catalogue = session.rules.catalogue();
    var buy = node('input', { type: 'checkbox' });

    openModal('Add an item from ' + session.rules.name, [
      node('label.checkbox.mb', {}, [
        buy, node('span', { text: 'Pay for it out of ' + character.name + '’s purse' })
      ]),
      node('table', {}, [
        node('thead', {}, [node('tr', {}, [
          node('th', { text: 'Item' }),
          node('th', { text: '' }),
          node('th.numeric', { text: 'gp' }),
          node('th', { text: '' })
        ])]),
        node('tbody', {}, catalogue.map(function (entry) {
          return node('tr', {}, [
            node('td', { text: entry.name }),
            node('td.faint', { style: 'font-size:12.5px', text: entry.detail }),
            node('td.numeric', { text: String(entry.cost_gp) }),
            node('td.right', {}, [
              node('button.btn.btn-sm', {
                text: 'Add',
                onclick: function () {
                  attempt(function () {
                    return buy.checked
                      ? session.inventory.buy(session.journal, character.id, entry.key, 1)
                      : session.inventory.give(session.journal, character.id, entry.key);
                  }, function () {
                    renderAll();
                    toast(entry.name + ' added.', 'good');
                  });
                }
              })
            ])
          ]);
        }))
      ])
    ], null, true);
  }

  function customItemDialog(character) {
    var name = node('input', { type: 'text', placeholder: 'Ember-Ash Amulet' });
    var kind = node('select', {}, Object.keys(DMAI.ItemKind).map(function (key) {
      return node('option', {
        value: DMAI.ItemKind[key], text: DMAI.titleCase(DMAI.ItemKind[key])
      });
    }));
    kind.value = DMAI.ItemKind.MISC;
    var quantity = node('input', { type: 'number', value: '1', min: '1' });
    var weight = node('input', { type: 'number', value: '0', min: '0', step: '0.1' });
    var value = node('input', { type: 'number', value: '0', min: '0', step: '0.1' });
    var description = node('textarea', { placeholder: 'What is it, and what does it do?' });

    openModal('A thing the DM invented', [
      node('label.field', {}, [node('span', { text: 'Name' }), name]),
      node('div.field-row', {}, [
        node('label.field', {}, [node('span', { text: 'Kind' }), kind]),
        node('label.field', {}, [node('span', { text: 'Quantity' }), quantity])
      ]),
      node('div.field-row', {}, [
        node('label.field', {}, [node('span', { text: 'Weight (lb)' }), weight]),
        node('label.field', {}, [node('span', { text: 'Value (gp)' }), value])
      ]),
      node('label.field', {}, [node('span', { text: 'Description' }), description])
    ], [
      node('button.btn', { text: 'Cancel', onclick: closeModal }),
      node('button.btn.btn-primary', {
        text: 'Give it to ' + character.name,
        onclick: function () {
          if (!name.value.trim()) { toast('It needs a name.', 'error'); return; }
          attempt(function () {
            return app.session.inventory.give(
              app.session.journal, character.id,
              DMAI.newItem({
                name: name.value.trim(),
                kind: kind.value,
                quantity: parseInt(quantity.value, 10) || 1,
                weight: parseFloat(weight.value) || 0,
                value_gp: parseFloat(value.value) || 0,
                description: description.value.trim()
              }),
              { quantity: parseInt(quantity.value, 10) || 1 }
            );
          }, function () { closeModal(); renderAll(); });
        }
      })
    ]);
  }

  function coinsDialog(character) {
    var fields = {};
    var rows = ['platinum', 'gold', 'electrum', 'silver', 'copper']
      .map(function (denomination) {
        fields[denomination] = node('input', { type: 'number', value: '0' });
        return node('label.field', {}, [
          node('span', {
            text: denomination + ' (has ' + character.currency[denomination] + ')'
          }),
          fields[denomination]
        ]);
      });

    openModal('Adjust ' + character.name + '’s purse', rows.concat([
      node('p.hint', {
        text: 'Positive adds, negative spends. The purse refuses to go '
          + 'negative in any denomination.'
      })
    ]), [
      node('button.btn', { text: 'Cancel', onclick: closeModal }),
      node('button.btn.btn-primary', {
        text: 'Apply',
        onclick: function () {
          var deltas = {};
          Object.keys(fields).forEach(function (denomination) {
            var value = parseInt(fields[denomination].value, 10);
            if (value) { deltas[denomination] = value; }
          });
          if (!Object.keys(deltas).length) { closeModal(); return; }
          attempt(function () {
            return app.session.inventory.adjustCurrency(
              app.session.journal, character.id, deltas, 'adjusted at the table'
            );
          }, function () { closeModal(); renderAll(); });
        }
      })
    ]);
  }

  function giveDialog(from, item) {
    var session = app.session;
    var others = session.partyList().filter(function (character) {
      return character.id !== from.id;
    });
    openModal('Give ' + item.name + ' to…',
      [node('div.cards', {}, others.map(function (character) {
        return node('div.card', {}, [
          node('h3', { text: character.name }),
          node('div.card-actions', {}, [
            node('button.btn.btn-sm.btn-primary', {
              text: 'Hand it over',
              onclick: function () {
                attempt(function () {
                  return session.inventory.transfer(
                    session.journal, from.id, character.id, item.id
                  );
                }, function () { closeModal(); renderAll(); });
              }
            })
          ])
        ]);
      }))]);
  }

  // ========================================================================
  // COMBAT
  // ========================================================================

  function renderCombat() {
    var session = app.session;
    var combat = session.state.combat;
    var body = el('combat-body');

    var header = node('div.sheet-head', {}, [
      node('div', {}, [
        node('h2', { text: combat.active ? 'Round ' + combat.round : 'Not in combat' }),
        node('div.subtitle', {
          text: combat.active
            ? 'The turn order is the source of truth for whose turn it is.'
            : 'Start a fight built against this party’s experience budget, '
              + 'or spawn creatures and roll initiative yourself.'
        })
      ]),
      node('div', { style: 'display:flex;gap:6px;flex-wrap:wrap' },
        combat.active
          ? [
            node('button.btn.btn-sm.btn-primary', {
              text: 'End turn',
              onclick: function () {
                attempt(function () {
                  return session.combat.advanceTurn(session.journal);
                }, function () { renderAll(); autoRunMonsters(); });
              }
            }),
            node('button.btn.btn-sm', {
              text: 'Run the monsters',
              onclick: function () { autoRunMonsters(true); }
            }),
            node('button.btn.btn-sm.btn-danger', {
              text: 'End combat',
              onclick: function () {
                attempt(function () {
                  return session.combat.end(session.journal, 'The DM calls it.');
                }, function () { renderAll(); });
              }
            })
          ]
          : [
            node('button.btn.btn-sm.btn-primary', {
              text: 'Build an encounter',
              onclick: encounterDialog
            }),
            node('button.btn.btn-sm', {
              text: 'Spawn creatures',
              onclick: spawnDialog
            }),
            session.bestiaryList().length
              ? node('button.btn.btn-sm', {
                text: 'Roll initiative',
                onclick: startFightDialog
              })
              : null
          ])
    ]);

    var sections = [header];

    if (combat.active) {
      var onTurn = DMAI.currentCombatant(combat);
      sections.push(node('div.section', {}, [
        node('h3', { text: 'Initiative' }),
        node('div.initiative', {}, combat.order.map(function (combatant) {
          var creature = DMAI.findCreature(session.state, combatant.creature_id);
          if (!creature) { return null; }

          var out = creature.dead || !DMAI.combatantActive(combatant);
          var classes = '.combatant'
            + (combatant.is_player ? '.player' : '')
            + (onTurn && onTurn.creature_id === combatant.creature_id ? '.on-turn' : '')
            + (out ? '.out' : '');

          return node('div' + classes, {}, [
            node('div.init', { text: String(combatant.initiative) }),
            node('div.who', {}, [
              node('div.name', { text: creature.name }),
              node('div.vitals', {}, [
                node('span', {
                  text: creature.hp.current + '/' + creature.hp.maximum + ' hp'
                }),
                node('span', { text: 'AC ' + session.rules.armorClass(creature) }),
                combatant.fled ? node('span', { text: 'fled' }) : null,
                combatant.surrendered ? node('span', { text: 'surrendered' }) : null,
                creature.dead ? node('span', { text: 'dead' }) : null
              ].concat((creature.conditions || []).map(function (condition) {
                return node('span', { text: condition.type });
              })).filter(Boolean))
            ]),
            node('div.actions', {}, [
              !out && combatant.is_player
                ? node('button.btn.btn-sm', {
                  text: 'Attack',
                  onclick: function () { attackDialog(creature); }
                })
                : null,
              !creature.dead
                ? node('button.btn.btn-sm', {
                  text: 'HP',
                  onclick: function () { adjustHitPointsDialog(creature); }
                })
                : null
            ])
          ]);
        }).filter(Boolean))
      ]));
    }

    var monsters = session.bestiaryList();
    if (monsters.length) {
      sections.push(node('div.section', {}, [
        node('h3', { text: 'On the board' }),
        node('table', {}, [
          node('thead', {}, [node('tr', {}, [
            node('th', { text: 'Creature' }),
            node('th.numeric', { text: 'HP' }),
            node('th.numeric', { text: 'AC' }),
            node('th', { text: 'Tactics' }),
            node('th', { text: '' })
          ])]),
          node('tbody', {}, monsters.map(function (creature) {
            return node('tr', {}, [
              node('td', {}, [
                node('span', { text: creature.name }),
                creature.dead ? node('span.tag.blood', {
                  style: 'margin-left:8px', text: 'dead'
                }) : null
              ]),
              node('td.numeric', {
                text: creature.hp.current + '/' + creature.hp.maximum
              }),
              node('td.numeric', { text: String(creature.armor_class) }),
              node('td.faint', { style: 'font-size:12.5px', text: creature.tactics || '' }),
              node('td.right', {}, [
                node('button.btn.btn-sm', {
                  text: 'Stat block',
                  onclick: function () { statBlockDialog(creature); }
                })
              ])
            ]);
          }))
        ])
      ]));
    }

    fill(body, sections);
  }

  /** Let every non-player combatant take its turn until a player is up. */
  function autoRunMonsters(force) {
    var session = app.session;
    var guard = 0;

    while (session.state.combat.active && guard < 40) {
      var creature = session.combat.current(session.journal);
      if (!creature) { break; }
      if (creature.kind === DMAI.CreatureKind.PLAYER) { break; }

      var acted = attempt(function () {
        return DMAI.takeMonsterTurn(session.combat, session.journal, creature.id);
      });
      if (!acted) { break; }
      if (session.state.combat.active) {
        attempt(function () { return session.combat.advanceTurn(session.journal); });
      }
      guard += 1;
    }

    renderAll();
    if (app.tab === 'play') { scrollStoryToBottom(); }
    if (force && !guard) { toast('It is a player’s turn.', 'error'); }
  }

  function attackDialog(attacker) {
    var session = app.session;
    var options = session.combat.attackOptions(attacker);
    var targets = session.combat.activeCombatants(session.journal)
      .filter(function (combatant) { return combatant.creature_id !== attacker.id; })
      .map(function (combatant) {
        return DMAI.findCreature(session.state, combatant.creature_id);
      })
      .filter(function (creature) { return creature && !creature.dead; });

    if (!targets.length) { toast('Nothing left to attack.', 'error'); return; }

    var weapon = node('select', {}, options.map(function (option) {
      return node('option', {
        value: option.key,
        text: option.name + '  (' + signed(option.bonus) + ' to hit, ' + option.damage + ')'
      });
    }));
    var target = node('select', {}, targets.map(function (creature) {
      return node('option', {
        value: creature.id,
        text: creature.name + '  (AC ' + session.rules.armorClass(creature)
          + ', ' + creature.hp.current + ' hp)'
      });
    }));
    var mode = node('select', {}, [
      node('option', { value: '', text: 'Let conditions decide' }),
      node('option', { value: DMAI.RollMode.ADVANTAGE, text: 'Advantage' }),
      node('option', { value: DMAI.RollMode.DISADVANTAGE, text: 'Disadvantage' })
    ]);

    openModal(attacker.name + ' attacks', [
      node('label.field', {}, [node('span', { text: 'With' }), weapon]),
      node('label.field', {}, [node('span', { text: 'Target' }), target]),
      node('label.field', {}, [node('span', { text: 'Roll mode' }), mode])
    ], [
      node('button.btn', { text: 'Cancel', onclick: closeModal }),
      node('button.btn.btn-primary', {
        text: 'Swing',
        onclick: function () {
          attempt(function () {
            return session.combat.attack(
              session.journal, attacker.id, target.value, weapon.value,
              { mode: mode.value || null }
            );
          }, function (outcome) {
            closeModal();
            renderAll();
            toast(outcome.hit
              ? (outcome.critical ? 'Critical hit! ' : 'Hit for ')
                + outcome.damage_dealt + ' damage.'
              : 'Miss.', outcome.hit ? 'good' : null);
          });
        }
      })
    ]);
  }

  function encounterDialog() {
    var session = app.session;
    var difficulty = node('select', {}, ['easy', 'moderate', 'hard', 'deadly']
      .map(function (value) {
        return node('option', { value: value, text: DMAI.titleCase(value) });
      }));
    difficulty.value = session.campaign.settings.difficulty || 'moderate';

    var themeBox = node('div', {
      style: 'display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:4px'
    }, session.rules.monsterKeys().map(function (key) {
      return node('label.checkbox', { style: 'font-size:13.5px' }, [
        node('input', { type: 'checkbox', value: key, 'data-theme': '1' }),
        node('span', { text: session.rules.monster(key).name })
      ]);
    }));

    openModal('Build an encounter', [
      node('label.field', {}, [node('span', { text: 'Difficulty' }), difficulty]),
      node('label.field', {}, [
        node('span', { text: 'Draw from (leave empty for the whole bestiary)' }),
        themeBox
      ]),
      node('p.hint', {
        text: 'The line-up is chosen against this party’s experience budget, '
          + 'so "moderate" means moderate for these characters at these levels. '
          + 'Everyone rolls initiative as soon as it is built.'
      })
    ], [
      node('button.btn', { text: 'Cancel', onclick: closeModal }),
      node('button.btn.btn-primary', {
        text: 'Start the fight',
        onclick: function () {
          var theme = all('input[data-theme]', themeBox)
            .filter(function (box) { return box.checked; })
            .map(function (box) { return box.value; });

          attempt(function () {
            return session.startEncounter(difficulty.value,
              theme.length ? { theme: theme } : {});
          }, function (started) {
            closeModal();
            renderAll();
            switchTab('combat');
            toast(DMAI.describeBudget(started.budget));
          });
        }
      })
    ], true);
  }

  function spawnDialog() {
    var session = app.session;
    var monster = node('select', {}, session.rules.monsterKeys().map(function (key) {
      var block = session.rules.monster(key);
      return node('option', {
        value: key, text: block.name + '  (CR ' + block.cr + ', ' + block.xp + ' xp)'
      });
    }));
    var count = node('input', { type: 'number', value: '1', min: '1', max: '12' });
    var rollHp = node('input', { type: 'checkbox', checked: 'checked' });

    openModal('Spawn creatures', [
      node('label.field', {}, [node('span', { text: 'Creature' }), monster]),
      node('label.field', {}, [node('span', { text: 'How many' }), count]),
      node('label.checkbox', {}, [
        rollHp,
        node('span', { text: 'Roll hit points (otherwise use the block’s average)' })
      ])
    ], [
      node('button.btn', { text: 'Cancel', onclick: closeModal }),
      node('button.btn.btn-primary', {
        text: 'Spawn',
        onclick: function () {
          attempt(function () {
            return session.spawn(
              monster.value, parseInt(count.value, 10) || 1, rollHp.checked
            );
          }, function (creatures) {
            closeModal();
            renderAll();
            toast(creatures.length + ' spawned.', 'good');
          });
        }
      })
    ]);
  }

  function startFightDialog() {
    var session = app.session;
    var candidates = session.partyList().filter(function (character) {
      return !character.dead;
    }).concat(session.bestiaryList().filter(function (creature) {
      return !creature.dead;
    }));

    var boxes = candidates.map(function (creature) {
      return node('label.checkbox', {}, [
        node('input', { type: 'checkbox', value: creature.id, checked: 'checked' }),
        node('span', { text: creature.name })
      ]);
    });

    openModal('Roll initiative', boxes.concat([
      node('p.hint', {
        text: 'Initiative is 1d20 + dexterity, and ties break on the dexterity '
          + 'modifier, so the order is reproducible under a seed.'
      })
    ]), [
      node('button.btn', { text: 'Cancel', onclick: closeModal }),
      node('button.btn.btn-primary', {
        text: 'Fight',
        onclick: function () {
          var chosen = boxes.map(function (box) {
            return box.querySelector('input');
          }).filter(function (input) { return input.checked; })
            .map(function (input) { return input.value; });

          attempt(function () {
            return session.combat.start(session.journal, chosen);
          }, function () { closeModal(); renderAll(); });
        }
      })
    ]);
  }

  function statBlockDialog(creature) {
    var session = app.session;
    openModal(creature.name, [
      node('div.muted.mb', {
        text: creature.species
          + (creature.cr !== undefined ? '  ·  CR ' + creature.cr : '')
          + (creature.xp ? '  ·  ' + creature.xp + ' xp' : '')
      }),
      abilityGrid(creature),
      node('div.stat-strip.mt', {}, [
        node('div.stat', {}, [
          node('div.stat-label', { text: 'HP' }),
          node('div.stat-value', { text: String(creature.hp.current) }),
          node('div.stat-sub', { text: 'of ' + creature.hp.maximum })
        ]),
        node('div.stat', {}, [
          node('div.stat-label', { text: 'AC' }),
          node('div.stat-value', { text: String(creature.armor_class) })
        ]),
        node('div.stat', {}, [
          node('div.stat-label', { text: 'Speed' }),
          node('div.stat-value', { text: String(creature.speed) })
        ]),
        node('div.stat', {}, [
          node('div.stat-label', { text: 'Morale' }),
          node('div.stat-value', { text: String(creature.morale) })
        ])
      ]),
      (creature.attacks || []).length
        ? node('div.section.mt', {}, [
          node('h3', { text: 'Attacks' }),
          node('table', {}, [node('tbody', {}, creature.attacks.map(function (attack) {
            return node('tr', {}, [
              node('td', { text: attack.name }),
              node('td.numeric.mono', { text: signed(attack.bonus) }),
              node('td.numeric.mono', {
                text: attack.damage + ' ' + attack.damage_type
              })
            ]);
          }))])
        ])
        : null,
      (creature.goals || []).length
        ? node('div.section', {}, [
          node('h3', { text: 'What it wants' }),
          node('ul.list-plain', {}, creature.goals.map(function (goal) {
            return node('li', { text: goal });
          })),
          creature.tactics
            ? node('div.hint', { text: 'Tactics: ' + creature.tactics })
            : null
        ])
        : null,
      node('div', { style: 'display:flex;gap:6px;flex-wrap:wrap;margin-top:12px' }, [
        node('button.btn.btn-sm', {
          text: 'Damage / heal',
          onclick: function () { closeModal(); adjustHitPointsDialog(creature); }
        }),
        node('button.btn.btn-sm', {
          text: 'Conditions',
          onclick: function () { closeModal(); conditionsDialog(creature); }
        })
      ])
    ], null, true);
  }

  // ========================================================================
  // WORLD
  // ========================================================================

  function renderWorld() {
    var session = app.session;
    var state = session.state;
    var location = DMAI.currentLocation(state);
    var locations = Object.keys(state.world.locations)
      .map(function (id) { return state.world.locations[id]; });
    var npcs = Object.keys(state.world.npcs)
      .map(function (id) { return state.world.npcs[id]; });
    var factions = Object.keys(state.world.factions)
      .map(function (id) { return state.world.factions[id]; });
    var quests = Object.keys(state.story.quests)
      .map(function (id) { return state.story.quests[id]; });

    fill(el('world-body'), [
      node('div.sheet-head', {}, [
        node('div', {}, [
          node('h2', { text: location ? location.name : 'Somewhere unmapped' }),
          node('div.subtitle', {
            text: DMAI.describeTime(state.world.time) + '  ·  '
              + state.world.weather
              + (state.campaign.settings.sandbox_mode ? '  ·  sandbox' : '')
          })
        ]),
        node('div', { style: 'display:flex;gap:6px;flex-wrap:wrap' }, [
          node('button.btn.btn-sm', {
            text: 'Short rest',
            onclick: function () { takeRest('short'); }
          }),
          node('button.btn.btn-sm', {
            text: 'Long rest',
            onclick: function () { takeRest('long'); }
          }),
          node('button.btn.btn-sm', {
            text: 'Pass time',
            onclick: passTimeDialog
          }),
          node('button.btn.btn-sm', {
            text: 'Roll weather',
            onclick: function () {
              attempt(function () {
                return session.world.rollWeather(session.journal);
              }, function () { renderAll(); });
            }
          })
        ])
      ]),

      location && location.description
        ? node('div.section', {}, [
          node('h3', { text: 'Here' }),
          node('div', { text: location.description }),
          (location.features || []).length
            ? node('ul.list-plain.mt', {}, location.features.map(function (feature) {
              return node('li', { text: feature });
            }))
            : null
        ])
        : null,

      node('div.two-col', {}, [
        node('div.section', {}, [
          node('h3', { text: 'Places' }),
          node('div.toolbar', {}, [
            node('button.btn.btn-sm', {
              text: 'Add a place',
              onclick: addLocationDialog
            })
          ]),
          locations.length
            ? node('table', {}, [node('tbody', {}, locations.map(function (place) {
              return node('tr', {}, [
                node('td', {}, [
                  node('div', { text: place.name }),
                  node('div.faint', { style: 'font-size:12px', text: place.kind })
                ]),
                node('td.right', {}, [
                  place.id === state.current_location_id
                    ? node('span.tag.ember', { text: 'here' })
                    : node('button.btn.btn-sm', {
                      text: 'Travel',
                      onclick: function () {
                        attempt(function () {
                          return session.atlas.enter(session.journal, place.id);
                        }, function () { renderAll(); });
                      }
                    })
                ])
              ]);
            }))])
            : node('div.empty', { text: 'The map is blank.' })
        ]),

        node('div.section', {}, [
          node('h3', { text: 'Quests' }),
          node('div.toolbar', {}, [
            node('button.btn.btn-sm', { text: 'Add a quest', onclick: addQuestDialog })
          ]),
          quests.length
            ? node('div', {}, quests.map(function (quest) {
              return node('div', {
                style: 'padding:9px 0;border-bottom:1px solid var(--line-soft)'
              }, [
                node('div', {
                  style: 'display:flex;justify-content:space-between;gap:10px;align-items:center'
                }, [
                  node('strong', { text: quest.title }),
                  node('span.tag' + questTone(quest.status), { text: quest.status })
                ]),
                quest.summary
                  ? node('div.faint', { style: 'font-size:12.5px', text: quest.summary })
                  : null,
                node('div.mt', {}, quest.objectives.map(function (objective) {
                  return node('div', {
                    style: 'display:flex;justify-content:space-between;'
                      + 'gap:10px;font-size:13px;padding:2px 0'
                  }, [
                    node('span', {
                      style: objective.completed
                        ? 'text-decoration:line-through;color:var(--text-faint)' : '',
                      text: (objective.completed ? '✓ ' : '□ ')
                        + objective.description
                    }),
                    !objective.completed
                      ? node('button.btn.btn-sm', {
                        text: 'Done',
                        onclick: function () {
                          attempt(function () {
                            return session.quests.completeObjective(
                              session.journal, quest.id, objective.id
                            );
                          }, function () { renderAll(); });
                        }
                      })
                      : null
                  ]);
                })),
                quest.status === DMAI.QuestStatus.RUMORED
                  ? node('button.btn.btn-sm.mt', {
                    text: 'Accept',
                    onclick: function () {
                      attempt(function () {
                        return session.quests.accept(session.journal, quest.id);
                      }, function () { renderAll(); });
                    }
                  })
                  : null
              ]);
            }))
            : node('div.empty', { text: 'No quests on the board.' })
        ])
      ]),

      node('div.two-col', {}, [
        node('div.section', {}, [
          node('h3', { text: 'People' }),
          node('div.toolbar', {}, [
            node('button.btn.btn-sm', { text: 'Add an NPC', onclick: addNpcDialog })
          ]),
          npcs.length
            ? node('table', {}, [node('tbody', {}, npcs.map(function (npc) {
              return node('tr', {}, [
                node('td', {}, [
                  node('div', { text: npc.name }),
                  node('div.faint', { style: 'font-size:12px', text: npc.role || '' })
                ]),
                node('td.right', {}, [
                  node('button.btn.btn-sm', {
                    text: 'Stat block',
                    onclick: function () { statBlockDialog(npc); }
                  })
                ])
              ]);
            }))])
            : node('div.empty', { text: 'Nobody here yet.' })
        ]),

        node('div.section', {}, [
          node('h3', { text: 'What the DM remembers' }),
          state.memories.length
            ? node('ul.list-plain', {}, state.memories.slice()
              .sort(function (left, right) { return right.importance - left.importance; })
              .slice(0, 18)
              .map(function (memory) {
                return node('li', {}, [
                  node('span.tag', { text: memory.tier }),
                  node('span', { text: '  ' + memory.content })
                ]);
              }))
            : node('div.empty', {
              text: 'Nothing yet. Memories are pulled out of the log as play '
                + 'happens — discoveries, decisions, deaths, promises.'
            })
        ])
      ]),

      factions.length
        ? node('div.section', {}, [
          node('h3', { text: 'Powers' }),
          node('table', {}, [node('tbody', {}, factions.map(function (faction) {
            return node('tr', {}, [
              node('td', { text: faction.name }),
              node('td.faint', {
                style: 'font-size:12.5px',
                text: faction.agenda[0] ? 'Next: ' + faction.agenda[0] : ''
              }),
              node('td.numeric', { text: 'standing ' + signed(faction.party_standing) })
            ]);
          }))])
        ])
        : null,

      state.world.world_events.length
        ? node('div.section', {}, [
          node('h3', { text: 'The world moved' }),
          node('ul.list-plain', {}, state.world.world_events.slice(-12)
            .map(function (line) { return node('li', { text: line }); }))
        ])
        : null
    ]);
  }

  function questTone(status) {
    return {
      active: '.ember', completed: '.moss', failed: '.blood',
      abandoned: '', rumored: '.steel'
    }[status] || '';
  }

  function takeRest(kind) {
    var session = app.session;
    var ids = session.partyList().map(function (character) { return character.id; });
    if (!ids.length) { toast('Nobody to rest.', 'error'); return; }

    if (kind === 'long') {
      attempt(function () { return session.world.longRest(session.journal, ids); },
        function () { renderAll(); toast('The party wakes rested.', 'good'); });
      return;
    }

    // A short rest is only worth anything if hit dice are spent, so ask.
    var fields = {};
    var rows = session.partyList().map(function (character) {
      fields[character.id] = node('input', {
        type: 'number', value: '0', min: '0', max: String(character.level)
      });
      return node('label.field', {}, [
        node('span', {
          text: character.name + ' — hit dice to spend (up to ' + character.level + ')'
        }),
        fields[character.id]
      ]);
    });

    openModal('Short rest', rows.concat([
      node('p.hint', {
        text: 'An hour passes either way. Spending a hit die rolls it plus '
          + 'constitution and heals that much.'
      })
    ]), [
      node('button.btn', { text: 'Cancel', onclick: closeModal }),
      node('button.btn.btn-primary', {
        text: 'Rest',
        onclick: function () {
          var spend = {};
          Object.keys(fields).forEach(function (id) {
            var value = parseInt(fields[id].value, 10);
            if (value > 0) { spend[id] = value; }
          });
          attempt(function () {
            return session.world.shortRest(session.journal, ids, spend);
          }, function () { closeModal(); renderAll(); });
        }
      })
    ]);
  }

  function passTimeDialog() {
    var minutes = node('input', { type: 'number', value: '60', min: '1' });
    var reason = node('input', { type: 'text', placeholder: 'travelling the Cinder Road' });

    openModal('Pass time', [
      node('label.field', {}, [node('span', { text: 'Minutes' }), minutes]),
      node('label.field', {}, [node('span', { text: 'What you were doing' }), reason]),
      node('p.hint', {
        text: 'Consequences the world owes you come due as the clock moves, '
          + 'and in sandbox mode factions advance their plans a day at a time.'
      })
    ], [
      node('button.btn', { text: 'Cancel', onclick: closeModal }),
      node('button.btn.btn-primary', {
        text: 'Let it pass',
        onclick: function () {
          attempt(function () {
            return app.session.world.tick(
              app.session.journal, parseInt(minutes.value, 10) || 0,
              reason.value.trim()
            );
          }, function (happened) {
            closeModal();
            renderAll();
            if (happened && happened.length) {
              toast(happened.length + ' thing(s) happened while you waited.');
            }
          });
        }
      })
    ]);
  }

  function addLocationDialog() {
    var name = node('input', { type: 'text', placeholder: 'The Ashen Hearth' });
    var kind = node('select', {}, ['place', 'settlement', 'building', 'room',
      'dungeon', 'wilderness'].map(function (value) {
      return node('option', { value: value, text: DMAI.titleCase(value) });
    }));
    var description = node('textarea', { placeholder: 'Smoke-blacked beams, a fire going.' });
    var enterNow = node('input', { type: 'checkbox', checked: 'checked' });

    openModal('Add a place', [
      node('label.field', {}, [node('span', { text: 'Name' }), name]),
      node('label.field', {}, [node('span', { text: 'Kind' }), kind]),
      node('label.field', {}, [node('span', { text: 'Description' }), description]),
      node('label.checkbox', {}, [enterNow, node('span', { text: 'Travel there now' })])
    ], [
      node('button.btn', { text: 'Cancel', onclick: closeModal }),
      node('button.btn.btn-primary', {
        text: 'Add',
        onclick: function () {
          if (!name.value.trim()) { toast('It needs a name.', 'error'); return; }
          attempt(function () {
            var session = app.session;
            var place = session.atlas.addLocation(session.journal, name.value.trim(), {
              kind: kind.value,
              description: description.value.trim(),
              discovered: true,
              connect_to: session.state.current_location_id
                ? [session.state.current_location_id] : []
            });
            if (enterNow.checked) {
              session.atlas.enter(session.journal, place.id);
            }
            return place;
          }, function () { closeModal(); renderAll(); });
        }
      })
    ]);
  }

  function addNpcDialog() {
    var session = app.session;
    var name = node('input', { type: 'text', placeholder: 'Maerin' });
    var role = node('input', { type: 'text', placeholder: 'innkeeper' });
    var template = node('select', {}, session.rules.monsterKeys().map(function (key) {
      return node('option', { value: key, text: session.rules.monster(key).name });
    }));
    template.value = 'guard';
    var knowledge = node('textarea', {
      placeholder: 'One fact per line — what they know and might let slip.'
    });

    openModal('Add an NPC', [
      node('label.field', {}, [node('span', { text: 'Name' }), name]),
      node('label.field', {}, [node('span', { text: 'Role' }), role]),
      node('label.field', {}, [
        node('span', { text: 'Stat-block template' }), template
      ]),
      node('label.field', {}, [node('span', { text: 'What they know' }), knowledge]),
      node('p.hint', {
        text: 'The template supplies the numbers, so a bartender who ends up '
          + 'in a brawl has real statistics.'
      })
    ], [
      node('button.btn', { text: 'Cancel', onclick: closeModal }),
      node('button.btn.btn-primary', {
        text: 'Add',
        onclick: function () {
          if (!name.value.trim()) { toast('They need a name.', 'error'); return; }
          attempt(function () {
            var npc = DMAI.spawnNpc(session.rules, name.value.trim(), {
              role: role.value.trim(),
              template: template.value,
              knowledge: knowledge.value.split('\n')
                .map(function (line) { return line.trim(); })
                .filter(Boolean)
            });
            return session.atlas.addNpc(session.journal, npc, {
              location_id: session.state.current_location_id
            });
          }, function () { closeModal(); renderAll(); });
        }
      })
    ]);
  }

  function addQuestDialog() {
    var title = node('input', { type: 'text', placeholder: 'Find the caravan' });
    var summary = node('textarea', { placeholder: 'What is being asked, and by whom.' });
    var objectives = node('textarea', {
      placeholder: 'One objective per line.'
    });
    var accept = node('input', { type: 'checkbox', checked: 'checked' });

    openModal('Add a quest', [
      node('label.field', {}, [node('span', { text: 'Title' }), title]),
      node('label.field', {}, [node('span', { text: 'Summary' }), summary]),
      node('label.field', {}, [node('span', { text: 'Objectives' }), objectives]),
      node('label.checkbox', {}, [
        accept, node('span', { text: 'The party has already taken the job' })
      ])
    ], [
      node('button.btn', { text: 'Cancel', onclick: closeModal }),
      node('button.btn.btn-primary', {
        text: 'Add',
        onclick: function () {
          if (!title.value.trim()) { toast('It needs a title.', 'error'); return; }
          attempt(function () {
            return app.session.quests.add(app.session.journal, title.value.trim(), {
              summary: summary.value.trim(),
              objectives: objectives.value.split('\n')
                .map(function (line) { return line.trim(); })
                .filter(Boolean),
              status: accept.checked
                ? DMAI.QuestStatus.ACTIVE : DMAI.QuestStatus.RUMORED
            });
          }, function () { closeModal(); renderAll(); });
        }
      })
    ]);
  }

  // ========================================================================
  // THE LOG
  // ========================================================================

  function renderLog() {
    var session = app.session;
    var events = session.store.all();
    var checkpoints = session.checkpoints();

    fill(el('log-body'), [
      node('div.sheet-head', {}, [
        node('div', {}, [
          node('h2', { text: 'The log' }),
          node('div.subtitle', {
            text: events.length + ' events. The game state is the fold of this '
              + 'list — nothing else is stored, so a save can never disagree '
              + 'with its own history.'
          })
        ]),
        node('div', { style: 'display:flex;gap:6px;flex-wrap:wrap' }, [
          node('button.btn.btn-sm', {
            text: 'Checkpoint',
            onclick: function () {
              var label = node('input', { type: 'text', placeholder: 'before the vault' });
              openModal('Mark this point', [
                node('label.field', {}, [node('span', { text: 'Label' }), label])
              ], [
                node('button.btn', { text: 'Cancel', onclick: closeModal }),
                node('button.btn.btn-primary', {
                  text: 'Mark',
                  onclick: function () {
                    attempt(function () {
                      return session.checkpoint(label.value.trim() || 'manual');
                    }, function () { closeModal(); renderAll(); });
                  }
                })
              ]);
            }
          }),
          node('button.btn.btn-sm', {
            text: 'Export for the CLI',
            onclick: function () {
              var files = DMAI.exportForPython(session);
              DMAI.download('campaign.json', files['campaign.json']);
              setTimeout(function () {
                DMAI.download('events.jsonl', files['events.jsonl'], 'application/x-ndjson');
              }, 350);
              toast('Two files: drop them in a folder under ~/.dmai/campaigns.', 'good');
            }
          })
        ])
      ]),

      checkpoints.length
        ? node('div.section', {}, [
          node('h3', { text: 'Checkpoints — rolling back truncates the log' }),
          node('table', {}, [node('tbody', {}, checkpoints.slice().reverse()
            .map(function (mark) {
              return node('tr', {}, [
                node('td', { text: mark.label || '(unlabelled)' }),
                node('td.numeric.mono', { text: '#' + mark.event_seq }),
                node('td.faint', {
                  style: 'font-size:12px',
                  text: String(mark.created_at).slice(0, 16).replace('T', ' ')
                }),
                node('td.right', {}, [
                  node('button.btn.btn-sm.btn-danger', {
                    text: 'Roll back here',
                    onclick: function () {
                      confirmAction(
                        'Roll back to "' + (mark.label || 'this point') + '"? '
                        + 'Everything after event #' + mark.event_seq
                        + ' is discarded and cannot be recovered.',
                        'Roll back',
                        function () {
                          attempt(function () {
                            return session.rollbackToCheckpoint(mark);
                          }, function () {
                            renderAll();
                            toast('Rolled back to #' + mark.event_seq + '.', 'good');
                          });
                        }
                      );
                    }
                  })
                ])
              ]);
            }))])
        ])
        : null,

      node('div.section', {}, [
        node('h3', { text: 'Every event' }),
        node('div', {}, events.slice().reverse().slice(0, 400).map(function (event) {
          var visibility = event.visibility === DMAI.Visibility.DM_ONLY
            ? '.dm-only'
            : (event.visibility === DMAI.Visibility.PRIVATE ? '.private' : '');
          return node('div.log-line' + visibility, {}, [
            node('div.seq', { text: '#' + event.seq }),
            node('div.type', { text: event.type }),
            node('div.body', { text: event.summary || '—' })
          ]);
        })),
        events.length > 400
          ? node('div.hint', { text: 'Showing the most recent 400 of ' + events.length + '.' })
          : null
      ])
    ]);
  }

  // ========================================================================
  // WIRING
  // ========================================================================

  function switchTab(name) {
    app.tab = name;
    all('.tab').forEach(function (tab) {
      tab.classList.toggle('active', tab.getAttribute('data-tab') === name);
    });
    all('.panel').forEach(function (panel) {
      panel.classList.toggle('active', panel.id === 'panel-' + name);
    });
    renderAll();
    if (name === 'play') { scrollStoryToBottom(); }
  }

  /** One render pass.  Cheap enough to run after every action. */
  function renderAll() {
    if (!app.session) { return; }
    var session = app.session;

    el('campaign-name').textContent = session.campaign.name;
    el('clock').textContent = DMAI.describeTime(session.state.world.time)
      + '  ·  ' + session.state.world.weather
      + (DMAI.currentLocation(session.state)
        ? '  ·  ' + DMAI.currentLocation(session.state).name : '');

    var combat = session.state.combat;
    var badge = el('combat-badge');
    if (combat.active) {
      badge.style.display = '';
      badge.textContent = 'R' + combat.round;
    } else {
      badge.style.display = 'none';
    }

    if (app.tab === 'play') { renderStory(); renderComposer(); }
    if (app.tab === 'party') { renderParty(); }
    if (app.tab === 'sheet') { renderSheet(); }
    if (app.tab === 'gear') { renderGear(); }
    if (app.tab === 'combat') { renderCombat(); }
    if (app.tab === 'world') { renderWorld(); }
    if (app.tab === 'log') { renderLog(); }
  }

  function wire() {
    // Library screen
    el('new-campaign').addEventListener('click', newCampaignDialog);
    el('import-file').addEventListener('change', function (event) {
      if (event.target.files[0]) { importBundle(event.target.files[0]); }
      event.target.value = '';
    });
    el('import-campaign').addEventListener('click', function () {
      el('import-file').click();
    });
    el('import-character').addEventListener('click', function () {
      el('import-file').click();
    });

    // Table screen
    el('back-to-library').addEventListener('click', leaveCampaign);
    el('save-now').addEventListener('click', function () { runCommand('/save'); });
    el('export-now').addEventListener('click', function () { exportCampaign(null); });

    all('.tab').forEach(function (tab) {
      tab.addEventListener('click', function () {
        switchTab(tab.getAttribute('data-tab'));
      });
    });

    el('say-button').addEventListener('click', say);
    var input = el('say-input');
    input.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        say();
      }
    });
    input.addEventListener('input', function () {
      input.style.height = 'auto';
      input.style.height = Math.min(160, input.scrollHeight) + 'px';
    });

    all('[data-quick]').forEach(function (button) {
      button.addEventListener('click', function () {
        var command = button.getAttribute('data-quick');
        if (command === '/help' || command === '/recap') { runCommand(command); return; }
        input.value = command + ' ';
        input.focus();
      });
    });

    el('add-character').addEventListener('click', addCharacterDialog);
    el('adopt-character').addEventListener('click', adoptFromVaultDialog);

    el('modal-close').addEventListener('click', closeModal);
    el('modal-backdrop').addEventListener('click', function (event) {
      if (event.target === el('modal-backdrop')) { closeModal(); }
    });

    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') { closeModal(); }
    });

    // Leaving with unsaved work should say so.
    window.addEventListener('beforeunload', function (event) {
      if (app.session && app.dirty) {
        event.preventDefault();
        event.returnValue = '';
      }
    });
  }

  function start() {
    wire();
    renderLibrary();
    el('screen-library').classList.add('active');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }

  //: Exposed so the desktop shell can drive the same client from outside.
  DMAI.app = app;
  DMAI.ui = {
    renderAll: renderAll,
    switchTab: switchTab,
    openCampaign: openCampaign,
    leaveCampaign: leaveCampaign,
    newCampaignDialog: newCampaignDialog,
    exportCampaign: exportCampaign,
    addCharacterDialog: addCharacterDialog,
    toast: toast,
    library: library
  };
})(window.DMAI = window.DMAI || {});
