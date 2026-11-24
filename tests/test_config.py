import unittest
from pathlib import Path

from simple_apps.config import MB, Config, ConfigError


class ConfigTest(unittest.TestCase):
    def test_requires_long_token(self):
        with self.assertRaises(ConfigError):
            Config.from_env({})
        with self.assertRaises(ConfigError):
            Config.from_env({"ADMIN_TOKEN": "short"})

    def test_defaults_and_overrides(self):
        cfg = Config.from_env({"ADMIN_TOKEN": "x" * 24, "DATA_DIR": "/tmp/sa", "MAX_BUNDLE_MB": "5"})
        self.assertEqual(cfg.data_dir, Path("/tmp/sa"))
        self.assertEqual(cfg.max_bundle_bytes, 5 * MB)
        self.assertEqual(cfg.keep_versions, 3)
        self.assertEqual(cfg.apps_dir, Path("/tmp/sa/apps"))

    def test_rejects_bad_numbers(self):
        with self.assertRaises(ConfigError):
            Config.from_env({"ADMIN_TOKEN": "x" * 24, "PORT": "abc"})
        with self.assertRaises(ConfigError):
            Config.from_env({"ADMIN_TOKEN": "x" * 24, "MAX_FILES": "0"})


if __name__ == "__main__":
    unittest.main()
