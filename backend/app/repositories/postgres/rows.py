"""PostgreSQL row types exposed to backend-neutral repository consumers."""
from __future__ import annotations

from typing import TypeAlias


PostgresRow: TypeAlias = dict[str, object]
