"""Exact-identifier fast path: store primitives, service bounds, ask wiring.

The feature exists because a manual's `set_db` section is chunked into main
description / Arguments / Examples and ordinary retrieval keeps only the
best-scoring one. These tests pin the three properties that make it worth the
code: `_` really is treated literally, the section really is fetched whole, and
a question without an identifier really does nothing at all.
"""
from __future__ import annotations

import json
from contextlib import contextmanager

import pytest

from app.core.config import Settings
from app.services.exact_lookup import (
    ExactLookupDeps,
    ExactLookupLimits,
    exact_lookup_chunks,
)
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository, _now
from app.models.schemas import AskRequest, NotebookCreate
from tests.model_testkit import bind_all_embedding_clients


# ------------------------------------------------------------------ fixtures
@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    monkeypatch.setenv("MODEL_SERVICES_CONFIG", "")
    for key in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
                "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL",
                "REASONING_LLM_MODEL"):
        monkeypatch.setenv(key, "")
    repository = SQLiteRepository(Settings())
    bind_all_embedding_clients(repository, FakeEmbedder(dim=16))
    return repository


def _seed_manual(repo, chunks, *, source_id="src-manual"):
    """Write chunk rows (+ their FTS rows) directly.

    Bypassing the chunker on purpose: these tests are about retrieval over a
    given chunk layout, and writing the layout out makes the fixture readable
    as the manual it stands for.
    """
    notebook = repo.create_notebook(NotebookCreate(name="manual"))
    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,file_name,"
            "file_path,file_size,file_hash,summary,doc_type,parse_status,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (source_id, notebook.id, "Tool Manual", "document", "m.md",
             "/tmp/m.md", 0, f"h-{source_id}", "", "", "extracted", now, now))
        for index, (chunk_id, section_path, text) in enumerate(chunks, 1):
            db.execute(
                "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,"
                "element_ids,created_at) VALUES (?,?,?,?,?,?,?)",
                (chunk_id, notebook.id, source_id, text, section_path,
                 json.dumps([f"el-{source_id}-{index:04d}"]), now))
            db.execute(
                "INSERT INTO chunks_fts(chunk_id,notebook_id,text) VALUES (?,?,?)",
                (chunk_id, notebook.id, text))
    return notebook


# Breadcrumb form (current chunker): section_path carries the full path, and
# the scored text carries only the bounded tail-two-segment prefix.
BREADCRUMB_MANUAL = [
    ("ck-main", "Manual > Commands > set_db",
     "[Commands > set_db] set_db 用于设置数据库属性。"),
    ("ck-args", "Manual > Commands > set_db > Arguments",
     "[set_db > Arguments] -name 属性名。-value 属性值。"),
    ("ck-examples", "Manual > Commands > set_db > Examples",
     "[set_db > Examples] 见下方脚本片段。"),
    ("ck-other", "Manual > Commands > report_timing",
     "[Commands > report_timing] report_timing 输出时序报告。"),
    ("ck-decoy", "Manual > Notes",
     "[Manual > Notes] setXdb 是一个拼写相近的历史别名。"),
]

# Legacy form (rows written before the breadcrumb change): section_path is a
# bare heading, and sibling chunks carry no command token of their own.
LEGACY_MANUAL = [
    ("ck-legacy-main", "set_db", "[set_db] set_db 用于设置数据库属性。"),
    ("ck-legacy-args", "Arguments", "[Arguments] -name 属性名。-value 属性值。"),
]


# ------------------------------------------------------------- store: SQLite
def test_sqlite_exact_search_treats_underscore_literally(repo):
    notebook = _seed_manual(repo, BREADCRUMB_MANUAL)
    with repo._connect() as db:
        hits = repo._runtime.knowledge.chunk_exact_search(db, notebook.id, "set_db", 50)
    ids = {hit["chunk_id"] for hit in hits}
    assert {"ck-main", "ck-args", "ck-examples"} <= ids
    # `setXdb` must NOT match: `_` is a literal here, not a wildcard.
    assert "ck-decoy" not in ids
    assert "ck-other" not in ids
    by_id = {hit["chunk_id"]: hit for hit in hits}
    assert by_id["ck-args"]["source_id"] == "src-manual"
    assert by_id["ck-args"]["section_path"] == "Manual > Commands > set_db > Arguments"


def test_sqlite_exact_search_does_not_decompose_the_needle(repo):
    """`chunk_fts_search` would OR `set`/`db`; the exact probe must not."""
    notebook = _seed_manual(repo, [
        ("ck-split", "Manual > Other", "[Manual > Other] set the db value"),
    ])
    with repo._connect() as db:
        union = repo._runtime.knowledge.chunk_fts_search(db, notebook.id, "set_db", 50)
        exact = repo._runtime.knowledge.chunk_exact_search(db, notebook.id, "set_db", 50)
    assert {hit["chunk_id"] for hit in union} == {"ck-split"}
    assert exact == []


def test_sqlite_chunks_by_section_takes_the_subtree_in_document_order(repo):
    notebook = _seed_manual(repo, BREADCRUMB_MANUAL)
    with repo._connect() as db:
        rows = repo._runtime.chunk_store.chunks_by_section(
            db, notebook.id, "src-manual", "Manual > Commands > set_db", 12)
    assert [row["id"] for row in rows] == ["ck-main", "ck-args", "ck-examples"]
    assert rows[0]["source_title"] == "Tool Manual"


def test_sqlite_chunks_by_section_bounds_and_scopes(repo):
    notebook = _seed_manual(repo, BREADCRUMB_MANUAL)
    other = _seed_manual(
        repo,
        [(f"{chunk_id}-other", path, text)
         for chunk_id, path, text in BREADCRUMB_MANUAL],
        source_id="src-other",
    )
    with repo._connect() as db:
        capped = repo._runtime.chunk_store.chunks_by_section(
            db, notebook.id, "src-manual", "Manual > Commands > set_db", 2)
        cross_notebook = repo._runtime.chunk_store.chunks_by_section(
            db, other.id, "src-manual", "Manual > Commands > set_db", 12)
        empty_path = repo._runtime.chunk_store.chunks_by_section(
            db, notebook.id, "src-manual", "", 12)
    assert [row["id"] for row in capped] == ["ck-main", "ck-args"]
    assert cross_notebook == []      # notebook scoping, not just source scoping
    assert empty_path == []


_EXACT_ROW_FIELDS = ("id", "source_id", "text", "section_path",
                     "element_ids", "source_title")


def test_sqlite_hydrate_rows_matches_the_section_row_shape(repo):
    """通道两条取数分支给出的行必须一致。

    有面包屑的库按小节整节取齐;没有面包屑的库(MinerU 解析的 PDF/DOCX)按命中
    id 直接取行。两者的结果落进同一个 `_build_chunks`,少一列就是生产上的静默
    缺字段。PostgreSQL 侧的对等断言在 tests/postgres/test_search_conformance.py。
    """
    notebook = _seed_manual(repo, BREADCRUMB_MANUAL)
    with repo._connect() as db:
        section = repo._runtime.chunk_store.chunks_by_section(
            db, notebook.id, "src-manual", "Manual > Commands > set_db", 12)
        hydrated = repo._runtime.chunk_store.hydrate_rows(
            db, ["ck-args", "ck-main"])
    by_section = {row["id"]: dict(row) for row in section}
    by_id = {row["id"]: dict(row) for row in hydrated}
    assert set(by_id) == {"ck-args", "ck-main"}
    for chunk_id, row in by_id.items():
        assert set(row) >= set(_EXACT_ROW_FIELDS), row
        assert ({field: row[field] for field in _EXACT_ROW_FIELDS}
                == {field: by_section[chunk_id][field]
                    for field in _EXACT_ROW_FIELDS})


def test_sqlite_chunks_by_section_escapes_like_wildcards(repo):
    """A section literally named `a_b` must not also drag in `axb`."""
    notebook = _seed_manual(repo, [
        ("ck-under", "a_b", "[a_b] real section"),
        ("ck-under-child", "a_b > Leaf", "[a_b > Leaf] real child"),
        ("ck-wild", "axb > Leaf", "[axb > Leaf] unrelated section"),
    ])
    with repo._connect() as db:
        rows = repo._runtime.chunk_store.chunks_by_section(
            db, notebook.id, "src-manual", "a_b", 12)
    assert [row["id"] for row in rows] == ["ck-under", "ck-under-child"]


# ------------------------------------------------------------------ service
class _StubDeps:
    """Records every store call so "zero I/O" can be asserted, not assumed."""

    def __init__(self, hits=(), sections=None, rows=()):
        self.hits = list(hits)
        self.sections = sections or {}
        # The by-id store: every row any section can hand back, plus any row
        # given only here (a chunk the section query would MISS — that gap is
        # the whole point of the by-id path).
        self.rows = {}
        for section_rows in self.sections.values():
            for row in section_rows:
                self.rows[str(row["id"])] = row
        for row in rows:
            self.rows[str(row["id"])] = row
        self.searches: list[tuple[str, int]] = []
        self.section_calls: list[tuple[str, str, int]] = []
        self.chunk_calls: list[list[str]] = []
        self.connects = 0

    @contextmanager
    def connect(self):
        self.connects += 1
        yield object()

    def exact_search(self, _db, _notebook_id, needle, k):
        self.searches.append((needle, k))
        return [hit for hit in self.hits if needle in hit["needles"]]

    def section_rows(self, _db, _notebook_id, source_id, section_path, limit):
        self.section_calls.append((source_id, section_path, limit))
        return self.sections.get((source_id, section_path), [])[:limit]

    def chunk_rows(self, _db, chunk_ids):
        self.chunk_calls.append(list(chunk_ids))
        # Deliberately returned in the store's own arbitrary order (reversed),
        # so a test asserting output order is testing the module's ordering
        # rather than the stub's.
        return [self.rows[cid] for cid in reversed(list(chunk_ids))
                if cid in self.rows]

    def as_deps(self):
        return ExactLookupDeps(
            connect=self.connect,
            exact_search=self.exact_search,
            section_rows=self.section_rows,
            chunk_rows=self.chunk_rows,
        )


def _hit(chunk_id, source_id, section_path, *needles):
    return {"chunk_id": chunk_id, "source_id": source_id,
            "section_path": section_path, "score": 1.0, "needles": set(needles)}


def _row(chunk_id, section_path, text, source_id="s1"):
    return {"id": chunk_id, "source_id": source_id, "text": text,
            "section_path": section_path, "element_ids": json.dumps([f"el-{chunk_id}"]),
            "source_title": "Manual"}


def test_service_without_identifier_makes_zero_store_calls():
    stub = _StubDeps(hits=[_hit("c1", "s1", "S", "set_db")])
    out = exact_lookup_chunks(
        stub.as_deps(), "nb", "这个工具怎么用", ExactLookupLimits())
    assert out == []
    assert stub.connects == 0 and stub.searches == [] and stub.section_calls == []


def test_service_gate_matches_the_shared_identifier_rule():
    """The gate is `identifier_terms`, so its measured cheap-term filter (>=4
    chars, must contain an ASCII letter) applies here for free — `第 2.1 节`
    must not open a database connection."""
    stub = _StubDeps()
    assert exact_lookup_chunks(
        stub.as_deps(), "nb", "第 2.1 节讲了什么", ExactLookupLimits()) == []
    assert stub.connects == 0


def test_service_truncates_identifiers_and_sections():
    hits = [
        _hit("h1", "s1", "Cmd > aaa_bbb", "aaa_bbb"),
        _hit("h2", "s1", "Cmd > ccc_ddd", "ccc_ddd"),
        _hit("h3", "s1", "Cmd > eee_fff", "eee_fff"),
        _hit("h4", "s1", "Cmd > ggg_hhh", "ggg_hhh"),
    ]
    sections = {
        ("s1", f"Cmd > {name}"): [_row(f"r-{name}", f"Cmd > {name}", name)]
        for name in ("aaa_bbb", "ccc_ddd", "eee_fff", "ggg_hhh")
    }
    stub = _StubDeps(hits=hits, sections=sections)
    limits = ExactLookupLimits(max_identifiers=2, max_sections=1, fts_k=7)
    out = exact_lookup_chunks(
        stub.as_deps(), "nb",
        "aaa_bbb ccc_ddd eee_fff ggg_hhh 有什么区别", limits)

    assert [needle for needle, _k in stub.searches] == ["aaa_bbb", "ccc_ddd"]
    assert {k for _needle, k in stub.searches} == {7}
    assert len(stub.section_calls) == 1               # max_sections honoured
    assert [chunk.chunk_id for chunk in out] == ["r-aaa_bbb"]


def test_hits_fold_onto_the_command_so_subsections_never_take_extra_slots():
    """一条命令的各子节折叠成一个组:祖先的子树取齐本来就覆盖它们,不该各占 slot。"""
    hits = [
        _hit("deep1", "s1", "Cmd > set_db > Examples", "set_db"),
        _hit("shallow1", "s1", "Cmd > set_db", "set_db"),
        _hit("rare", "s1", "Cmd > set_db > Notes", "set_db"),
        _hit("rare2", "s1", "Cmd > set_db > Notes", "set_db"),
    ]
    sections = {
        ("s1", path): [_row(f"row-{path}", path, path)]
        for path in ("Cmd > set_db", "Cmd > set_db > Examples", "Cmd > set_db > Notes")
    }
    stub = _StubDeps(hits=hits, sections=sections)
    out = exact_lookup_chunks(
        stub.as_deps(), "nb", "set_db 怎么用",
        ExactLookupLimits(max_sections=2))
    # 四条命中分属三个面包屑,但只属于一条命令 → 一次取数,发在命令主节上,
    # 第二个 slot 原封不动留给别的命令(这里没有,于是根本不发第二次)。
    assert [call[1] for call in stub.section_calls] == ["Cmd > set_db"]
    assert [chunk.chunk_id for chunk in out] == ["row-Cmd > set_db"]


def test_equal_hit_groups_are_ordered_by_the_shortest_breadcrumb():
    """组间同命中数时仍按最短面包屑定序——确定性,且偏向更浅/更泛的那个节点。"""
    hits = [
        _hit("h1", "s1", "Cmd > set_db", "set_db"),
        _hit("h2", "s1", "Reference > Commands > get_db", "get_db"),
    ]
    sections = {
        ("s1", path): [_row(f"row-{path}", path, path)]
        for path in ("Cmd > set_db", "Reference > Commands > get_db")
    }
    stub = _StubDeps(hits=hits, sections=sections)
    exact_lookup_chunks(
        stub.as_deps(), "nb", "set_db 和 get_db",
        ExactLookupLimits(max_sections=2))
    assert [call[1] for call in stub.section_calls] == [
        "Cmd > set_db", "Reference > Commands > get_db"]


def test_a_second_command_keeps_its_slot_when_subsections_out_hit_the_main():
    """整支评审阻塞项 1 的复现形状(命中数**不相等**)。

    `set_db > Arguments` 4 命中 / `> Examples` 3 / 主节 2 / `report_timing` 2,
    三个 slot。按面包屑各自排名时前三名全是同一条命令的三个节点,report_timing
    整条丢失——而「子节块数多于主节」正是参考手册的常态,不是边角情形。折叠之后
    每条命令花一个 slot,两条都在。
    """
    hits = (
        [_hit(f"a{i}", "s1", "Manual > Commands > set_db > Arguments", "set_db")
         for i in range(4)]
        + [_hit(f"e{i}", "s1", "Manual > Commands > set_db > Examples", "set_db")
           for i in range(3)]
        + [_hit(f"m{i}", "s1", "Manual > Commands > set_db", "set_db")
           for i in range(2)]
        + [_hit(f"t{i}", "s1", "Manual > Commands > report_timing", "report_timing")
           for i in range(2)]
    )
    sections = {
        ("s1", "Manual > Commands > set_db"): [
            _row("row-set-db", "Manual > Commands > set_db", "set_db 语法"),
            _row("row-set-db-args", "Manual > Commands > set_db > Arguments",
                 "-name 属性名"),
        ],
        ("s1", "Manual > Commands > report_timing"): [
            _row("row-report", "Manual > Commands > report_timing", "report_timing 语法"),
        ],
    }
    stub = _StubDeps(hits=hits, sections=sections)
    out = exact_lookup_chunks(
        stub.as_deps(), "nb", "set_db 和 report_timing 分别怎么用",
        ExactLookupLimits(max_sections=3))

    assert [call[1] for call in stub.section_calls] == [
        "Manual > Commands > set_db", "Manual > Commands > report_timing"]
    assert {chunk.chunk_id for chunk in out} == {
        "row-set-db", "row-set-db-args", "row-report"}


def test_nested_subsections_are_absorbed_so_a_second_identifier_keeps_its_slot():
    """质量评审阻塞项 1 的复现:x_yz 的 Examples/Arguments 子节不得各占一个
    slot——它们已被祖先的子树取齐覆盖;否则第二个标识符整条命令被挤掉。"""
    hits = [
        _hit("m", "s1", "A > x_yz", "x_yz"),
        _hit("e", "s1", "A > x_yz > Examples", "x_yz"),
        _hit("a", "s1", "A > x_yz > Arguments", "x_yz"),
        _hit("o", "s1", "B > very_long_command_name_here",
             "very_long_command_name_here"),
    ]
    sections = {
        ("s1", "A > x_yz"): [_row("row-x", "A > x_yz", "x_yz syntax")],
        ("s1", "B > very_long_command_name_here"): [
            _row("row-y", "B > very_long_command_name_here", "other syntax")],
    }
    stub = _StubDeps(hits=hits, sections=sections)
    out = exact_lookup_chunks(
        stub.as_deps(), "nb", "x_yz 和 very_long_command_name_here 有什么区别",
        ExactLookupLimits(max_sections=3))
    assert [call[1] for call in stub.section_calls] == [
        "A > x_yz", "B > very_long_command_name_here"]
    assert {chunk.chunk_id for chunk in out} == {"row-x", "row-y"}


def test_document_root_section_contributes_only_its_matching_chunks():
    """质量评审阻塞项 2 的复现:文档根(前言/目录段提到命令名)不得触发子树
    取齐——那等于把整篇文档拉进来;它只贡献真正命中的 chunk 本身,真命令节
    照常整节取齐。"""
    hits = [
        _hit("ck-toc", "s1", "Manual", "set_db"),
        _hit("ck-main", "s1", "Manual > Commands > set_db", "set_db"),
    ]
    sections = {
        # 这一组行只用来喂 stub 的按 id 取行表(以及证明它们不会被整组带出来);
        # 文档根这一组现在走按命中 id 取行,不再发按 path 的查询。
        ("s1", "Manual"): [
            _row("ck-toc", "Manual", "本手册包含 set_db、report_timing 等命令"),
            _row("ck-intro", "Manual", "前言与版权说明"),
            _row("ck-lic", "Manual", "许可协议"),
        ],
        ("s1", "Manual > Commands > set_db"): [
            _row("ck-main", "Manual > Commands > set_db", "[Commands > set_db] 语法"),
            _row("ck-args", "Manual > Commands > set_db > Arguments",
                 "[set_db > Arguments] -name 属性名"),
        ],
    }
    stub = _StubDeps(hits=hits, sections=sections)
    out = exact_lookup_chunks(
        stub.as_deps(), "nb", "set_db 命令是怎样的", ExactLookupLimits())
    ids = {chunk.chunk_id for chunk in out}
    assert "ck-toc" in ids                       # 命中的目录块本身保留
    assert "ck-intro" not in ids and "ck-lic" not in ids   # 子树不展开
    assert {"ck-main", "ck-args"} <= ids         # 真命令节整节取齐
    assert stub.chunk_calls == [["ck-toc"]]      # 文档根只按命中 id 取
    assert [call[1] for call in stub.section_calls] == [
        "Manual > Commands > set_db"]


def test_unnamed_group_hydrates_the_hit_ids_instead_of_requerying_by_path():
    """整支评审阻塞项 2 的复现:MinerU 解析的 PDF 没有面包屑。

    全库几十个裸「Arguments」小节共享同一个 (source, section_path) 键,按 path
    重查 + LIMIT 只能拿回其中的前 12 个,真正命中的那一块排在窗口之后被通道自己
    静默丢掉——探测明明找到了参数表,通道把它扔了。按命中 id 直接取行不会丢。
    """
    crowd = [_row(f"ck-other-{index:02d}", "Arguments", f"别的命令的参数表 {index}")
             for index in range(20)]
    target = _row("ck-target", "Arguments", "set_db 的 -name / -value 参数")
    stub = _StubDeps(
        hits=[_hit("ck-target", "s1", "Arguments", "set_db")],
        # store 的按 path 查询会先返回那 20 个不相关的同名小节块,
        # 目标块排在 LIMIT(12) 之外。
        sections={("s1", "Arguments"): crowd + [target]},
    )
    out = exact_lookup_chunks(
        stub.as_deps(), "nb", "set_db 命令是怎样的", ExactLookupLimits())

    assert stub.section_calls == []            # 不按 path 重查
    assert stub.chunk_calls == [["ck-target"]]  # 只取命中的那一块
    assert [chunk.chunk_id for chunk in out] == ["ck-target"]


def test_unnamed_group_output_follows_probe_order_not_store_order():
    """按 id 取行的顺序由本模块钉死:SQLite 按 id、PostgreSQL 按 ordinal 返回,
    都不是契约。通道对外必须两侧一致,且强命中排在字符预算的切点之前。"""
    rows = [_row(f"ck-{index}", "Arguments", f"正文 {index}") for index in range(3)]
    stub = _StubDeps(
        hits=[_hit("ck-0", "s1", "Arguments", "set_db"),
              _hit("ck-1", "s1", "Arguments", "set_db"),
              _hit("ck-2", "s1", "Arguments", "set_db")],
        sections={},
        rows=rows,                       # stub 的 chunk_rows 刻意逆序返回
    )
    out = exact_lookup_chunks(
        stub.as_deps(), "nb", "set_db 命令是怎样的", ExactLookupLimits())
    assert [chunk.chunk_id for chunk in out] == ["ck-0", "ck-1", "ck-2"]


def test_unnamed_group_hit_ids_obey_the_per_section_bound():
    stub = _StubDeps(
        hits=[_hit(f"ck-{index}", "s1", "Arguments", "set_db") for index in range(9)],
        sections={},
        rows=[_row(f"ck-{index}", "Arguments", f"正文 {index}") for index in range(9)],
    )
    out = exact_lookup_chunks(
        stub.as_deps(), "nb", "set_db 命令是怎样的",
        ExactLookupLimits(max_chunks_per_section=4))
    assert stub.chunk_calls == [["ck-0", "ck-1", "ck-2", "ck-3"]]
    assert len(out) == 4


def test_channel_scores_the_probed_name_not_the_callers_query():
    """S2:通道分是「对名称的覆盖率」,不是「对问题的相关度」。

    此前 mix 传整句问题、reasoning 传名称串,同一块证据两个模式口径相反:整句
    打分把它压到 0.2857(低于 tau_high=0.35),唯一锚点时 grounded 被判成 overview。
    """
    from app.services.retrieval import keyword_score

    section_path = "Manual > Commands > set_db"
    text = "[Commands > set_db] 用于设置数据库属性"
    stub = _StubDeps(
        hits=[_hit("h", "s1", section_path, "set_db")],
        sections={("s1", section_path): [_row("c-main", section_path, text)]},
    )
    long_question = "set_db 命令是怎样的,它有哪些参数、默认值和常见用法"
    out = exact_lookup_chunks(
        stub.as_deps(), "nb", long_question, ExactLookupLimits())

    assert [chunk.relevance for chunk in out] == [1.0]
    # 这条断言让上面的 1.0 有意义:同一块证据按整句问题打分确实低到会掉预算、
    # 掉接地阈值——所以两种口径不是同一个数,必须选定一个。
    assert keyword_score(long_question, f"{section_path} {text}") < 0.35


def test_breadcrumb_sibling_scores_nonzero_via_section_path():
    """打分 haystack 含 section_path:面包屑库的 Arguments 兄弟块正文无命令
    token,但路径里带——它是被精确匹配到的可寻址内容,计入覆盖率是诚实分,
    也避免『唯一锚点 0 分把 grounded 打成 overview』。"""
    stub = _StubDeps(
        hits=[_hit("h", "s1", "Manual > Commands > set_db", "set_db")],
        sections={("s1", "Manual > Commands > set_db"): [
            _row("c-args", "Manual > Commands > set_db > Arguments",
                 "-name 属性名, -value 属性值"),
        ]},
    )
    out = exact_lookup_chunks(
        stub.as_deps(), "nb", "set_db 命令是怎样的", ExactLookupLimits())
    assert out and out[0].chunk_id == "c-args"
    assert out[0].relevance > 0.0


def test_zero_section_budget_is_a_zero_io_gate():
    stub = _StubDeps(hits=[_hit("h", "s1", "set_db", "set_db")], sections={})
    for limits in (ExactLookupLimits(max_sections=0),
                   ExactLookupLimits(max_chunks_per_section=0)):
        assert exact_lookup_chunks(
            stub.as_deps(), "nb", "set_db 是什么", limits) == []
    assert stub.connects == 0 and stub.searches == []


def test_service_skips_hits_without_a_section_and_dedupes_chunks():
    hits = [
        _hit("floating", "s1", "", "set_db"),      # chunk outside any heading
        _hit("anchored", "s1", "Cmd > set_db", "set_db"),
    ]
    shared = _row("dup", "Cmd > set_db", "body")
    stub = _StubDeps(hits=hits, sections={("s1", "Cmd > set_db"): [shared, shared]})
    out = exact_lookup_chunks(stub.as_deps(), "nb", "set_db", ExactLookupLimits())
    assert [call[1] for call in stub.section_calls] == ["Cmd > set_db"]
    assert [chunk.chunk_id for chunk in out] == ["dup"]


def test_service_scores_keyword_only_without_a_relevance_floor():
    """A legacy sibling carries no command token and would score 0.0 — the
    whole point of the channel is that it survives anyway. Scores stay honest
    ([0,1], no fabricated semantic component) so tau/grounded is unaffected."""
    stub = _StubDeps(
        hits=[_hit("h", "s1", "set_db", "set_db")],
        sections={("s1", "set_db"): [
            _row("c-main", "set_db", "[set_db] 设置数据库属性"),
            _row("c-args", "Arguments", "[Arguments] -name 属性名"),
        ]},
    )
    out = exact_lookup_chunks(
        stub.as_deps(), "nb", "set_db 命令是怎样的", ExactLookupLimits())
    by_id = {chunk.chunk_id: chunk for chunk in out}
    assert set(by_id) == {"c-main", "c-args"}
    assert by_id["c-args"].relevance == 0.0
    assert 0.0 < by_id["c-main"].relevance <= 1.0
    assert by_id["c-main"].score == by_id["c-main"].relevance
    assert by_id["c-args"].element_ids == ["el-c-args"]
    assert [support.origin for support in by_id["c-args"].retrieval_supports] == [
        "lexical"]


def test_service_opens_exactly_one_connection():
    stub = _StubDeps(
        hits=[_hit("h", "s1", "Cmd > set_db", "set_db"),
              _hit("h2", "s1", "Cmd > get_db", "get_db")],
        sections={("s1", "Cmd > set_db"): [_row("a", "Cmd > set_db", "x")],
                  ("s1", "Cmd > get_db"): [_row("b", "Cmd > get_db", "y")]},
    )
    exact_lookup_chunks(
        stub.as_deps(), "nb", "set_db 和 get_db", ExactLookupLimits())
    assert stub.connects == 1


def test_service_returns_early_when_nothing_matched():
    stub = _StubDeps(hits=[])
    assert exact_lookup_chunks(
        stub.as_deps(), "nb", "set_db 命令", ExactLookupLimits()) == []
    assert stub.connects == 1 and stub.section_calls == []


# -------------------------------------------------- service over real SQLite
def _lookup(repo, notebook_id, query):
    return repo.retrieval.exact_lookup_chunks(notebook_id, query)


def test_breadcrumb_library_returns_the_whole_command_section(repo):
    notebook = _seed_manual(repo, BREADCRUMB_MANUAL)
    out = _lookup(repo, notebook.id, "set_db 命令是怎样的")
    assert [chunk.chunk_id for chunk in out] == ["ck-main", "ck-args", "ck-examples"]
    assert all(chunk.source_title == "Tool Manual" for chunk in out)


def test_legacy_single_heading_library_still_matches_exactly(repo):
    """Old rows have no breadcrumb: the subtree branch matches nothing, so the
    channel degrades to the exactly-matched section — correct, not crashing."""
    notebook = _seed_manual(repo, LEGACY_MANUAL)
    out = _lookup(repo, notebook.id, "set_db 命令是怎样的")
    assert [chunk.chunk_id for chunk in out] == ["ck-legacy-main"]


def test_disabled_setting_short_circuits_the_channel(repo, monkeypatch):
    notebook = _seed_manual(repo, BREADCRUMB_MANUAL)
    monkeypatch.setattr(repo.settings, "exact_lookup_enabled", False)
    assert _lookup(repo, notebook.id, "set_db 命令是怎样的") == []


def test_channel_failure_is_fail_open(repo, monkeypatch):
    notebook = _seed_manual(repo, BREADCRUMB_MANUAL)
    candidates = repo.retrieval.candidates

    def _boom(*_args, **_kwargs):
        raise RuntimeError("index unavailable")

    monkeypatch.setattr(candidates.knowledge, "chunk_exact_search", _boom)
    assert _lookup(repo, notebook.id, "set_db 命令是怎样的") == []


# ------------------------------------------------------ ask path end-to-end
class _IdentityRerank:
    """Configured rerank that preserves candidate order.

    Deliberately identity rather than "smart": the exact section chunks are
    appended last by the merge, so an identity reranker is the adversarial
    case the reserve exists for — it is exactly where truncation would drop
    them.
    """

    configured = True

    def rerank(self, query, documents, on_error=None):
        return list(range(len(documents)))


def _filler(index):
    from app.services.retrieval import RetrievalSupport, RetrievedChunk

    text = f"[Manual > Notes] 无关背景段落 {index} " * 12
    return RetrievedChunk(
        chunk_id=f"ck-filler-{index}", source_id="src-manual",
        source_title="Tool Manual", section_path="Manual > Notes", text=text,
        element_ids=[f"el-filler-{index}"], score=0.9, relevance=0.9,
        retrieval_supports=(
            RetrievalSupport("semantic", "chunk", f"ck-filler-{index}", 0.9),),
    )


def _enable_mix(repo, notebook_id, monkeypatch, candidates):
    """overlay_on = chunk_kg_overlay_enabled ∧ rerank.configured ∧ has_kg."""
    from tests.model_testkit import bind_rerank_client

    bind_rerank_client(repo, _IdentityRerank())
    repo.store_kg(
        notebook_id, None,
        [{"local_id": "a", "object_type": "concept",
          "payload": {"name": "set_db"},
          "evidence": [{"source_id": "src-manual", "source_title": "Tool Manual",
                        "element_id": "el-src-manual-0001",
                        "element_type": "paragraph", "location_label": "1",
                        "quoted_span": "set_db", "confidence": 1.0}]}],
        [])
    assert repo.retrieval.has_kg(notebook_id) is True
    monkeypatch.setattr(
        repo.retrieval.candidates, "_mix_retrieve",
        lambda nb_id, q, hl, subs: (list(candidates), "", {}, [], 0))


def _spy_selection(monkeypatch):
    import app.services.retrieval as retrieval_module

    captured: dict = {}
    real = retrieval_module.select_with_reserves

    def _spy(ranked, max_tokens, rules):
        out = real(ranked, max_tokens, rules)
        captured["ranked"] = [chunk.chunk_id for chunk in ranked]
        captured["selected"] = [chunk.chunk_id for chunk in out]
        captured["budget"] = max_tokens
        return out

    monkeypatch.setattr(retrieval_module, "select_with_reserves", _spy)
    return captured


SET_DB_CHUNKS = {"ck-main", "ck-args", "ck-examples"}
# ask_chunk spends `_MIX_PROMPT_BUFFER_TOKENS` (2000) of max_total_tokens on the
# prompt before any chunk is considered, so the setting has to clear that floor
# for the chunk budget to be a real, non-degenerate number.
_BUDGET_TOKENS = 2400


def test_ask_mix_keeps_the_whole_command_section_past_truncation(repo, monkeypatch):
    """The end the feature exists for: main description + Arguments + Examples
    all survive the token budget even though the reranker ranked them last."""
    notebook = _seed_manual(repo, BREADCRUMB_MANUAL)
    _enable_mix(repo, notebook.id, monkeypatch, [_filler(i) for i in range(8)])
    monkeypatch.setattr(repo.settings, "max_total_tokens", _BUDGET_TOKENS)
    captured = _spy_selection(monkeypatch)

    repo.ask_chunk(notebook.id, AskRequest(question="set_db 命令是怎样的"))

    ranked = captured["ranked"]
    assert SET_DB_CHUNKS <= set(ranked), "exact hits must reach the candidate pool"
    selected = captured["selected"]
    assert len(selected) < len(ranked), "budget must actually truncate"
    assert SET_DB_CHUNKS <= set(selected), (
        f"whole set_db section must survive truncation, got {selected}")


def test_ask_mix_reserve_is_bounded_and_never_grows_the_budget(repo, monkeypatch):
    from app.services.retrieval import est_tokens

    notebook = _seed_manual(repo, BREADCRUMB_MANUAL)
    _enable_mix(repo, notebook.id, monkeypatch, [_filler(i) for i in range(8)])
    monkeypatch.setattr(repo.settings, "max_total_tokens", _BUDGET_TOKENS)
    captured = _spy_selection(monkeypatch)

    repo.ask_chunk(notebook.id, AskRequest(question="set_db 命令是怎样的"))

    by_id = {}
    for chunk in [_filler(i) for i in range(8)]:
        by_id[chunk.chunk_id] = chunk
    with repo._connect() as db:
        for row in repo._runtime.chunk_store.rows_by_ids(db, captured["selected"]):
            by_id.setdefault(row["id"], None)
            if by_id[row["id"]] is None:
                by_id[row["id"]] = type("_T", (), {"text": row["text"]})()
    used = sum(est_tokens(by_id[cid].text) for cid in captured["selected"])
    assert used <= captured["budget"], (
        f"reserve must live inside the budget: {used} > {captured['budget']}")


def test_ask_without_an_identifier_is_bit_for_bit_neutral(repo, monkeypatch):
    """The neutrality claim, measured rather than asserted: an identifier-free
    question must produce the same candidate pool AND the same selection as a
    run with the whole feature switched off, and must not touch the store."""
    notebook = _seed_manual(repo, BREADCRUMB_MANUAL)
    fillers = [_filler(i) for i in range(8)]

    def _run(question, *, enabled):
        with monkeypatch.context() as patch:
            _enable_mix(repo, notebook.id, patch, fillers)
            patch.setattr(repo.settings, "max_total_tokens", _BUDGET_TOKENS)
            patch.setattr(repo.settings, "exact_lookup_enabled", enabled)
            probes = {"n": 0}
            real = repo.retrieval.candidates.knowledge.chunk_exact_search

            def _counting(*args, **kwargs):
                probes["n"] += 1
                return real(*args, **kwargs)

            patch.setattr(
                repo.retrieval.candidates.knowledge, "chunk_exact_search", _counting)
            captured = _spy_selection(patch)
            repo.ask_chunk(notebook.id, AskRequest(question=question))
            return captured, probes["n"]

    neutral_on, probes_on = _run("这个工具的背景是什么", enabled=True)
    neutral_off, _ = _run("这个工具的背景是什么", enabled=False)
    assert probes_on == 0, "an identifier-free question must issue zero exact probes"
    assert neutral_on["ranked"] == neutral_off["ranked"]
    assert neutral_on["selected"] == neutral_off["selected"]
    assert not SET_DB_CHUNKS & set(neutral_on["ranked"])

    # Sanity: the same harness with an identifier DOES change the outcome, so
    # the equality above is a real neutrality result and not a dead harness.
    identified, probes_identified = _run("set_db 命令是怎样的", enabled=True)
    assert probes_identified == 1
    assert SET_DB_CHUNKS <= set(identified["selected"])


def test_nested_identifier_section_keeps_its_own_slot():
    """Codex #399 P2:命中按**产生它的 term** 折叠,不按全量 terms。

    问题同时点名 `set_db` 与 `config.yaml`,而 `config.yaml` 的小节嵌在
    `Ref > set_db` 之下。按全量 terms 折叠时,config.yaml 的命中会塌进
    `set_db` 组(它的面包屑含 set_db),而 set_db 子树的有界取数在触达
    config.yaml 小节前就耗尽——探测到了、通道自己丢了。按产生命中的 term
    折叠后,config.yaml 保住自己的组与 slot。
    """
    hits = [
        _hit("m0", "s1", "Ref > set_db", "set_db"),
        _hit("m1", "s1", "Ref > set_db", "set_db"),
        _hit("cfg", "s1", "Ref > set_db > config.yaml", "config.yaml"),
    ]
    sections = {
        # set_db 子树取数(有界)只吐得出它自己的 3 块,够不到 config 小节。
        ("s1", "Ref > set_db"): [
            _row(f"row-main-{i}", "Ref > set_db", f"set_db body {i}")
            for i in range(3)
        ],
        ("s1", "Ref > set_db > config.yaml"): [
            _row("row-cfg", "Ref > set_db > config.yaml", "config.yaml keys"),
        ],
    }
    stub = _StubDeps(hits=hits, sections=sections)
    out = exact_lookup_chunks(
        stub.as_deps(), "nb", "set_db 的 config.yaml 怎么配",
        ExactLookupLimits(max_sections=2, max_chunks_per_section=3))
    assert [call[1] for call in stub.section_calls] == [
        "Ref > set_db", "Ref > set_db > config.yaml"]
    assert "row-cfg" in [chunk.chunk_id for chunk in out]


def test_probe_anchors_via_text_only_so_prose_free_grandchild_sections_are_not_found():
    """反向护栏(codex #399 R2 的已知锚定边界,刻意延期):探测只搜 chunk 文本,
    打分文本前缀只有尾部两段面包屑。命令节与子节两层都零正文、只有孙层 chunk
    (前缀 `[Arguments > Required]`)且正文不提命令名时,探测锚不上——这是当前
    合同的一部分:修复需要 section_path 进 FTS/trigram 索引(schema 迁移),
    无索引的 section_path LIKE 全列扫描违反有界红线,不许作为捷径。若这条用例
    因「探测开始命中 section_path」而变红,说明索引化修复落了地,应把它改写成
    正向断言而不是删除。"""
    hits: list = []   # 探测(仅文本)一无所获——孙层文本不含 set_db
    stub = _StubDeps(hits=hits, sections={})
    out = exact_lookup_chunks(
        stub.as_deps(), "nb", "set_db 命令是怎样的", ExactLookupLimits())
    assert out == []
    assert stub.section_calls == []
