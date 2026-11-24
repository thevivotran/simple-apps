# simple-apps — Architecture

> A tiny, self-hosted "drop a zip, get an app" host for small family web apps,
> running on the homelab k3s cluster and reachable only over Tailscale.

The document goes from the broadest view to the most concrete:

1. [High-level design](#1-high-level-design) — why it exists, what it is, where it lives
2. [Conceptual design](#2-conceptual-design) — the ideas and vocabulary
3. [Logical design](#3-logical-design) — components, data model, interfaces
4. [Functional design](#4-functional-design) — exact behaviour of every operation
5. [Evolution](#5-evolution) — how it grows, and when an app should leave

---

## 1. High-level design

### 1.1 Problem

Small apps (usually a single HTML/JS page written with Claude) currently need
the full delivery pipeline: a new git repo, CI that builds a container image,
new manifests in the `homelab` repo, then a Flux reconcile. That is the right
amount of ceremony for multi-component apps, but far too much
for a grocery list or a chore chart.

### 1.2 Goal

Deploying a small family app should be: **upload a zip → it is live**.
The app should also be able to **save data** without a backend of its own —
from schema-free JSON collections up to its own relational tables.

### 1.3 Scope

| In scope (v1)                                   | Out of scope (by design)                       |
|-------------------------------------------------|------------------------------------------------|
| Static apps: HTML, JS, CSS, images, fonts       | Running any server-side code from an app       |
| Per-app JSON data storage (collections + kv)    | Public-internet exposure                       |
| Per-app relational tables: bundled migrations + named SQL queries (§3.10) | SQL sent from the browser |
| Upload / replace / rollback / delete / export   | Multi-tenant or untrusted users                |
| One URL per app on one Tailscale hostname       | High availability, horizontal scaling          |

### 1.4 Guiding principles

1. **Small and boring.** Python standard library only — zero third-party
   dependencies, so the only thing to patch is the Python base image.
2. **The platform stores data; it never runs app code.** The only app-supplied
   logic it executes is SQL declared in the bundle, inside a sandbox (§3.10).
   An app that needs any other server logic graduates to the full delivery flow (see §5.3).
3. **Private by network.** Reachable only through the tailnet; no public ingress.
4. **Every app is self-contained on disk.** Its site, versions and data live in
   one folder, so delete / backup / restore / migrate are folder operations.
5. **Safe by default.** Atomic deploys, strict upload validation, quotas.

### 1.5 System context

```
  Family devices (on tailnet)            You / Claude Code (on tailnet)
     │  browse apps, read/write data        │  upload zip with admin token
     └───────────────┬──────────────────────┘
                     ▼  HTTPS  simple-apps.<tailnet>.ts.net
            ┌─────────────────────┐
            │ Tailscale ingress   │  (TLS termination, tailnet-only)
            └─────────┬───────────┘
                      ▼  HTTP :8080
            ┌─────────────────────┐        ┌──────────────────────┐
            │ simple-apps pod     │◄──────►│ PVC simple-apps-data │
            │ (k3s, single node)  │        │ (local-path, on node)│
            └─────────────────────┘        └──────────┬───────────┘
                                                      │ nightly
                                           ┌──────────▼───────────┐
                                           │ restic → backup disk │
                                           └──────────────────────┘
```

Delivery of the platform itself follows the normal homelab flow (GitHub Actions
→ GHCR image → Flux image automation). Delivery of *apps on the platform* is
just an HTTP upload — no git, no CI, no manifests.

---

## 2. Conceptual design

### 2.1 Vocabulary

| Concept        | Meaning                                                                 |
|----------------|-------------------------------------------------------------------------|
| **Platform**   | The simple-apps service itself.                                         |
| **App**        | A named static website plus its private data, e.g. `grocery`.           |
| **Bundle**     | The zip you upload. Contains `index.html` at its root and optionally `app.json`. |
| **Manifest**   | `app.json` inside a bundle: title, quota, SPA mode, SDK version.        |
| **Version**    | One deployed bundle. Versions are numbered `v1, v2, …`; one is *current*. |
| **Collection** | A named list of records inside an app, e.g. `items`, `stores`. Created on first write. |
| **Record**     | One JSON object in a collection, with a server-assigned id and timestamps. |
| **KV entry**   | A single named JSON value inside an app, for settings-like data.        |
| **Table**      | An app-defined SQLite table in the app's `data.db`, created by a migration. |
| **Migration**  | `db/migrations/<n>_<name>.sql` in a bundle: schema change applied once, in order, at deploy. |
| **Named query**| A SQL block in the bundle's `db/queries.sql`, called by name with JSON parameters. |
| **SDK**        | `/_sdk/v1.js`, a small browser library apps use instead of raw URLs.    |
| **Admin**      | Whoever holds the admin token (you, or Claude Code acting for you).     |
| **Member**     | Anyone on the tailnet using an app.                                     |

### 2.2 Relationships

```
Platform 1 ──── * App
App      1 ──── * Version        (exactly one current; last 3 kept)
App      1 ──── * Collection 1 ──── * Record
App      1 ──── * KV entry
App      1 ──── * Migration (applied once, never edited) ──► * Table
Version  1 ──── * Named query    (rollback reverts queries, not schema)
```

### 2.3 Isolation model

* **Storage isolation — strong.** Each app has its own SQLite file. The server
  derives the file path from the validated app name in the URL; no request can
  name another app's file, and the platform's own state lives in a separate
  file that no data endpoint ever opens.
* **Browser isolation — none (accepted).** All apps share one origin, so
  JavaScript in one app *could* deliberately call another app's data URLs or
  read its `localStorage`. The SDK never does this. This is acceptable for
  family apps you or Claude wrote; true isolation requires one hostname per
  app (§5.2), which the per-app folder layout keeps easy.

### 2.4 Trust model

| Actor   | Can                                                      | Proven by              |
|---------|----------------------------------------------------------|------------------------|
| Member  | Open any app; read & write any app's data                | Being on the tailnet   |
| Admin   | Everything a member can, plus deploy/rollback/delete/export | `Authorization: Bearer <token>` |
| Outsider| Nothing — the hostname is not reachable off-tailnet      | —                      |

Members' browsers may also visit hostile websites, so the platform defends
against cross-site request forgery (§4.8).

### 2.5 Lifecycle of an app

```
   (none) ──deploy──► live(v1) ──deploy──► live(v2) ──rollback──► live(v1)
                          │                    │
                          └───────delete───────┴──► (none: site + data removed)
```

---

## 3. Logical design

### 3.1 Components

```
simple_apps/
  __main__.py   entry point: load config, build server, serve forever
  config.py     Config dataclass from environment variables
  names.py      validation of app / collection / record-id / kv-key names
  errors.py     ApiError(status, message) — raised anywhere, rendered by server.py
  db.py         SQLite helpers: connection settings, transactions, timestamps
  registry.py   Registry: platform state in system/system.db
  deploy.py     Deployer: bundle validation, extraction, version swap, rollback, delete, export
  datastore.py  DataStore: per-app data.db — collections, records, kv, quota
  sqlstore.py   app-declared SQL: bundle parsing, sandbox, migrations, named queries
  server.py     HTTP server (bounded threads, timeouts), router, request handler
  static/
    index.html  admin portal: list apps, upload, rollback, delete
    sdk-v1.js   browser SDK served at /_sdk/v1.js
```

Dependencies point one way: `server → deploy → sqlstore → datastore, registry → db, names, config`.
Only `server.py` knows about HTTP; the other modules are plain Python and are
unit-tested directly.

### 3.2 Filesystem layout (the PVC, mounted at `/data`)

```
/data/
  system/
    system.db                 platform state (Registry)
  apps/
    <app>/
      site/
        v1/ v2/ v3/           extracted bundles (last 3 kept)
        current -> v3         symlink, swapped atomically
      data.db                 this app's data (DataStore + app tables)
      snapshots/
        before-v4.db          copy of data.db taken before v4's migrations ran
  tmp/                        upload staging; same filesystem so renames are atomic
```

### 3.3 Data model

**Platform — `system/system.db`**

```sql
apps(
  name            TEXT PRIMARY KEY,
  title           TEXT NOT NULL,
  quota_mb        INTEGER NOT NULL,
  spa             INTEGER NOT NULL,     -- 0/1: unknown paths fall back to index.html
  current_version INTEGER NOT NULL,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
)
deployments(
  app         TEXT NOT NULL,
  version     INTEGER NOT NULL,
  deployed_at TEXT NOT NULL,
  size_bytes  INTEGER NOT NULL,
  file_count  INTEGER NOT NULL,
  PRIMARY KEY (app, version)
)
```

**Per app — `apps/<app>/data.db`**

```sql
records(
  collection TEXT NOT NULL,
  id         TEXT NOT NULL,
  data       TEXT NOT NULL,             -- JSON object
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (collection, id)
)
INDEX records_by_time ON records(collection, created_at)
kv(
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,             -- any JSON value
  updated_at TEXT NOT NULL
)
_migrations(
  name       TEXT PRIMARY KEY,          -- file name, e.g. 001_init.sql
  sha256     TEXT NOT NULL,             -- an applied migration may never change
  applied_at TEXT NOT NULL
)
-- plus any tables, indexes, views and triggers the app's migrations create
```

`records`, `kv`, `_migrations` and `records_by_time` are *platform objects*:
app SQL may read them but never write or alter them.

Records are schema-free JSON: when an app's data shape changes, the app's own
code handles old and new shapes. Apps that want a fixed schema use their own
tables instead, changed only by the app's own migrations (§3.10).
All databases run in WAL mode with a 5 s busy timeout. Timestamps are UTC ISO-8601.

### 3.4 Naming rules

| Name        | Pattern                          | Notes                                   |
|-------------|----------------------------------|-----------------------------------------|
| App         | `^[a-z0-9][a-z0-9-]{0,39}$`      | Cannot start with `_` or `.`; reserved: `system`, `apps`, `tmp`, `health`, `static` |
| Collection  | `^[a-z0-9][a-z0-9_-]{0,63}$`     | Cannot start with `_` (reserved for `_kv`) |
| Record id   | `^[A-Za-z0-9_-]{1,64}$`          | Server generates 12-char ids; clients may choose ids via PUT |
| KV key      | `^[A-Za-z0-9_.-]{1,128}$`        |                                          |
| Query       | `^[a-z0-9][a-z0-9_-]{0,63}$`     | Name of a block in `db/queries.sql`      |
| Migration   | `^\d{1,9}_[A-Za-z0-9_-]{1,64}\.sql$` | Applied in numeric order              |

URL prefixes reserved by the platform (`/_api`, `/_data`, `/_sdk`, `/health`)
all fall outside the app-name pattern, so an app can never shadow them.

### 3.5 HTTP interface

| Method & path                                   | Auth  | Purpose |
|-------------------------------------------------|-------|---------|
| `GET /health`                                   | —     | Liveness/readiness |
| `GET /`                                         | —     | Admin portal (listing is open; actions need the token) |
| `GET /_sdk/v1.js`                               | —     | Browser SDK |
| `GET /_api/v1/apps`                             | —     | List apps |
| `GET /_api/v1/apps/<app>`                       | —     | App details + deployment history |
| `PUT /_api/v1/apps/<app>` (body: zip)           | admin | Deploy a new version (creates the app if new) |
| `POST /_api/v1/apps/<app>/rollback`             | admin | Make the previous kept version current |
| `DELETE /_api/v1/apps/<app>`                    | admin | Delete site, versions **and data** |
| `GET /_api/v1/apps/<app>/export`                | admin | Zip of current site + all data as JSON |
| `GET /_data/v1/<app>/<coll>`                    | —     | List records (`?order=created_at|-created_at|updated_at|-updated_at&limit=&offset=`) |
| `POST /_data/v1/<app>/<coll>`                   | —     | Create a record (server id) |
| `GET /_data/v1/<app>/<coll>/<id>`               | —     | Read one record |
| `PUT /_data/v1/<app>/<coll>/<id>`               | —     | Create or replace a record (client id) |
| `PATCH /_data/v1/<app>/<coll>/<id>`             | —     | Merge top-level fields into a record |
| `DELETE /_data/v1/<app>/<coll>/<id>`            | —     | Delete a record |
| `GET\|PUT\|DELETE /_data/v1/<app>/_kv/<key>`    | —     | Read / write / delete a KV entry |
| `POST /_data/v1/<app>/_sql/<query>` (body: params) | —   | Run a named query (§4.13) |
| `GET /<app>`                                    | —     | Redirect to `/<app>/` (so relative asset paths work) |
| `GET /<app>/<path>`                             | —     | Static file from the current version |

"—" means "anyone on the tailnet". All `/_api` and `/_data` responses are JSON;
errors are `{"error": "<message>"}` with a matching status code.

### 3.6 Record representation

```json
{ "id": "Xk3f9aQ1bZ0c",
  "data": { "name": "Milk", "qty": 2, "done": false },
  "created_at": "2026-11-24T10:00:00Z",
  "updated_at": "2026-11-24T10:00:00Z" }
```

`data` is kept separate from metadata so app fields can never collide with
`id` / timestamps. Lists return `{"items": [...], "total": N}`.

### 3.7 SDK interface (`/_sdk/v1.js`)

The SDK is the contract apps code against; URLs underneath may evolve.
It infers the app name from the first path segment of the page URL.

```js
simpleApps.app                                // "grocery"
const items = simpleApps.collection('items');
await items.list({ order: '-created_at', limit: 100, offset: 0 }) // → [record]
await items.page({ order, limit, offset })    // → { items: [record], total }
await items.get(id)                           // → record | null
await items.add(data)                         // → record
await items.put(id, data)                     // → record (create or replace)
await items.update(id, patch)                 // → record (merge)
await items.remove(id)                        // → true | false
await simpleApps.kv.get(key, fallback)        // → value | fallback
await simpleApps.kv.set(key, value)           // → value
await simpleApps.kv.remove(key)               // → true | false
await simpleApps.sql(name, params)            // → { rows: [object], changes, truncated }
```

Failures reject with an `Error` carrying `.status`.

### 3.8 Configuration (environment)

| Variable          | Default | Meaning |
|-------------------|---------|---------|
| `ADMIN_TOKEN`     | —       | **Required**, ≥ 24 chars. Refuses to start otherwise. |
| `DATA_DIR`        | `/data` | Root of the layout in §3.2 |
| `PORT`            | `8080`  | Listen port |
| `MAX_BUNDLE_MB`   | `50`    | Max compressed upload size |
| `MAX_UNPACKED_MB` | `200`   | Max total extracted size (zip-bomb guard) |
| `MAX_FILES`       | `2000`  | Max files per bundle |
| `MAX_BODY_KB`     | `1024`  | Max JSON body for data requests |
| `DEFAULT_QUOTA_MB`| `10`    | Data quota when the manifest gives none |
| `MAX_QUOTA_MB`    | `100`   | Upper bound a manifest may request |
| `KEEP_VERSIONS`   | `3`     | Versions retained per app (including current) |
| `MAX_THREADS`     | `32`    | Concurrent request threads |
| `REQUEST_TIMEOUT` | `30`    | Socket timeout in seconds |

### 3.9 Deployment topology (homelab)

| Resource | Detail |
|---|---|
| Namespace | `simple-apps` (PSA baseline enforce / restricted warn) |
| Workload | app-template HelmRelease, 1 replica, `Recreate`, pinned to one node, uid 1000, read-only root FS, all capabilities dropped |
| Storage | PVC `simple-apps-data`, 5 Gi, `local-path` |
| Exposure | Tailscale ingress host `simple-apps`, through a proxy class pinned to the same node |
| Secret | `simple-apps-secrets` (SOPS) → `ADMIN_TOKEN` |
| Image | `ghcr.io/thevivotran/simple-apps:main-<ts>-<sha>`, built by GitHub Actions on every push **and weekly** (so base-image security patches ship without code changes), kept current by Flux image automation |
| Backup | Nightly CronJob: init container snapshots every SQLite file with the online-backup API, then restic backs up sites + snapshots to an off-node backup disk (7 daily + 4 weekly) |

### 3.10 App-declared SQL (relational apps)

A bundle may contain a `db/` folder:

```
db/migrations/001_init.sql      schema changes, applied once each, in numeric order
db/migrations/002_add_price.sql
db/queries.sql                  named queries the browser may call
```

```sql
-- name: items_by_store
SELECT i.*, s.name AS store FROM items i JOIN stores s ON s.id = i.store_id WHERE s.id = :store;

-- name: add_item (write)
INSERT INTO items (store_id, name) VALUES (:store, :name) RETURNING *;
```

A query is everything from its `-- name:` line to the next one; it may hold
several statements (run in one transaction; the rows of the last statement that
returns rows are the result). `(write)` marks a query that changes data.
Parameters are named (`:name`); JSON arrays/objects are bound as JSON text, so
`json_each(:ids)` works.

**The browser never sends SQL.** Everything app-supplied runs under an SQLite
authorizer (`Connection.set_authorizer`, stdlib):

| Allowed in…            | Migrations | Write query | Read query |
|------------------------|:----------:|:-----------:|:----------:|
| `SELECT`, reads (incl. platform tables), functions, CTEs | ✓ | ✓ | ✓ |
| `INSERT` / `UPDATE` / `DELETE` on app tables | ✓ | ✓ | — |
| `CREATE` / `DROP` / `ALTER` table, index, view, trigger; `ANALYZE`, `REINDEX` | ✓ | — | — |
| Writing or altering platform objects (§3.3) | — | — | — |
| `ATTACH`, `DETACH`, `PRAGMA`, `BEGIN`/`COMMIT`/`SAVEPOINT`, temp objects, virtual tables, `load_extension` | — | — | — |

`db/` is served like any other bundle file — it holds no secrets, only SQL the
app may call anyway. Read queries additionally run with `PRAGMA query_only`. Request-time queries are
aborted after 2 s (progress handler) and return at most 1000 rows.

---

## 4. Functional design

### 4.1 Request handling (all requests)

1. A connection gets one of `MAX_THREADS` slots. When all are busy it is
   answered `503 Retry-After: 1` immediately — the accept loop never blocks.
2. Two timeouts bound every connection:
   * a socket timeout of `REQUEST_TIMEOUT` per read/write catches idle clients;
   * a **hard deadline** (watchdog timer that shuts the socket down) of
     `REQUEST_TIMEOUT` for the whole connection, so a client dripping one byte
     at a time cannot hold a thread. Only an *authenticated* deploy extends it
     (by upload size ÷ 256 KiB/s). Downloads (static files, exports) are
     streamed in 64 KiB chunks under two limits: a 10 s stall limit per chunk
     and an overall limit of `REQUEST_TIMEOUT` + size ÷ 64 KiB/s for the file,
     so a reader that trickles just fast enough is still cut off.
   Per-client-IP limits would not help: every request arrives from the
   Tailscale proxy's address.
3. Bodies require `Content-Length` (chunked uploads are rejected with 411) and
   are refused with 413 *before reading* when over the route's limit.
4. The router matches the path against an explicit route table (regexes);
   unmatched → 404, wrong method → 405.
5. Every response carries `X-Content-Type-Options: nosniff` and
   `Referrer-Policy: same-origin`. One log line per request (except `/health`).

### 4.2 Deploy — `PUT /_api/v1/apps/<app>`

1. **Authorise** with a constant-time token compare → 401 on failure.
2. **Validate** the app name (§3.4) → 400.
3. **Stage** the body into `/data/tmp/<random>.zip`, streaming in 64 KiB chunks,
   aborting past `MAX_BUNDLE_MB` → 413. Not a valid zip → 400.
4. **Inspect entries** (any failure → 400 with the reason):
   * ignore directories, `__MACOSX/` and `.DS_Store`;
   * reject absolute paths, drive letters, backslashes, `..` segments, and
     symlinks (from the Unix mode bits in `external_attr`);
   * reject more than `MAX_FILES` files, or declared total size over `MAX_UNPACKED_MB`.
5. **Normalise**: if every file sits under one top-level folder and there is no
   root `index.html`, strip that folder (zipping a `dist/` folder just works).
   A root `index.html` is then required.
6. **Read the manifest** `app.json` if present: `name` (must equal the URL name
   if given), `title` (≤ 100 chars, default = name), `quota_mb`
   (1…`MAX_QUOTA_MB`, default `DEFAULT_QUOTA_MB`), `spa` (bool, default false),
   `sdk` (must be `"v1"` if given).
7. **Extract** into `/data/tmp/<random>/`, copying each entry in chunks and
   counting *actual* bytes, so a lying zip header cannot exceed the limits.
8. **Commit** under the global deploy lock:
   0. **Migrate** (only does work when the bundle has `db/`, §3.10): create
      `data.db` if new; check that every migration in `_migrations` is still in
      the bundle with the same sha256 and that no new one is numbered before an
      applied one; if migrations are pending on an existing app, snapshot
      `data.db` to `snapshots/before-v<version>.db`; then in **one** transaction
      apply them and prepare every named query with `EXPLAIN` under the query
      sandbox (unknown tables, missing `(write)`, forbidden statements → 400).
      Any failure rolls the transaction back, the live version stays current,
      and a brand-new app leaves nothing behind;
   1. `version = max(existing) + 1`; rename staging dir → `site/v<version>`;
   2. create symlink `site/current.tmp → v<version>`, then `os.replace` it over
      `site/current` — the atomic switch, readers see old or new, never a mix;
   3. upsert the `apps` row and insert a `deployments` row;
   4. prune: delete version dirs, rows and snapshots beyond the newest `KEEP_VERSIONS`.
9. Always remove the staged zip and any leftover staging dir.
10. **Respond** 201 (new app) or 200 (new version) with the app details.

If anything fails before step 8.2, the live version is untouched.

### 4.3 Rollback — `POST /_api/v1/apps/<app>/rollback`

Admin only. Under the deploy lock, choose the newest kept version older than
`current_version` (409 if none), swap `current` to it, update `apps.current_version`.
The newer version stays on disk, so a second deploy or rollback is possible.
Rollback restores **code only** (including that version's `db/queries.sql`);
data and schema are shared by all versions, so migrations must stay compatible
with the previous version's queries (add columns/tables; don't rename or drop
until a later release). To undo a migration's *data* effect, restore
`snapshots/before-v<N>.db` by hand (§4.12).

### 4.4 Delete — `DELETE /_api/v1/apps/<app>`

Admin only. Under the deploy lock: delete the registry rows, then remove
`apps/<app>/` entirely (site, versions, data). 404 if unknown. Irreversible
except via backup restore — the portal asks for confirmation by retyping the name.

### 4.5 Export — `GET /_api/v1/apps/<app>/export`

Admin only. Streams a zip containing `site/…` (current version),
`data/records.json` (`{collection: [record, …]}`), `data/kv.json`,
`data/schema.sql` (the app's own tables, indexes, views, triggers),
`data/tables.json` (`{table: [row, …]}`, BLOBs as base64) and
`app.json` (registry details). This is the migration path when an app graduates.

### 4.6 Static serving — `GET /<app>/<path>`

1. Unknown app → 404. `GET /<app>` without trailing slash → 301 to `/<app>/`.
2. Resolve `site/current/<path>`; a directory serves its `index.html`.
3. The resolved real path must stay inside `site/current/` → otherwise 404.
4. Missing file → 404, or `index.html` when the app is in SPA mode and the
   path has no file extension.
5. Streamed in 64 KiB chunks (§4.1). `Content-Type` from `mimetypes`;
   `Cache-Control: no-cache` plus an `ETag`
   (version + mtime + size) so redeploys show up immediately and unchanged
   files revalidate with 304.

### 4.7 Data operations — `/_data/v1/<app>/…`

* Unknown app → 404 (data only exists for deployed apps). Bad collection,
  id or key → 400.
* Write bodies must be `Content-Type: application/json` (→ 415), valid JSON
  (→ 400) and, for records, a JSON object (→ 400).
* **List**: ordered by `order` (default `created_at`), `limit` 1–1000
  (default 100), `offset` ≥ 0. Returns `{"items", "total"}`. Unknown
  collection → empty list.
* **Create** (`POST`): new 12-char URL-safe id → 201.
* **Put** (`PUT …/<id>`): create (201) or replace data keeping `created_at` (200).
* **Patch** (`PATCH …/<id>`): inside `BEGIN IMMEDIATE`, read, shallow-merge
  (a `null` value deletes the field), write → 200; 404 if missing. Two members
  patching different fields of the same record both succeed.
* **Delete**: 204, or 404 if missing.
* **KV**: `GET` → `{"key","value","updated_at"}` or 404; `PUT` upserts the
  whole body as the value; `DELETE` → 204 / 404.
* **Quota**: before any write, if the database size
  (`page_count × page_size`) is at or above the app's `quota_mb` → 507.

### 4.8 Cross-site request forgery defence

A member's browser could be tricked by an outside page into sending requests
to the tailnet hostname. For every non-GET request:

1. If an `Origin` header is present, its host must equal the request host
   (`X-Forwarded-Host` or `Host`) → otherwise 403.
2. If `Sec-Fetch-Site` is `cross-site` → 403.
3. JSON writes require `Content-Type: application/json`, which forces a CORS
   preflight from other origins; the platform sends no CORS headers, so the
   preflight fails and the request is never sent.

### 4.9 Admin portal — `GET /`

A single static page: lists apps (title, link, current version, last deploy),
an upload form (name + zip + token; the token is remembered in the browser's
`localStorage`), and per-app *Rollback*, *Export* and *Delete* buttons.
It uses the same `/_api/v1` endpoints as `curl`.

### 4.10 Deploying from the command line (and from Claude Code)

```sh
(cd dist && zip -r ../grocery.zip .)
curl -fsS -X PUT --data-binary @grocery.zip \
  -H "Content-Type: application/zip" \
  -H "Authorization: Bearer $SIMPLE_APPS_TOKEN" \
  https://simple-apps.<tailnet>.ts.net/_api/v1/apps/grocery
```

### 4.11 Failure behaviour

| Situation                         | Behaviour |
|-----------------------------------|-----------|
| Pod restarts mid-deploy           | Staging leftovers in `tmp/` are wiped at start-up; `current` still points at the old version |
| Two deploys at once               | Serialised by the deploy lock |
| Migration or query invalid        | 400 at deploy; the migration transaction rolls back, the live version keeps serving |
| Two members write at once         | SQLite serialises writes (busy timeout 5 s); per-record writes don't clobber each other |
| Disk / quota full                 | 507 to the writer; reads keep working |
| Admin workstation offline                  | No effect — the platform runs on its own node |
| PVC lost                          | Restore from restic (§3.9); redeploying manifests alone gives an empty platform |

### 4.12 Backup and restore

* **Backup (nightly):** an init container (uid 1000, so SQLite side files keep
  the right owner) copies `system.db` and every `data.db` with SQLite's online
  backup API into an `emptyDir` at `/dump`. restic then backs up `/data`
  (excluding `*.db*` and `tmp/`) plus `/dump`.
* **Restore everything:** scale to 0, `restic restore` into the PVC, move the
  `dump/` databases back into place, scale to 1.
* **Restore one app:** restore only `apps/<app>/` and `dump/apps/<app>/data.db`.
* **Undo a bad migration:** scale to 0, copy `apps/<app>/snapshots/before-v<N>.db`
  over `data.db` (delete `data.db-wal` / `-shm`), roll back, scale to 1. Snapshots
  are local only (the backup excludes `*.db*` outside the dump) and are pruned
  with their version.

### 4.13 Named queries — `POST /_data/v1/<app>/_sql/<query>`

1. Unknown app → 404; invalid query name → 400; not in the **current**
   version's `db/queries.sql` → 404 (parsed once per file, cached by path + mtime).
2. Body: JSON object of parameters (→ 400 otherwise; 415 / 413 as §4.7). Extra
   keys are ignored; a missing parameter → 400.
3. **Write query:** quota check (→ 507), then `BEGIN IMMEDIATE`, run all
   statements under the write sandbox, `COMMIT`. **Read query:** `query_only`
   connection, one deferred transaction so all statements see one snapshot.
4. Response `200 {"rows": [{column: value}], "changes": N, "truncated": bool}`;
   `changes` counts rows changed (including by triggers/cascades); `truncated`
   is true when more than 1000 rows matched.
5. Errors: constraint failures and `RAISE(ABORT, 'msg')` in triggers → 409 with
   SQLite's message (use them for validation); over 2 s → 503; database busy → 503;
   other SQL errors → 400.

CSRF defence (§4.8) applies unchanged: it is a `POST` with a JSON body.

---

## 5. Evolution

### 5.1 Built-in extension points

* **Versioned API and SDK** (`/_data/v1`, `/_sdk/v1.js`): a v2 can run beside v1.
* **Manifest** (`app.json`): new capabilities are opt-in per app via new keys
  (e.g. `"features": ["files", "realtime"]`).
* **Per-app folder**: everything an app owns moves together.
* **Export**: every app can leave with its data.

### 5.2 Roadmap (only when a real app needs it)

| Need                                 | Planned approach |
|--------------------------------------|------------------|
| Filtering / sorting on fields        | Use app tables + named queries (§3.10); `?where=` on collections only if still needed |
| Simple table CRUD without writing queries | Generic `/_data/v1/<app>/_tables/<t>` routes over declared tables |
| File / photo uploads                 | `/_data/v1/<app>/_files/` stored in `apps/<app>/files/`, counted in quota |
| Live updates between devices         | Server-sent events per collection; `collection.subscribe(cb)` in SDK v2 |
| Knowing which member did something   | Tailscale identity headers → `simpleApps.me()` |
| Calling external APIs with a secret  | Per-app allow-listed proxy; secret held server-side |
| Strong isolation between apps        | One Tailscale hostname per app, routed by `Host` |

### 5.3 When an app graduates

Move an app to the full flow (own repo, image, HelmRelease) when it needs any of:
server-side code beyond SQL (validation, external calls), background jobs, more
than ~100 MB of data, migrations that cannot stay backward compatible, or other
services. Relational schemas with migrations are supported on the platform (§3.10). Use **Export** to take its data along.
