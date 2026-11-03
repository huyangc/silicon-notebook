from datetime import datetime, timezone

import pytest

from app.core.config import Settings
from app.domain.instant_order import utc_microsecond_key
from app.repositories.sqlite.database import SqliteDatabase


@pytest.mark.parametrize("value,expected", [
    ("1970-01-01T00:00:00Z", 0),
    ("1970-01-01T01:00:00.000001+01:00", 1),
    ("1969-12-31T19:00:00.000001-05:00", 1),
    ("1969-12-31T23:59:59.999999+00:00", -1),
    (datetime(1970, 1, 1, 0, 0, 0, 1, tzinfo=timezone.utc), 1),
    ("1970-01-01T00:00:00.000001", 1),
    (None, None),
    ("not-an-instant", None),
])
def test_microsecond_key_is_exact_and_offset_independent(value, expected):
    assert utc_microsecond_key(value) == expected


def test_sqlite_read_and_write_connections_share_the_pure_key_without_rewriting_text(tmp_path):
    database = SqliteDatabase(Settings(database_url=f"sqlite:///{tmp_path / 'instant.db'}", _env_file=None), tmp_path)
    stamp = "1970-01-01T01:00:00.000001+01:00"
    try:
        with database.write() as db:
            db.execute("CREATE TABLE times (stamp TEXT)")
            db.execute("INSERT INTO times VALUES (?)", (stamp,))
            assert db.execute("SELECT sn_utc_us(stamp) FROM times").fetchone()[0] == 1
        with database.connect() as db:
            row = db.execute("SELECT stamp,sn_utc_us(stamp) FROM times").fetchone()
            assert tuple(row) == (stamp, 1)
            assert tuple(db.execute("SELECT sn_utc_us(NULL),sn_utc_us('bad'),sn_utc_us('1970-01-01T00:00:00.000001')").fetchone()) == (None, None, 1)
    finally:
        database.close()


def test_far_epoch_microseconds_do_not_lose_precision():
    before = utc_microsecond_key("9999-12-31T23:59:59.999998+00:00")
    after = utc_microsecond_key("9999-12-31T23:59:59.999999+00:00")
    assert after - before == 1
