"""Entry point for an OS memory-scoped synthetic-data operation."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from dexter.logging_config import configure_logging
from dexter.services.synthetic_pipeline import run_synthetic_operation


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--config-dir", required=True, type=Path)
    parser.add_argument("--temporary-dir", required=True, type=Path)
    arguments = parser.parse_args()
    configure_logging(arguments.database.parent / "logs")
    logging.getLogger("dexter.synthetic").info("Synthetic worker started operation=%s", arguments.operation_id)
    run_synthetic_operation(operation_id=arguments.operation_id, worker_id=arguments.worker_id,
        database=arguments.database, config_dir=arguments.config_dir, temporary_dir=arguments.temporary_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
