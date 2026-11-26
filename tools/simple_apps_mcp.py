#!/usr/bin/env python3
"""Local MCP server (stdio) that lets Claude chat deploy and manage simple-apps apps.

Claude chat's code sandbox runs in the cloud and cannot reach the tailnet, but the
Claude desktop app starts MCP servers listed in claude_desktop_config.json on the
user's own machine — which is on the tailnet. This server is that bridge: chat
passes the bundle's files as tool arguments, the server zips them locally and
uploads them with the admin token, which never enters the chat.

Standard library only (ARCHITECTURE.md §1.4). Configuration, from the environment:

    SIMPLE_APPS_URL        base URL, e.g. https://simple-apps.<tailnet>.ts.net (required)
    SIMPLE_APPS_TOKEN      admin token, or
    SIMPLE_APPS_TOKEN_CMD  command printing the token (e.g. a `sops -d --extract ...` call)

Speaks MCP's stdio transport: one JSON-RPC 2.0 message per line on stdin/stdout.
"""

import base64
import io
import json
import os
import posixpath
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "simple-apps", "version": "1.0.0"}
APP_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
RESERVED_APPS = {"system", "apps", "tmp", "health", "static"}
TEXT_SUFFIXES = {
    ".html", ".htm", ".js", ".mjs", ".css", ".json", ".sql", ".txt", ".svg", ".webmanifest", ".xml",
    ".csv", ".map",
}  # fmt: skip
HTTP_TIMEOUT = 60

APP_PROP = {"type": "string", "description": "App name (URL path), e.g. 'grocery'."}
TOOLS = [
    {
        "name": "list_apps",
        "title": "List simple-apps apps",
        "description": "List the apps deployed on the simple-apps platform (name, title, version, URL).",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "get_app",
        "title": "Get app details",
        "description": "Details and deployment history of one app; reports clearly if the name is free.",
        "inputSchema": {"type": "object", "properties": {"app": APP_PROP}, "required": ["app"]},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "get_app_source",
        "title": "Get app source",
        "description": (
            "Download the current version's source files of an existing app (its bundle: "
            "index.html, app.json, db/migrations, ...) to edit and redeploy. App data is never returned. "
            "Text files come back in `files`, others base64-encoded in `binary_files`."
        ),
        "inputSchema": {"type": "object", "properties": {"app": APP_PROP}, "required": ["app"]},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "deploy_app",
        "title": "Deploy app",
        "description": (
            "Zip the given bundle files and deploy them as <app>, live for everyone on the tailnet. "
            "Creates the app or replaces its live version (data is kept; the previous version can be "
            "restored with rollback_app). Confirm with the user before the first deploy and before "
            "replacing a live app. Pass every file of the bundle; index.html must be at the root."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "app": APP_PROP,
                "files": {
                    "type": "object",
                    "description": "Text files: bundle-relative path -> UTF-8 content, "
                    "e.g. {'index.html': '...', 'db/migrations/001_init.sql': '...'}.",
                    "additionalProperties": {"type": "string"},
                },
                "binary_files": {
                    "type": "object",
                    "description": "Optional binary files (images, fonts): path -> base64 content.",
                    "additionalProperties": {"type": "string"},
                },
            },
            "required": ["app", "files"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True},
    },
    {
        "name": "rollback_app",
        "title": "Roll back app",
        "description": "Make the previous version of an app current again (code and queries only; "
        "data and schema stay).",
        "inputSchema": {"type": "object", "properties": {"app": APP_PROP}, "required": ["app"]},
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
    {
        "name": "delete_app",
        "title": "Delete app",
        "description": "Permanently delete an app: its site AND all its data (irreversible except from "
        "backup). Only on the user's explicit request, after confirming. `confirm` must repeat the name.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "app": APP_PROP,
                "confirm": {"type": "string", "description": "Must equal `app`."},
            },
            "required": ["app", "confirm"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
]


class ToolError(Exception):
    """A failure reported to the model as a tool result with isError, not a protocol error."""


# -- platform client -----------------------------------------------------------


class Platform:
    def __init__(self, env: dict[str, str]):
        self.env = env
        self._token: str | None = None

    @property
    def base_url(self) -> str:
        url = self.env.get("SIMPLE_APPS_URL", "").rstrip("/")
        if not url:
            raise ToolError("SIMPLE_APPS_URL is not set in the MCP server's environment")
        return url

    def token(self) -> str:
        if self._token is None:
            token = self.env.get("SIMPLE_APPS_TOKEN", "").strip()
            cmd = self.env.get("SIMPLE_APPS_TOKEN_CMD", "").strip()
            if not token and cmd:
                try:
                    out = subprocess.run(  # noqa: S603 - command comes from the user's own config
                        shlex.split(cmd), capture_output=True, text=True, timeout=30, check=False
                    )
                except (OSError, subprocess.TimeoutExpired) as e:
                    raise ToolError(f"SIMPLE_APPS_TOKEN_CMD failed to run: {e}") from None
                if out.returncode != 0:
                    detail = out.stderr.strip().splitlines()[-1:] or [""]
                    raise ToolError(f"SIMPLE_APPS_TOKEN_CMD exited with status {out.returncode}: {detail[0]}")
                token = out.stdout.strip()
            if not token:
                raise ToolError("no admin token: set SIMPLE_APPS_TOKEN or SIMPLE_APPS_TOKEN_CMD")
            self._token = token
        return self._token

    def request(self, method: str, path: str, body: bytes | None = None, ctype: str | None = None):
        headers = {"Accept": "application/json"}
        if method != "GET" or path.endswith("/export"):
            headers["Authorization"] = f"Bearer {self.token()}"
        if ctype:
            headers["Content-Type"] = ctype
        req = urllib.request.Request(self.base_url + path, data=body, method=method, headers=headers)  # noqa: S310
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as res:  # noqa: S310
                return res.status, res.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read()
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise ToolError(
                f"cannot reach {self.base_url}: {reason} (is this machine on the tailnet?)"
            ) from None

    def json_call(self, method: str, path: str, body: bytes | None = None, ctype: str | None = None):
        status, data = self.request(method, path, body, ctype)
        payload = None
        if data:
            try:
                payload = json.loads(data)
            except ValueError:
                payload = None
        if status >= 400:
            msg = payload.get("error") if isinstance(payload, dict) else None
            raise ToolError(f"HTTP {status}: {msg or data[:300].decode('utf-8', 'replace')}")
        return status, payload


def check_app(app) -> str:
    if not isinstance(app, str) or not APP_RE.fullmatch(app) or app in RESERVED_APPS:
        raise ToolError(
            "invalid app name: lowercase letters, digits and '-', max 40 chars, "
            f"not one of {sorted(RESERVED_APPS)}"
        )
    return app


def clean_path(path) -> str:
    if not isinstance(path, str) or not path or "\\" in path or "\x00" in path:
        raise ToolError(f"invalid file path: {path!r}")
    norm = posixpath.normpath(path)
    if norm.startswith(("/", "../")) or norm in (".", "..") or path.startswith("/"):
        raise ToolError(f"file path must be relative and inside the bundle: {path!r}")
    return norm


def build_zip(files, binary_files) -> bytes:
    if not isinstance(files, dict) or not files:
        raise ToolError("`files` must be a non-empty object of path -> content")
    if binary_files is not None and not isinstance(binary_files, dict):
        raise ToolError("`binary_files` must be an object of path -> base64 content")
    entries: dict[str, bytes] = {}
    for path, content in files.items():
        if not isinstance(content, str):
            raise ToolError(f"content of {path!r} must be a string")
        entries[clean_path(path)] = content.encode("utf-8")
    for path, content in (binary_files or {}).items():
        norm = clean_path(path)
        if norm in entries:
            raise ToolError(f"{path!r} is in both files and binary_files")
        try:
            entries[norm] = base64.b64decode(content, validate=True)
        except (ValueError, TypeError):
            raise ToolError(f"content of {path!r} is not valid base64") from None
    if "index.html" not in entries:
        raise ToolError("the bundle needs index.html at its root")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(entries):
            zf.writestr(name, entries[name])
    return buf.getvalue()


def summary(info: dict) -> dict:
    keep = ("name", "title", "current_version", "updated_at", "url", "quota_mb", "spa")
    return {k: info[k] for k in keep if k in info} | {"deployments": info.get("deployments", [])[:5]}


# -- tools ---------------------------------------------------------------------


def tool_list_apps(p: Platform, _args: dict) -> dict:
    _, payload = p.json_call("GET", "/_api/v1/apps")
    apps = payload.get("apps", []) if isinstance(payload, dict) else []
    for app in apps:
        app["url"] = p.base_url + app.get("url", f"/{app.get('name')}/")
    return {"platform": p.base_url, "apps": apps}


def tool_get_app(p: Platform, args: dict) -> dict:
    app = check_app(args.get("app"))
    status, data = p.request("GET", f"/_api/v1/apps/{app}")
    if status == 404:
        return {"app": app, "exists": False, "note": "name is free: deploying creates a new app"}
    if status >= 400:
        raise ToolError(f"HTTP {status}: {data[:300].decode('utf-8', 'replace')}")
    info = json.loads(data)
    info["url"] = p.base_url + info.get("url", f"/{app}/")
    return {"exists": True} | info


def tool_get_app_source(p: Platform, args: dict) -> dict:
    app = check_app(args.get("app"))
    status, data = p.request("GET", f"/_api/v1/apps/{app}/export")
    if status >= 400:
        raise ToolError(f"HTTP {status}: {data[:300].decode('utf-8', 'replace')}")
    files: dict[str, str] = {}
    binary: dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            # Only the site: data/ holds every record of the app and must not enter the chat.
            if not name.startswith("site/") or name.endswith("/"):
                continue
            rel, content = name[len("site/") :], zf.read(name)
            if posixpath.splitext(rel)[1].lower() in TEXT_SUFFIXES:
                try:
                    files[rel] = content.decode("utf-8")
                    continue
                except UnicodeDecodeError:
                    pass
            binary[rel] = base64.b64encode(content).decode("ascii")
    note = "db/migrations/ that are already applied must be redeployed byte-for-byte unchanged."
    return {"app": app, "files": files, "binary_files": binary, "note": note}


def tool_deploy_app(p: Platform, args: dict) -> dict:
    app = check_app(args.get("app"))
    body = build_zip(args.get("files"), args.get("binary_files"))
    status, info = p.json_call("PUT", f"/_api/v1/apps/{app}", body, "application/zip")
    info = info or {}
    info["url"] = p.base_url + info.get("url", f"/{app}/")
    return {"result": "created" if status == 201 else "new version", "zip_bytes": len(body)} | summary(info)


def tool_rollback_app(p: Platform, args: dict) -> dict:
    app = check_app(args.get("app"))
    _, info = p.json_call("POST", f"/_api/v1/apps/{app}/rollback", b"{}", "application/json")
    info = info or {}
    info["url"] = p.base_url + info.get("url", f"/{app}/")
    return {"result": "rolled back"} | summary(info)


def tool_delete_app(p: Platform, args: dict) -> dict:
    app = check_app(args.get("app"))
    if args.get("confirm") != app:
        raise ToolError("not deleted: `confirm` must equal the app name")
    p.json_call("DELETE", f"/_api/v1/apps/{app}")
    return {"result": "deleted", "app": app}


HANDLERS = {
    "list_apps": tool_list_apps,
    "get_app": tool_get_app,
    "get_app_source": tool_get_app_source,
    "deploy_app": tool_deploy_app,
    "rollback_app": tool_rollback_app,
    "delete_app": tool_delete_app,
}


# -- JSON-RPC / MCP ------------------------------------------------------------


def call_tool(p: Platform, name: str, args: dict) -> dict:
    handler = HANDLERS.get(name)
    if handler is None:
        return {"content": [{"type": "text", "text": f"unknown tool: {name}"}], "isError": True}
    try:
        result = handler(p, args if isinstance(args, dict) else {})
    except ToolError as e:
        text = str(e)
        if p._token:
            text = text.replace(p._token, "[token]")
        return {"content": [{"type": "text", "text": text}], "isError": True}
    return {
        "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}],
        "structuredContent": result,
        "isError": False,
    }


def handle(p: Platform, msg: dict) -> dict | None:
    """Answer one JSON-RPC message; None for notifications."""
    method, msg_id = msg.get("method"), msg.get("id")
    if msg_id is None:
        return None  # notifications (initialized, cancelled, ...) need no answer
    params = msg.get("params") or {}
    if method == "initialize":
        version = params.get("protocolVersion") or PROTOCOL_VERSION
        result = {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": "Deploy and manage apps on the simple-apps platform from chat. Build and "
            "check the bundle first (simple-apps-app skill), then pass its files to deploy_app.",
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        result = call_tool(p, params.get("name", ""), params.get("arguments") or {})
    else:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"no method {method}"}}
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def serve(stdin=sys.stdin, stdout=sys.stdout, env=None) -> None:
    p = Platform(dict(os.environ) if env is None else env)
    for line in stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            if not isinstance(msg, dict):
                reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "bad request"}}
            else:
                reply = handle(p, msg)
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()


if __name__ == "__main__":
    serve()
