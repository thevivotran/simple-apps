"""The app-authoring skill (skill/simple-apps-app) must stay in step with the platform."""

import contextlib
import importlib.util
import io
import re
import unittest
import zipfile
from pathlib import Path

from .helpers import TempDirMixin

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skill" / "simple-apps-app"

spec = importlib.util.spec_from_file_location("check_bundle", SKILL / "scripts" / "check_bundle.py")
assert spec and spec.loader
check_bundle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_bundle)

MIGRATION = "CREATE TABLE notes (id INTEGER PRIMARY KEY, text TEXT NOT NULL);"
QUERIES = (
    "-- name: add (write)\nINSERT INTO notes (text) VALUES (:text) RETURNING id;\n"
    "-- name: all\nSELECT * FROM notes"
)


class SkillDocsTest(unittest.TestCase):
    def test_references_match_docs(self):
        for name in ("APP_AUTHORING.md", "ARCHITECTURE.md"):
            with self.subTest(name=name):
                self.assertEqual(
                    (SKILL / "references" / name).read_text(),
                    (ROOT / name).read_text(),
                    f"skill copy of {name} is stale: run `make skill`",
                )

    def test_frontmatter(self):
        text = (SKILL / "SKILL.md").read_text()
        m = re.match(r"---\nname: (.+)\ndescription: (.+)\n---\n", text)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "simple-apps-app")
        self.assertLessEqual(len(m.group(2)), 1024)


class CheckBundleTest(TempDirMixin, unittest.TestCase):
    def make(self, files):
        app = self.tmp / "notes"
        for rel, content in files.items():
            path = app / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        return app

    def run_check(self, app, *extra):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = check_bundle.main([str(app), "--name", "notes", *extra])
        return code, out.getvalue()

    def good(self, **overrides):
        files = {
            "index.html": '<script src="/_sdk/v1.js"></script><script src="app.js"></script>',
            "app.js": "simpleApps.sql('all', {});",
            "app.json": '{"name": "notes", "title": "Notes", "sdk": "v1"}',
            "db/migrations/001_init.sql": MIGRATION,
            "db/queries.sql": QUERIES,
            "README.md": "docs",
            ".git/config": "x",
        }
        files.update(overrides)
        return self.make(files)

    def test_good_bundle_zips(self):
        out_zip = self.tmp / "notes.zip"
        code, out = self.run_check(self.good(), "--zip", str(out_zip))
        self.assertEqual(code, 0, out)
        with zipfile.ZipFile(out_zip) as zf:
            self.assertEqual(
                sorted(zf.namelist()),
                ["app.js", "app.json", "db/migrations/001_init.sql", "db/queries.sql", "index.html"],
            )

    def test_good_bundle_deploys_on_the_platform(self):
        """What the checker accepts, the platform accepts."""
        from simple_apps.deploy import Deployer
        from simple_apps.registry import Registry

        from .helpers import make_config

        out_zip = self.tmp / "notes.zip"
        self.assertEqual(self.run_check(self.good(), "--zip", str(out_zip))[0], 0)
        config = make_config(self.tmp / "platform")
        body = out_zip.read_bytes()
        _info, created = Deployer(config, Registry(config.system_dir)).deploy(
            "notes", io.BytesIO(body), len(body)
        )
        self.assertTrue(created)

    def test_errors(self):
        cases = {
            "absolute asset": ({"index.html": '<script src="/app.js"></script>'}, "absolute asset path"),
            "name mismatch": ({"app.json": '{"name": "other"}'}, "does not match"),
            "quota": ({"app.json": '{"quota_mb": 500}'}, "quota_mb"),
            "no sdk tag": ({"index.html": '<script src="app.js"></script>'}, "no page loads"),
            "bad migration name": ({"db/migrations/init.sql": MIGRATION}, "expected <number>_<name>.sql"),
            "migration fails": (
                {"db/migrations/002_x.sql": "INSERT INTO nope VALUES (1);"},
                "no such table: nope",
            ),
            "platform table": ({"db/migrations/002_x.sql": "DROP TABLE records;"}, "platform objects"),
            "unknown column": ({"db/queries.sql": "-- name: a\nSELECT missing FROM notes"}, "no such column"),
            "missing (write)": (
                {"db/queries.sql": "-- name: a\nDELETE FROM notes"},
                "mark the query '(write)'",
            ),
            "ddl in query": ({"db/queries.sql": "-- name: a (write)\nDROP TABLE notes"}, "db/migrations"),
            "pragma": ({"db/queries.sql": "-- name: a\nPRAGMA table_info(notes)"}, "PRAGMA"),
            "duplicate query": (
                {"db/queries.sql": "-- name: a\nSELECT 1\n-- name: a\nSELECT 2"},
                "duplicate",
            ),
        }
        for label, (overrides, fragment) in cases.items():
            with self.subTest(label):
                self._tmp.cleanup()
                self.setUp()
                code, out = self.run_check(self.good(**overrides))
                self.assertEqual(code, 1, out)
                self.assertIn(fragment, out)

    def test_warnings_do_not_fail(self):
        code, out = self.run_check(
            self.good(**{"app.js": "simpleApps.kv; el.innerHTML = x; localStorage.getItem('a');"})
        )
        self.assertEqual(code, 0, out)
        self.assertIn("innerHTML", out)
        self.assertIn("localStorage", out)


if __name__ == "__main__":
    unittest.main()
