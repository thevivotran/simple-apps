import http.client
import json
import socket
import threading
import time
import unittest
from unittest import mock

from simple_apps import server as server_module
from simple_apps.server import make_server

from .helpers import TOKEN, TempDirMixin, make_config, make_zip

AUTH = {"Authorization": f"Bearer {TOKEN}"}
JSON = {"Content-Type": "application/json"}


class ServerTest(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.server = make_server(make_config(self.tmp, max_body_bytes=4096), host="127.0.0.1")
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        conn.request(method, path, body=body, headers=headers or {})
        res = conn.getresponse()
        data = res.read()
        conn.close()
        ctype = res.getheader("Content-Type", "")
        payload = json.loads(data) if data and ctype.startswith("application/json") else data
        return res.status, payload, res

    def deploy(self, app="grocery", files=None, headers=AUTH):
        body = make_zip(files or {"index.html": "<p>hello</p>", "app.js": "x"})
        return self.request(
            "PUT", f"/_api/v1/apps/{app}", body, {**headers, "Content-Type": "application/zip"}
        )

    # -- platform ------------------------------------------------------------

    def test_health_portal_sdk(self):
        self.assertEqual(self.request("GET", "/health")[:2], (200, b"ok"))
        status, body, res = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Simple Apps", body)
        self.assertEqual(res.getheader("X-Content-Type-Options"), "nosniff")
        status, body, res = self.request("GET", "/_sdk/v1.js")
        self.assertEqual(status, 200)
        self.assertIn(b"simpleApps", body)
        self.assertTrue(res.getheader("Content-Type").startswith("text/javascript"))

    def test_unknown_routes(self):
        self.assertEqual(self.request("GET", "/_nope")[0], 404)
        self.assertEqual(self.request("POST", "/health")[0], 405)

    # -- admin ---------------------------------------------------------------

    def test_deploy_requires_token(self):
        self.assertEqual(self.deploy(headers={})[0], 401)
        self.assertEqual(self.deploy(headers={"Authorization": "Bearer wrong"})[0], 401)
        self.assertEqual(self.request("GET", "/_api/v1/apps")[1], {"apps": []})

    def test_deploy_serve_and_list(self):
        status, body, _ = self.deploy()
        self.assertEqual(status, 201)
        self.assertEqual(body["current_version"], 1)
        self.assertEqual(self.deploy()[0], 200)

        status, body, res = self.request("GET", "/grocery/")
        self.assertEqual((status, body), (200, b"<p>hello</p>"))
        self.assertTrue(res.getheader("Content-Type").startswith("text/html"))
        etag = res.getheader("ETag")
        self.assertEqual(self.request("GET", "/grocery/", headers={"If-None-Match": etag})[0], 304)
        self.assertEqual(self.request("HEAD", "/grocery/app.js")[0], 200)

        status, _, res = self.request("GET", "/grocery?x=1")
        self.assertEqual((status, res.getheader("Location")), (301, "/grocery/?x=1"))
        self.assertEqual(self.request("GET", "/grocery/missing.js")[0], 404)
        self.assertEqual(self.request("GET", "/grocery/%2e%2e/%2e%2e/system/system.db")[0], 404)
        self.assertEqual(self.request("GET", "/other/")[0], 404)

        apps = self.request("GET", "/_api/v1/apps")[1]["apps"]
        self.assertEqual([a["name"] for a in apps], ["grocery"])
        details = self.request("GET", "/_api/v1/apps/grocery")[1]
        self.assertEqual([d["version"] for d in details["deployments"]], [2, 1])

    def test_invalid_app_name(self):
        self.assertEqual(self.deploy(app="Bad_Name")[0], 400)
        self.assertEqual(self.deploy(app="system")[0], 400)

    def test_rollback_export_delete(self):
        self.deploy(files={"index.html": "one"})
        self.deploy(files={"index.html": "two"})
        self.assertEqual(self.request("POST", "/_api/v1/apps/grocery/rollback")[0], 401)
        status, body, _ = self.request("POST", "/_api/v1/apps/grocery/rollback", headers=AUTH)
        self.assertEqual((status, body["current_version"]), (200, 1))
        self.assertEqual(self.request("GET", "/grocery/")[1], b"one")

        status, body, res = self.request("GET", "/_api/v1/apps/grocery/export", headers=AUTH)
        self.assertEqual((status, res.getheader("Content-Type")), (200, "application/zip"))
        self.assertTrue(body.startswith(b"PK"))

        self.assertEqual(self.request("DELETE", "/_api/v1/apps/grocery", headers=AUTH)[0], 204)
        self.assertEqual(self.request("GET", "/grocery/")[0], 404)
        self.assertEqual(self.request("DELETE", "/_api/v1/apps/grocery", headers=AUTH)[0], 404)

    def test_bad_upload_reports_reason(self):
        status, body, _ = self.deploy(files={"readme.txt": "x"})
        self.assertEqual(status, 400)
        self.assertIn("index.html", body["error"])
        status, _, _ = self.request("PUT", "/_api/v1/apps/x", b"", {**AUTH, "Transfer-Encoding": "chunked"})
        self.assertEqual(status, 411)

    # -- data ----------------------------------------------------------------

    def test_data_api(self):
        self.deploy()
        base = "/_data/v1/grocery"
        self.assertEqual(self.request("GET", "/_data/v1/nope/items")[0], 404)

        status, rec, _ = self.request("POST", f"{base}/items", {"name": "Milk"}, JSON)
        self.assertEqual(status, 201)
        rid = rec["id"]
        status, rec, _ = self.request("PATCH", f"{base}/items/{rid}", {"done": True}, JSON)
        self.assertEqual(rec["data"], {"name": "Milk", "done": True})
        status, page, _ = self.request("GET", f"{base}/items?order=-created_at&limit=10")
        self.assertEqual((status, page["total"]), (200, 1))
        self.assertEqual(self.request("GET", f"{base}/items?limit=abc")[0], 400)
        self.assertEqual(self.request("PUT", f"{base}/items/custom-1", {"a": 1}, JSON)[0], 201)
        self.assertEqual(self.request("PUT", f"{base}/items/custom-1", {"a": 2}, JSON)[0], 200)
        self.assertEqual(self.request("DELETE", f"{base}/items/{rid}")[0], 204)
        self.assertEqual(self.request("GET", f"{base}/items/{rid}")[0], 404)

        self.assertEqual(
            self.request("PUT", f"{base}/_kv/theme", json.dumps("dark"), JSON)[1]["value"], "dark"
        )
        self.assertEqual(self.request("GET", f"{base}/_kv/theme")[1]["value"], "dark")
        self.assertEqual(self.request("DELETE", f"{base}/_kv/theme")[0], 204)

    def test_data_isolated_per_app(self):
        self.deploy("a")
        self.deploy("b")
        self.request("POST", "/_data/v1/a/items", {"owner": "a"}, JSON)
        self.assertEqual(self.request("GET", "/_data/v1/b/items")[1]["total"], 0)
        self.assertTrue((self.tmp / "apps" / "a" / "data.db").exists())
        self.assertTrue((self.tmp / "apps" / "b" / "data.db").exists())

    def test_data_request_validation(self):
        self.deploy()
        base = "/_data/v1/grocery/items"
        self.assertEqual(self.request("POST", base, {"a": 1}, {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("POST", base, "{bad", JSON)[0], 400)
        self.assertEqual(self.request("POST", base, [1], JSON)[0], 400)
        self.assertEqual(self.request("POST", base, {"x": "y" * 5000}, JSON)[0], 413)
        self.assertEqual(self.request("POST", "/_data/v1/grocery/Bad", {}, JSON)[0], 400)

    def test_csrf(self):
        self.deploy()
        base = "/_data/v1/grocery/items"
        host = f"127.0.0.1:{self.port}"
        evil = {**JSON, "Origin": "https://evil.example"}
        self.assertEqual(self.request("POST", base, {}, evil)[0], 403)
        self.assertEqual(self.request("POST", base, {}, {**JSON, "Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.request("POST", base, {}, {**JSON, "Origin": f"http://{host}"})[0], 201)
        proxied = {
            **JSON,
            "Origin": "https://simple-apps.tail.ts.net",
            "X-Forwarded-Host": "simple-apps.tail.ts.net",
        }
        self.assertEqual(self.request("POST", base, {}, proxied)[0], 201)
        # reads are never blocked
        self.assertEqual(self.request("GET", base, headers={"Origin": "https://evil.example"})[0], 200)


class ResourceLimitTest(TempDirMixin, unittest.TestCase):
    """Slow clients and connection floods must not exhaust the server (ARCHITECTURE.md §4.1)."""

    def start(self, **overrides):
        self.server = make_server(make_config(self.tmp, **overrides), host="127.0.0.1")
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def test_dripping_client_is_cut_off(self):
        self.start(request_timeout=1.0)
        sock = socket.create_connection(("127.0.0.1", self.port))
        sock.settimeout(0.4)  # each recv() doubles as a 0.4 s pause between bytes
        started = time.monotonic()
        response = b""
        try:
            # ~12 s if never cut off; every gap is shorter than the 1 s socket timeout
            for ch in b"GET /health HTTP/1.0\r\nX-Slow: " + b"a" * 30:
                sock.send(bytes([ch]))
                try:
                    response = sock.recv(1024)  # b"" once the server closes the socket
                    break
                except TimeoutError:
                    continue
        except OSError:
            pass
        sock.close()
        self.assertLess(time.monotonic() - started, 4)
        self.assertNotIn(b"200", response)

    def test_stalled_download_is_cut_off(self):
        self.start(request_timeout=5.0)
        big = b"x" * (8 * 1024 * 1024)
        body = make_zip({"index.html": "x", "big.bin": big})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("PUT", "/_api/v1/apps/big", body, {**AUTH, "Content-Type": "application/zip"})
        self.assertEqual(conn.getresponse().status, 201)
        conn.close()

        with (
            mock.patch.object(server_module, "STREAM_CHUNK_SECONDS", 0.5),
            self.assertLogs("simple_apps.server", "WARNING") as logs,
        ):
            sock = socket.create_connection(("127.0.0.1", self.port))
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            sock.sendall(b"GET /big/big.bin HTTP/1.0\r\n\r\n")
            time.sleep(1.5)  # never read: the server's sends stall and the chunk deadline fires
            sock.close()
        self.assertIn("request deadline exceeded", "\n".join(logs.output))

    def test_slow_steady_download_hits_overall_limit(self):
        self.start(request_timeout=0.5)
        body = make_zip({"index.html": "x", "big.bin": b"x" * (8 * 1024 * 1024)})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("PUT", "/_api/v1/apps/big", body, {**AUTH, "Content-Type": "application/zip"})
        self.assertEqual(conn.getresponse().status, 201)
        conn.close()

        # 8 MiB at >= 1 GiB/s minimum: the overall limit is ~0.5 s, while the per-chunk
        # stall limit (10 s) alone would let a trickling reader hold the slot.
        with mock.patch.object(server_module, "MIN_DOWNLOAD_BYTES_PER_SEC", 1024**3):
            sock = socket.create_connection(("127.0.0.1", self.port))
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            sock.sendall(b"GET /big/big.bin HTTP/1.0\r\n\r\n")
            sock.settimeout(0.05)
            freed = False
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not freed:
                try:
                    sock.recv(1024)  # keep trickling ~20 KiB/s
                except TimeoutError:
                    pass
                time.sleep(0.05)
                freed = self.server._slots._value == self.server.platform.config.max_threads
            sock.close()
        self.assertTrue(freed, "request slot still held by a slow reader")

    def test_busy_server_answers_503(self):
        self.start(max_threads=1, request_timeout=2.0)
        hog = socket.create_connection(("127.0.0.1", self.port))
        hog.send(b"GET /health HTTP/1.0\r\n")  # incomplete: holds the only slot
        time.sleep(0.2)
        status = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        status.request("GET", "/health")
        self.assertEqual(status.getresponse().status, 503)
        status.close()
        hog.close()


if __name__ == "__main__":
    unittest.main()
