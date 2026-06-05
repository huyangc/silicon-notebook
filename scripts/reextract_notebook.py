#!/usr/bin/env python
"""CLI: re-extract all sources of a notebook.

Usage (from repo root):
  PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python \
      scripts/reextract_notebook.py <notebook_id>
"""
import sys

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.reextract import reextract_notebook


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: reextract_notebook.py <notebook_id>")
        return 2
    notebook_id = sys.argv[1]
    repo = SQLiteRepository(Settings())
    done = reextract_notebook(repo, notebook_id)
    print(f"[reextract] re-extracted {len(done)} source(s): {done}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
