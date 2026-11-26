"""tools/simple_apps_mcp.py: the local MCP bridge Claude chat uses to deploy apps."""

import base64
import importlib.util
import io
import json
import sys
import threading
import unittest
from pathlib import Path

from simple_apps.server import make_server

from .helpers import TOKEN, TempDirMixin, make_config

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("simple_apps_mcp", ROOT / "tools" / "simple_apps_mcp.py")
assert spec and spec.loader
mcp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mcp)

FILES = {
    "index.html": '<script src="/_sdk/v1.js"></script><script src="app.js"></script>',
    "app.js": "console.log(1)",
    "app.json": '{"name": "notes", "title": "Notes", "sdk": "v1"}',
    "db/migrations/001_init.sql": "CREATE TABLE notes (id INTEGER PRIMARY KEY, text TEXT NOT NULL);",
}


class McpTest(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.server = make_server(make_config(self.tmp), host="127.0.0.1")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.env = {"SIMPLE_APPS_URL": f"http://127.0.0.1:{self.server.server_address[1]}/"}
        self.env["SIMPLE_APPS_TOKEN_CMD"] = f"{sys.executable} -c \"print('{TOKEN}')\""

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def rpc(self, *messages, env=None):
        stdin = io.StringIO("".join(json.dumps(m) + "\n" for m in messages))
        stdout = io.StringIO()
        mcp.serve(stdin, stdout, self.env if env is None else env)
        return [json.loads(line) for line in stdout.getvalue().splitlines()]

    def call(self, name, env=None, **args):
        msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}
        result = self.rpc(msg, env=env)[0]["result"]
        return result["isError"], result.get("structuredContent") or result["content"][0]["text"]

    def test_handshake_and_tool_list(self):
        replies = self.rpc(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "nope"},
        )
        self.assertEqual(len(replies), 3)  # the notification gets no reply
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-03-26")
        names = {t["name"] for t in replies[1]["result"]["tools"]}
        self.assertEqual(names, set(mcp.HANDLERS))
        self.assertEqual(replies[2]["error"]["code"], -32601)

    def test_deploy_source_rollback_delete(self):
        err, res = self.call("get_app", app="notes")
        self.assertFalse(err)
        self.assertFalse(res["exists"])

        err, res = self.call("deploy_app", app="notes", files=FILES)
        self.assertFalse(err, res)
        self.assertEqual(res["result"], "created")
        self.assertTrue(res["url"].endswith("/notes/"))
        png = base64.b64encode(b"\x89PNG\r\n\x1a\nxx").decode()
        files = FILES | {"app.js": "console.log(2)"}
        err, res = self.call("deploy_app", app="notes", files=files, binary_files={"icon.png": png})
        self.assertEqual(res["result"], "new version")

        err, res = self.call("list_apps")
        self.assertEqual([a["name"] for a in res["apps"]], ["notes"])

        err, res = self.call("get_app_source", app="notes")
        self.assertFalse(err, res)
        self.assertEqual(res["files"]["app.js"], "console.log(2)")
        self.assertEqual(res["binary_files"], {"icon.png": png})
        self.assertNotIn("data/records.json", res["files"])

        err, res = self.call("rollback_app", app="notes")
        self.assertFalse(err, res)
        err, res = self.call("get_app_source", app="notes")
        self.assertEqual(res["files"]["app.js"], "console.log(1)")

        err, res = self.call("delete_app", app="notes", confirm="note")
        self.assertTrue(err)
        err, res = self.call("delete_app", app="notes", confirm="notes")
        self.assertFalse(err, res)
        self.assertFalse(self.call("get_app", app="notes")[1]["exists"])

    def test_platform_errors_are_tool_errors(self):
        err, text = self.call("deploy_app", app="notes", files={"index.html": "x", "db/queries.sql": "nope"})
        self.assertTrue(err)
        self.assertIn("HTTP 400", text)

        err, text = self.call(
            "deploy_app", env=self.env | {"SIMPLE_APPS_TOKEN_CMD": "false"}, app="a", files=FILES
        )
        self.assertTrue(err)
        self.assertIn("exited with status", text)

        env = {"SIMPLE_APPS_URL": "http://127.0.0.1:1", "SIMPLE_APPS_TOKEN": TOKEN}
        err, text = self.call("list_apps", env=env)
        self.assertTrue(err)
        self.assertIn("cannot reach", text)

    def test_bundle_validation(self):
        for files, binary in [
            ({"app.js": "x"}, None),  # no index.html
            ({"index.html": "x", "../evil": "x"}, None),
            ({"index.html": "x", "/abs": "x"}, None),
            ({"index.html": "x"}, {"a.png": "not base64!"}),
        ]:
            with self.subTest(files=files), self.assertRaises(mcp.ToolError):
                mcp.build_zip(files, binary)
        for bad in ("Notes", "system", "a/b", ""):
            with self.subTest(app=bad), self.assertRaises(mcp.ToolError):
                mcp.check_app(bad)
