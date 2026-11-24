/*
 * simple-apps SDK v1 — include with <script src="/_sdk/v1.js"></script>
 *
 * The app name is taken from the first path segment of the page URL, so an app
 * served at /grocery/ automatically reads and writes /_data/v1/grocery/...
 *
 *   const items = simpleApps.collection('items');
 *   await items.add({ name: 'Milk' });           // -> record {id, data, created_at, updated_at}
 *   await items.list({ order: '-created_at' });  // -> [record]
 *   await items.update(id, { done: true });      // merge fields (null removes a field)
 *   await simpleApps.kv.set('settings', {...});
 */
(function () {
  'use strict';

  var app = (location.pathname.split('/')[1] || '').toLowerCase();
  var base = '/_data/v1/' + encodeURIComponent(app);

  function request(method, path, body) {
    var opts = { method: method, headers: { Accept: 'application/json' }, credentials: 'same-origin' };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    return fetch(base + path, opts).then(function (res) {
      if (res.status === 204) return null;
      return res.text().then(function (text) {
        var payload = null;
        try { payload = text ? JSON.parse(text) : null; } catch (e) { payload = null; }
        if (!res.ok) {
          var err = new Error((payload && payload.error) || res.statusText || 'request failed');
          err.status = res.status;
          throw err;
        }
        return payload;
      });
    });
  }

  function notFoundAs(value) {
    return function (err) {
      if (err && err.status === 404) return value;
      throw err;
    };
  }

  function enc(part) {
    return encodeURIComponent(String(part));
  }

  function collection(name) {
    var path = '/' + enc(name);
    return {
      list: function (opts) {
        opts = opts || {};
        var q = [];
        if (opts.order) q.push('order=' + enc(opts.order));
        if (opts.limit != null) q.push('limit=' + enc(opts.limit));
        if (opts.offset != null) q.push('offset=' + enc(opts.offset));
        return request('GET', path + (q.length ? '?' + q.join('&') : '')).then(function (r) {
          return r.items;
        });
      },
      page: function (opts) {
        opts = opts || {};
        var q = ['order=' + enc(opts.order || 'created_at'), 'limit=' + enc(opts.limit || 100), 'offset=' + enc(opts.offset || 0)];
        return request('GET', path + '?' + q.join('&'));
      },
      get: function (id) { return request('GET', path + '/' + enc(id)).catch(notFoundAs(null)); },
      add: function (data) { return request('POST', path, data); },
      put: function (id, data) { return request('PUT', path + '/' + enc(id), data); },
      update: function (id, patch) { return request('PATCH', path + '/' + enc(id), patch); },
      remove: function (id) {
        return request('DELETE', path + '/' + enc(id)).then(function () { return true; }).catch(notFoundAs(false));
      }
    };
  }

  var kv = {
    get: function (key, fallback) {
      return request('GET', '/_kv/' + enc(key)).then(function (r) { return r.value; })
        .catch(notFoundAs(fallback === undefined ? null : fallback));
    },
    set: function (key, value) {
      return request('PUT', '/_kv/' + enc(key), value).then(function (r) { return r.value; });
    },
    remove: function (key) {
      return request('DELETE', '/_kv/' + enc(key)).then(function () { return true; }).catch(notFoundAs(false));
    }
  };

  window.simpleApps = Object.freeze({ version: 'v1', app: app, collection: collection, kv: kv });
})();
