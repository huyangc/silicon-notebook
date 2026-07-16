"""SourceStore 论文元数据持久化/水合/搜索测试(paper-metadata Task 3)。"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.repositories.sqlite.source_store import SourceStore
from app.services.sqlite_repository import SQLiteRepository


META = {
    "is_paper": True, "paper_title": "FinFET Scaling Study",
    "venue": "IEDM", "pub_year": 2024, "doi": "10.1109/x.2024",
    "keywords": ["finfet", "scaling"], "model": "m1",
    "raw_json": '{"llm":{},"dropped":{}}',
    "authors": [
        {"position": 0, "name": "Alice Wu", "affiliation": "NTU"},
        {"position": 1, "name": "Bob Li", "affiliation": "TSMC; NTU"},
    ],
}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


@pytest.fixture
def store(repo) -> SourceStore:
    return repo._runtime.source_store


@pytest.fixture
def notebook_id(repo) -> str:
    return repo.create_notebook(NotebookCreate(name="nb")).id


def _insert_source(store: SourceStore, notebook_id: str, source_id: str, **overrides):
    """Mirrors test_source_store_component.py's ``_insert`` helper."""
    kwargs = dict(
        source_id=source_id,
        notebook_id=notebook_id,
        title=f"Doc {source_id}",
        source_type="document",
        status="parsed",
        parse_status="parsed",
        file_name=f"{source_id}.pdf",
        file_path=f"/tmp/{source_id}.pdf",
        file_size=0,
        file_hash=f"h-{source_id}",
        summary="",
        doc_type="",
    )
    kwargs.update(overrides)
    store.insert_source(**kwargs)


# ---------------------------------------------------------------------------
# upsert_paper_meta / get_paper_meta
# ---------------------------------------------------------------------------


def test_upsert_then_get_roundtrip(store, notebook_id):
    """upsert 后 get_paper_meta 返回全字段 + authors 按 position 升序;再次
    upsert(改 paper_title、去掉一个作者)后 get 反映覆盖(作者整组替换,无残留)。"""
    _insert_source(store, notebook_id, "src-a")

    store.upsert_paper_meta("src-a", notebook_id, META)

    got = store.get_paper_meta("src-a")
    assert got is not None
    assert got["source_id"] == "src-a"
    assert got["is_paper"] is True
    assert got["paper_title"] == "FinFET Scaling Study"
    assert got["venue"] == "IEDM"
    assert got["pub_year"] == 2024
    assert got["doi"] == "10.1109/x.2024"
    assert got["keywords"] == ["finfet", "scaling"]
    assert got["model"] == "m1"
    assert [a["position"] for a in got["authors"]] == [0, 1]
    assert [a["name"] for a in got["authors"]] == ["Alice Wu", "Bob Li"]
    assert got["authors"][0]["affiliation"] == "NTU"
    assert got["authors"][1]["affiliation"] == "TSMC; NTU"

    # Re-upsert with a changed title and one fewer author: full overwrite,
    # no residue from the prior author set.
    meta2 = dict(META)
    meta2["paper_title"] = "FinFET Scaling Study v2"
    meta2["authors"] = [{"position": 0, "name": "Alice Wu", "affiliation": "NTU"}]
    store.upsert_paper_meta("src-a", notebook_id, meta2)

    got2 = store.get_paper_meta("src-a")
    assert got2["paper_title"] == "FinFET Scaling Study v2"
    assert len(got2["authors"]) == 1
    assert got2["authors"][0]["name"] == "Alice Wu"


def test_get_missing_returns_none(store, notebook_id):
    """未写过的 source get_paper_meta() is None。"""
    _insert_source(store, notebook_id, "src-a")
    assert store.get_paper_meta("src-a") is None


def test_marker_row_not_paper(store, notebook_id):
    """upsert is_paper=False 空字段 → get 返回 is_paper False、authors==[]
    (行存在,幂等标记语义)。"""
    _insert_source(store, notebook_id, "src-b")
    marker = {
        "is_paper": False, "paper_title": None, "venue": None, "pub_year": None,
        "doi": None, "keywords": [], "model": "m1",
        "raw_json": '{"llm":{"is_paper":false},"dropped":{}}',
        "authors": [],
    }
    store.upsert_paper_meta("src-b", notebook_id, marker)

    got = store.get_paper_meta("src-b")
    assert got is not None
    assert got["is_paper"] is False
    assert got["authors"] == []
    assert got["paper_title"] is None
    assert got["venue"] is None
    assert got["pub_year"] is None
    assert got["doi"] is None
    assert got["keywords"] == []


# ---------------------------------------------------------------------------
# paper_meta_for_sources (batched hydration)
# ---------------------------------------------------------------------------


def test_batched_hydration(store, notebook_id, monkeypatch):
    """3 个源两个有 meta,paper_meta_for_sources 返回两键;monkeypatch
    SourceStore.IN_CHUNK=1 再跑一次结果一致(IN 分批覆盖)。"""
    _insert_source(store, notebook_id, "src-1")
    _insert_source(store, notebook_id, "src-2")
    _insert_source(store, notebook_id, "src-3")

    store.upsert_paper_meta("src-1", notebook_id, META)
    meta2 = dict(META)
    meta2["paper_title"] = "Other Paper"
    store.upsert_paper_meta("src-2", notebook_id, meta2)
    # src-3 deliberately left without a meta row.

    with store.database.connect() as db:
        result = store.paper_meta_for_sources(db, ["src-1", "src-2", "src-3"])
    assert set(result.keys()) == {"src-1", "src-2"}
    assert result["src-1"]["paper_title"] == "FinFET Scaling Study"
    assert result["src-2"]["paper_title"] == "Other Paper"
    assert [a["name"] for a in result["src-1"]["authors"]] == ["Alice Wu", "Bob Li"]

    monkeypatch.setattr(SourceStore, "IN_CHUNK", 1)
    with store.database.connect() as db:
        result_chunked = store.paper_meta_for_sources(db, ["src-1", "src-2", "src-3"])
    assert result_chunked == result


# ---------------------------------------------------------------------------
# sources_missing_paper_meta
# ---------------------------------------------------------------------------


def test_sources_missing_paper_meta(store, notebook_id):
    """建 4 源 —— A(paper,无 meta,parsed)命中;B(有 meta)不命中但
    include_existing=True 时命中;C(doc_type='textbook')不命中;
    D(source_type='memory')不命中。"""
    _insert_source(store, notebook_id, "src-a", doc_type="", parse_status="parsed")
    _insert_source(store, notebook_id, "src-b", doc_type="", parse_status="parsed")
    store.upsert_paper_meta("src-b", notebook_id, META)
    _insert_source(
        store, notebook_id, "src-c", doc_type="textbook", parse_status="parsed"
    )
    _insert_source(
        store, notebook_id, "src-d", source_type="memory", doc_type="",
        parse_status="parsed",
    )

    missing = store.sources_missing_paper_meta(notebook_id)
    assert missing == ["src-a"]

    missing_all = store.sources_missing_paper_meta(notebook_id, include_existing=True)
    assert set(missing_all) == {"src-a", "src-b"}


# ---------------------------------------------------------------------------
# list_sources_page q= search (title/file_name/author/paper_title)
# ---------------------------------------------------------------------------


def test_list_page_q_matches_author_and_paper_title(store, notebook_id):
    """list_sources_page(nb, q="alice wu") 命中 A;q="finfet scaling study" 命中
    A;q="不存在的名字" total_count==0;返回的 SourceSummary.authors ==
    ["Alice Wu","Bob Li"]、pub_year==2024、venue=="IEDM"。"""
    _insert_source(store, notebook_id, "src-a", title="Unrelated Title")
    store.upsert_paper_meta("src-a", notebook_id, META)
    _insert_source(store, notebook_id, "src-b", title="Some Other Doc")

    page = store.list_sources_page(notebook_id, q="alice wu")
    assert page.total_count == 1
    assert page.items[0].id == "src-a"
    assert page.items[0].authors == ["Alice Wu", "Bob Li"]
    assert page.items[0].pub_year == 2024
    assert page.items[0].venue == "IEDM"

    page2 = store.list_sources_page(notebook_id, q="finfet scaling study")
    assert page2.total_count == 1
    assert page2.items[0].id == "src-a"

    page3 = store.list_sources_page(notebook_id, q="不存在的名字")
    assert page3.total_count == 0


# ---------------------------------------------------------------------------
# get_source / SourceDetail.paper_meta
# ---------------------------------------------------------------------------


def test_get_source_detail_carries_paper_meta(store, notebook_id):
    """get_source(A).paper_meta.title=="FinFET Scaling Study" 且
    .authors[0].affiliation=="NTU";无 meta 的源 paper_meta is None。"""
    _insert_source(store, notebook_id, "src-a")
    store.upsert_paper_meta("src-a", notebook_id, META)
    _insert_source(store, notebook_id, "src-b")

    detail_a = store.get_source("src-a")
    assert detail_a.paper_meta is not None
    assert detail_a.paper_meta.is_paper is True
    assert detail_a.paper_meta.title == "FinFET Scaling Study"
    assert detail_a.paper_meta.venue == "IEDM"
    assert detail_a.paper_meta.year == 2024
    assert detail_a.paper_meta.doi == "10.1109/x.2024"
    assert detail_a.paper_meta.authors[0].name == "Alice Wu"
    assert detail_a.paper_meta.authors[0].affiliation == "NTU"

    detail_b = store.get_source("src-b")
    assert detail_b.paper_meta is None


# ---------------------------------------------------------------------------
# cascade delete
# ---------------------------------------------------------------------------


def test_source_delete_cascades(repo, store, notebook_id):
    """删 sources 行后 source_authors/source_paper_meta 空。"""
    _insert_source(store, notebook_id, "src-a")
    store.upsert_paper_meta("src-a", notebook_id, META)

    with repo._write() as db:
        store.delete_source_row(db, "src-a")

    with store.database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) c FROM source_paper_meta WHERE source_id=?", ("src-a",)
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT COUNT(*) c FROM source_authors WHERE source_id=?", ("src-a",)
        ).fetchone()["c"] == 0


# ---------------------------------------------------------------------------
# Fix round 1 (review findings on cf5df12): marker-row hydration at every
# model call site, and sources_missing_paper_meta filter variety.
# ---------------------------------------------------------------------------


def test_marker_row_hydrates_empty_everywhere_and_is_unsearchable(store, notebook_id):
    """A marker row (is_paper=False, upserted once the paper-check LLM ran
    and decided "not a paper") must hydrate consistently at every call site
    that touches paper meta: list_sources_page rows show empty/None paper
    fields (not stale or omitted), get_source still returns a PaperMeta
    object (is_paper False — the row DOES exist, so this must not collapse
    to None the way "no row at all" does), and the marker must never surface
    via the paper-metadata search dimensions (author name / paper_title)
    that a real paper's meta row would match on."""
    _insert_source(store, notebook_id, "src-marker", title="Marker Source Doc")
    marker = {
        "is_paper": False, "paper_title": None, "venue": None, "pub_year": None,
        "doi": None, "keywords": [], "model": "m1",
        "raw_json": '{"llm":{"is_paper":false},"dropped":{}}',
        "authors": [],
    }
    store.upsert_paper_meta("src-marker", notebook_id, marker)
    _insert_source(store, notebook_id, "src-real", title="Real Paper Doc")
    store.upsert_paper_meta("src-real", notebook_id, META)

    page = store.list_sources_page(notebook_id)
    marker_row = next(i for i in page.items if i.id == "src-marker")
    assert marker_row.authors == []
    assert marker_row.pub_year is None
    assert marker_row.venue is None

    detail = store.get_source("src-marker")
    assert detail.paper_meta is not None
    assert detail.paper_meta.is_paper is False

    # Search on the paper-metadata dimensions (author name / paper_title):
    # the marker's source_paper_meta row exists but every field is
    # None/empty, so it must never be the match — only src-real (which
    # carries the real META) should come back.
    by_author = store.list_sources_page(notebook_id, q="alice wu")
    assert by_author.total_count == 1
    assert by_author.items[0].id == "src-real"

    by_title = store.list_sources_page(notebook_id, q="finfet scaling study")
    assert by_title.total_count == 1
    assert by_title.items[0].id == "src-real"


def test_sources_missing_paper_meta_parse_status_and_source_type_variety(
    store, notebook_id
):
    """extracting/extracted 与 parsed 同样是"已有解析产物"应命中补抽;
    source_type='knowhow' 与 'memory' 同样是隐藏合成源应排除(现有
    test_sources_missing_paper_meta 只覆盖了 parsed/memory/doc_type,这里补
    parse_status 的另外两档 + knowhow 这个 source_type)。"""
    _insert_source(
        store, notebook_id, "src-extracting", doc_type="", parse_status="extracting"
    )
    _insert_source(
        store, notebook_id, "src-extracted", doc_type="", parse_status="extracted"
    )
    _insert_source(
        store, notebook_id, "src-knowhow", source_type="knowhow", doc_type="",
        parse_status="parsed",
    )

    missing = store.sources_missing_paper_meta(notebook_id)
    assert set(missing) == {"src-extracting", "src-extracted"}


# ---------------------------------------------------------------------------
# SourceSummary.paper_meta_status derived field (paper-metadata Task 3)
# ---------------------------------------------------------------------------


def test_source_summary_paper_meta_status_four_states(repo, notebook_id):
    """四态：has_meta / not_paper / missing / None。"""
    store = repo._runtime.source_store

    # a: 合规源 + has_meta 行
    store.insert_source(
        source_id="src-a", notebook_id=notebook_id, title="A",
        source_type="pdf", status="parsed", parse_status="parsed",
        file_name="a.pdf", file_path="/tmp/a.pdf", file_size=0,
        file_hash="h-a", summary="", doc_type="",
    )
    store.upsert_paper_meta("src-a", notebook_id, {
        "is_paper": True, "paper_title": "T", "venue": None,
        "pub_year": None, "doi": None, "keywords": [], "authors": [],
        "raw_json": "{}", "model": "test",
    })

    # b: 合规源 + not_paper 标记行
    store.insert_source(
        source_id="src-b", notebook_id=notebook_id, title="B",
        source_type="pdf", status="parsed", parse_status="parsed",
        file_name="b.pdf", file_path="/tmp/b.pdf", file_size=0,
        file_hash="h-b", summary="", doc_type="",
    )
    store.upsert_paper_meta("src-b", notebook_id, {
        "is_paper": False, "paper_title": None, "venue": None,
        "pub_year": None, "doi": None, "keywords": [], "authors": [],
        "raw_json": "{}", "model": "test",
    })

    # c: 合规源 + 无 meta 行（missing）
    store.insert_source(
        source_id="src-c", notebook_id=notebook_id, title="C",
        source_type="pdf", status="parsed", parse_status="parsed",
        file_name="c.pdf", file_path="/tmp/c.pdf", file_size=0,
        file_hash="h-c", summary="", doc_type="",
    )

    # d: 非合规源（memory）→ None
    store.insert_source(
        source_id="src-d", notebook_id=notebook_id, title="D",
        source_type="memory", status="parsed", parse_status="parsed",
        file_name="", file_path="", file_size=0,
        file_hash="h-d", summary="", doc_type="",
    )

    # 详情单取
    assert repo.get_source("src-a").paper_meta_status == "has_meta"
    assert repo.get_source("src-b").paper_meta_status == "not_paper"
    assert repo.get_source("src-c").paper_meta_status == "missing"
    assert repo.get_source("src-d").paper_meta_status is None

    # 列表批量：口径一致
    page = repo.list_sources_page(notebook_id)
    by_id = {s.id: s for s in page.items}
    assert by_id["src-a"].paper_meta_status == "has_meta"
    assert by_id["src-b"].paper_meta_status == "not_paper"
    assert by_id["src-c"].paper_meta_status == "missing"
    # 注意 memory 源可能不在 list_sources_page（既有过滤），若 assertion 失败可去掉此项
