from __future__ import annotations

import configparser
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from dexter.adapters.databricks_setup import normalize_workspace_host, setup_pair
from dexter.adapters.databricks import discover_profiles, profile_config_file
from dexter.paths import DexterPaths


class DatabricksSetupTests(unittest.TestCase):
    def test_workspace_host_normalization_rejects_credentials_and_paths(self):
        self.assertEqual(normalize_workspace_host(" HTTPS://DEV.EXAMPLE.COM/ "), "https://dev.example.com")
        with self.assertRaises(ValueError):
            normalize_workspace_host("https://user:password@dev.example.com")
        with self.assertRaises(ValueError):
            normalize_workspace_host("https://dev.example.com/workspace")

    def test_setup_creates_two_profiles_after_both_workspaces_validate_and_preserves_other_profiles(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "databrickscfg"
            state = root / "dexter" / "profiles.json"
            original = "# user's unrelated profile\n[OTHER]\nhost = https://other.example\ntoken = preserved-external-value\n"
            config.write_text(original, encoding="utf-8")
            calls = []

            def validate(host, tenant):
                calls.append((host, tenant))
                return {"ok": True}

            result = setup_pair("https://dev.example.com", "https://prod.example.com", "tenant-guid",
                                config_path=config, state_path=state, validator=validate)
            parser = configparser.ConfigParser(interpolation=None)
            parser.read(config)
            self.assertEqual(calls, [("https://dev.example.com", "tenant-guid"), ("https://prod.example.com", "tenant-guid")])
            self.assertEqual(parser["OTHER"]["token"], "preserved-external-value")
            self.assertEqual(parser["DEXTER-DEV"]["auth_type"], "azure-cli")
            self.assertEqual(parser["DEXTER-PRD"]["host"], "https://prod.example.com")
            self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o600)
            self.assertEqual(result["status"], "configured")

    def test_failed_validation_does_not_write_either_profile_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, state = root / "databrickscfg", root / "profiles.json"

            def validate(host, _tenant):
                return {"ok": "dev.example" in host}

            with self.assertRaisesRegex(ValueError, "PRD"):
                setup_pair("https://dev.example.com", "https://prod.example.com", "tenant",
                           config_path=config, state_path=state, validator=validate)
            self.assertFalse(config.exists())
            self.assertFalse(state.exists())

    def test_existing_profile_name_is_never_taken_over_without_dexter_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, state = root / "databrickscfg", root / "profiles.json"
            config.write_text("[DEXTER-DEV]\nhost=https://someone.example\nauth_type=pat\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "without Dexter ownership"):
                setup_pair("https://dev.example.com", "https://prod.example.com", "tenant",
                           config_path=config, state_path=state, validator=lambda *_: {"ok": True})
            self.assertIn("someone.example", config.read_text(encoding="utf-8"))

    def test_managed_and_regular_profiles_are_discovered_from_their_own_files(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            managed_home = root / "dexter-config"
            regular = root / "databrickscfg"
            paths = DexterPaths.from_env({"DEXTER_CONFIG_HOME": str(managed_home)})
            paths.databricks_config.parent.mkdir(parents=True)
            paths.databricks_config.write_text("[DEXTER-DEV]\nhost=https://dev.example.com\nauth_type=azure-cli\n", encoding="utf-8")
            regular.write_text("[SHARED]\nhost=https://shared.example.com\n", encoding="utf-8")
            env = {"DEXTER_CONFIG_HOME": str(managed_home), "DATABRICKS_CONFIG_FILE": str(regular)}
            with patch.dict(os.environ, env):
                profiles, _ = discover_profiles()
                self.assertEqual(profiles, ["DEXTER-DEV", "SHARED"])
                self.assertEqual(profile_config_file("DEXTER-DEV"), paths.databricks_config)
                self.assertEqual(profile_config_file("SHARED"), regular)


if __name__ == "__main__":
    unittest.main()
