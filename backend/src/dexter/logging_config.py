"""Structured, local JSON logging with conservative secret redaction."""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
import re
from datetime import datetime, timezone
from pathlib import Path

SECRET_PATTERN = re.compile(r"(?i)(bearer\s+|access[_-]?token[=: ]+|refresh[_-]?token[=: ]+|client[_-]?secret[=: ]+|password[=: ]+)([^\s,;]+)")


def redact(value: str) -> str:
    return SECRET_PATTERN.sub(r"\1[REDACTED]", value)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = redact(record.getMessage())
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": message,
        }
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(log_dir: Path | None = None) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        file_handler = RotatingFileHandler(log_dir / "dexter.jsonl", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(JsonFormatter())
        root.addHandler(file_handler)
        try:
            (log_dir / "dexter.jsonl").chmod(0o600)
        except OSError:
            pass
    root.setLevel(logging.INFO)
