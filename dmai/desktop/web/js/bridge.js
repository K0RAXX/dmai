/* The page's half of the bridge.
 *
 * `pywebview.api.*` is available only after the window fires `pywebviewready`,
 * and every call is a promise that resolves to `{ok: true, ...}` or
 * `{ok: false, error}` -- the Python side never throws across the boundary.
 * `call()` turns the failure case into a rejected promise so the UI can use
 * try/catch, and `ready()` lets the app wait for the bridge to exist.
 */
window.DMAI = (function () {
  'use strict';

  var readyPromise = null;

  /** Resolves once `pywebview.api` is callable. */
  function ready() {
    if (readyPromise) { return readyPromise; }
    readyPromise = new Promise(function (resolve) {
      if (window.pywebview && window.pywebview.api) { resolve(); return; }
      window.addEventListener('pywebviewready', function () { resolve(); }, { once: true });
    });
    return readyPromise;
  }

  /**
   * Call a bridge operation.
   *
   * Rejects with an Error carrying the Python side's own message, so the
   * caller can show the player what went wrong rather than "undefined".
   */
  function call(operation, ...args) {
    return ready().then(function () {
      var api = window.pywebview && window.pywebview.api;
      if (!api || typeof api[operation] !== 'function') {
        throw new Error('the desktop bridge has no operation "' + operation + '"');
      }
      return api[operation].apply(api, args);
    }).then(function (result) {
      if (!result || result.ok !== true) {
        var error = new Error((result && result.error) || 'the game did not answer');
        error.cancelled = Boolean(result && result.cancelled);
        throw error;
      }
      return result;
    });
  }

  return { ready: ready, call: call };
}());
