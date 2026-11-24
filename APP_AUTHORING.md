# Writing an app for simple-apps

Paste this file (or point Claude at it) when asking for a small family app that
will be hosted on simple-apps. It is the complete contract an app needs.

## Rules

1. **Static files only.** HTML, CSS, JS, images, fonts. No server code, no build
   step required on the platform (a `dist/` from Vite etc. is fine).
2. **`index.html` at the root** of the zip (a single top-level folder such as
   `dist/` is stripped automatically).
3. **Use relative paths** for your own assets: `src="app.js"`, `href="./style.css"`
   — never `/app.js`. The app is served under `/<app-name>/`, not at `/`.
   For Vite set `base: './'`.
4. **Store data through the SDK** — never `localStorage` for anything that should
   be shared between family members or devices.
5. Optional `app.json` at the root:
   ```json
   { "name": "grocery", "title": "Grocery List", "quota_mb": 10, "spa": false, "sdk": "v1" }
   ```
   `spa: true` makes unknown paths without a file extension return `index.html`
   (for client-side routers).

## SDK

```html
<script src="/_sdk/v1.js"></script>
```

```js
const items = simpleApps.collection('items');        // any name: [a-z0-9][a-z0-9_-]*

const all  = await items.list({ order: '-created_at', limit: 100 }); // [record]
const one  = await items.get(id);                     // record or null
const rec  = await items.add({ name: 'Milk', done: false });
await items.update(rec.id, { done: true });           // merge; null removes a field
await items.put('main', { theme: 'dark' });           // create/replace with your own id
await items.remove(rec.id);                           // true / false

await simpleApps.kv.set('settings', { currency: 'VND' });
const settings = await simpleApps.kv.get('settings', { currency: 'VND' }); // value or fallback
```

A **record** looks like:

```json
{ "id": "Xk3f9aQ1bZ0c", "data": { "name": "Milk", "done": false },
  "created_at": "2026-11-24T10:00:00.000000Z", "updated_at": "2026-11-24T10:00:00.000000Z" }
```

Your fields are always under `record.data`. Errors reject with an `Error` that
has `.status` (e.g. 507 = the app's data quota is full).

Prefer `update()` (field merge) over rewriting whole records, so two family
members editing different items or fields at the same time never overwrite
each other. There is no live push in v1: refresh with `list()` on page focus
or on a timer (e.g. every 15–30 s) if several people use the app at once.

## Deploy

```sh
(cd dist && zip -r ../grocery.zip .)
curl -fsS -X PUT --data-binary @grocery.zip \
  -H "Content-Type: application/zip" \
  -H "Authorization: Bearer $SIMPLE_APPS_TOKEN" \
  https://simple-apps.<tailnet>.ts.net/_api/v1/apps/grocery
```

…or upload the zip on the portal at `https://simple-apps.<tailnet>.ts.net/`.
The app is then live at `https://simple-apps.<tailnet>.ts.net/grocery/`.
