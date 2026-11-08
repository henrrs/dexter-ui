from __future__ import annotations

import json
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import patch

from dexter.adapters.databricks_volume import (
    VolumeTransportError,
    configured_volume,
    create_run_directory,
    ensure_staging_volume,
    upload_verified,
)


class FakeError(RuntimeError):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


class SyntheticVolumeTests(unittest.TestCase):
    def test_default_volume_is_created_and_re_read_with_same_identity(self):
        class Volumes:
            def __init__(self):
                self.reads = 0
                self.created = []

            def read(self, *, name):
                self.reads += 1
                if self.reads == 1:
                    raise FakeError("missing", 404)
                return {"name": name}

            def create(self, **kwargs):
                self.created.append(kwargs)

        volumes = Volumes()
        workspace = SimpleNamespace(volumes=volumes)
        package_names = ("databricks", "databricks.sdk", "databricks.sdk.service")
        packages = {name: ModuleType(name) for name in package_names}
        for package in packages.values():
            package.__path__ = []
        catalog = ModuleType("databricks.sdk.service.catalog")
        catalog.VolumeType = SimpleNamespace(MANAGED="MANAGED")
        with patch.dict(sys.modules, {**packages, "databricks.sdk.service.catalog": catalog}):
            with tempfile.TemporaryDirectory() as folder:
                result = ensure_staging_volume(workspace, "tech_dev", "gold", Path(folder))
        self.assertEqual(result, "/Volumes/tech_dev/gold/dexter")
        self.assertEqual(volumes.created[0]["catalog_name"], "tech_dev")
        self.assertEqual(volumes.created[0]["schema_name"], "gold")
        self.assertEqual(volumes.created[0]["name"], "dexter")

    def test_authorized_configured_volume_uses_its_actual_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder)
            (config / "synthetic.json").write_text(json.dumps({
                "dev_staging_volume": "/Volumes/shared/staging/dexter_transfer",
            }))
            requested = configured_volume(config, "target_dev", "gold")
            workspace = SimpleNamespace(volumes=SimpleNamespace(read=lambda *, name: {"name": name}))
            result = ensure_staging_volume(workspace, "target_dev", "gold", config)
        self.assertEqual(requested, "/Volumes/shared/staging/dexter_transfer")
        self.assertEqual(result, requested)

    def test_volume_permissions_failure_does_not_attempt_another_identity(self):
        class Volumes:
            def __init__(self):
                self.created = False

            def read(self, *, name):
                raise FakeError("forbidden", 403)

            def create(self, **kwargs):
                self.created = True

        volumes = Volumes()
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(VolumeTransportError):
                ensure_staging_volume(SimpleNamespace(volumes=volumes), "tech_dev", "gold", Path(folder))
        self.assertFalse(volumes.created)

    def test_upload_checks_remote_length_before_cleanup_is_allowed(self):
        class Files:
            checksum = None

            def upload_from(self, remote, local, overwrite, **_kwargs):
                self.uploaded = (remote, local, overwrite)

            def get_metadata(self, remote):
                return {"content_length": 42, "sha256": self.checksum}

        files = Files()
        with tempfile.TemporaryDirectory() as folder:
            local = Path(folder) / "part-000001.parquet"
            local.write_bytes(b"x" * 42)
            files.checksum = hashlib.sha256(local.read_bytes()).hexdigest()
            result = upload_verified(SimpleNamespace(files=files), local, "/Volumes/c/s/dexter/run/part-000001.parquet")
        self.assertEqual(result["bytes"], 42)
        self.assertEqual(result["remote_checksum"], files.checksum)
        self.assertEqual(result["verified_by"], "sha256")

    def test_remote_checksum_mismatch_fails_closed(self):
        files = SimpleNamespace(upload_from=lambda *args, **kwargs: None,
                                get_metadata=lambda remote: {"content_length": 42, "sha256": "0" * 64})
        with tempfile.TemporaryDirectory() as folder:
            local = Path(folder) / "part.parquet"
            local.write_bytes(b"x" * 42)
            with self.assertRaisesRegex(VolumeTransportError, "checksum"):
                upload_verified(SimpleNamespace(files=files), local, "/Volumes/c/s/v/part.parquet")

    def test_local_file_must_still_match_saved_manifest_hash(self):
        files = SimpleNamespace(upload_from=lambda *args, **kwargs: self.fail("must not upload"),
                                get_metadata=lambda remote: {"content_length": 42})
        with tempfile.TemporaryDirectory() as folder:
            local = Path(folder) / "part.parquet"
            local.write_bytes(b"x" * 42)
            with self.assertRaisesRegex(VolumeTransportError, "manifest checksum"):
                upload_verified(SimpleNamespace(files=files), local, "/Volumes/c/s/v/part.parquet", expected_sha256="0" * 64)

    def test_remote_size_mismatch_fails_closed(self):
        files = SimpleNamespace(upload_from=lambda *args, **kwargs: None,
                                get_metadata=lambda remote: {"content_length": 1})
        with tempfile.TemporaryDirectory() as folder:
            local = Path(folder) / "part.parquet"
            local.write_bytes(b"x" * 42)
            with self.assertRaises(VolumeTransportError):
                upload_verified(SimpleNamespace(files=files), local, "/Volumes/c/s/v/part.parquet")

    def test_run_directory_is_stable_and_idempotent(self):
        class AlreadyExists(FakeError):
            pass

        calls = []

        def create(path):
            calls.append(path)
            raise AlreadyExists("already exists", 409)

        workspace = SimpleNamespace(files=SimpleNamespace(create_directory=create))
        result = create_run_directory(workspace, "/Volumes/c/s/dexter", "123e4567-e89b-12d3-a456-426614174000")
        self.assertEqual(result, "/Volumes/c/s/dexter/synthetic/123e4567-e89b-12d3-a456-426614174000")
        self.assertEqual(calls, [result])


if __name__ == "__main__":
    unittest.main()
