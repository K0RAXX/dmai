/* Quests, objectives, and the ledger of what the world owes the party.
 *
 * A port of dmai/engine/quests/tracker.py.  Two ideas carry this module:
 *
 * - A quest is written to the log whole.  Status changes, new objectives and
 *   rewards all record the resulting quest, not a delta, so replay is exact
 *   and a save written by a newer build degrades gracefully in an older one.
 * - A decision is not the same as its consequence.  `decide` records what the
 *   players chose; `promise` records what the world will do about it, later.
 *   Keeping the two apart is what lets the DM honour a choice made three
 *   sessions ago instead of quietly forgetting it.
 */
(function (DMAI) {
  'use strict';

  var QuestStatus = DMAI.QuestStatus;
  var EventType = DMAI.EventType;
  var Visibility = DMAI.Visibility;

  var QuestError = DMAI.defineError('QuestError');

  //: Transitions the tracker will make.  Anything else is a bug in the caller,
  //: not a judgement call: a completed quest does not quietly reopen.
  var ALLOWED_TRANSITIONS = {};
  ALLOWED_TRANSITIONS[QuestStatus.RUMORED] = [
    QuestStatus.ACTIVE, QuestStatus.ABANDONED, QuestStatus.FAILED
  ];
  ALLOWED_TRANSITIONS[QuestStatus.ACTIVE] = [
    QuestStatus.COMPLETED, QuestStatus.FAILED, QuestStatus.ABANDONED
  ];
  ALLOWED_TRANSITIONS[QuestStatus.ABANDONED] = [QuestStatus.ACTIVE];
  ALLOWED_TRANSITIONS[QuestStatus.COMPLETED] = [];
  ALLOWED_TRANSITIONS[QuestStatus.FAILED] = [];

  function newObjective(spec) {
    if (spec && typeof spec === 'object') {
      return Object.assign({
        id: DMAI.newId('obj'), description: '', completed: false,
        optional: false, visibility: Visibility.PUBLIC
      }, spec);
    }
    return {
      id: DMAI.newId('obj'), description: String(spec), completed: false,
      optional: false, visibility: Visibility.PUBLIC
    };
  }

  function newQuest(fields) {
    return Object.assign({
      id: DMAI.newId('quest'),
      title: 'Untitled',
      summary: '',
      status: QuestStatus.RUMORED,
      objectives: [],
      giver_npc_id: null,
      location_id: null,
      reward: '',
      is_main_plot: false,
      //: Quests unlocked when this one resolves.
      follow_ups: []
    }, fields || {});
  }

  /** The write path for everything in the story ledger. */
  function QuestTracker() {}

  // --- creating ------------------------------------------------------------

  /** Put a quest on the board.  Rumoured by default: heard, not accepted. */
  QuestTracker.prototype.add = function (journal, title, options) {
    var settings = options || {};
    var quest = newQuest({
      title: title,
      summary: settings.summary || '',
      objectives: (settings.objectives || []).map(newObjective),
      status: settings.status || QuestStatus.RUMORED,
      giver_npc_id: settings.giver_npc_id || null,
      location_id: settings.location_id || null,
      reward: settings.reward || '',
      is_main_plot: Boolean(settings.is_main_plot),
      follow_ups: settings.follow_ups || []
    });
    journal.record(EventType.QUEST_ADDED, {
      target_id: quest.id,
      summary: 'New quest: ' + quest.title + '.',
      quest: quest,
      visibility: settings.visibility || Visibility.PUBLIC
    });
    return journal.state.story.quests[quest.id];
  };

  /** Extend a quest the party is already running -- the plot thickens. */
  QuestTracker.prototype.addObjective = function (journal, questId, description, options) {
    var settings = options || {};
    var quest = this._require(journal, questId);
    var objective = newObjective({
      description: description,
      optional: Boolean(settings.optional),
      visibility: settings.visibility || Visibility.PUBLIC
    });
    var updated = DMAI.deepCopy(quest);
    updated.objectives = updated.objectives.concat([objective]);
    this._write(journal, updated, quest.title + ': ' + description);
    return objective;
  };

  // --- moving a quest along ------------------------------------------------

  /** The party takes the job. */
  QuestTracker.prototype.accept = function (journal, questId) {
    return this.setStatus(journal, questId, QuestStatus.ACTIVE);
  };

  /** Tick one box.  The quest completes itself when the last one falls. */
  QuestTracker.prototype.completeObjective = function (journal, questId, objectiveId) {
    var quest = this._require(journal, questId);
    var objective = quest.objectives.filter(function (candidate) {
      return candidate.id === objectiveId;
    })[0];
    if (!objective) {
      throw new QuestError(quest.title + ' has no objective ' + objectiveId);
    }

    if (!objective.completed) {
      journal.record(EventType.OBJECTIVE_COMPLETED, {
        target_id: questId,
        summary: quest.title + ': ' + objective.description + '.',
        quest_id: questId,
        objective_id: objectiveId,
        visibility: objective.visibility
      });
    }
    // The reducer flips ACTIVE -> COMPLETED once every required objective is
    // done; log that resolution so the story ledger reads correctly.
    quest = journal.state.story.quests[questId];
    if (quest.status === QuestStatus.COMPLETED) {
      this._write(journal, quest, 'Quest complete: ' + quest.title + '.');
    }
    return journal.state.story.quests[questId];
  };

  /**
   * Close a quest out, marking every required objective done.
   *
   * Used when the players resolve a quest in a way its objectives never
   * anticipated -- which is a success and not an error.
   */
  QuestTracker.prototype.complete = function (journal, questId, reward) {
    var quest = this._require(journal, questId);
    var updated = DMAI.deepCopy(quest);
    updated.objectives.forEach(function (objective) {
      if (!objective.optional) { objective.completed = true; }
    });
    updated.status = QuestStatus.COMPLETED;
    if (reward) { updated.reward = reward; }
    this._write(journal, updated, 'Quest complete: ' + updated.title + '.');
    return journal.state.story.quests[questId];
  };

  QuestTracker.prototype.fail = function (journal, questId, reason) {
    return this.setStatus(journal, questId, QuestStatus.FAILED, reason);
  };

  QuestTracker.prototype.abandon = function (journal, questId, reason) {
    return this.setStatus(journal, questId, QuestStatus.ABANDONED, reason);
  };

  QuestTracker.prototype.setStatus = function (journal, questId, status, reason) {
    var quest = this._require(journal, questId);
    if (status === quest.status) { return quest; }
    if (ALLOWED_TRANSITIONS[quest.status].indexOf(status) === -1) {
      throw new QuestError(
        quest.title + ' is ' + quest.status + '; it cannot become ' + status
      );
    }
    var updated = DMAI.deepCopy(quest);
    updated.status = status;

    var headlines = {};
    headlines[QuestStatus.ACTIVE] = 'Quest accepted: ' + quest.title + '.';
    headlines[QuestStatus.COMPLETED] = 'Quest complete: ' + quest.title + '.';
    headlines[QuestStatus.FAILED] = 'Quest failed: ' + quest.title + '.';
    headlines[QuestStatus.ABANDONED] = 'Quest abandoned: ' + quest.title + '.';
    headlines[QuestStatus.RUMORED] = 'Quest set aside: ' + quest.title + '.';

    var headline = headlines[status];
    if (reason) { headline = headline + ' (' + reason + ')'; }
    this._write(journal, updated, headline);
    return journal.state.story.quests[questId];
  };

  /**
   * Move a resolved quest's follow-ups from nowhere onto the board.
   *
   * Follow-ups are stored as titles, so a DM can author a chain before the
   * later links exist as quests.
   */
  QuestTracker.prototype.unlockFollowUps = function (journal, questId) {
    var quest = this._require(journal, questId);
    if ([QuestStatus.COMPLETED, QuestStatus.FAILED].indexOf(quest.status) === -1) {
      throw new QuestError(quest.title + ' has not resolved yet');
    }
    var existing = Object.keys(journal.state.story.quests).map(function (id) {
      return journal.state.story.quests[id].title;
    });
    var self = this;
    return quest.follow_ups
      .filter(function (title) { return existing.indexOf(title) === -1; })
      .map(function (title) {
        return self.add(journal, title, {
          summary: 'Follows from ' + quest.title + '.',
          is_main_plot: quest.is_main_plot
        });
      });
  };

  // --- the story ledger ----------------------------------------------------

  /** Record what the party now knows. */
  QuestTracker.prototype.discover = function (journal, content, options) {
    var settings = options || {};
    journal.record(EventType.DISCOVERY, {
      actor_id: settings.actor_id || null,
      summary: content,
      content: content,
      visibility: settings.visibility || Visibility.PUBLIC,
      audience_id: settings.audience_id || null
    });
  };

  /**
   * Log a player choice, and optionally what the world owes it.
   *
   * The DM never decides *for* the players; it records what they chose and
   * lets the world answer later.
   */
  QuestTracker.prototype.decide = function (journal, decision, options) {
    var settings = options || {};
    journal.record(EventType.STORY_DECISION, {
      actor_id: settings.actor_id || null,
      summary: decision,
      decision: decision
    });
    if (!settings.effect) { return null; }
    return this.promise(journal, decision, settings.effect, settings.due_day);
  };

  /** Owe the players a consequence.  Fired later by the world simulator. */
  QuestTracker.prototype.promise = function (journal, trigger, effect, dueDay) {
    var consequence = {
      id: DMAI.newId('csq'),
      trigger: trigger,
      effect: effect,
      resolved: false,
      //: In-world day this should fire; null means "when narratively apt".
      due_day: dueDay === undefined ? null : dueDay
    };
    journal.record(EventType.CONSEQUENCE_ADDED, {
      summary: 'Pending consequence: ' + effect,
      consequence: consequence,
      visibility: Visibility.DM_ONLY
    });
    return consequence;
  };

  /** Pay a debt the world owed the party. */
  QuestTracker.prototype.resolve = function (journal, consequenceId, narration) {
    var consequence = this._consequence(journal, consequenceId);
    if (consequence.resolved) { return consequence; }
    journal.record(EventType.CONSEQUENCE_RESOLVED, {
      summary: narration || consequence.effect,
      consequence_id: consequenceId
    });
    return this._consequence(journal, consequenceId);
  };

  // --- reading -------------------------------------------------------------

  /**
   * Unresolved consequences, optionally only those now due.
   *
   * A consequence with no due_day is always returned: it fires when the moment
   * is narratively apt, and that is a judgement the DM layer makes.
   */
  QuestTracker.prototype.pending = function (journal, onOrBeforeDay) {
    var pending = journal.state.story.consequences.filter(function (consequence) {
      return !consequence.resolved;
    });
    if (onOrBeforeDay === undefined || onOrBeforeDay === null) { return pending; }
    return pending.filter(function (consequence) {
      return consequence.due_day === null || consequence.due_day <= onOrBeforeDay;
    });
  };

  QuestTracker.prototype.byStatus = function (journal, status) {
    var quests = journal.state.story.quests;
    return Object.keys(quests)
      .map(function (id) { return quests[id]; })
      .filter(function (quest) { return quest.status === status; });
  };

  /** Every box still unticked on an active quest: the party's to-do list. */
  QuestTracker.prototype.openObjectives = function (journal) {
    var quests = journal.state.story.quests;
    var open = [];
    Object.keys(quests).forEach(function (id) {
      var quest = quests[id];
      if (quest.status !== QuestStatus.ACTIVE) { return; }
      quest.objectives.forEach(function (objective) {
        if (!objective.completed) { open.push({ quest: quest, objective: objective }); }
      });
    });
    return open;
  };

  // --- internals -----------------------------------------------------------

  QuestTracker.prototype._require = function (journal, questId) {
    var quest = journal.state.story.quests[questId];
    if (!quest) {
      throw new QuestError('no quest ' + questId + ' in this campaign');
    }
    return quest;
  };

  QuestTracker.prototype._consequence = function (journal, consequenceId) {
    var found = journal.state.story.consequences.filter(function (consequence) {
      return consequence.id === consequenceId;
    })[0];
    if (!found) {
      throw new QuestError('no pending consequence ' + consequenceId);
    }
    return found;
  };

  QuestTracker.prototype._write = function (journal, quest, summary) {
    journal.record(EventType.QUEST_UPDATED, {
      target_id: quest.id,
      summary: summary,
      quest: quest,
      status: quest.status
    });
  };

  DMAI.QuestError = QuestError;
  DMAI.QuestTracker = QuestTracker;
  DMAI.ALLOWED_TRANSITIONS = ALLOWED_TRANSITIONS;
  DMAI.newQuest = newQuest;
  DMAI.newObjective = newObjective;
})(window.DMAI = window.DMAI || {});
