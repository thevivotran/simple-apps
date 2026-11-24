import io
import json
import unittest
import zipfile
from unittest import mock

from simple_apps import sqlstore
from simple_apps.datastore import AppData, data_path
from simple_apps.db import connect
from simple_apps.deploy import Deployer
from simple_apps.errors import ApiError
from simple_apps.registry import Registry

from .helpers import TempDirMixin, make_config, make_zip

INIT = """
CREATE TABLE stores (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE items (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id INTEGER NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    name     TEXT NOT NULL CHECK (length(name) > 0),
    qty      INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX items_by_store ON items(store_id);
-- a trigger body contains semicolons and must stay one statement
CREATE TRIGGER items_no_zero BEFORE UPDATE OF qty ON items WHEN NEW.qty < 1
BEGIN
    SELECT RAISE(ABORT, 'qty must be at least 1');
END;
"""

QUERIES = """
-- Grocery queries
-- name: stores
SELECT id, name FROM stores ORDER BY name;

-- name: items_by_store
SELECT i.id, i.name, i.qty, s.name AS store
FROM items i JOIN stores s ON s.id = i.store_id
WHERE s.id = :store ORDER BY i.id;

-- name: items_in
SELECT name FROM items WHERE id IN (SELECT value FROM json_each(:ids)) ORDER BY id;

-- name: add_store (write)
INSERT INTO stores (name) VALUES (:name) RETURNING id, name;

-- name: add_item (write)
INSERT INTO items (store_id, name, qty) VALUES (:store, :name, :qty) RETURNING *;

-- name: set_qty (write)
UPDATE items SET qty = :qty WHERE id = :id;

-- name: move_all (write)
UPDATE items SET store_id = :to WHERE store_id = :from;
DELETE FROM stores WHERE id = :from;
SELECT count(*) AS n FROM items WHERE store_id = :to

-- name: blob
SELECT x'00ff' AS b, 1 AS n
"""


class SplitTest(unittest.TestCase):
    def test_split(self):
        stmts = sqlstore.split_statements(INIT)
        self.assertEqual(len(stmts), 4)
        self.assertTrue(stmts[3].startswith("-- a trigger body"))
        self.assertTrue(stmts[3].endswith("END;"))
        self.assertEqual(sqlstore.split_statements("SELECT 1; SELECT ';'"), ["SELECT 1;", "SELECT ';'"])
        self.assertEqual(sqlstore.split_statements("-- only\n/* comments */ ;"), [])

    def test_parse_queries(self):
        queries = {q.name: q for q in sqlstore.parse_queries(QUERIES)}
        self.assertFalse(queries["stores"].write)
        self.assertTrue(queries["add_item"].write)
        self.assertEqual(len(queries["move_all"].statements), 3)

    def test_parse_errors(self):
        cases = {
            "SELECT 1;\n-- name: a\nSELECT 2": "before the first",
            "-- name: a\nSELECT 1\n-- name: a\nSELECT 2": "duplicate",
            "-- name: Bad!\nSELECT 1": "invalid query name",
            "-- name: a\n-- nothing here\n": "is empty",
        }
        for text, fragment in cases.items():
            with self.subTest(text=text), self.assertRaises(ApiError) as ctx:
                sqlstore.parse_queries(text)
            self.assertIn(fragment, ctx.exception.message)


class SqlTest(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.config = make_config(self.tmp, keep_versions=2)
        self.registry = Registry(self.config.system_dir)
        self.deployer = Deployer(self.config, self.registry)

    def deploy(self, migrations=None, queries=QUERIES, app="app"):
        files = {"index.html": "<p>hi</p>"}
        for name, sql in (migrations if migrations is not None else {"001_init.sql": INIT}).items():
            files[f"db/migrations/{name}"] = sql
        if queries is not None:
            files["db/queries.sql"] = queries
        body = make_zip(files)
        return self.deployer.deploy(app, io.BytesIO(body), len(body))

    def rejected(self, fragment, **kw):
        with self.assertRaises(ApiError) as ctx:
            self.deploy(**kw)
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn(fragment, ctx.exception.message)

    def store(self, app="app", quota_mb=10):
        return AppData(data_path(self.config.apps_dir, app), quota_mb)

    def q(self, name, params=None, app="app", store=None):
        current = self.deployer.site_dir(app) / "current"
        return sqlstore.run_query(store or self.store(app), current, name, params or {})

    def tables(self, app="app"):
        conn = connect(data_path(self.config.apps_dir, app))
        try:
            return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        finally:
            conn.close()

    # -- migrations ----------------------------------------------------------

    def test_migrations_apply_once(self):
        self.deploy()
        self.assertLessEqual({"stores", "items", "records", "kv", "_migrations"}, self.tables())
        self.deploy()  # same migrations again: nothing to do
        self.deploy({"001_init.sql": INIT, "002_price.sql": "ALTER TABLE items ADD COLUMN price INTEGER;"})
        applied = sqlstore.applied_migrations(data_path(self.config.apps_dir, "app"))
        self.assertEqual(sorted(applied), ["001_init.sql", "002_price.sql"])
        self.assertEqual(self.registry.get("app").current_version, 3)

    def test_history_is_immutable(self):
        self.deploy()
        self.rejected("was changed after being applied", migrations={"001_init.sql": INIT + "\n"})
        self.rejected("must stay in the bundle", migrations={})
        self.deploy({"001_init.sql": INIT, "005_b.sql": "CREATE TABLE b (x);"})
        self.rejected(
            "numbered before",
            migrations={
                "001_init.sql": INIT,
                "003_a.sql": "CREATE TABLE a (x);",
                "005_b.sql": "CREATE TABLE b (x);",
            },
        )

    def test_bad_migration_names(self):
        self.rejected("expected <number>_<name>.sql", migrations={"init.sql": INIT})
        self.rejected("share number", migrations={"1_a.sql": "CREATE TABLE a (x);", "001_b.sql": "SELECT 1;"})

    def test_failed_migration_changes_nothing(self):
        self.deploy()
        bad = "CREATE TABLE extra (x); INSERT INTO nope VALUES (1);"
        self.rejected(
            "002_bad.sql failed: no such table: nope", migrations={"001_init.sql": INIT, "002_bad.sql": bad}
        )
        self.assertNotIn("extra", self.tables())
        self.assertEqual(self.registry.get("app").current_version, 1)
        # a brand-new app that fails leaves nothing behind
        self.rejected("syntax error", app="fresh", migrations={"001_x.sql": "CREATE TABLE ("})
        self.assertFalse((self.config.apps_dir / "fresh").exists())
        self.assertIsNone(self.registry.get("fresh"))

    def test_migration_sandbox(self):
        cases = {
            "ATTACH DATABASE 'x.db' AS x;": "ATTACH",
            "PRAGMA writable_schema = ON;": "PRAGMA",
            "BEGIN; CREATE TABLE a (x); COMMIT;": "transactions are managed",
            "DROP TABLE records;": "platform object",
            "CREATE INDEX r2 ON records(updated_at);": "platform object",
            "INSERT INTO kv VALUES ('a', '1', 'now');": "platform table",
            "CREATE TEMP TABLE t (x);": "not allowed",
        }
        for sql, fragment in cases.items():
            with self.subTest(sql=sql):
                self.rejected(fragment, migrations={"001_x.sql": sql}, queries=None)

    def test_migration_may_read_collections(self):
        self.deploy(migrations={}, queries=None)
        self.store().create("items", {"name": "Milk"})
        self.deploy(
            {
                "001_t.sql": "CREATE TABLE t (name TEXT);"
                " INSERT INTO t SELECT json_extract(data, '$.name') FROM records WHERE collection = 'items';"
            },
            queries="-- name: all\nSELECT name FROM t",
        )
        self.assertEqual(self.q("all")["rows"], [{"name": "Milk"}])

    def test_snapshot_before_migrating(self):
        self.deploy()
        self.q("add_store", {"name": "Aldi"})
        self.deploy(
            {"001_init.sql": INIT, "002_drop.sql": "DROP TABLE items; DROP TABLE stores;"}, queries=None
        )
        snap = self.deployer.snapshot_dir("app") / "before-v2.db"
        self.assertTrue(snap.is_file())
        conn = connect(snap)
        try:
            self.assertEqual(conn.execute("SELECT name FROM stores").fetchall()[0][0], "Aldi")
        finally:
            conn.close()
        # snapshots are pruned with their versions (keep_versions=2)
        self.deploy(
            {"001_init.sql": INIT, "002_drop.sql": "DROP TABLE items; DROP TABLE stores;"}, queries=None
        )
        self.deploy(
            {
                "001_init.sql": INIT,
                "002_drop.sql": "DROP TABLE items; DROP TABLE stores;",
                "003_x.sql": "CREATE TABLE x (a);",
            },
            queries=None,
        )
        self.assertEqual(
            sorted(p.name for p in self.deployer.snapshot_dir("app").iterdir()), ["before-v4.db"]
        )

    # -- query validation at deploy -------------------------------------------

    def test_queries_validated_at_deploy(self):
        cases = {
            "-- name: a\nSELECT * FROM nope": "no such table: nope",
            "-- name: a\nINSERT INTO stores (name) VALUES ('x')": "mark the query '(write)'",
            "-- name: a (write)\nCREATE TABLE z (x)": "db/migrations",
            "-- name: a (write)\nDROP TABLE items": "db/migrations",
            "-- name: a\nPRAGMA table_info(items)": "PRAGMA",
            "-- name: a\nATTACH DATABASE '/data/system/system.db' AS sys": "ATTACH",
            "-- name: a (write)\nDELETE FROM records": "platform table",
            "-- name: a (write)\nBEGIN": "transactions are managed",
            "-- name: a\nSELECT load_extension('x')": "load_extension",
        }
        for queries, fragment in cases.items():
            with self.subTest(queries=queries):
                self.rejected(fragment, queries=queries)
        # A failing query rolls back the migrations deployed with it (a new app leaves nothing).
        self.assertFalse((self.config.apps_dir / "app").exists())
        self.deploy(queries=None)
        self.rejected(
            "no such table: nope",
            migrations={"001_init.sql": INIT, "002_x.sql": "CREATE TABLE x (a);"},
            queries="-- name: a\nSELECT * FROM nope",
        )
        self.assertNotIn("x", self.tables())

    # -- queries at request time ----------------------------------------------

    def test_crud(self):
        self.deploy()
        store = self.q("add_store", {"name": "Aldi"})
        self.assertEqual(store, {"rows": [{"id": 1, "name": "Aldi"}], "changes": 1, "truncated": False})
        milk = self.q("add_item", {"store": 1, "name": "Milk", "qty": 2})["rows"][0]
        self.q("add_item", {"store": 1, "name": "Eggs", "qty": 12})
        self.assertEqual(milk["qty"], 2)
        self.assertEqual(self.q("set_qty", {"id": milk["id"], "qty": 3})["changes"], 1)
        rows = self.q("items_by_store", {"store": 1})["rows"]
        self.assertEqual(
            [(r["name"], r["qty"], r["store"]) for r in rows], [("Milk", 3, "Aldi"), ("Eggs", 12, "Aldi")]
        )
        self.assertEqual(self.q("items_in", {"ids": [2]})["rows"], [{"name": "Eggs"}])
        # extra parameters are ignored; booleans bind as integers
        self.assertEqual(len(self.q("stores", {"unused": True})["rows"]), 1)

    def test_multi_statement_write_is_one_transaction(self):
        self.deploy()
        self.q("add_store", {"name": "Aldi"})
        self.q("add_store", {"name": "Lidl"})
        self.q("add_item", {"store": 1, "name": "Milk", "qty": 1})
        result = self.q("move_all", {"from": 1, "to": 2})
        self.assertEqual(result["rows"], [{"n": 1}])
        self.assertEqual(result["changes"], 2)
        self.assertEqual([r["name"] for r in self.q("stores")["rows"]], ["Lidl"])
        # a failure in the last statement undoes the earlier ones
        with self.assertRaises(ApiError) as ctx:
            self.q("move_all", {"from": 2, "to": 99})
        self.assertEqual(ctx.exception.status, 409)  # FOREIGN KEY constraint failed
        self.assertEqual([r["name"] for r in self.q("stores")["rows"]], ["Lidl"])

    def test_errors(self):
        self.deploy()
        self.q("add_store", {"name": "Aldi"})
        cases = [
            ("nope", {}, 404, "query not found"),
            ("Bad!", {}, 400, "invalid query name"),
            ("items_by_store", {}, 400, "store"),
            ("add_store", {"name": "Aldi"}, 409, "UNIQUE"),
            ("add_item", {"store": 1, "name": "", "qty": 1}, 409, "CHECK"),
            ("add_item", {"store": 9, "name": "x", "qty": 1}, 409, "FOREIGN KEY"),
        ]
        for name, params, status, fragment in cases:
            with self.subTest(name=name, params=params), self.assertRaises(ApiError) as ctx:
                self.q(name, params)
            self.assertEqual(ctx.exception.status, status)
            self.assertIn(fragment, ctx.exception.message)
        self.q("add_item", {"store": 1, "name": "Milk", "qty": 1})
        with self.assertRaises(ApiError) as ctx:
            self.q("set_qty", {"id": 1, "qty": 0})
        self.assertEqual((ctx.exception.status, ctx.exception.message), (409, "qty must be at least 1"))
        with self.assertRaises(ApiError) as ctx:
            sqlstore.run_query(self.store(), self.deployer.site_dir("app") / "current", "stores", [1])
        self.assertEqual(ctx.exception.status, 400)

    def test_limits(self):
        self.deploy(
            queries=QUERIES
            + "\n-- name: many\nWITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n LIMIT 1500)"
            " SELECT i FROM n"
            "\n-- name: forever\nWITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n)"
            " SELECT count(*) FROM n"
        )
        result = self.q("many")
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["rows"]), sqlstore.MAX_ROWS)
        self.assertEqual(self.q("blob")["rows"], [{"b": "AP8=", "n": 1}])
        with mock.patch.object(sqlstore, "QUERY_SECONDS", 0.05), self.assertRaises(ApiError) as ctx:
            self.q("forever")
        self.assertEqual(ctx.exception.status, 503)

    def test_quota(self):
        self.deploy()
        full = self.store(quota_mb=0)
        with self.assertRaises(ApiError) as ctx:
            self.q("add_store", {"name": "x"}, store=full)
        self.assertEqual(ctx.exception.status, 507)
        self.assertEqual(self.q("stores", store=full)["rows"], [])  # reads still work

    def test_rollback_reverts_queries(self):
        self.deploy()
        self.deploy(queries=QUERIES + "\n-- name: newer\nSELECT 1 AS one")
        self.assertEqual(self.q("newer")["rows"], [{"one": 1}])
        self.deployer.rollback("app")
        with self.assertRaises(ApiError) as ctx:
            self.q("newer")
        self.assertEqual(ctx.exception.status, 404)

    def test_export_tables(self):
        self.deploy()
        self.q("add_store", {"name": "Aldi"})
        self.store().create("notes", {"t": 1})
        path = self.deployer.export("app")
        with zipfile.ZipFile(path) as zf:
            schema = zf.read("data/schema.sql").decode()
            tables = json.loads(zf.read("data/tables.json"))
        path.unlink()
        self.assertIn("CREATE TABLE stores", schema)
        self.assertIn("CREATE TRIGGER items_no_zero", schema)
        self.assertNotIn("records", schema)
        self.assertEqual(tables, {"stores": [{"id": 1, "name": "Aldi"}], "items": []})


if __name__ == "__main__":
    unittest.main()
