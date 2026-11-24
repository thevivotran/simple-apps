import threading
import unittest

from simple_apps.datastore import AppData, data_path, init_app_data
from simple_apps.errors import ApiError

from .helpers import TempDirMixin


class DataStoreTest(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        init_app_data(self.tmp, "app")
        self.store = AppData(data_path(self.tmp, "app"), quota_mb=1)

    def assertStatus(self, status, fn, *args):
        with self.assertRaises(ApiError) as ctx:
            fn(*args)
        self.assertEqual(ctx.exception.status, status)

    def test_crud(self):
        rec = self.store.create("items", {"name": "Milk", "qty": 2})
        self.assertEqual(len(rec["id"]), 12)
        self.assertEqual(self.store.get("items", rec["id"])["data"], {"name": "Milk", "qty": 2})
        patched = self.store.patch("items", rec["id"], {"qty": 3, "done": True})
        self.assertEqual(patched["data"], {"name": "Milk", "qty": 3, "done": True})
        patched = self.store.patch("items", rec["id"], {"done": None})
        self.assertEqual(patched["data"], {"name": "Milk", "qty": 3})
        self.assertEqual(patched["created_at"], rec["created_at"])
        self.store.delete("items", rec["id"])
        self.assertStatus(404, self.store.get, "items", rec["id"])
        self.assertStatus(404, self.store.delete, "items", rec["id"])
        self.assertStatus(404, self.store.patch, "items", rec["id"], {})

    def test_put_upsert(self):
        rec, created = self.store.put("settings", "main", {"a": 1})
        self.assertTrue(created)
        rec2, created = self.store.put("settings", "main", {"b": 2})
        self.assertFalse(created)
        self.assertEqual(rec2["data"], {"b": 2})
        self.assertEqual(rec2["created_at"], rec["created_at"])

    def test_list_order_paging_and_isolation(self):
        ids = [self.store.create("items", {"n": i})["id"] for i in range(5)]
        self.store.create("other", {"n": 99})
        page = self.store.list("items", limit=2, offset=1)
        self.assertEqual(page["total"], 5)
        self.assertEqual([r["id"] for r in page["items"]], ids[1:3])
        newest = self.store.list("items", order="-created_at", limit=1)
        self.assertEqual(newest["items"][0]["id"], ids[-1])
        self.assertEqual(self.store.list("empty")["total"], 0)

    def test_validation(self):
        self.assertStatus(400, self.store.create, "_kv", {})
        self.assertStatus(400, self.store.create, "items", [1, 2])
        self.assertStatus(400, self.store.get, "items", "bad/id")
        self.assertStatus(400, self.store.list, "items", "random")
        self.assertStatus(400, self.store.list, "items", "created_at", 0)
        self.assertStatus(400, self.store.list, "items", "created_at", 10, -1)
        self.assertStatus(400, self.store.kv_get, "..")

    def test_kv(self):
        self.assertStatus(404, self.store.kv_get, "theme")
        self.store.kv_set("theme", "dark")
        self.store.kv_set("theme", {"mode": "light"})
        self.assertEqual(self.store.kv_get("theme")["value"], {"mode": "light"})
        self.store.kv_delete("theme")
        self.assertStatus(404, self.store.kv_delete, "theme")

    def test_quota(self):
        blob = "x" * 100_000
        with self.assertRaises(ApiError) as ctx:
            for _ in range(50):
                self.store.create("items", {"blob": blob})
        self.assertEqual(ctx.exception.status, 507)
        # deletes still work and free space for new writes
        for rec in self.store.list("items", limit=1000)["items"]:
            self.store.delete("items", rec["id"])
        self.store.create("items", {"ok": True})

    def test_concurrent_patches_do_not_clobber(self):
        rec = self.store.create("items", {})
        errors = []

        def worker(i):
            try:
                AppData(self.store.path, 1).patch("items", rec["id"], {f"f{i}": i})
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.store.get("items", rec["id"])["data"]), 10)


if __name__ == "__main__":
    unittest.main()
