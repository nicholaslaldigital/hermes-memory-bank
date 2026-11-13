/* Standalone viewer boot (served by core/server.py). */
(function () {
  'use strict';
  function meta(name) {
    var node = document.querySelector('meta[name="' + name + '"]');
    return node ? node.getAttribute('content') || '' : '';
  }
  // The access key arrives in the link's fragment (never in the page, never
  // sent to the server). It is kept for this tab only, so a reload still
  // works, and removed from the address bar.
  var KEY = 'memorybank-key';

  function takeKeyFromLink() {
    var fragment = (window.location.hash || '').replace(/^#/, '');
    if (!fragment) return '';
    try { window.sessionStorage.setItem(KEY, fragment); } catch (e) { /* private mode: keep it in memory */ }
    try { window.history.replaceState(null, '', window.location.pathname + window.location.search); } catch (e) { /* ignore */ }
    return fragment;
  }

  var token = takeKeyFromLink();
  if (!token) {
    try { token = window.sessionStorage.getItem(KEY) || ''; } catch (e) { token = ''; }
  }
  // The link pasted into a tab that already shows this page only changes the
  // part after "#", which does not reload anything by itself: start over with
  // the new key (also when the viewer was restarted and the old key is stale).
  window.addEventListener('hashchange', function () {
    var fresh = takeKeyFromLink();
    if (fresh && fresh !== token) window.location.reload();
  });

  var profile = meta('mb-title');
  var app = document.getElementById('app');

  if (!token) {
    var note = document.createElement('p');
    note.style.cssText = 'max-width:34rem;margin:20vh auto 0;padding:0 1.5rem;line-height:1.5;font-size:15px';
    note.textContent = 'This page needs the link that "hermes memorybank ui" printed when it started ' +
      '(it carries the access key after the # sign). Open that link, or start the viewer again.';
    app.appendChild(note);
    return;
  }

  function fetchJSON(path, options) {
    options = options || {};
    return fetch('/api' + path, {
      method: options.method || 'GET',
      headers: { 'X-MemoryBank-Token': token, 'Content-Type': 'application/json' },
      body: options.body ? JSON.stringify(options.body) : undefined,
      cache: 'no-store'
    }).then(function (res) {
      if (!res.ok) {
        var err = new Error('HTTP ' + res.status);
        err.status = res.status;
        if (res.status === 401) {
          try { window.sessionStorage.removeItem(KEY); } catch (e) { /* ignore */ }
        }
        throw err;
      }
      return res.json();
    });
  }

  window.memoryBank = createMemoryGraph(app, {
    fetchJSON: fetchJSON,
    title: 'Memory Bank' + (profile && profile !== 'hermes' ? ' · ' + profile : '')
  });
})();
