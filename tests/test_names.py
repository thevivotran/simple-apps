import unittest

from simple_apps import names


class NamesTest(unittest.TestCase):
    def test_app_names(self):
        for ok in ["grocery", "a", "chore-chart", "x1", "a" * 40]:
            self.assertTrue(names.is_app_name(ok), ok)
        for bad in ["", "Grocery", "-a", "_api", ".git", "a/b", "a" * 41, "system", "tmp", "a b", "..", "é"]:
            self.assertFalse(names.is_app_name(bad), bad)

    def test_collections(self):
        self.assertTrue(names.is_collection("items"))
        self.assertTrue(names.is_collection("shopping_list-2"))
        for bad in ["_kv", "", "Items", "a/b", "a" * 65]:
            self.assertFalse(names.is_collection(bad), bad)

    def test_record_ids_and_keys(self):
        self.assertTrue(names.is_record_id("Xk3f9aQ1bZ0c"))
        self.assertFalse(names.is_record_id("a.b"))
        self.assertFalse(names.is_record_id(""))
        self.assertTrue(names.is_kv_key("settings.v2"))
        for bad in ["..", ".", "a/b", ""]:
            self.assertFalse(names.is_kv_key(bad), bad)


if __name__ == "__main__":
    unittest.main()
