from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.core.config import Settings


class SqliteDatabase:
    def __init__(self, settings: Settings, root_dir: Path) -> None:
        self.settings = settings
        self.root_dir = root_dir
        self.db_path = self.resolve_path(settings.sqlite_path)
        self.write_lock = threading.RLock()

    def resolve_path(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.root_dir / path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path, timeout=self.settings.db_busy_timeout_ms / 1000
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(f"PRAGMA busy_timeout = {int(self.settings.db_busy_timeout_ms)}")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA cache_size = -65536")
        connection.execute("PRAGMA temp_store = MEMORY")
        connection.execute("PRAGMA mmap_size = 268435456")
        return connection

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        with self.write_lock:
            with self.connect() as db:
                yield db
