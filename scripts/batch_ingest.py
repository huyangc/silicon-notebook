#!/usr/bin/env python3
"""离线批量摄取 CLI(薄包装)。逻辑见 app.services.batch_ingest。

用法:
  PYTHONPATH=backend python scripts/batch_ingest.py ingest --input-dir DIR
  PYTHONPATH=backend python scripts/batch_ingest.py kg --notebook-id nb-xxx [--limit 50]
  PYTHONPATH=backend python scripts/batch_ingest.py all --input-dir DIR --notebook-name NAME
"""
from app.services.batch_ingest import main

if __name__ == "__main__":
    raise SystemExit(main())
