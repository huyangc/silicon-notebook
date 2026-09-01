"""PG 侧体检 H4/H5 memo 的后端相关钉子。

CheckupService 的聚合/缓存逻辑后端中性,已由 tests/test_checkup_service.py 在 sqlite 上
全量覆盖;这里只钉三件**必须真 PG 才能证**的事:

- PostgresRepository 构造期真把事件插槽(``runtime.on_source_vectors_written``)指向
  facade 的 __dict__ 晚解析转发器,且懒构造出的 checkup 收得到它转发的失效;
- ``kg_mutation_seq`` seam(``unified_kg.graph_seq_row``)在真 psycopg 连接上可读,
  且 seq 前进立即让 memo 失效(不推时钟、不等 300s 背底 TTL);
- 事件通知(``SourceEmbeddingService.note_source_vectors_written``)经插槽真的打到
  checkup 的失效上。
"""
from __future__ import annotations

import pytest

from app.models.notebooks import NotebookCreate
from app.repositories.postgres._store_utils import jsonb, normalize_timestamp


pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_checkup_h45"),
]


@pytest.fixture
def postgres_repository(postgres_settings):
    from app.repositories.postgres.repository import PostgresRepository

    repository = PostgresRepository(postgres_settings)
    try:
        yield repository
    finally:
        repository.close()


def _seed_source(repository, notebook_id: str) -> None:
    runtime = repository._runtime
    now = normalize_timestamp(runtime.seams.now())
    with runtime.database.write() as db:
        db.execute(
            "INSERT INTO sources "
            "(id,notebook_id,title,source_type,status,parse_status,file_name,"
            "file_path,file_size,file_hash,summary,created_at,updated_at,doc_type) "
            "VALUES ('src-h45-cache',%s,'source','markdown','extracted','parsed',"
            "'a.md','',0,'hash-h45','',%s,%s,'textbook')",
            (notebook_id, now, now),
        )


def _insert_chunk(repository, notebook_id: str, chunk_id: str) -> None:
    runtime = repository._runtime
    now = normalize_timestamp(runtime.seams.now())
    with runtime.database.write() as db:
        db.execute(
            "INSERT INTO chunks "
            "(id,notebook_id,source_id,text,section_path,element_ids,created_at) "
            "VALUES (%s,%s,'src-h45-cache',%s,'',%s,%s)",
            (chunk_id, notebook_id, chunk_id, jsonb([]), now),
        )


def _h4(repository, notebook_id: str) -> int:
    result = repository.checkup.run(notebook_id)
    return next(c.count for c in result.checks if c.code == "H4")


def test_checkup_h45_memo_seq_key_and_event_invalidation(postgres_repository):
    repo = postgres_repository
    nb = repo.create_notebook(NotebookCreate(name="checkup-h45")).id
    _seed_source(repo, nb)
    _insert_chunk(repo, nb, "ck-1")
    assert repo.checkup is not None  # 触发懒构造(转发器按 __dict__ 晚解析到本实例)
    # 接线:插槽指向 facade 的转发方法(不绑具体 checkup 实例——懒构造竞态防护)。
    assert (
        repo._runtime.on_source_vectors_written
        == repo._invalidate_checkup_missing_vector_counts
    )
    assert _h4(repo, nb) == 1                      # 首算并缓存(键含当前 seq)
    # ① seq 驱动失效:结构变更(build_chunks / delete_source 同款 bump)不经
    #    embedding 写路径,靠键里的 kg_mutation_seq 分量捕获。
    _insert_chunk(repo, nb, "ck-2")
    assert _h4(repo, nb) == 1                      # seq 未动 → 命中旧计数(证明在缓存)
    repo.maintenance.mark_unified_kg_dirty(nb)     # kg_mutation_seq +1
    assert _h4(repo, nb) == 2                      # seq 前进 → 立即重算,不等 TTL
    # ② 事件驱动失效:向量写完的通知(embed 成功不 bump seq)。
    _insert_chunk(repo, nb, "ck-3")
    assert _h4(repo, nb) == 2                      # 键未动 → 仍命中
    repo._runtime.source_embedding.note_source_vectors_written(nb)
    assert _h4(repo, nb) == 3                      # 事件失效 → 立即重算


def test_checkup_h45_wiring_returns_the_epoch_seq_pair_not_a_bare_int(postgres_repository):
    """R1 (P1-1, post-review): the real ``kg_mutation_seq`` seam wired into
    ``PostgresRepository.checkup`` (``postgres/repository.py``) once
    independently called a bare ``int(graph_seq_row(db, nb)[0])`` — a SECOND,
    epoch-unaware copy of the seam ``sqlite_repository.py`` had already moved
    to ``(kg_reset_epoch, kg_mutation_seq)``. The injected-seam tests above
    never caught it: they only ever bump kg_mutation_seq (``mark_unified_kg_
    dirty``), which a bare-seq wiring detects just as well as an epoch-aware
    one — the two forms only diverge once epoch itself moves. This asserts
    the REAL wiring's shape directly, not a fake seam, so a future
    regression back to a bare int fails here immediately regardless of
    which mutation actually exercises it.

    变异锚点:把 postgres/repository.py 的 kg_mutation_seq 接线改回裸
    ``int(graph_seq_row(db,nb)[0])``,本条必须报红(TypeError:'int' 不可解包
    成 2 元组,或返回值不是 tuple)。
    """
    repo = postgres_repository
    nb = repo.create_notebook(NotebookCreate(name="checkup-h45-wiring")).id
    seam = repo.checkup._kg_version
    with repo._runtime.database.connect() as db:
        version = seam(db, nb)
    assert isinstance(version, tuple) and len(version) == 2, (
        f"the wired kg_mutation_seq seam must return (epoch, seq), got {version!r}"
    )
    epoch, seq = version
    assert isinstance(epoch, int) and isinstance(seq, int)
    assert (epoch, seq) == (0, 0)  # fresh notebook, never reset, never mutated

    repo.maintenance.mark_unified_kg_dirty(nb)
    with repo._runtime.database.connect() as db:
        after_seq_bump = seam(db, nb)
    assert after_seq_bump == (0, 1)

    repo.delete_notebook_kg(nb)
    with repo._runtime.database.connect() as db:
        after_delete = seam(db, nb)
    assert after_delete == (1, 0), (
        "delete_notebook_kg must advance the epoch half the wired seam "
        "reports, not just the seq half"
    )
