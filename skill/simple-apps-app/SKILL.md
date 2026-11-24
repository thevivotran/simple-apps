---
name: simple-apps-app
description: Build, update and deploy a small static web app on the homelab simple-apps platform ("drop a zip, get an app") — a grocery list, chore chart, tracker, planner or any single-page tool for the family, with data stored through the /_sdk/v1.js SDK (JSON collections/kv, or the app's own SQLite tables via bundled migrations + named queries). Works in Claude Code (tests locally and deploys) and in chat (produces a checked bundle zip to upload on the portal). Use whenever the user asks for a new small web app, a "simple app", something to "host on simple-apps" or "put on the homelab", or wants to change, redeploy, roll back or export an app on simple-apps. Not for apps needing server-side code beyond SQL, background jobs or other services.
---

# Build and host an app on simple-apps

**simple-apps** is the homelab platform that hosts small static web apps:
upload a zip (a *bundle*) → it is live at `https://simple-apps.leopard-mackarel.ts.net/<app>/`,
reachable only on the user's tailnet. There is no git repo, CI or manifest per
app — delivery is one HTTP upload. The platform never runs app code; apps store
data through its SDK, and the only app-supplied logic it runs is sandboxed SQL.

Bundled with this skill:
- `references/APP_AUTHORING.md` — **the contract for app code. Read it in full first.**
- `references/ARCHITECTURE.md` — platform design; consult the sections cited below.
- `scripts/check_bundle.py` — stdlib pre-flight check + zip builder (mirrors the
  platform's deploy checks, including applying migrations and preparing queries).

## 0. Which environment am I in?

Run `test -d /home/thevix/Documents/personal/project/simple-apps && echo code || echo chat`
(or judge from the tools you have).

| | **Code** — Claude Code on the user's machine | **Chat** — claude.ai / desktop chat sandbox |
|---|---|---|
| Reach the platform | yes (tailnet) | **no** — the sandbox is not on the tailnet |
| Test | real platform locally (`make run`) | `check_bundle.py` only |
| Deploy | `curl` with the admin token (after confirming) | user uploads the zip on the portal |
| Admin token | `$SIMPLE_APPS_TOKEN` or `sops` | **never ask for it or accept it in chat** |

Everything else (naming, design, rules) is the same.

## 1. Name the app

The **app name** keys everything: URL `/<app>/`, data API `/_data/v1/<app>/`, the
platform folder `apps/<app>/` and the source folder `<app>/`. Pattern
`^[a-z0-9][a-z0-9-]{0,39}$`, not `system apps tmp health static`
(ARCHITECTURE.md §3.4). Pick a short noun (`grocery`, `chores`) with the user.

Deploying to a name that already exists **replaces that live app** (data is kept).
- Code: `curl -s -o /dev/null -w '%{http_code}' https://simple-apps.leopard-mackarel.ts.net/_api/v1/apps/<app>` (404 = free).
- Chat: ask the user whether the name is already on the portal list.

## 2. Get the source

- **New app:** create folder `<app>/` (Code: in the current directory unless that
  is the platform repo; Chat: in the sandbox working directory).
- **Existing app:** the platform is the source of truth (it keeps the last 3
  versions); its *export* zip holds the current version under `site/`.
  - Code — fetch it, keep only `site/`, delete the rest (it contains all the app's data):
    ```bash
    curl -fsS -H "Authorization: Bearer $TOKEN" -o /tmp/<app>-export.zip \
      https://simple-apps.leopard-mackarel.ts.net/_api/v1/apps/<app>/export
    unzip -q /tmp/<app>-export.zip 'site/*' -d /tmp/<app>-export && cp -r /tmp/<app>-export/site ./<app>
    rm -rf /tmp/<app>-export.zip /tmp/<app>-export
    ```
  - Chat — ask the user to click **Export** for the app on the portal and attach
    the zip (or just its `site/` folder); work from `site/` only and don't echo data.
  - Keep `db/migrations/` byte-for-byte as exported: applied migrations may never change.

## 3. Choose storage

| Data | Use |
|---|---|
| Settings, single values | `simpleApps.kv` |
| Independent lists, no joins/totals, shape may drift | `simpleApps.collection(name)` |
| Relations, constraints, sums/group-by, reporting, filtering on fields | own tables: `db/migrations/*.sql` + `db/queries.sql` + `simpleApps.sql()` |

Mixable in one app. Collections only sort by `created_at`/`updated_at` and can't
filter on fields — if the app needs that, use tables.

## 4. Bundle layout

```
<app>/
  index.html            # <script src="/_sdk/v1.js"></script> — absolute: platform path
  app.js  style.css     # own assets RELATIVE: src="app.js", never "/app.js"
  app.json              # {"name": "<app>", "title": "…", "sdk": "v1"}
  db/                   # only with tables
    migrations/001_init.sql
    queries.sql
```

`app.json`: `name` = the URL name; `title` ≤ 100 chars; `quota_mb` 1–100 (default
10, covers the whole data file); `spa: true` only for a client-side router.
Default to plain HTML/CSS/JS with no build step; with Vite set `base: './'` and
check/zip `dist/`.

## 5. Rules that matter (details in APP_AUTHORING.md)

**Data**
- Everything shared between members or devices goes through the SDK.
- `collection.list()` returns **100 records by default** (max 1000 per call) —
  pass `limit` or page with `page({limit, offset})` (returns `total`).
- Collection names `[a-z0-9][a-z0-9_-]*`; app fields live under `record.data`.
- Prefer `update(id, patch)` (merge; `null` deletes a field) over `put`, so two
  members editing at once don't overwrite each other.
- JSON write bodies ≤ 1 MB. No file uploads, live push or "who am I" yet
  (ARCHITECTURE.md §5.2) — don't fake them.
- Refresh on `visibilitychange`/focus, plus a 15–30 s timer for shared lists.

**Tables** (`simpleApps.sql`, ARCHITECTURE.md §3.10, §4.13)
- Never edit, rename or delete an applied migration — add `00N_….sql`. Keep
  changes additive so rolling back to the previous version still works.
- Mark data-changing queries `(write)`; named params only (`:x`); `RETURNING` when
  the UI needs the row; alias joined columns (`s.name AS store`).
- Validate in SQL (`NOT NULL`, `CHECK`, `UNIQUE`, FKs, trigger
  `SELECT RAISE(ABORT, 'message')`) and show `err.message` on a 409.
- Not allowed: `PRAGMA`, `ATTACH`, `BEGIN/COMMIT`, temp/virtual tables, writing
  `records`/`kv`/`_migrations`. Queries: ≤ 2 s and ≤ 1000 rows.

**Browser**
- All apps share one origin: never call another app's data URLs; prefix any
  `localStorage` key with the app name (per-device UI prefs only).
- Handle SDK errors (`err.status`): 409 constraint, 507 quota full, 503 retry.
- Phones first: viewport meta, big tap targets, works at 360 px wide.
- No external CDNs unless the user agrees. Build DOM with
  `createElement`/`textContent`, not `innerHTML` with data.

## 6. Check and package (both environments)

```bash
python3 <skill-dir>/scripts/check_bundle.py <app> --name <app> --zip <app>.zip
```

Fix every `error:`; read the `warning:`s. The zip leaves out dotfiles, `*.md`,
`node_modules`. In chat the sandbox has no server, so this check (plus careful
reading of the JS against the SDK in APP_AUTHORING.md) is the test.

## 7a. Code: test on a local platform, then deploy

```bash
cd /home/thevix/Documents/personal/project/simple-apps && make run   # background; :8080, ./data is throwaway
curl -fsS -X PUT --data-binary @<app>.zip -H "Content-Type: application/zip" \
  -H "Authorization: Bearer dev-token-dev-token-dev-token" http://localhost:8080/_api/v1/apps/<app>
```

Open `http://localhost:8080/<app>/` in the browser pane, exercise every create /
update / delete path, watch the console, check phone width; stop the server.

Deploying publishes to everyone on the tailnet — **confirm with the user before
the first deploy and before replacing a live app.** Never print the token.

```bash
TOKEN="${SIMPLE_APPS_TOKEN:-$(sops -d --extract '["stringData"]["ADMIN_TOKEN"]' \
  /home/thevix/Documents/personal/project/homelab/apps/simple-apps/simple-apps-secrets.sops.yaml)}"
curl -fsS -X PUT --data-binary @<app>.zip -H "Content-Type: application/zip" \
  -H "Authorization: Bearer $TOKEN" https://simple-apps.leopard-mackarel.ts.net/_api/v1/apps/<app>
```

201 = new app, 200 = new version. Smoke-test the live URL, then delete `<app>.zip`.

## 7b. Chat: hand over the zip

Save `<app>.zip` as a downloadable output file and give the user these steps:

1. On a device on the tailnet, open `https://simple-apps.leopard-mackarel.ts.net/`.
2. Enter app name `<app>`, choose the zip, open **Admin token** and paste the
   token there (the portal keeps it in that browser) — never into chat.
3. Upload. An error message names the exact problem (bundle, migration or query) —
   they can paste it back here to fix.
4. Open `https://simple-apps.leopard-mackarel.ts.net/<app>/` and try it.

Or, from a tailnet machine with the token in `$SIMPLE_APPS_TOKEN`:
`curl -fsS -X PUT --data-binary @<app>.zip -H "Content-Type: application/zip" -H "Authorization: Bearer $SIMPLE_APPS_TOKEN" https://simple-apps.leopard-mackarel.ts.net/_api/v1/apps/<app>`

## 8. Other admin actions (portal buttons, or `curl` with the token in Code)

- **Rollback** `POST /_api/v1/apps/<app>/rollback` — code and its `queries.sql`
  only; data and schema stay (ARCHITECTURE.md §4.3, §4.12 for undoing a migration).
- **Export** `GET /_api/v1/apps/<app>/export` — current site + all data.
- **Delete** `DELETE /_api/v1/apps/<app>` — removes site **and data**,
  irreversible except from backup. Only on explicit request; confirm first.

## 9. Finish

Report the URL, the storage used and where the source/zip is. If the app needs
something the platform doesn't do (server code beyond SQL, background jobs,
> ~100 MB data, non-additive schema changes, other services), say so and point
to ARCHITECTURE.md §5.3 rather than working around it.
