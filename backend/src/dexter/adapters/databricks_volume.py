"""Databricks Unity Catalog Volume transport for staged synthetic Parquet files."""

from __future__ import annotations

import json
import hashlib
import random
import time
from pathlib import Path
from typing import Any


class VolumeTransportError(RuntimeError):
    pass


def verify_remote_file(workspace: Any, remote_file: str, *, expected_size: int,
                       expected_sha256: str | None = None) -> dict[str, Any]:
    try:
        metadata = workspace.files.get_metadata(remote_file)
    except Exception as exc:
        raise VolumeTransportError("The previously uploaded file is no longer available in the selected Volume") from exc
    size = _field(metadata, "content_length")
    if size is None or int(size) != expected_size:
        raise VolumeTransportError("The remote file size does not match the saved manifest")
    checksum = _field(metadata, "sha256")
    if expected_sha256 and isinstance(checksum, str) and checksum.strip():
        normalized = checksum.strip().lower().removeprefix("sha256:")
        if normalized != expected_sha256.casefold():
            raise VolumeTransportError("The remote file checksum does not match the saved manifest")
    return {"remote_path": remote_file, "bytes": int(size), "remote_checksum": checksum,
            "verified_by": "sha256" if checksum else "length"}


def configured_volume(config_dir: Path, catalog: str, schema: str) -> str | None:
    """Read an optional authorized staging Volume path from Dexter local config."""
    path = config_dir / "synthetic.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = document.get("dev_staging_volume") if isinstance(document, dict) else None
    if not isinstance(value, str):
        return None
    parts = value.strip().split("/")
    if len(parts) != 5 or parts[0] != "" or parts[1] != "Volumes" or not all(parts[2:]):
        raise ValueError("Configured staging volume must use /Volumes/catalog/schema/volume")
    if any(part in {".", ".."} or not part.replace("_", "a").replace("-", "a").isalnum() for part in parts[2:]):
        raise ValueError("Configured staging volume contains an invalid identifier")
    return value.rstrip("/")


def ensure_staging_volume(workspace: Any, catalog: str, schema: str, config_dir: Path) -> str:
    configured = configured_volume(config_dir, catalog, schema)
    volume_name = ".".join(configured.strip("/").split("/")[1:]) if configured else f"{catalog}.{schema}.dexter"
    try:
        workspace.volumes.read(name=volume_name)
        return configured or f"/Volumes/{volume_name.replace('.', '/', 2)}"
    except Exception as read_error:
        status = getattr(read_error, "status_code", None)
        text = str(read_error).lower()
        missing = status == 404 or any(token in text for token in ("not found", "does not exist", "404"))
        if not missing:
            if configured:
                raise VolumeTransportError("The configured authorized staging Volume is not readable") from None
            raise VolumeTransportError("The Dexter staging Volume could not be inspected; no alternate identity was used") from None
    if configured:
        raise VolumeTransportError("The configured staging Volume does not exist or is not accessible")
    try:
        from databricks.sdk.service.catalog import VolumeType
        workspace.volumes.create(catalog_name=catalog, schema_name=schema, name="dexter",
                                  volume_type=VolumeType.MANAGED,
                                  comment="Dexter temporary synthetic-data staging")
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        if status not in {409} and not any(token in str(exc).lower() for token in ("already exists", "volume_already_exists")):
            raise VolumeTransportError("The user identity cannot create the Dexter staging Volume. Configure an authorized Volume in Dexter local config.") from None
    try:
        workspace.volumes.read(name=volume_name)
    except Exception:
        raise VolumeTransportError("The Dexter staging Volume could not be verified after creation") from None
    return f"/Volumes/{catalog}/{schema}/dexter"


def create_run_directory(workspace: Any, volume_root: str, run_id: str) -> str:
    if not run_id or any(char not in "0123456789abcdef-" for char in run_id.lower()):
        raise ValueError("run_id must be a stable UUID-like identifier")
    target = f"{volume_root.rstrip('/')}/synthetic/{run_id}"
    try:
        workspace.files.create_directory(target)
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        if status != 409 and not any(token in str(exc).lower() for token in ("already exists", "resource_already_exists")):
            raise VolumeTransportError("Databricks could not create a private staging directory for this run") from None
    return target


def upload_verified(workspace: Any, local_file: Path, remote_file: str, *, expected_sha256: str | None = None,
                    retries: int = 4) -> dict[str, Any]:
    """Stream a local file, retrying to a stable target and verifying remote length."""
    expected_size = local_file.stat().st_size
    local_checksum = _sha256(local_file)
    if expected_sha256 is not None and expected_sha256.casefold() != local_checksum:
        raise VolumeTransportError("The local Parquet file no longer matches its saved manifest checksum")
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            # The SDK defaults to parallel multipart upload. Keep one uploader
            # single-threaded and bound its transfer buffer inside the shared
            # generation/upload memory budget.
            workspace.files.upload_from(remote_file, str(local_file), overwrite=True,
                                         part_size=4 * 1024 * 1024, use_parallel=False)
            metadata = workspace.files.get_metadata(remote_file)
            actual_size = _field(metadata, "content_length")
            if actual_size is None or int(actual_size) != expected_size:
                raise VolumeTransportError("The uploaded file size did not match its local checksum record")
            etag = _field(metadata, "etag")
            checksum = _field(metadata, "sha256")
            if isinstance(checksum, str) and checksum.strip():
                normalized_remote = checksum.strip().lower().removeprefix("sha256:")
                if normalized_remote != local_checksum:
                    raise VolumeTransportError("The remote checksum did not match the local Parquet file")
            return {"remote_path": remote_file, "bytes": expected_size, "sha256": local_checksum, "etag": etag,
                    "remote_checksum": checksum, "verified_by": "length" if not checksum else "sha256"}
        except VolumeTransportError as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(min(5.0, 0.25 * (2**attempt)) + random.uniform(0, 0.1))
        except Exception as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(min(5.0, 0.25 * (2**attempt)) + random.uniform(0, 0.1))
    if isinstance(last_error, VolumeTransportError):
        raise VolumeTransportError(f"Upload to the selected Databricks Volume failed after retries: {last_error}") from last_error
    raise VolumeTransportError("Upload to the selected Databricks Volume failed after retries") from last_error


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
