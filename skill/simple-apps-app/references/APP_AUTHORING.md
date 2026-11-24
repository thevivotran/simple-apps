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

## Relational data (optional)

For data with relations, constraints or reporting (joins, sums, group-by), give
the app its own SQLite tables instead of collections. Add a `db/` folder:

```
db/migrations/001_init.sql
db/queries.sql
```

**Migrations** create and change tables. They run once each, in number order,
when you deploy; if one fails the deploy fails and nothing changes.

```sql
-- db/migrations/001_init.sql
CREATE TABLE stores (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE items (
  id       INTEGER PRIMARY KEY,
  store_id INTEGER NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
  name     TEXT NOT NULL CHECK (length(name) > 0),
  qty      INTEGER NOT NULL DEFAULT 1,
  done     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX items_by_store ON items(store_id);
```

* **Never edit or delete a migration once deployed** — add `002_….sql` instead
  (the deploy is refused otherwise).
* Keep changes **additive** (new tables, `ALTER TABLE … ADD COLUMN`), so the
  previous version still works after a rollback.
* Allowed: tables, indexes, views, triggers, `INSERT/UPDATE/DELETE`. Not allowed:
  `PRAGMA`, `ATTACH`, `BEGIN/COMMIT`, temp tables, virtual tables (FTS), and
  writing the platform tables `records`, `kv`, `_migrations` (reading them is fine,
  e.g. to copy collection data into a new table).

**Queries** are the only SQL the page can run. Each starts with a `-- name:` line;
add `(write)` if it changes data. Use named parameters (`:store`), never `?`.

```sql
-- db/queries.sql
-- name: stores
SELECT id, name FROM stores ORDER BY name;

-- name: items_by_store
SELECT i.id, i.name, i.qty, i.done, s.name AS store
FROM items i JOIN stores s ON s.id = i.store_id
WHERE s.id = :store ORDER BY i.done, i.name;

-- name: add_item (write)
INSERT INTO items (store_id, name, qty) VALUES (:store, :name, :qty) RETURNING *;

-- name: toggle_item (write)
UPDATE items SET done = NOT done WHERE id = :id RETURNING *;

-- name: remove_items (write)
DELETE FROM items WHERE id IN (SELECT value FROM json_each(:ids));
```

```js
const { rows } = await simpleApps.sql('items_by_store', { store: 3 });
const [item]   = (await simpleApps.sql('add_item', { store: 3, name: 'Milk', qty: 2 })).rows;
await simpleApps.sql('remove_items', { ids: [4, 7] });  // arrays/objects arrive as JSON text
```

`simpleApps.sql()` resolves to `{ rows, changes, truncated }`: `rows` from the
last statement that returns rows (use `RETURNING` on writes), at most 1000 —
`truncated: true` means add a `LIMIT` / paging. Give joined columns distinct
names (`s.name AS store`). A query may hold several statements; they run in one
transaction. Every query is checked against the schema at deploy time, so typos
fail the deploy, not the page.

Errors: `409` for a broken constraint (`UNIQUE`, `CHECK`, `FOREIGN KEY`, or
`SELECT RAISE(ABORT, 'message')` in a trigger — use these for validation and show
`err.message`), `400` for a missing parameter, `404` unknown query, `503` query
too slow (> 2 s) or database busy, `507` quota full.

Collections, kv and tables can be mixed in one app.

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
