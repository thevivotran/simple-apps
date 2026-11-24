# CLAUDE.md

simple-apps: a Python **standard-library-only** service that hosts small static
family apps (zip upload) with per-app JSON storage. Read `ARCHITECTURE.md` first;
keep it in sync when behaviour changes (sections are numbered and referenced from code).

## Commands

- `make all` — ruff lint + format check + unit tests (run before every commit; CI runs the same)
- `python3 -m unittest tests.test_deploy -v` — one test module
- `make run` — local server on :8080 with ./data

## Rules

- **No third-party runtime dependencies.** Not even small ones — zero deps is the
  point of the design (only the base image needs patching). Dev tools (ruff) are fine.
- Only `server.py` knows HTTP. Other modules raise `ApiError(status, message)`.
- Every name that reaches a path or SQL goes through `names.py` first.
- One SQLite connection per operation (`db.connect` / `db.transaction`); no shared connections across threads.
- App data lives only in `apps/<app>/data.db`; platform state only in `system/system.db`.
  No data endpoint may open `system.db`.
- Writes to app data must stay field-level (`patch`) or record-level — never rewrite a whole collection.
- Public API is versioned (`/_api/v1`, `/_data/v1`, `/_sdk/v1.js`): add, don't break.
  `APP_AUTHORING.md` is the contract given to app authors — update it with any API change.
- Deployment manifests live in the separate `homelab` repo under `apps/simple-apps/`.
- `skill/simple-apps-app/` is the Claude skill for building apps on the platform
  (Claude Code + claude.ai chat). Its `references/` are copies of `APP_AUTHORING.md`
  and `ARCHITECTURE.md`: after editing either doc run `make skill` (refreshes the
  copies, installs to `~/.claude/skills`, builds `dist/simple-apps-app.zip` for chat);
  `tests/test_skill.py` fails while they are stale. Keep `scripts/check_bundle.py`
  in step with `deploy.py` / `sqlstore.py` checks (stdlib only — it runs in chat).
