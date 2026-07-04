"""回填进程池默认放宽到 min(32, cpu)——64 核不再白闲 56 核。"""
import os
from app.services import batch_ingest


def test_backfill_default_workers_cap_32():
    assert batch_ingest._BACKFILL_DEFAULT_WORKERS == min(32, os.cpu_count() or 1)
