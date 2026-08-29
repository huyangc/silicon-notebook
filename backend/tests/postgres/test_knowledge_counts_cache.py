from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.repositories.postgres import knowledge_counts_cache as kcc
from app.repositories.postgres.migrator import PostgresMigrator


pytestmark = pytest.mark.postgres_integration


def _insert_source(
    connection,
    source_id: str,
    *,
    source_type: str = "markdown",
    parsed: bool = True,
) -> None:
    now = datetime(2026, 8, 4, tzinfo=timezone.utc)
    connection.execute(
        "INSERT INTO sources"
        "(id,notebook_id,title,source_type,status,parse_status,created_at,updated_at) "
        "VALUES (%s,'nb-pending','source',%s,'ready','parsed',%s,%s)",
        (source_id, source_type, now, now),
    )
    if parsed:
        connection.execute(
            "INSERT INTO source_elements"
            "(id,source_id,element_type,location_label,text,created_at) "
            "VALUES (%s,%s,'paragraph','p1','parsed',%s)",
            (f"element-{source_id}", source_id, now),
        )


def _insert_object(connection, source_id: str) -> None:
    now = datetime(2026, 8, 4, tzinfo=timezone.utc)
    connection.execute(
        "INSERT INTO knowledge_objects"
        "(id,notebook_id,object_type,status,source_id,created_at,updated_at) "
        "VALUES (%s,'nb-pending','concept','approved',%s,%s,%s)",
        (f"object-{source_id}", source_id, now, now),
    )


def _insert_run(
    connection,
    source_id: str,
    *,
    status: str,
    error_message: str = "",
    created_at: datetime,
    run_type: str = "kg",
) -> None:
    connection.execute(
        "INSERT INTO extraction_runs"
        "(id,notebook_id,source_id,run_type,status,error_message,created_at,updated_at) "
        "VALUES (%s,'nb-pending',%s,%s,%s,%s,%s,%s)",
        (
            f"run-{source_id}-{run_type}-{status}-{error_message}",
            source_id,
            run_type,
            status,
            error_message,
            created_at,
            created_at,
        ),
    )


def test_pending_query_preserves_latest_run_and_visibility_semantics(postgres_database):
    assert PostgresMigrator(postgres_database).migrate() == 42
    now = datetime(2026, 8, 4, tzinfo=timezone.utc)

    with postgres_database.write() as connection:
        connection.execute(
            "INSERT INTO notebooks(id,name,created_at,updated_at) "
            "VALUES ('nb-pending','pending',%s,%s)",
            (now, now),
        )

        _insert_source(connection, "unparsed", parsed=False)
        _insert_source(connection, "missing-object")

        _insert_source(connection, "direct-graph")
        _insert_object(connection, "direct-graph")

        _insert_source(connection, "completed")
        _insert_object(connection, "completed")
        _insert_run(connection, "completed", status="completed", created_at=now)

        _insert_source(connection, "failed-latest")
        _insert_object(connection, "failed-latest")
        _insert_run(
            connection,
            "failed-latest",
            status="completed",
            created_at=now - timedelta(minutes=1),
        )
        _insert_run(
            connection,
            "failed-latest",
            status="failed",
            created_at=now,
        )

        _insert_source(connection, "partial-windows")
        _insert_object(connection, "partial-windows")
        _insert_run(
            connection,
            "partial-windows",
            status="completed",
            error_message="windows_failed=10/12",
            created_at=now,
        )

        _insert_source(connection, "retry-incomplete")
        _insert_object(connection, "retry-incomplete")
        _insert_run(
            connection,
            "retry-incomplete",
            status="completed",
            error_message="retry_incomplete=1",
            created_at=now,
        )

        # Same timestamp: PostgreSQL's identity ordinal mirrors SQLite rowid and
        # the later insert must win.
        _insert_source(connection, "ordinal-tie")
        _insert_object(connection, "ordinal-tie")
        _insert_run(connection, "ordinal-tie", status="failed", created_at=now)
        _insert_run(connection, "ordinal-tie", status="completed", created_at=now)

        # A newer non-KG run must not replace the latest KG status.
        _insert_source(connection, "non-kg-newer")
        _insert_object(connection, "non-kg-newer")
        _insert_run(
            connection,
            "non-kg-newer",
            status="completed",
            created_at=now - timedelta(minutes=1),
        )
        _insert_run(
            connection,
            "non-kg-newer",
            run_type="paper_meta",
            status="failed",
            created_at=now,
        )

        _insert_source(connection, "hidden-memory", source_type="memory")
        _insert_source(connection, "hidden-knowhow", source_type="knowhow")

    with postgres_database.connect() as connection:
        assert kcc._pending_query(connection, "nb-pending", visible_only=False) == 6
        assert kcc._pending_query(connection, "nb-pending", visible_only=True) == 4


def test_type_counts_and_chunk_count_are_seq_gated_against_real_postgres(
    postgres_database,
):
    """大库打开卡死修复的移植主证:type_counts/chunk_count 在真 PostgreSQL 上算对,
    并且真的按 kg_mutation_seq memo——直插数据不 bump seq 时读到旧值,``invalidate``
    (安全阀)或 seq bump(正常路径)都能让下一次读取反映新值。"""
    assert PostgresMigrator(postgres_database).migrate() == 42
    now = datetime(2026, 8, 5, tzinfo=timezone.utc)
    notebook_id = "nb-counts"

    with postgres_database.write() as connection:
        connection.execute(
            "INSERT INTO notebooks(id,name,created_at,updated_at) "
            "VALUES (%s,'counts',%s,%s)",
            (notebook_id, now, now),
        )
        connection.execute(
            "INSERT INTO unified_kg_state(notebook_id,kg_mutation_seq,updated_at) "
            "VALUES (%s,1,%s)",
            (notebook_id, now),
        )
        connection.execute(
            "INSERT INTO sources"
            "(id,notebook_id,title,source_type,status,parse_status,created_at,updated_at) "
            "VALUES ('src-counts',%s,'source','markdown','ready','parsed',%s,%s)",
            (notebook_id, now, now),
        )
        connection.execute(
            "INSERT INTO knowledge_objects"
            "(id,notebook_id,object_type,status,source_id,created_at,updated_at) "
            "VALUES ('ko-approved',%s,'concept','approved','src-counts',%s,%s)",
            (notebook_id, now, now),
        )
        connection.execute(
            "INSERT INTO knowledge_objects"
            "(id,notebook_id,object_type,status,source_id,created_at,updated_at) "
            "VALUES ('ko-deprecated',%s,'concept','deprecated','src-counts',%s,%s)",
            (notebook_id, now, now),
        )
        connection.execute(
            "INSERT INTO chunks(id,notebook_id,source_id,text,created_at) "
            "VALUES ('chunk-1',%s,'src-counts','first chunk',%s)",
            (notebook_id, now),
        )

    with postgres_database.connect() as connection:
        # type_counts 默认排除 deprecated;object_type_total 的 falsy status 含它。
        assert kcc.type_counts(connection, notebook_id) == {"concept": 1}
        assert kcc.active_object_count(connection, notebook_id) == 1
        assert kcc.object_type_total(connection, notebook_id, "concept") == 2
        assert (
            kcc.object_type_total(connection, notebook_id, "concept", "approved") == 1
        )
        assert kcc.chunk_count(connection, notebook_id) == 1

    # 直插一条新对象和一条新 chunk,不 bump kg_mutation_seq → memo 命中旧值。
    with postgres_database.write() as connection:
        connection.execute(
            "INSERT INTO knowledge_objects"
            "(id,notebook_id,object_type,status,source_id,created_at,updated_at) "
            "VALUES ('ko-claim',%s,'claim','approved','src-counts',%s,%s)",
            (notebook_id, now, now),
        )
        connection.execute(
            "INSERT INTO chunks(id,notebook_id,source_id,text,created_at) "
            "VALUES ('chunk-2',%s,'src-counts','second chunk',%s)",
            (notebook_id, now),
        )

    with postgres_database.connect() as connection:
        assert kcc.type_counts(connection, notebook_id) == {"concept": 1}  # 仍是旧值
        assert kcc.chunk_count(connection, notebook_id) == 1  # 仍是旧值

    # invalidate 是安全阀:不依赖 seq bump 也能让下一次读取反映新值。
    kcc.invalidate(notebook_id)
    with postgres_database.connect() as connection:
        assert kcc.type_counts(connection, notebook_id) == {"concept": 1, "claim": 1}
        assert kcc.chunk_count(connection, notebook_id) == 2

    # 正常路径:bump kg_mutation_seq 后自动重算,不需要显式 invalidate。
    with postgres_database.write() as connection:
        connection.execute(
            "INSERT INTO knowledge_objects"
            "(id,notebook_id,object_type,status,source_id,created_at,updated_at) "
            "VALUES ('ko-claim-2',%s,'claim','approved','src-counts',%s,%s)",
            (notebook_id, now, now),
        )
        connection.execute(
            "UPDATE unified_kg_state SET kg_mutation_seq=2 WHERE notebook_id=%s",
            (notebook_id,),
        )

    with postgres_database.connect() as connection:
        assert kcc.type_counts(connection, notebook_id) == {"concept": 1, "claim": 2}
