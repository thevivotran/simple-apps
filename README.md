# simple-apps

Drop a zip, get an app. A tiny self-hosted host for small static family web apps,
with per-app JSON storage — Python standard library only, zero dependencies.

* **Design:** [ARCHITECTURE.md](ARCHITECTURE.md) (high-level → conceptual → logical → functional)
* **Writing an app for it:** [APP_AUTHORING.md](APP_AUTHORING.md)

## Run locally

```sh
make run        # http://localhost:8080, data in ./data, token dev-token-dev-token-dev-token
```

## Develop

```sh
make all        # ruff lint + format check + unit tests
make fmt        # auto-format
make image      # podman build
```

Requires Python ≥ 3.12 and `ruff` for linting. There is nothing to `pip install`.

## Deploy an app

```sh
curl -fsS -X PUT --data-binary @app.zip \
  -H "Content-Type: application/zip" \
  -H "Authorization: Bearer $SIMPLE_APPS_TOKEN" \
  https://simple-apps.<tailnet>.ts.net/_api/v1/apps/<name>
```

Other admin calls: `POST …/apps/<name>/rollback`, `DELETE …/apps/<name>`,
`GET …/apps/<name>/export`.

## Claude skill for writing apps

An optional Claude skill can build, check and deploy apps for the platform — in
Claude Code (tests on a local platform, deploys with the token) and in claude.ai chat
(builds a checked zip to upload on the portal). It names a specific deployment, so it
is kept locally in `skill/simple-apps-app/` and is not part of this repository.

```sh
make skill      # install to ~/.claude/skills and build dist/simple-apps-app.zip
```

For chat, upload `dist/simple-apps-app.zip` in claude.ai → Settings → Capabilities → Skills.

Chat's sandbox can't reach the tailnet. To let chat in the Claude **desktop** app
deploy anyway, register the local MCP server `tools/simple_apps_mcp.py` (stdlib only)
in `claude_desktop_config.json`. It runs on your tailnet machine and holds the token
itself; see ARCHITECTURE.md §4.10.

## Delivery

Every push to `main` (and a weekly scheduled run, to pick up base-image security
patches) runs the tests and pushes `ghcr.io/thevivotran/simple-apps:main-<ts>-<sha>`.
Flux image automation in the `homelab` repo (`apps/simple-apps/`) rolls it out.
