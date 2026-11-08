#!/usr/bin/env python3
"""Offline check that the committed installation locks match their manifests."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_PIN = re.compile(r"^\s*([A-Za-z0-9_.-]+)==([^;\\\s]+)")


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _python_lock(path: Path) -> tuple[dict[str, tuple[str, bool]], list[str]]:
    entries: dict[str, tuple[str, bool]] = {}
    errors: list[str] = []
    if not path.is_file():
        return entries, ["requirements.lock is missing"]
    current: str | None = None
    block = ""
    for line in path.read_text(encoding="utf-8").splitlines() + [""]:
        match = _PIN.match(line)
        if match:
            if current is not None:
                version, has_hash = entries[current]
                entries[current] = (version, has_hash or "--hash=sha256:" in block)
            name = _normalize(match.group(1))
            if name in entries:
                errors.append(f"requirements.lock contains duplicate package {name}")
            current = name
            entries[name] = (match.group(2), False)
            block = line
        elif current is not None:
            block += "\n" + line
    if current is not None:
        version, has_hash = entries[current]
        entries[current] = (version, has_hash or "--hash=sha256:" in block)
    for name, (_version, has_hash) in entries.items():
        if not has_hash:
            errors.append(f"requirements.lock has no SHA-256 hash for {name}")
    if re.search(r"(?im)^\s*--extra-index-url\b", path.read_text(encoding="utf-8")):
        errors.append("requirements.lock enables an extra package index")
    return entries, errors


def check_lock_files(root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    try:
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        return [f"could not read dependency manifests ({type(exc).__name__})"]

    python_entries, lock_errors = _python_lock(root / "requirements.lock")
    errors.extend(lock_errors)
    if (root / "requirements.lock").is_file():
        for raw in project.get("project", {}).get("dependencies", []):
            match = re.match(r"^\s*([A-Za-z0-9_.-]+)==([^;\s]+)\s*$", raw)
            if not match:
                errors.append(f"Python dependency is not exactly pinned: {raw.split(';', 1)[0]}")
                continue
            name, version = _normalize(match.group(1)), match.group(2)
            locked = python_entries.get(name)
            if locked is None:
                errors.append(f"requirements.lock is missing direct dependency {name}")
            elif locked[0] != version:
                errors.append(f"requirements.lock drift for {name}: manifest pins {version}, lock has {locked[0]}")

    package_lock_path = root / "package-lock.json"
    if not package_lock_path.is_file():
        errors.append("package-lock.json is missing")
    else:
        try:
            lock = json.loads(package_lock_path.read_text(encoding="utf-8"))
            locked_root = lock.get("packages", {}).get("", {})
            for group in ("dependencies", "devDependencies", "optionalDependencies"):
                expected = package.get(group, {})
                actual = locked_root.get(group, {})
                for name, version in expected.items():
                    if actual.get(name) != version:
                        errors.append(f"package-lock.json drift for {name}: package.json has {version}, lock has {actual.get(name, 'no entry')}")
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"could not parse package-lock.json ({type(exc).__name__})")
    return errors


def main() -> int:
    errors = check_lock_files()
    if errors:
        for error in errors:
            print(f"CHECK  Dependency lock: {error}")
        return 1
    print("OK     Dependency locks match pyproject/package manifests and include Python hashes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
