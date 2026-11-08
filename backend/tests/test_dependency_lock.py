from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.check_dependency_lock import check_lock_files
from scripts.doctor import pinned_dependency_status


class DependencyLockTests(unittest.TestCase):
    def test_doctor_reports_installed_version_drift_and_missing_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pyproject.toml").write_text(
                '[project]\ndependencies = ["mimesis==22.2.0", "duckdb==1.5.6"]\n', encoding="utf-8")

            def lookup(name: str) -> str:
                if name == "mimesis":
                    return "22.1.0"
                from importlib.metadata import PackageNotFoundError
                raise PackageNotFoundError(name)

            results = pinned_dependency_status(root, lookup)
            self.assertFalse(results[0][1])
            self.assertIn("installed 22.1.0", results[0][2])
            self.assertFalse(results[1][1])
            self.assertIn("not installed", results[1][2])

    def write_fixture(self, root: Path, *, python_lock: str, npm_version: str = "1.0.0") -> None:
        (root / "pyproject.toml").write_text(
            '[project]\ndependencies = ["mimesis==22.2.0", "duckdb==1.5.6"]\n', encoding="utf-8")
        (root / "package.json").write_text(json.dumps({"dependencies": {"vite": "1.0.0"}}), encoding="utf-8")
        (root / "package-lock.json").write_text(json.dumps({"packages": {"": {"dependencies": {"vite": npm_version}}}}), encoding="utf-8")
        (root / "requirements.lock").write_text(python_lock, encoding="utf-8")

    def test_matches_exact_python_and_npm_pins_with_python_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_fixture(root, python_lock=(
                "mimesis==22.2.0 \\\n --hash=sha256:abc\n"
                "duckdb==1.5.6 \\\n --hash=sha256:def\n"))
            self.assertEqual(check_lock_files(root), [])

    def test_reports_missing_hash_and_manifest_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_fixture(root, python_lock="mimesis==22.1.0\nduckdb==1.5.6 --hash=sha256:def\n", npm_version="0.9.0")
            errors = " ".join(check_lock_files(root))
            self.assertIn("no SHA-256 hash for mimesis", errors)
            self.assertIn("manifest pins 22.2.0, lock has 22.1.0", errors)
            self.assertIn("package-lock.json drift for vite", errors)

    def test_reports_missing_lockfiles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pyproject.toml").write_text('[project]\ndependencies = ["mimesis==22.2.0"]\n')
            (root / "package.json").write_text('{}')
            errors = " ".join(check_lock_files(root))
            self.assertIn("requirements.lock is missing", errors)
            self.assertIn("package-lock.json is missing", errors)


if __name__ == "__main__":
    unittest.main()
