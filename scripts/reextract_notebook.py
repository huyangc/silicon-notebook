#!/usr/bin/env python
"""CLI: re-extract all sources of a notebook.

Usage (from repo root):
  PYTHONPATH=backend python scripts/reextract_notebook.py <notebook_id>
"""
import sys

from app.core.config import Settings
from app.services.maintenance_cli import (
    MaintenanceCliError,
    open_maintenance_cli_repository,
)
from app.services.reextract import reextract_notebook


def main() -> int:
    args = [arg for arg in sys.argv[1:] if arg != "--confirm-service-stopped"]
    confirmed = len(args) != len(sys.argv[1:])
    if len(args) != 1:
        print("usage: reextract_notebook.py <notebook_id> [--confirm-service-stopped]")
        return 2
    notebook_id = args[0]
    settings = Settings()
    try:
        with open_maintenance_cli_repository(
            settings, confirm_service_stopped=confirmed
        ) as repo:
            done = reextract_notebook(repo, notebook_id)
            print(f"[reextract] re-extracted {len(done)} source(s): {done}")
    except MaintenanceCliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
