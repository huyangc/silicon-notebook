# backend/tests/test_knowhow_copy.py
"""knowhow-tables PR-2+3 Task 13: full deep-copy with id remap.

PR-1 excluded ALL knowhow content from `copy_notebook` (see
`test_notebook_share_copy.py::test_copy_notebook_excludes_all_knowhow_content`,
now updated alongside this file). This task flips that: the five business
tables (`knowhow_tables/columns/rows/cells/cell_code`) plus `notebook_assets`
now travel with a deep copy (fresh, remapped ids — never the source's own),
and the hidden knowhow source + its elements/chunks/chunk_embeddings copy
WITH RECOMPUTED STABLE ids (`app.services.knowhow.projection.element_id`/
`cell_chunk_id`) instead of being excluded. knowledge_objects/knowledge_
relations stay excluded (design doc: rebuilt by projection, never copied
directly) — `copy_notebook` schedules `project_table` for every copied table
right after publish (`ProjectionScheduler`, PR-2+3 Task 3), so the copy's own
graph shows up moments later WITHOUT re-embedding anything: every chunk
project_table's per-cell diff recomputes lands on the exact (id, text,
section_path) already sitting in the copy (same stable-id formula, same
content, same table title/row-title/column-name — nothing textual changed),
so `old_specs == new_specs` short-circuits every cell straight past the
embedder.

Fixture style mirrors test_knowhow_projection.py (real SQLiteRepository +
a call-counting fake embedder) for the projector-level assertions, and
test_notebook_share_copy.py (`_mk_user`, raw-SQL row peeks) for the copy-level
assertions — this file is the intersection of both domains.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.knowhow.assets import ALLOWED_MIME_EXTENSIONS, AssetService
from app.services.knowhow.projection import KnowhowProjector
from app.services.sqlite_repository import SQLiteRepository, _now
from tests.model_testkit import bind_all_embedding_clients


class _FakeEmbedder:
    """Records every embed_texts call so tests can assert exactly how many
    (and which) batches fired — mirrors test_knowhow_projection.py's own
    fixture verbatim."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed_texts(self, texts):
        texts = list(texts)
        self.calls.append(texts)
        return [[0.1, 0.2, 0.3] for _ in texts]

    @property
    def call_count(self) -> int:
        return len(self.calls)


@pytest.fixture
def repo_factory(tmp_path, monkeypatch):
    """Mirrors test_knowhow_projection.py's repo_factory: four EMBED_* env
    vars make Settings.embedder_configured true."""

    def _make():
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
        monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
        monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
        monkeypatch.setenv("LLM_LOG_ENABLED", "false")
        return SQLiteRepository(Settings())

    return _make


@pytest.fixture
def repo(repo_factory):
    return repo_factory()


@pytest.fixture
def embedder(repo) -> _FakeEmbedder:
    emb = _FakeEmbedder()
    bind_all_embedding_clients(repo, emb)
    assert repo.configured("knowhow_embedding")
    return emb


@pytest.fixture
def projector(repo) -> KnowhowProjector:
    rt = repo._runtime
    return KnowhowProjector(
        settings=repo.settings,
        database=rt.database,
        knowhow=rt.knowhow_store,
        sources=rt.source_store,
        chunks=rt.chunk_store,
        knowledge=rt.knowledge,
        embedding=rt.source_embedding,
        note_model_error=rt.models.note_model_error,
        invalidate_unified_cache=rt.kg_mutations.invalidate_unified_cache,
        mark_unified_dirty_in_tx=rt.kg_mutations.mark_unified_kg_dirty_in_tx,
        new_id=rt.seams.new_id,
        now=rt.seams.now,
    )


COLUMNS = [
    {"name": "违例类型", "role": "anchor"},
    {"name": "现象识别", "role": "procedure"},
    {"name": "依赖工具", "role": "entity"},
]


def _notebook(repo) -> str:
    return repo.create_notebook(
        NotebookCreate(name="KH", purpose="p", primary_domain="d")
    ).id


def _mk_user(repo, uid: str) -> str:
    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO users (id,email,display_name,role,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (uid, f"{uid}@e.test", uid, "user", now, now),
        )
    return uid


def _mk_table_with_row(repo, nb: str, *, title: str = "时序修复"):
    table_id = repo.create_knowhow_table(nb, title, "desc", COLUMNS)
    cols = {c["name"]: c["id"] for c in repo.get_knowhow_table(table_id)["columns"]}
    row_id = repo.add_knowhow_row(
        table_id,
        {
            cols["违例类型"]: "过冲问题",
            cols["现象识别"]: "示波器观察过冲",
            cols["依赖工具"]: "示波器\n万用表",
        },
    )
    return table_id, row_id, cols


def _only_table_id(repo, notebook_id: str) -> str:
    with repo._connect() as db:
        row = db.execute(
            "SELECT id FROM knowhow_tables WHERE notebook_id=?", (notebook_id,)
        ).fetchone()
    assert row is not None
    return row["id"]


def _poll_rows_settled(repo, table_id: str, timeout: float = 6.0):
    """Mirrors test_knowhow_editing_api.py's _poll_all_rows_settled: wait for
    every row's projection_status to leave pending/syncing."""
    deadline = time.time() + timeout
    detail = None
    while time.time() < deadline:
        detail = repo.get_knowhow_table(table_id)
        rows = detail.get("rows", [])
        if rows and all(r["projection_status"] in ("synced", "failed") for r in rows):
            return detail
        time.sleep(0.05)
    return detail


def _kos_for_source(repo, hidden_source_id: str):
    with repo._connect() as db:
        return db.execute(
            "SELECT object_type, payload FROM knowledge_objects WHERE source_id=?",
            (hidden_source_id,),
        ).fetchall()


def _poll_projected_kos(repo, table_id: str, timeout: float = 6.0):
    """Wait for the table-level KO transaction that follows per-row sync.

    ``projection_status='synced'`` is a row-level checkpoint.  The projector
    commits the accumulated knowledge objects after every row reaches that
    checkpoint, so a test that asserts on KOs must synchronize on the KO batch
    itself rather than treating row status as table-level completion.
    """
    deadline = time.time() + timeout
    detail = None
    kos = []
    while time.time() < deadline:
        detail = repo.get_knowhow_table(table_id)
        rows = detail.get("rows", [])
        hidden_source_id = detail.get("hidden_source_id")
        if (
            rows
            and all(row["projection_status"] == "synced" for row in rows)
            and hidden_source_id
        ):
            kos = _kos_for_source(repo, hidden_source_id)
            if kos:
                return detail, kos
        time.sleep(0.02)
    raise AssertionError(
        "post-copy projection did not commit its knowledge-object batch: "
        f"detail={detail!r}, ko_count={len(kos)}"
    )


# ===========================================================================
# Business-table remap: tables/columns/rows/cells/cell_code/assets
# ===========================================================================


def test_copy_remaps_business_tables_and_asset_reference(repo):
    nb = _notebook(repo)
    table_id, row_id, cols = _mk_table_with_row(repo, nb)
    repo.upsert_knowhow_cell_code(
        row_id, cols["现象识别"], "print('hi')", "python", "user-local", "hash-abc"
    )
    asset = AssetService(repo).save(nb, "pic.png", "image/png", b"fakebytes", "user-local")
    md = f"过冲截图 ![img](asset://{asset['id']})"
    repo.update_knowhow_cell(row_id, cols["现象识别"], md)

    old_column_ids = set(cols.values())

    _mk_user(repo, "user-remap1")
    new_nb = repo.copy_notebook(nb, new_owner_id="user-remap1")

    with repo._connect() as db:
        new_table = db.execute(
            "SELECT * FROM knowhow_tables WHERE notebook_id=?", (new_nb.id,)
        ).fetchone()
        assert new_table is not None
        assert new_table["id"] != table_id
        assert new_table["title"] == "时序修复"

        new_cols = db.execute(
            "SELECT id, name FROM knowhow_columns WHERE table_id=?", (new_table["id"],)
        ).fetchall()
        assert {c["name"] for c in new_cols} == {"违例类型", "现象识别", "依赖工具"}
        assert old_column_ids.isdisjoint({c["id"] for c in new_cols})
        new_col_name_by_id = {c["id"]: c["name"] for c in new_cols}

        new_rows = db.execute(
            "SELECT id, projection_status FROM knowhow_rows WHERE table_id=?",
            (new_table["id"],),
        ).fetchall()
        assert len(new_rows) == 1
        assert new_rows[0]["id"] != row_id
        # Not-yet-scheduled copy: every row starts 'pending' regardless of
        # what the SOURCE row's status was (it was 'synced' — never projected
        # here on purpose, so this also proves the copy doesn't inherit it).
        assert new_rows[0]["projection_status"] == "pending"
        new_row_id = new_rows[0]["id"]

        new_cells = db.execute(
            "SELECT column_id, content_md FROM knowhow_cells WHERE row_id=?", (new_row_id,)
        ).fetchall()
        cell_md_by_col_name = {
            new_col_name_by_id[c["column_id"]]: c["content_md"] for c in new_cells
        }
        assert "asset://" in cell_md_by_col_name["现象识别"]
        assert asset["id"] not in cell_md_by_col_name["现象识别"]

        new_code_rows = db.execute(
            "SELECT row_id, code_text, cell_content_hash FROM knowhow_cell_code "
            "WHERE row_id=?",
            (new_row_id,),
        ).fetchall()
        assert len(new_code_rows) == 1
        assert new_code_rows[0]["code_text"] == "print('hi')"
        assert new_code_rows[0]["cell_content_hash"] == "hash-abc"

        new_assets = db.execute(
            "SELECT id, mime FROM notebook_assets WHERE notebook_id=?", (new_nb.id,)
        ).fetchall()
        assert len(new_assets) == 1
        assert new_assets[0]["id"] != asset["id"]
        new_asset_id = new_assets[0]["id"]
        new_asset_mime = new_assets[0]["mime"]

    ext = ALLOWED_MIME_EXTENSIONS[new_asset_mime]
    new_asset_path = Path(repo.storage_dir) / "assets" / new_nb.id / f"{new_asset_id}.{ext}"
    assert new_asset_path.is_file()
    assert new_asset_path.read_bytes() == b"fakebytes"
    assert new_asset_id in cell_md_by_col_name["现象识别"]

    # Original untouched.
    with repo._connect() as db:
        orig_cell = db.execute(
            "SELECT content_md FROM knowhow_cells WHERE row_id=? AND column_id=?",
            (row_id, cols["现象识别"]),
        ).fetchone()
    assert asset["id"] in orig_cell["content_md"]


def test_copy_table_never_projected_has_null_hidden_source(repo):
    """A table with zero rows (never projected -> hidden_source_id NULL)
    must copy cleanly instead of KeyError-ing on a source_map lookup."""
    nb = _notebook(repo)
    repo.create_knowhow_table(nb, "空表", "", COLUMNS)
    _mk_user(repo, "user-neverproj")
    new_nb = repo.copy_notebook(nb, new_owner_id="user-neverproj")
    with repo._connect() as db:
        row = db.execute(
            "SELECT hidden_source_id FROM knowhow_tables WHERE notebook_id=?",
            (new_nb.id,),
        ).fetchone()
    assert row is not None
    assert row["hidden_source_id"] is None


# ===========================================================================
# Chunk/element/vector transplant (zero re-embed contract)
# ===========================================================================


def test_copy_source_elements_metadata_ids_remapped(repo, projector, embedder):
    nb = _notebook(repo)
    table_id, row_id, cols = _mk_table_with_row(repo, nb)
    projector.project_table(table_id)

    _mk_user(repo, "user-elemmeta")
    new_nb = repo.copy_notebook(nb, new_owner_id="user-elemmeta")
    new_table_id = _only_table_id(repo, new_nb.id)

    with repo._connect() as db:
        new_hidden = db.execute(
            "SELECT hidden_source_id FROM knowhow_tables WHERE id=?", (new_table_id,)
        ).fetchone()["hidden_source_id"]
        elements = db.execute(
            "SELECT metadata FROM source_elements WHERE source_id=?", (new_hidden,)
        ).fetchall()
    assert elements  # at least one non-empty cell projected
    import json as _json

    new_row_ids = set()
    with repo._connect() as db:
        new_row_ids = {
            r["id"] for r in db.execute(
                "SELECT id FROM knowhow_rows WHERE table_id=?", (new_table_id,)
            )
        }
        new_col_ids = {
            r["id"] for r in db.execute(
                "SELECT id FROM knowhow_columns WHERE table_id=?", (new_table_id,)
            )
        }
    for element in elements:
        meta = _json.loads(element["metadata"])["knowhow"]
        assert meta["table_id"] == new_table_id
        assert meta["row_id"] in new_row_ids
        assert meta["row_id"] != row_id
        assert meta["column_id"] in new_col_ids
        assert meta["column_id"] not in cols.values()


def test_copy_chunks_and_vectors_present_before_any_projection(repo, projector, embedder):
    nb = _notebook(repo)
    table_id, row_id, cols = _mk_table_with_row(repo, nb)
    projector.project_table(table_id)

    with repo._connect() as db:
        src_hidden = db.execute(
            "SELECT hidden_source_id FROM knowhow_tables WHERE id=?", (table_id,)
        ).fetchone()["hidden_source_id"]
        src_chunks = db.execute(
            "SELECT id, text FROM chunks WHERE source_id=?", (src_hidden,)
        ).fetchall()
        src_vec_count = db.execute(
            "SELECT COUNT(*) FROM chunk_embeddings WHERE chunk_id IN "
            "(SELECT id FROM chunks WHERE source_id=?)",
            (src_hidden,),
        ).fetchone()[0]
    assert len(src_chunks) >= 1
    assert src_vec_count == len(src_chunks)

    _mk_user(repo, "user-preproj")
    new_nb = repo.copy_notebook(nb, new_owner_id="user-preproj")
    # No sleep: the scheduler's 0.5s debounce timer cannot have fired yet, so
    # whatever is in place right now came ONLY from copy_notebook's own raw
    # row copy — proving chunks+vectors transplant before any projection run.
    new_table_id = _only_table_id(repo, new_nb.id)
    with repo._connect() as db:
        new_hidden = db.execute(
            "SELECT hidden_source_id FROM knowhow_tables WHERE id=?", (new_table_id,)
        ).fetchone()["hidden_source_id"]
        new_chunks = db.execute(
            "SELECT id, text FROM chunks WHERE source_id=?", (new_hidden,)
        ).fetchall()
        new_vec_count = db.execute(
            "SELECT COUNT(*) FROM chunk_embeddings WHERE chunk_id IN "
            "(SELECT id FROM chunks WHERE source_id=?)",
            (new_hidden,),
        ).fetchone()[0]

    assert len(new_chunks) == len(src_chunks)
    assert new_vec_count == len(src_chunks)
    assert {c["text"] for c in new_chunks} == {c["text"] for c in src_chunks}
    assert {c["id"] for c in new_chunks}.isdisjoint({c["id"] for c in src_chunks})


def test_copy_chunks_fts_hit_for_knowhow_content(repo, projector, embedder):
    """'副本检索命中': the copy's FTS mirror is searchable immediately, no
    projection wait required (chunks_fts is populated by copy_notebook's own
    backfill, same as every other copied leg)."""
    nb = _notebook(repo)
    table_id, row_id, cols = _mk_table_with_row(repo, nb)
    projector.project_table(table_id)

    _mk_user(repo, "user-ftscopy")
    new_nb = repo.copy_notebook(nb, new_owner_id="user-ftscopy")
    with repo._connect() as db:
        hits = db.execute(
            "SELECT chunk_id FROM chunks_fts WHERE notebook_id=? AND chunks_fts MATCH ?",
            (new_nb.id, "示波器"),
        ).fetchall()
    assert len(hits) >= 1


def test_copy_zero_embed_calls_and_post_projection_kos_have_dynamic_types(
    repo, projector, embedder
):
    nb = _notebook(repo)
    table_id, row_id, cols = _mk_table_with_row(repo, nb)
    projector.project_table(table_id)
    calls_after_source_projection = embedder.call_count
    assert calls_after_source_projection > 0  # sanity: the source projection really embedded

    _mk_user(repo, "user-zeroembed")
    new_nb = repo.copy_notebook(nb, new_owner_id="user-zeroembed")
    # copy_notebook's own row-copy step never touches the embedder at all.
    assert embedder.call_count == calls_after_source_projection

    new_table_id = _only_table_id(repo, new_nb.id)
    detail, kos = _poll_projected_kos(repo, new_table_id)
    assert all(r["projection_status"] == "synced" for r in detail["rows"]), detail

    # The post-copy project_table run rebuilt the KO/edge graph structurally
    # WITHOUT a single additional embedder call — every chunk it diffed was
    # already (id, text, section_path)-identical to what it independently
    # recomputes, so nothing needed re-embedding.
    assert embedder.call_count == calls_after_source_projection

    new_hidden = detail["hidden_source_id"]
    assert new_hidden is not None
    assert {ko["object_type"] for ko in kos} <= {"违例类型", "现象识别", "依赖工具"}
    # And the copy's KOs are under the copy's own hidden source, never the
    # original's (object_type == column name is exactly the cell-level model
    # Task 2 landed — proving this is a REAL rebuild, not a stale carry-over).
    with repo._connect() as db:
        src_hidden = db.execute(
            "SELECT hidden_source_id FROM knowhow_tables WHERE id=?", (table_id,)
        ).fetchone()["hidden_source_id"]
    assert new_hidden != src_hidden


def test_original_notebook_untouched_after_copy_and_projection(repo, projector, embedder):
    nb = _notebook(repo)
    table_id, row_id, cols = _mk_table_with_row(repo, nb)
    projector.project_table(table_id)
    calls_before = embedder.call_count

    with repo._connect() as db:
        src_hidden = db.execute(
            "SELECT hidden_source_id FROM knowhow_tables WHERE id=?", (table_id,)
        ).fetchone()["hidden_source_id"]
        src_chunk_ids_before = {
            r["id"] for r in db.execute("SELECT id FROM chunks WHERE source_id=?", (src_hidden,))
        }
        src_ko_count_before = db.execute(
            "SELECT COUNT(*) FROM knowledge_objects WHERE source_id=?", (src_hidden,)
        ).fetchone()[0]

    _mk_user(repo, "user-untouched")
    new_nb = repo.copy_notebook(nb, new_owner_id="user-untouched")
    new_table_id = _only_table_id(repo, new_nb.id)
    _poll_rows_settled(repo, new_table_id)

    with repo._connect() as db:
        src_chunk_ids_after = {
            r["id"] for r in db.execute("SELECT id FROM chunks WHERE source_id=?", (src_hidden,))
        }
        src_ko_count_after = db.execute(
            "SELECT COUNT(*) FROM knowledge_objects WHERE source_id=?", (src_hidden,)
        ).fetchone()[0]
        orig_row = db.execute(
            "SELECT projection_status FROM knowhow_rows WHERE id=?", (row_id,)
        ).fetchone()

    assert src_chunk_ids_after == src_chunk_ids_before
    assert src_ko_count_after == src_ko_count_before
    assert embedder.call_count == calls_before  # no embed activity attributable to the source
    assert orig_row["projection_status"] == "synced"  # untouched by the copy's own scheduling


# ===========================================================================
# Failure compensation
# ===========================================================================


def test_copy_failure_compensation_removes_assets_dir(repo, monkeypatch):
    import app.services.sqlite_repository as sr

    nb = _notebook(repo)
    table_id, row_id, cols = _mk_table_with_row(repo, nb)
    AssetService(repo).save(nb, "pic.png", "image/png", b"boom-bytes", "user-local")
    _mk_user(repo, "user-crashcopy")

    captured: dict = {}
    orig_new_id = sr._new_id

    def spy_new_id(prefix):
        value = orig_new_id(prefix)
        if prefix == "nb" and "id" not in captured:
            captured["id"] = value
        return value

    monkeypatch.setattr(sr, "_new_id", spy_new_id)

    orig_insert_row = repo._insert_row

    def boom_on_cells(db, table, data):
        if table == "knowhow_cells":
            raise RuntimeError("simulated crash after assets copied")
        return orig_insert_row(db, table, data)

    monkeypatch.setattr(repo, "_insert_row", boom_on_cells)

    with pytest.raises(RuntimeError):
        repo.copy_notebook(nb, new_owner_id="user-crashcopy")

    assert "id" in captured, "copy must reach _new_id('nb') before the injected failure"
    new_id = captured["id"]
    assets_dest_dir = Path(repo.storage_dir) / "assets" / new_id
    assert not assets_dest_dir.exists(), "compensation must remove the copy's assets dir"

    # Original untouched.
    with repo._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM notebook_assets WHERE notebook_id=?", (nb,)
        ).fetchone()[0] == 1


# NOTE: appended at EOF on purpose — the failure-compensation test above has
# its _new_id/_insert_row monkeypatch sites line-pinned in
# test_repository_surface_manifest.py's EXPECTED_PATCH_DELTAS; inserting a
# test before it would shift those pins.
def test_schedule_failure_after_publish_never_compensates(
    repo, projector, embedder, monkeypatch
):
    """Review fix (post-publish corruption): publish_copy flips the sentinel
    off 'copying', after which compensate_copy can no longer reap the
    notebooks row (its DELETE is `WHERE status='copying'` only,
    sharing_store.py) — running fallible code after publish INSIDE the
    compensation try/except would, on a raise, delete the published copy's
    chunks_fts/kg_objects_fts/knowledge_embeddings rows and rmtree its files
    while the row itself SURVIVES: a persistent, silently-corrupted copy that
    sweep_stale_copies never reaps. So a projection-scheduling failure AFTER
    publish (realistic trigger: threading.Timer.start() RuntimeError under
    thread/fd exhaustion) must be logged and swallowed, NOT compensated:
    copy_notebook returns normally and every row/index/file of the published
    copy stays intact."""
    nb = _notebook(repo)
    table_id, row_id, cols = _mk_table_with_row(repo, nb)
    projector.project_table(table_id)
    AssetService(repo).save(nb, "pic.png", "image/png", b"keep-bytes", "user-local")
    _mk_user(repo, "user-schedfail")

    def boom(new_table_id):
        raise RuntimeError("simulated Timer.start failure under thread exhaustion")

    monkeypatch.setattr(repo._runtime.notebook_copies, "_schedule_projection", boom)

    new_nb = repo.copy_notebook(nb, new_owner_id="user-schedfail")  # must NOT raise

    with repo._connect() as db:
        row = db.execute(
            "SELECT status FROM notebooks WHERE id=?", (new_nb.id,)
        ).fetchone()
        assert row is not None, "published copy row must survive a scheduling failure"
        assert row["status"] != "copying"  # published, not a reapable sentinel
        # Business rows intact.
        assert db.execute(
            "SELECT COUNT(*) FROM knowhow_tables WHERE notebook_id=?", (new_nb.id,)
        ).fetchone()[0] == 1
        # The index rows compensate_copy would have deleted are all present.
        chunk_count = db.execute(
            "SELECT COUNT(*) FROM chunks WHERE notebook_id=?", (new_nb.id,)
        ).fetchone()[0]
        fts_count = db.execute(
            "SELECT COUNT(*) FROM chunks_fts WHERE notebook_id=?", (new_nb.id,)
        ).fetchone()[0]
        assert chunk_count > 0
        assert fts_count == chunk_count
    # Files intact (compensation would have rmtree'd the assets tree).
    assets_dir = Path(repo.storage_dir) / "assets" / new_nb.id
    assert assets_dir.is_dir() and any(assets_dir.iterdir())


def test_copied_table_gets_a_genesis_change_and_can_revert_to_the_copied_state(repo):
    """codex 第 2 轮 P2：整本深拷贝来的 knowhow 表必须带一条 table_create 创世
    流水（单表 copy_table 早有，此前整本深拷贝这条路径漏了）。否则拷贝表的第一次
    编辑成 seq 1、无法回退到刚拷贝好的样子。"""
    nb = _notebook(repo)
    table_id, row_id, cols = _mk_table_with_row(repo, nb)

    _mk_user(repo, "user-genesis")
    new_nb = repo.copy_notebook(nb, new_owner_id="user-genesis")
    new_table_id = _only_table_id(repo, new_nb.id)

    hist = repo._runtime.knowhow_history_store

    # 拷贝后恰好一条创世流水，kind=table_create、origin=import、note 提到"复制"。
    changes = hist.list_changes(new_table_id, limit=10)
    assert len(changes) == 1, "拷贝表应恰有一条创世流水"
    genesis = changes[0]
    assert genesis["kind"] == "table_create"
    assert genesis["seq"] == 1
    assert genesis["origin"] == "import"
    assert "复制" in genesis["note"]

    # 拷贝态是可回退的目标：编辑一格后回退到创世点，内容回到拷贝时的样子。
    new_col_id = repo.get_knowhow_table(new_table_id)["columns"][1]["id"]
    new_row_id = repo.get_knowhow_table(new_table_id)["rows"][0]["id"]
    before = repo.get_knowhow_table(new_table_id)["rows"][0]["cells"].get(new_col_id, None)
    repo.update_knowhow_cell(new_row_id, new_col_id, "拷贝后新编辑的内容")

    hist.revert_to(new_table_id, target_seq=1, expected_head_seq=hist.head_seq(new_table_id))

    restored = repo.get_knowhow_table(new_table_id)["rows"][0]["cells"].get(new_col_id, None)
    assert restored == before, "回退到创世点后，格子应回到刚拷贝好的内容"
