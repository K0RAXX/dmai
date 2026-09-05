/* DOM helpers, overlays and toasts.
 *
 * Kept apart from app.js so the game code below reads as game code.  Nothing
 * here knows what a campaign is.
 */
window.UI = (function () {
  'use strict';

  function el(id) { return document.getElementById(id); }

  /**
   * Build an element: node('div.hero.is-active', {onclick: f}, [children]).
   *
   * Text always goes through `text`, never `html`, so a campaign name or a
   * line of DM narration cannot inject markup into the page.
   */
  function node(spec, attributes, children) {
    var parts = String(spec).split('.');
    var element = document.createElement(parts[0] || 'div');
    if (parts.length > 1) { element.className = parts.slice(1).join(' '); }

    Object.keys(attributes || {}).forEach(function (key) {
      var value = attributes[key];
      if (value === null || value === undefined || value === false) { return; }
      if (key === 'text') { element.textContent = value; }
      else if (key === 'svg') { element.appendChild(icon(value)); }
      else if (key.indexOf('on') === 0) { element.addEventListener(key.slice(2), value); }
      else if (key === 'data') {
        Object.keys(value).forEach(function (name) {
          element.setAttribute('data-' + name, value[name]);
        });
      } else { element.setAttribute(key, value); }
    });

    (children || []).forEach(function (child) {
      if (child === null || child === undefined || child === false) { return; }
      element.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
    });
    return element;
  }

  /** An <svg><use href="#id"> referencing one of the page's symbols. */
  function icon(name) {
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    var use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', '#' + name);
    svg.appendChild(use);
    return svg;
  }

  function clear(element) {
    while (element.firstChild) { element.removeChild(element.firstChild); }
    return element;
  }

  function fill(element, children) {
    clear(element);
    (children || []).forEach(function (child) {
      if (child) { element.appendChild(child); }
    });
    return element;
  }

  // --- overlay -------------------------------------------------------------

  var overlay = null;
  var overlayBody = null;
  var onOverlayClose = null;

  function openOverlay(children, options) {
    overlay = overlay || el('overlay');
    overlayBody = overlayBody || el('overlay-body');
    onOverlayClose = (options || {}).onClose || null;

    fill(overlayBody, children);
    overlay.hidden = false;

    var focusable = overlayBody.querySelector('input, select, textarea, button');
    if (focusable) { focusable.focus(); }
  }

  function closeOverlay() {
    if (!overlay || overlay.hidden) { return; }
    overlay.hidden = true;
    clear(overlayBody);
    var handler = onOverlayClose;
    onOverlayClose = null;
    if (handler) { handler(); }
  }

  function overlayIsOpen() {
    return Boolean(overlay) && !overlay.hidden;
  }

  // --- toasts --------------------------------------------------------------

  function toast(message, kind) {
    var host = el('toasts');
    var item = node('div.toast' + (kind === 'bad' ? '.is-bad' : ''), { text: message });
    host.appendChild(item);
    window.setTimeout(function () {
      item.style.transition = 'opacity 240ms ease, transform 240ms ease';
      item.style.opacity = '0';
      item.style.transform = 'translateY(6px)';
      window.setTimeout(function () { item.remove(); }, 260);
    }, kind === 'bad' ? 5200 : 3000);
  }

  // --- form pieces ---------------------------------------------------------

  function field(label, control) {
    return node('div.form-row', {}, [node('label', { text: label }), control]);
  }

  function input(id, attributes) {
    return node('input.field', Object.assign({ id: id, type: 'text' }, attributes || {}));
  }

  function select(id, options, selected) {
    var element = node('select.field', { id: id });
    options.forEach(function (option) {
      var value = typeof option === 'string' ? option : option.value;
      var label = typeof option === 'string' ? option : option.label;
      var item = node('option', { value: value, text: label });
      if (value === selected) { item.setAttribute('selected', 'selected'); }
      element.appendChild(item);
    });
    return element;
  }

  /** Sentence-case a machine word: "half-orc" -> "Half-orc". */
  function title(text) {
    var value = String(text || '').replace(/_/g, ' ');
    return value.charAt(0).toUpperCase() + value.slice(1);
  }

  // --- wiring --------------------------------------------------------------

  document.addEventListener('DOMContentLoaded', function () {
    el('overlay-close').addEventListener('click', closeOverlay);
    el('overlay').addEventListener('mousedown', function (event) {
      if (event.target === el('overlay')) { closeOverlay(); }
    });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && overlayIsOpen()) { closeOverlay(); }
    });
  });

  return {
    el: el,
    node: node,
    icon: icon,
    clear: clear,
    fill: fill,
    field: field,
    input: input,
    select: select,
    title: title,
    toast: toast,
    openOverlay: openOverlay,
    closeOverlay: closeOverlay,
    overlayIsOpen: overlayIsOpen
  };
}());
