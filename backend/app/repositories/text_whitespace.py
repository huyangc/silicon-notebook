"""后端中性的空白字符集常量,供 sqlite / postgres maintenance 的 SQL 共用。

放在这里(而非某个后端包内)是为了让两个后端的「缺 element 向量」查询用**同一份**
charset 做 TRIM——否则两后端判据漂移,同一 notebook 的 H5 计数会因后端不同而不同。
"""
from __future__ import annotations

# All code points where Python's ``str.isspace()`` is True — i.e. EXACTLY the set
# ``str.strip()`` removes. **Derived, not hardcoded**, so it can never drift from
# the embed-eligibility filter and stays byte-identical across sqlite/postgres:
# SourceEmbeddingService.embed_source skips elements whose ``text.strip()`` is
# empty, so the element-vector "missing" queries must TRIM by exactly this set —
# otherwise a tab/newline-only element they call "missing" but the embed path
# refuses to embed would be reported missing forever and backfill never converges.
# (SQLite's bare TRIM(X) / Postgres' bare btrim(X) strip U+0020 only, strictly
# weaker.) All Unicode whitespace lies in the BMP (highest is U+3000), so scanning
# through 0x3000 is complete. Pinned by tests/test_batch_ingest.py.
PY_WHITESPACE = "".join(chr(code) for code in range(0x3001) if chr(code).isspace())

__all__ = ["PY_WHITESPACE"]
