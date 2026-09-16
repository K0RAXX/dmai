/* The library: campaigns and character sheets that survive the tab closing.
 *
 * This is the standalone build's answer to dmai/persistence/store.py, and it
 * keeps that module's central promise: **the state is never saved, only the
 * log**.  A campaign on disk is its metadata plus its event log, and loading
 * it means replaying that log.  A save can therefore never disagree with its
 * own history, and a truncated write costs the last event rather than the game.
 *
 * Two shelves:
 *
 * - Campaigns.  Everything needed to resume a table.
 * - The character vault.  Sheets that outlive the campaign they were rolled
 *   for, so a favourite character can be carried into the next one.
 *
 * Storage is localStorage, which is per-origin and per-browser.  It is the
 * convenience layer, not the archive: export writes a .dmai.json bundle that
 * is the real portable artefact, and it interchanges with the Python engine's
 * campaign.json + events.jsonl pair.
 */
(function (DMAI) {
  'use strict';

  var CAMPAIGN_PREFIX = 'dmai.campaign.';
  var VAULT_PREFIX = 'dmai.character.';
  var INDEX_KEY = 'dmai.index';
  var SETTINGS_KEY = 'dmai.settings';

  //: Bumped when the stored shape changes.  A bundle carrying a higher number
  //: than this build understands is opened anyway, on the same reasoning as
  //: the reducer's tolerance for unknown event types: read what you can.
  var BUNDLE_VERSION = 1;

  var LibraryError = DMAI.defineError('LibraryError');

  /**
   * A storage backend.
   *
   * localStorage is the default, but the desktop shell swaps in a bridge that
   * writes real files, so the library code is identical in both.  Anything
   * with get/set/remove/keys will do.
   */
  function BrowserStorage() {
    this.available = probe();
  }

  function probe() {
    try {
      var key = 'dmai.probe';
      window.localStorage.setItem(key, '1');
      window.localStorage.removeItem(key);
      return true;
    } catch (error) {
      return false;
    }
  }

  BrowserStorage.prototype.get = function (key) {
    if (!this.available) { return null; }
    return window.localStorage.getItem(key);
  };

  BrowserStorage.prototype.set = function (key, value) {
    if (!this.available) {
      throw new LibraryError(
        'this browser is not allowing local storage, so nothing can be saved '
        + 'here -- use Export to write a file instead'
      );
    }
    try {
      window.localStorage.setItem(key, value);
    } catch (error) {
      throw new LibraryError(
        'local storage is full; export a campaign to a file and delete it here'
      );
    }
  };

  BrowserStorage.prototype.remove = function (key) {
    if (!this.available) { return; }
    window.localStorage.removeItem(key);
  };

  BrowserStorage.prototype.keys = function (prefix) {
    if (!this.available) { return []; }
    var found = [];
    for (var index = 0; index < window.localStorage.length; index += 1) {
      var key = window.localStorage.key(index);
      if (key && key.indexOf(prefix) === 0) { found.push(key); }
    }
    return found;
  };

  /** A shelf of campaigns and a vault of characters. */
  function Library(storage) {
    this.storage = storage || new BrowserStorage();
  }

  Library.prototype.available = function () {
    return this.storage.available !== false;
  };

  // --- campaigns -----------------------------------------------------------

  /**
   * Persist a session.  Cheap enough to call after every action.
   *
   * Only the log and the campaign metadata are written; the state is not,
   * because the state is a function of the log.
   */
  Library.prototype.saveCampaign = function (session) {
    var campaign = session.campaign;
    campaign.updated_at = DMAI.utcnow();

    var bundle = {
      bundle_version: BUNDLE_VERSION,
      campaign: DMAI.deepCopy(campaign),
      events: session.store.all()
    };
    this.storage.set(CAMPAIGN_PREFIX + campaign.id, JSON.stringify(bundle));
    this._index(campaign, session.store.length());
    return bundle;
  };

  /** Replay a campaign back into a live session. */
  Library.prototype.loadCampaign = function (campaignId) {
    var raw = this.storage.get(CAMPAIGN_PREFIX + campaignId);
    if (!raw) {
      throw new LibraryError('no campaign saved as ' + campaignId);
    }
    return this.openBundle(JSON.parse(raw));
  };

  /** Open a bundle from anywhere -- storage, a file, a paste. */
  Library.prototype.openBundle = function (bundle) {
    var normalised = normaliseBundle(bundle);
    return DMAI.GameSession.restore(normalised.campaign, normalised.events);
  };

  Library.prototype.deleteCampaign = function (campaignId) {
    this.storage.remove(CAMPAIGN_PREFIX + campaignId);
    var index = this.index();
    delete index.campaigns[campaignId];
    this.storage.set(INDEX_KEY, JSON.stringify(index));
  };

  /**
   * Copy a campaign under a new name and identity.
   *
   * The log comes along, so the copy has the same history -- which is what
   * makes this the safe way to try a different road from the same point.
   */
  Library.prototype.duplicateCampaign = function (campaignId, name) {
    var raw = this.storage.get(CAMPAIGN_PREFIX + campaignId);
    if (!raw) {
      throw new LibraryError('no campaign saved as ' + campaignId);
    }
    var bundle = JSON.parse(raw);
    bundle.campaign = DMAI.deepCopy(bundle.campaign);
    bundle.campaign.id = DMAI.newId('camp');
    bundle.campaign.name = name || (bundle.campaign.name + ' (copy)');
    bundle.campaign.created_at = DMAI.utcnow();
    bundle.campaign.updated_at = DMAI.utcnow();

    this.storage.set(
      CAMPAIGN_PREFIX + bundle.campaign.id, JSON.stringify(bundle)
    );
    this._index(bundle.campaign, bundle.events.length);
    return bundle.campaign;
  };

  /**
   * Enough to draw a load-game list without replaying anything.
   *
   * The index is a cache, not the record: if it disagrees with what is on the
   * shelf, the shelf wins, and a missing entry is rebuilt from the bundle.
   */
  Library.prototype.campaigns = function () {
    var index = this.index();
    var self = this;
    var summaries = [];

    this.storage.keys(CAMPAIGN_PREFIX).forEach(function (key) {
      var campaignId = key.slice(CAMPAIGN_PREFIX.length);
      var entry = index.campaigns[campaignId];
      if (!entry) {
        try {
          var bundle = JSON.parse(self.storage.get(key));
          entry = self._index(bundle.campaign, bundle.events.length);
        } catch (error) {
          return;  // an unreadable bundle is skipped, not fatal
        }
      }
      summaries.push(entry);
    });

    return summaries.sort(function (left, right) {
      return String(right.updated_at).localeCompare(String(left.updated_at));
    });
  };

  Library.prototype.index = function () {
    var raw = this.storage.get(INDEX_KEY);
    if (!raw) { return { campaigns: {} }; }
    try {
      var parsed = JSON.parse(raw);
      return { campaigns: parsed.campaigns || {} };
    } catch (error) {
      return { campaigns: {} };
    }
  };

  Library.prototype._index = function (campaign, eventCount) {
    var index = this.index();
    var entry = {
      id: campaign.id,
      name: campaign.name,
      premise: campaign.premise,
      updated_at: campaign.updated_at,
      created_at: campaign.created_at,
      events: eventCount,
      rules_pack: campaign.settings.rules_pack,
      tone: campaign.settings.tone
    };
    index.campaigns[campaign.id] = entry;
    this.storage.set(INDEX_KEY, JSON.stringify(index));
    return entry;
  };

  /** A one-line summary of a campaign, for the shelf. */
  Library.prototype.describe = function (entry) {
    return entry.name + ' (' + entry.events + ' events, updated '
      + String(entry.updated_at).slice(0, 16).replace('T', ' ') + ')';
  };

  // --- the character vault -------------------------------------------------

  /**
   * Keep a sheet outside any campaign.
   *
   * A vaulted sheet is a snapshot, not a live link: taking it into a campaign
   * copies it under a fresh id, so levelling the copy does not silently edit
   * the original.
   */
  Library.prototype.saveCharacter = function (character, options) {
    var settings = options || {};
    var sheet = DMAI.exportCharacter(character);
    var record = {
      id: sheet.id,
      saved_at: DMAI.utcnow(),
      origin_campaign: settings.campaign_name || '',
      origin_campaign_id: settings.campaign_id || '',
      notes: settings.notes || '',
      character: sheet
    };
    this.storage.set(VAULT_PREFIX + sheet.id, JSON.stringify(record));
    return record;
  };

  Library.prototype.loadCharacter = function (characterId) {
    var raw = this.storage.get(VAULT_PREFIX + characterId);
    if (!raw) {
      throw new LibraryError('no character saved as ' + characterId);
    }
    return JSON.parse(raw);
  };

  Library.prototype.deleteCharacter = function (characterId) {
    this.storage.remove(VAULT_PREFIX + characterId);
  };

  Library.prototype.characters = function () {
    var self = this;
    var records = [];
    this.storage.keys(VAULT_PREFIX).forEach(function (key) {
      try {
        records.push(JSON.parse(self.storage.get(key)));
      } catch (error) {
        // Skip an unreadable sheet rather than breaking the vault.
      }
    });
    return records.sort(function (left, right) {
      return String(right.saved_at).localeCompare(String(left.saved_at));
    });
  };

  // --- table settings ------------------------------------------------------

  Library.prototype.settings = function () {
    var raw = this.storage.get(SETTINGS_KEY);
    if (!raw) { return {}; }
    try {
      return JSON.parse(raw);
    } catch (error) {
      return {};
    }
  };

  Library.prototype.saveSettings = function (settings) {
    this.storage.set(SETTINGS_KEY, JSON.stringify(settings));
    return settings;
  };

  // --- import and export ---------------------------------------------------

  /**
   * Accept a bundle in any of the shapes this project writes.
   *
   * Three are understood: the standalone bundle, the Python engine's
   * campaign.json + events.jsonl pair handed over as one object, and a bare
   * campaign with no log (a fresh table someone else set up).
   */
  function normaliseBundle(bundle) {
    if (!bundle || typeof bundle !== 'object') {
      throw new LibraryError('that file is not a campaign bundle');
    }
    var campaign = bundle.campaign || bundle;
    if (!campaign || !campaign.id || !campaign.name) {
      throw new LibraryError(
        'that file has no campaign in it (expected a "campaign" object with an id and a name)'
      );
    }

    var events = bundle.events || [];
    if (typeof events === 'string') {
      // events.jsonl pasted in whole: one JSON object per line.
      events = events.split(/\r?\n/)
        .filter(function (line) { return line.trim(); })
        .map(function (line) { return JSON.parse(line); });
    }

    // Fill in anything an older or foreign bundle left out, so a campaign
    // written by a different build still opens.
    campaign = Object.assign(DMAI.newCampaign(), campaign);
    campaign.settings = Object.assign(
      DMAI.newCampaignSettings(), campaign.settings || {}
    );

    events = events.slice().sort(function (left, right) {
      return left.seq - right.seq;
    });
    return { campaign: campaign, events: events };
  }

  /** The portable artefact: one JSON object holding metadata and the log. */
  function exportBundle(session) {
    return {
      bundle_version: BUNDLE_VERSION,
      exported_at: DMAI.utcnow(),
      engine: 'dmai-standalone',
      campaign: DMAI.deepCopy(session.campaign),
      events: session.store.all()
    };
  }

  /**
   * The Python engine's on-disk pair, for moving a campaign to the CLI.
   *
   * Written as two strings rather than files, because the browser can only
   * hand the user a download -- the desktop shell writes them as real files
   * into a campaign directory.
   */
  function exportForPython(session) {
    return {
      'campaign.json': JSON.stringify(DMAI.deepCopy(session.campaign), null, 2),
      'events.jsonl': session.store.all().map(function (event) {
        return JSON.stringify(event);
      }).join('\n') + '\n'
    };
  }

  /** Hand the user a file.  The one place the library touches the DOM. */
  function download(filename, text, mime) {
    var blob = new Blob([text], { type: mime || 'application/json' });
    var url = URL.createObjectURL(blob);
    var link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    // Revoke on the next tick: revoking synchronously can beat the download.
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }

  /** A filename that will not fight with the filesystem. */
  function safeFilename(name, extension) {
    var stem = String(name || 'campaign')
      .replace(/[^a-z0-9]+/gi, '-')
      .replace(/^-+|-+$/g, '')
      .toLowerCase() || 'campaign';
    return stem + extension;
  }

  DMAI.Library = Library;
  DMAI.LibraryError = LibraryError;
  DMAI.BrowserStorage = BrowserStorage;
  DMAI.BUNDLE_VERSION = BUNDLE_VERSION;
  DMAI.normaliseBundle = normaliseBundle;
  DMAI.exportBundle = exportBundle;
  DMAI.exportForPython = exportForPython;
  DMAI.download = download;
  DMAI.safeFilename = safeFilename;
})(window.DMAI = window.DMAI || {});
