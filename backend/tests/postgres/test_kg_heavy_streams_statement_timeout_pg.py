"""KG 重建的两条流式整表读在**真实 PostgreSQL** 上扛得住慢消费者。

背景:`cursor().stream()` 下服务端语句要等客户端读完最后一行才算结束,
`statement_timeout` 因此覆盖**整个消费期**。生产上两条流(836 万关系 / 878 万对象)的
消费期按 ~2 µs/行推算要多出十几到二十几秒(推算),默认
`POSTGRES_STATEMENT_TIMEOUT_SECONDS=30` 下有被 `QueryCanceled` 的风险。所以
`community_graph_rows` / `source_canonical_rows` 在函数一开头把**本事务**的
`statement_timeout` 抬到与离线维护同一个 600 s 底线(只抬不压、0 保持、事务本地)。

两件事都在真库上钉:
  1. session 超时压到 500 ms、消费者在读到第 100 行时人为停 1.5 s —— 放宽之前服务端
     卡在 send 上、超时先到,下一次取行 `QueryCanceled`;放宽之后读得完,而且在事务内
     看到的是 600000 ms。夹具刻意做大(每行 ~1.4 KB × 12000 行 ≈ 17 MB),远超任何
     socket 缓冲,所以「服务端被背压卡住」在 macOS 与 Linux 上都成立,不依赖缓冲大小。
  2. 事务提交之后**同一条连接**的 statement_timeout 回到调用前的 session 值(500 ms),
     而不是 600000 —— 放宽是事务本地的,没有泄漏给池里的下一个借用者。
"""
from __future__ import annotations

import time

import pytest

from app.repositories.postgres._store_utils import normalize_timestamp
from app.repositories.postgres.migrator import PostgresMigrator
from app.repositories.postgres.unified_kg_store import UnifiedKgStore as PgUnifiedKgStore

pytestmark = pytest.mark.postgres_integration

NOW = "2026-10-19T00:00:00+00:00"
NB = "nb-heavy-stream"
ROWS = 12_000
PAD = 700
TIGHT_MS = 500
PAUSE_SECONDS = 1.5
FLOOR_MS = 600_000


def _seed(connection) -> None:
    stamp = normalize_timestamp(NOW)
    # 造数据不是被测对象:池里 2 s 的 statement_timeout 在 CI 高负载下会让
    # 12000 行宽行插入 + 外键检查 QueryCanceled;只放开这次 write 事务。
    connection.execute("SET LOCAL statement_timeout = 0")
    connection.execute(
        "INSERT INTO notebooks (id,name,created_at,updated_at) VALUES (%s,%s,%s,%s)",
        (NB, NB, stamp, stamp),
    )
    connection.execute(
        "INSERT INTO sources (id,notebook_id,title,source_type,created_at,updated_at) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        ("src-big", NB, "src-big", "file", stamp, stamp),
    )
    # 关系:两端各 ~700 字符的对象 id,没进任何簇 → s / t 就是这两个长 id。
    connection.execute(
        "INSERT INTO knowledge_relations (id, notebook_id, source_id, source_object_id, "
        "target_object_id, edge_type, evidence, created_at) "
        f"SELECT 'rel-' || g, %s, 'src-big', repeat('s', {PAD}) || g, "
        f"repeat('t', {PAD}) || g, 'about', '[]'::jsonb, %s::timestamptz "
        f"FROM generate_series(1, {ROWS}) g",
        (NB, stamp),
    )
    # 对象:长 id、长(孤儿)来源 id → source_canonical_rows 每行 ~1.4 KB。
    connection.execute(
        "INSERT INTO knowledge_objects (id, notebook_id, object_type, status, owner, "
        "payload, evidence, source_candidate_id, source_id, created_at, updated_at) "
        f"SELECT repeat('o', {PAD}) || g, %s, 'concept', 'approved', '', '{{}}'::jsonb, "
        f"'[]'::jsonb, NULL, repeat('z', {PAD}) || (g %% 7), %s::timestamptz, "
        f"%s::timestamptz FROM generate_series(1, {ROWS}) g",
        (NB, stamp, stamp),
    )


def _timeout_ms(db) -> int:
    return int(
        db.execute(
            "SELECT setting::bigint AS c FROM pg_settings WHERE name='statement_timeout'"
        ).fetchone()["c"]
    )


def _open(store, db, which: str):
    if which == "community_graph_rows":
        _names, rows = store.community_graph_rows(db, NB)
        return rows
    return store.source_canonical_rows(db, NB)


@pytest.mark.parametrize("which", ["community_graph_rows", "source_canonical_rows"])
def test_a_slow_consumer_survives_a_tight_session_timeout(postgres_database, which):
    PostgresMigrator(postgres_database).migrate()
    with postgres_database.write() as connection:
        _seed(connection)
    store = PgUnifiedKgStore(postgres_database, now=lambda: NOW)

    with postgres_database.connect() as db:
        db.execute(f"SET statement_timeout = '{TIGHT_MS}ms'")
        assert _timeout_ms(db) == TIGHT_MS            # 前提:session 超时确实压得很小
        count = 0
        for _row in _open(store, db, which):
            count += 1
            if count == 100:
                time.sleep(PAUSE_SECONDS)             # 服务端此刻卡在 send 上,超时在跑
        assert count == ROWS
        assert _timeout_ms(db) == FLOOR_MS            # 事务内:放宽到维护底线
        db.commit()
        # 事务结束:同一条连接回到调用前的 session 值,放宽没有泄漏。
        assert _timeout_ms(db) == TIGHT_MS
