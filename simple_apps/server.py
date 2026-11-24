"""HTTP layer: bounded threaded server, route table and request handler
(ARCHITECTURE.md §3.5, §4.1, §4.8).

This is the only module that knows about HTTP; it validates the request shape,
then delegates to Deployer / AppData / sqlstore / Registry.
"""

import hmac
import json
import logging
import mimetypes
import os
import re
import socket
import sys
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from . import datastore, names, sqlstore
from .config import Config
from .deploy import Deployer
from .errors import ApiError
from .registry import AppInfo, Registry

log = logging.getLogger(__name__)

STATIC = resources.files("simple_apps") / "static"
SAFE_METHODS = {"GET", "HEAD"}
# An authenticated deploy gets extra time on top of REQUEST_TIMEOUT for its upload,
# assuming at least this rate. Downloads are streamed in chunks: each chunk must be
# sent within STREAM_CHUNK_SECONDS (stall limit) and the whole file within
# REQUEST_TIMEOUT + size / MIN_DOWNLOAD_BYTES_PER_SEC (overall limit).
MIN_UPLOAD_BYTES_PER_SEC = 256 * 1024
MIN_DOWNLOAD_BYTES_PER_SEC = 64 * 1024
STREAM_CHUNK = 64 * 1024
STREAM_CHUNK_SECONDS = 10.0
BUSY_RESPONSE = (
    b"HTTP/1.0 503 Service Unavailable\r\nContent-Type: application/json\r\n"
    b"Retry-After: 1\r\nContent-Length: 26\r\n\r\n"
    b'{"error": "server busy"}\r\n'
)

Handler = Callable[["RequestHandler", re.Match], None]


class Platform:
    """Everything a request needs, shared by all handler threads."""

    def __init__(self, config: Config):
        self.config = config
        self.registry = Registry(config.system_dir)
        self.deployer = Deployer(config, self.registry)
        self.deployer.cleanup_tmp()
        self.portal = (STATIC / "index.html").read_bytes()
        self.sdk = (STATIC / "sdk-v1.js").read_bytes()

    def app_or_404(self, name: str) -> AppInfo:
        info = self.registry.get(name) if names.is_app_name(name) else None
        if info is None:
            raise ApiError(404, "app not found")
        return info

    def data(self, name: str) -> datastore.AppData:
        info = self.app_or_404(name)
        return datastore.AppData(datastore.data_path(self.config.apps_dir, name), info.quota_mb)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, platform: Platform, address: tuple[str, int]):
        self.platform = platform
        self._slots = threading.BoundedSemaphore(platform.config.max_threads)
        super().__init__(address, RequestHandler)

    def process_request(self, request, client_address):  # type: ignore[override]
        # Never block the accept loop: when every slot is busy, refuse at once.
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(BUSY_RESPONSE)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):  # type: ignore[override]
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):  # type: ignore[override]
        exc = sys.exception()
        if isinstance(exc, OSError):  # client went away or the watchdog closed the socket
            log.debug("connection from %s dropped: %s", client_address[0], exc)
        else:
            log.exception("unhandled error for %s", client_address[0])


class RequestHandler(BaseHTTPRequestHandler):
    server: Server
    server_version = "simple-apps"
    protocol_version = "HTTP/1.0"

    # -- plumbing ------------------------------------------------------------

    def version_string(self) -> str:
        return self.server_version

    def setup(self) -> None:
        # Per-recv timeout catches idle clients; the watchdog enforces a hard deadline
        # for the whole connection, so a client dripping bytes cannot hold a thread.
        self.timeout = self.server.platform.config.request_timeout
        self._watchdog: threading.Timer | None = None
        self._arm(self.timeout)
        super().setup()

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            if self._watchdog is not None:
                self._watchdog.cancel()

    def _arm(self, seconds: float) -> None:
        """(Re)start the hard deadline: the socket is shut down after `seconds`."""
        if self._watchdog is not None:
            self._watchdog.cancel()
        self._watchdog = threading.Timer(seconds, self._expire)
        self._watchdog.daemon = True
        self._watchdog.start()

    def _expire(self) -> None:
        log.warning("closing %s: request deadline exceeded", self.client_address[0])
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    @property
    def platform(self) -> Platform:
        return self.server.platform

    def log_message(self, format: str, *args: Any) -> None:
        log.info("%s %s", self.address_string(), format % args)

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        if self.path != "/health":
            super().log_request(code, size)

    def _dispatch(self) -> None:
        url = urlsplit(self.path)
        self.query = parse_qs(url.query)
        path = url.path
        try:
            self._check_csrf()
            for method_routes, pattern in ROUTES:
                match = pattern.fullmatch(path)
                if match is None:
                    continue
                handler = method_routes.get(self.command)
                if handler is None and self.command == "HEAD":
                    handler = method_routes.get("GET")
                if handler is None:
                    raise ApiError(405, "method not allowed")
                handler(self, match)
                return
            raise ApiError(404, "not found")
        except ApiError as err:
            self.send_json(err.status, {"error": err.message})
        except (TimeoutError, ConnectionError):
            self.close_connection = True
        except Exception:
            log.exception("unhandled error for %s %s", self.command, self.path)
            self.send_json(500, {"error": "internal error"})

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = _dispatch

    # -- security ------------------------------------------------------------

    def _check_csrf(self) -> None:
        """Reject cross-site writes (ARCHITECTURE.md §4.8)."""
        if self.command in SAFE_METHODS:
            return
        if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
            raise ApiError(403, "cross-site request refused")
        origin = self.headers.get("Origin")
        if origin:
            host = (self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "").split(",")[0]
            if urlsplit(origin).netloc.lower() != host.strip().lower():
                raise ApiError(403, "cross-origin request refused")

    def require_admin(self) -> None:
        auth = self.headers.get("Authorization", "")
        scheme, _, token = auth.partition(" ")
        expected = self.platform.config.admin_token.encode()
        if scheme.lower() != "bearer" or not hmac.compare_digest(token.strip().encode(), expected):
            raise ApiError(401, "admin token required")

    # -- request bodies ------------------------------------------------------

    def content_length(self, limit: int) -> int:
        if self.headers.get("Transfer-Encoding"):
            raise ApiError(411, "chunked bodies are not supported; send Content-Length")
        raw = self.headers.get("Content-Length")
        if raw is None:
            raise ApiError(411, "Content-Length required")
        try:
            length = int(raw)
        except ValueError:
            raise ApiError(400, "invalid Content-Length") from None
        if length < 0:
            raise ApiError(400, "invalid Content-Length")
        if length > limit:
            self.close_connection = True
            raise ApiError(413, "request body too large")
        return length

    def read_json(self) -> Any:
        ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ApiError(415, "Content-Type must be application/json")
        length = self.content_length(self.platform.config.max_body_bytes)
        body = self.rfile.read(length)
        if len(body) != length:
            raise ApiError(400, "request body ended early")
        try:
            return json.loads(body)
        except ValueError:
            raise ApiError(400, "body is not valid JSON") from None

    # -- responses -----------------------------------------------------------

    def _common_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")

    def send_body(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._common_headers()
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD" and status not in (204, 304):
            self.wfile.write(body)

    def send_json(self, status: int, payload: Any) -> None:
        body = b"" if status == 204 else json.dumps(payload, ensure_ascii=False).encode()
        self.send_body(status, body, "application/json; charset=utf-8", {"Cache-Control": "no-store"})

    def send_empty(self, status: int, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self._common_headers()
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def send_file(self, status: int, path: Path, content_type: str, extra: dict) -> None:
        """Stream a file under a stall limit per chunk and an overall limit for the file."""
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            overall = time.monotonic() + self.timeout + size / MIN_DOWNLOAD_BYTES_PER_SEC
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self._common_headers()
            for key, value in extra.items():
                self.send_header(key, value)
            self.end_headers()
            if self.command == "HEAD":
                return
            while chunk := f.read(STREAM_CHUNK):
                remaining = overall - time.monotonic()
                if remaining <= 0:
                    self._expire()
                    return
                self._arm(min(STREAM_CHUNK_SECONDS, remaining))
                self.wfile.write(chunk)

    # -- platform routes -----------------------------------------------------

    def health(self, _m: re.Match) -> None:
        self.send_body(200, b"ok", "text/plain; charset=utf-8", {"Cache-Control": "no-store"})

    def portal(self, _m: re.Match) -> None:
        self.send_body(200, self.platform.portal, "text/html; charset=utf-8", {"Cache-Control": "no-cache"})

    def sdk(self, _m: re.Match) -> None:
        self.send_body(
            200, self.platform.sdk, "text/javascript; charset=utf-8", {"Cache-Control": "no-cache"}
        )

    # -- admin API -----------------------------------------------------------

    def _app_details(self, info: AppInfo) -> dict:
        deployments = self.platform.registry.deployments(info.name)
        return info.to_dict() | {"url": f"/{info.name}/", "deployments": [d.to_dict() for d in deployments]}

    def list_apps(self, _m: re.Match) -> None:
        apps = [info.to_dict() | {"url": f"/{info.name}/"} for info in self.platform.registry.list()]
        self.send_json(200, {"apps": apps})

    def get_app(self, m: re.Match) -> None:
        self.send_json(200, self._app_details(self.platform.app_or_404(m["app"])))

    def deploy_app(self, m: re.Match) -> None:
        self.require_admin()
        app = m["app"]
        if not names.is_app_name(app):
            raise ApiError(400, "invalid app name: use lowercase letters, digits and '-', max 40 chars")
        length = self.content_length(self.platform.config.max_bundle_bytes)
        # Only after auth: anonymous clients never get a size-based time budget.
        self._arm(self.timeout + length / MIN_UPLOAD_BYTES_PER_SEC)
        info, created = self.platform.deployer.deploy(app, self.rfile, length)
        self.send_json(201 if created else 200, self._app_details(info))

    def rollback_app(self, m: re.Match) -> None:
        self.require_admin()
        self.platform.app_or_404(m["app"])
        self.send_json(200, self._app_details(self.platform.deployer.rollback(m["app"])))

    def delete_app(self, m: re.Match) -> None:
        self.require_admin()
        self.platform.app_or_404(m["app"])
        self.platform.deployer.delete(m["app"])
        self.send_json(204, None)

    def export_app(self, m: re.Match) -> None:
        self.require_admin()
        app = self.platform.app_or_404(m["app"]).name
        path = self.platform.deployer.export(app)
        try:
            headers = {
                "Content-Disposition": f'attachment; filename="{app}-export.zip"',
                "Cache-Control": "no-store",
            }
            self.send_file(200, path, "application/zip", headers)
        finally:
            path.unlink(missing_ok=True)

    # -- data API ------------------------------------------------------------

    def _int_param(self, name: str, default: int) -> int:
        values = self.query.get(name)
        if not values:
            return default
        try:
            return int(values[0])
        except ValueError:
            raise ApiError(400, f"{name} must be an integer") from None

    def list_records(self, m: re.Match) -> None:
        store = self.platform.data(m["app"])
        order = (self.query.get("order") or ["created_at"])[0]
        result = store.list(
            m["coll"],
            order=order,
            limit=self._int_param("limit", datastore.DEFAULT_LIMIT),
            offset=self._int_param("offset", 0),
        )
        self.send_json(200, result)

    def create_record(self, m: re.Match) -> None:
        store = self.platform.data(m["app"])
        self.send_json(201, store.create(m["coll"], self.read_json()))

    def get_record(self, m: re.Match) -> None:
        self.send_json(200, self.platform.data(m["app"]).get(m["coll"], m["id"]))

    def put_record(self, m: re.Match) -> None:
        store = self.platform.data(m["app"])
        record, created = store.put(m["coll"], m["id"], self.read_json())
        self.send_json(201 if created else 200, record)

    def patch_record(self, m: re.Match) -> None:
        store = self.platform.data(m["app"])
        self.send_json(200, store.patch(m["coll"], m["id"], self.read_json()))

    def delete_record(self, m: re.Match) -> None:
        self.platform.data(m["app"]).delete(m["coll"], m["id"])
        self.send_json(204, None)

    def kv_get(self, m: re.Match) -> None:
        self.send_json(200, self.platform.data(m["app"]).kv_get(m["key"]))

    def kv_put(self, m: re.Match) -> None:
        store = self.platform.data(m["app"])
        self.send_json(200, store.kv_set(m["key"], self.read_json()))

    def kv_delete(self, m: re.Match) -> None:
        self.platform.data(m["app"]).kv_delete(m["key"])
        self.send_json(204, None)

    def run_query(self, m: re.Match) -> None:
        store = self.platform.data(m["app"])
        current = self.platform.deployer.site_dir(m["app"]) / "current"
        self.send_json(200, sqlstore.run_query(store, current, m["name"], self.read_json()))

    # -- static apps ---------------------------------------------------------

    def app_redirect(self, m: re.Match) -> None:
        info = self.platform.app_or_404(m["app"])
        location = f"/{info.name}/"
        query = urlsplit(self.path).query
        self.send_empty(301, {"Location": f"{location}?{query}" if query else location})

    def static_file(self, m: re.Match) -> None:
        info = self.platform.app_or_404(m["app"])
        path = self.platform.deployer.resolve_static(info, unquote(m["rest"]))
        if path is None:
            raise ApiError(404, "file not found")
        st = path.stat()
        etag = f'"v{info.current_version}-{st.st_mtime_ns:x}-{st.st_size:x}"'
        headers = {"Cache-Control": "no-cache", "ETag": etag}
        if etag in (self.headers.get("If-None-Match") or ""):
            self.send_empty(304, headers)
            return
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self.send_file(200, path, ctype, headers)


def _r(pattern: str) -> re.Pattern:
    return re.compile(pattern)


APP = r"(?P<app>[a-z0-9][a-z0-9-]{0,39})"
DATA = r"/_data/v1/" + APP
H = RequestHandler

# Ordered: the first matching pattern wins. Patterns are deliberately loose about
# names (validated in the handlers / datastore) so bad names give 400, not 404.
ROUTES: list[tuple[dict[str, Handler], re.Pattern]] = [
    ({"GET": H.health}, _r(r"/health")),
    ({"GET": H.portal}, _r(r"/")),
    ({"GET": H.sdk}, _r(r"/_sdk/v1\.js")),
    ({"GET": H.list_apps}, _r(r"/_api/v1/apps/?")),
    ({"GET": H.get_app, "PUT": H.deploy_app, "DELETE": H.delete_app}, _r(r"/_api/v1/apps/(?P<app>[^/]+)")),
    ({"POST": H.rollback_app}, _r(r"/_api/v1/apps/(?P<app>[^/]+)/rollback")),
    ({"GET": H.export_app}, _r(r"/_api/v1/apps/(?P<app>[^/]+)/export")),
    ({"GET": H.kv_get, "PUT": H.kv_put, "DELETE": H.kv_delete}, _r(DATA + r"/_kv/(?P<key>[^/]+)")),
    ({"POST": H.run_query}, _r(DATA + r"/_sql/(?P<name>[^/]+)")),
    ({"GET": H.list_records, "POST": H.create_record}, _r(DATA + r"/(?P<coll>[^/]+)/?")),
    (
        {"GET": H.get_record, "PUT": H.put_record, "PATCH": H.patch_record, "DELETE": H.delete_record},
        _r(DATA + r"/(?P<coll>[^/]+)/(?P<id>[^/]+)"),
    ),
    ({"GET": H.app_redirect}, _r(r"/" + APP)),
    ({"GET": H.static_file}, _r(r"/" + APP + r"/(?P<rest>.*)")),
]


def make_server(config: Config, host: str = "0.0.0.0") -> Server:
    mimetypes.add_type("text/javascript", ".mjs")
    mimetypes.add_type("application/manifest+json", ".webmanifest")
    mimetypes.add_type("image/svg+xml", ".svg")
    mimetypes.add_type("application/wasm", ".wasm")
    return Server(Platform(config), (host, config.port))
