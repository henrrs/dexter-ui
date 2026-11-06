"""XDG paths and local storage initialization."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DexterPaths:
    home: Path
    config: Path
    data: Path
    metadata: Path
    datasets: Path
    artifacts: Path
    logs: Path
    temporary: Path

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "DexterPaths":
        values = env if env is not None else os.environ
        home = Path(values.get("DEXTER_HOME", Path(values.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "dexter")).expanduser()
        config = Path(values.get("DEXTER_CONFIG_HOME", Path(values.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "dexter")).expanduser()
        return cls(
            home=home,
            config=config,
            data=home,
            metadata=home / "metadata",
            datasets=home / "datasets",
            artifacts=home / "artifacts",
            logs=home / "logs",
            temporary=home / "tmp",
        )

    @property
    def database(self) -> Path:
        return self.metadata / "dexter.sqlite3"

    def ensure(self) -> None:
        for path in (self.home, self.config, self.metadata, self.datasets, self.artifacts, self.logs, self.temporary):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                path.chmod(0o700)
            except OSError:
                pass
