import io
import json
import os
import unittest
import zipfile

from simple_apps.config import MB
from simple_apps.deploy import Deployer
from simple_apps.errors import ApiError
from simple_apps.registry import Registry

from .helpers import TempDirMixin, make_config, make_zip

INDEX = {"index.html": "<h1>hi</h1>"}


class DeployTest(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.config = make_config(self.tmp)
        self.registry = Registry(self.config.system_dir)
        self.deployer = Deployer(self.config, self.registry)

    def deploy(self, app, files, **kw):
        data = make_zip(files, **kw)
        return self.deployer.deploy(app, io.BytesIO(data), len(data))

    def current(self, app):
        return self.deployer.site_dir(app) / "current"

    def assertRejected(self, files, fragment, **kw):
        with self.assertRaises(ApiError) as ctx:
            self.deploy("app", files, **kw)
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn(fragment, ctx.exception.message)
        # nothing staged is left behind
        self.assertEqual(list(self.config.tmp_dir.iterdir()), [])

    def test_first_deploy_creates_app(self):
        info, created = self.deploy("grocery", INDEX | {"js/app.js": "1"})
        self.assertTrue(created)
        self.assertEqual(info.current_version, 1)
        self.assertEqual(info.title, "grocery")
        self.assertEqual(os.readlink(self.current("grocery")), "v1")
        self.assertEqual((self.current("grocery") / "js/app.js").read_text(), "1")
        self.assertTrue((self.config.apps_dir / "grocery" / "data.db").exists())

    def test_redeploy_and_prune(self):
        for i in range(5):
            info, created = self.deploy("grocery", {"index.html": str(i)})
            self.assertEqual(created, i == 0)
        self.assertEqual(info.current_version, 5)
        self.assertEqual(
            sorted(p.name for p in self.deployer.site_dir("grocery").iterdir()), ["current", "v3", "v4", "v5"]
        )
        self.assertEqual([d.version for d in self.registry.deployments("grocery")], [5, 4, 3])
        self.assertEqual((self.current("grocery") / "index.html").read_text(), "4")

    def test_rollback(self):
        for i in range(3):
            self.deploy("grocery", {"index.html": str(i)})
        info = self.deployer.rollback("grocery")
        self.assertEqual(info.current_version, 2)
        info = self.deployer.rollback("grocery")
        self.assertEqual(info.current_version, 1)
        with self.assertRaises(ApiError) as ctx:
            self.deployer.rollback("grocery")
        self.assertEqual(ctx.exception.status, 409)
        # the next deploy continues numbering and keeps current
        info, _ = self.deploy("grocery", {"index.html": "new"})
        self.assertEqual(info.current_version, 4)

    def test_strips_single_top_folder(self):
        self.deploy("app", {"dist/index.html": "x", "dist/a/b.css": "y"})
        self.assertTrue((self.current("app") / "a" / "b.css").exists())

    def test_ignores_mac_junk(self):
        info, _ = self.deploy("app", INDEX | {"__MACOSX/._index.html": "z", ".DS_Store": "z"})
        self.assertFalse((self.current("app") / ".DS_Store").exists())
        self.assertEqual(self.registry.deployments("app")[0].file_count, 1)

    def test_requires_index(self):
        self.assertRejected({"main.html": "x"}, "index.html")

    def test_rejects_traversal(self):
        self.assertRejected(INDEX | {"../evil.txt": "x"}, "unsafe path")
        self.assertRejected(INDEX | {"/etc/passwd": "x"}, "unsafe path")
        self.assertRejected(INDEX | {"a\\b.txt": "x"}, "invalid path")
        self.assertRejected(INDEX | {"C:/x.txt": "x"}, "unsafe path")

    def test_rejects_symlinks(self):
        self.assertRejected(INDEX, "symlinks", symlinks={"link": "/etc/passwd"})

    def test_rejects_conflicting_paths(self):
        self.assertRejected(INDEX | {"a": "file", "a/b": "nested"}, "conflicting")

    def test_rejects_too_many_files(self):
        self.deployer.config = make_config(self.tmp, max_files=3)
        self.assertRejected(INDEX | {f"f{i}": "" for i in range(3)}, "more than 3 files")

    def test_rejects_zip_bomb(self):
        self.deployer.config = make_config(self.tmp, max_unpacked_bytes=1 * MB)
        self.assertRejected(INDEX | {"big.bin": b"\0" * (2 * MB)}, "unpacks to more")

    def test_rejects_lying_header(self):
        # declared size small, real content large: caught while extracting
        self.deployer.config = make_config(self.tmp, max_unpacked_bytes=1 * MB)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
            zf.writestr("index.html", "x")
            zf.writestr("big.bin", b"\0" * (2 * MB))
        raw = bytearray(buf.getvalue())
        # patch the central-directory "uncompressed size" of big.bin to 1 byte
        cd = raw.rfind(b"PK\x01\x02")
        raw[cd + 24 : cd + 28] = (1).to_bytes(4, "little")
        with self.assertRaises(ApiError) as ctx:
            self.deployer.deploy("app", io.BytesIO(bytes(raw)), len(raw))
        self.assertEqual(ctx.exception.status, 400)

    def test_rejects_non_zip_and_oversize(self):
        with self.assertRaises(ApiError) as ctx:
            self.deployer.deploy("app", io.BytesIO(b"nope"), 4)
        self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(ApiError) as ctx:
            self.deployer.deploy("app", io.BytesIO(b""), self.config.max_bundle_bytes + 1)
        self.assertEqual(ctx.exception.status, 413)

    def test_short_body(self):
        with self.assertRaises(ApiError) as ctx:
            self.deployer.deploy("app", io.BytesIO(b"abc"), 10)
        self.assertIn("ended early", ctx.exception.message)

    def test_manifest(self):
        manifest = {"name": "app", "title": "My App", "quota_mb": 5, "spa": True, "sdk": "v1"}
        info, _ = self.deploy("app", INDEX | {"app.json": json.dumps(manifest)})
        self.assertEqual((info.title, info.quota_mb, info.spa), ("My App", 5, True))

    def test_manifest_validation(self):
        cases = [
            ({"name": "other"}, "does not match"),
            ({"sdk": "v2"}, "sdk"),
            ({"title": ""}, "title"),
            ({"quota_mb": 1000}, "quota_mb"),
            ({"quota_mb": True}, "quota_mb"),
            ({"spa": "yes"}, "spa"),
            ([], "JSON object"),
        ]
        for manifest, fragment in cases:
            with self.subTest(manifest=manifest):
                self.assertRejected(INDEX | {"app.json": json.dumps(manifest)}, fragment)
        self.assertRejected(INDEX | {"app.json": "{nope"}, "not valid JSON")

    def test_failed_deploy_keeps_live_version(self):
        self.deploy("app", {"index.html": "good"})
        with self.assertRaises(ApiError):
            self.deploy("app", {"../bad": "x", "index.html": "bad"})
        self.assertEqual((self.current("app") / "index.html").read_text(), "good")

    def test_delete(self):
        self.deploy("app", INDEX)
        self.deployer.delete("app")
        self.assertIsNone(self.registry.get("app"))
        self.assertFalse((self.config.apps_dir / "app").exists())
        with self.assertRaises(ApiError):
            self.deployer.delete("app")

    def test_cleanup_tmp(self):
        (self.config.tmp_dir / "stale").mkdir()
        (self.config.tmp_dir / "stale.zip").write_bytes(b"x")
        self.deployer.cleanup_tmp()
        self.assertEqual(list(self.config.tmp_dir.iterdir()), [])

    def test_resolve_static(self):
        info, _ = self.deploy("app", INDEX | {"css/site.css": "c", "sub/index.html": "s"})
        self.assertEqual(self.deployer.resolve_static(info, "").name, "index.html")
        self.assertEqual(self.deployer.resolve_static(info, "css/site.css").read_text(), "c")
        self.assertEqual(self.deployer.resolve_static(info, "sub/").read_text(), "s")
        self.assertIsNone(self.deployer.resolve_static(info, "../../system/system.db"))
        self.assertIsNone(self.deployer.resolve_static(info, "missing"))
        self.assertIsNone(self.deployer.resolve_static(info, "a\x00b"))

    def test_spa_fallback(self):
        info, _ = self.deploy("app", INDEX | {"app.json": json.dumps({"spa": True})})
        self.assertEqual(self.deployer.resolve_static(info, "some/route").name, "index.html")
        self.assertIsNone(self.deployer.resolve_static(info, "missing.js"))

    def test_export(self):
        from simple_apps.datastore import AppData, data_path

        self.deploy("app", INDEX)
        store = AppData(data_path(self.config.apps_dir, "app"), 10)
        store.create("items", {"name": "Milk"})
        store.kv_set("settings", {"a": 1})
        path = self.deployer.export("app")
        with zipfile.ZipFile(path) as zf:
            self.assertEqual(
                sorted(zf.namelist()), ["app.json", "data/kv.json", "data/records.json", "site/index.html"]
            )
            records = json.loads(zf.read("data/records.json"))
            self.assertEqual(records["items"][0]["data"], {"name": "Milk"})
            self.assertEqual(json.loads(zf.read("data/kv.json")), {"settings": {"a": 1}})
        path.unlink()


if __name__ == "__main__":
    unittest.main()
