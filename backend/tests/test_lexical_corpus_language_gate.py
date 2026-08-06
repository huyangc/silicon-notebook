"""The corpus-language gate on lexical candidate terms.

A long Chinese question decomposes into ~60 CJK tri-gram terms. On PostgreSQL
each one is a real LATERAL probe: measured on a 7,026-chunk English-only
corpus, 64 terms took 29.7s cold / 3.38s warm and returned 26 rows, every one
of which came from the handful of Latin terms. Four report sections running
that in parallel exceed POSTGRES_STATEMENT_TIMEOUT_SECONDS and the lexical arm
dies fail-open, so the real cost of a guaranteed-empty probe is not latency —
it is the silent loss of the hits that would have matched.

These tests pin the three asymmetries that make the filter safe: CJK direction
only, tail only (the user's quoted spans and the whole-sentence term are never
touched), and only terms whose emptiness is provable.
"""
from __future__ import annotations

import sqlite3

import pytest

from app.repositories.lexical_query import (
    corpus_gated_recall_terms,
    corpus_language_drops_cjk,
    lexical_recall_terms,
    sqlite_fts_match_expression,
)


LONG_CHINESE_QUERY = (
    "请围绕这份资料回答：在这个知识库里，关于时序收敛与布局布线优化的整体方法论是什么？"
    "必须覆盖以下主题：时钟树综合的关键步骤、静态时序分析的约束建模、"
    "拥塞驱动的布局策略、以及物理验证阶段的常见失败模式。"
    "请给出结构化的对比与结论，并说明每个结论的依据来源。"
    "重点关注 set_db 与 place_opt_design 在 timing closure 流程中的作用。"
)

ENGLISH_QUERY = "how does set_db affect timing closure during place_opt_design"

CHINESE_QUERY = "深度学习系统架构设计指南"


def _is_all_cjk(term: str) -> bool:
    from app.repositories.lexical_query import _is_cjk

    return bool(term) and all(_is_cjk(char) for char in term)


# ── the gate predicate ────────────────────────────────────────────────


def test_unprobed_corpus_never_gates():
    """`None`/empty is "unknown", and a term is only ever dropped on evidence."""
    assert corpus_language_drops_cjk(None) is False
    assert corpus_language_drops_cjk([]) is False
    assert corpus_language_drops_cjk(["en"]) is True
    assert corpus_language_drops_cjk(["zh"]) is False
    assert corpus_language_drops_cjk(["zh", "en"]) is False


# ── byte-identical for every corpus that is not proven CJK-free ───────


@pytest.mark.parametrize("query", [LONG_CHINESE_QUERY, ENGLISH_QUERY, CHINESE_QUERY])
@pytest.mark.parametrize("langs", [None, [], ["zh"], ["zh", "en"]])
def test_terms_unchanged_unless_corpus_is_proven_cjk_free(query, langs):
    assert corpus_gated_recall_terms(query, langs) == lexical_recall_terms(query)


@pytest.mark.parametrize(
    "query",
    [
        ENGLISH_QUERY,
        "current mirror design guidelines",
        'the "state-of-the-art" cascode report_timing flow',
    ],
)
def test_latin_only_query_is_byte_identical_on_an_english_corpus(query):
    """The overwhelmingly common case must not pay, or change, anything."""
    assert corpus_gated_recall_terms(query, ["en"]) == lexical_recall_terms(query)


def test_chinese_corpus_keeps_every_latin_term():
    """The mirror rule is deliberately NOT implemented.

    Latin identifiers, product names and formulas are ordinary content inside
    Chinese documents, and `_notebook_langs` answers ["en"] for an unknown or
    empty corpus — so a symmetric "drop Latin terms when the corpus has no en"
    would be a filter that fires on a guess. Reversing the direction here must
    turn this test red.
    """
    query = "在 set_db 与 place_opt_design 之间 timing closure 如何收敛"
    assert corpus_gated_recall_terms(query, ["zh"]) == lexical_recall_terms(query)
    assert "set_db" in corpus_gated_recall_terms(query, ["zh"])
    assert "timing" in corpus_gated_recall_terms(query, ["zh"])


# ── the filter itself ─────────────────────────────────────────────────


def test_english_corpus_drops_the_cjk_fragment_probes():
    full = lexical_recall_terms(LONG_CHINESE_QUERY)
    gated = corpus_gated_recall_terms(LONG_CHINESE_QUERY, ["en"])
    assert len(full) == 64                      # the term budget is saturated
    assert len(gated) <= 8, gated               # measured: 64 probes -> 3
    # Order is preserved and nothing is invented.
    assert gated == [term for term in full if term in set(gated)]


@pytest.mark.parametrize(
    "query",
    [LONG_CHINESE_QUERY, CHINESE_QUERY, "set_db 的时序收敛用法", ENGLISH_QUERY],
)
def test_only_provably_empty_terms_are_ever_dropped(query):
    """Every dropped term must be all-CJK — never merely CJK-containing.

    A mixed term such as `abc时序` still shares the `abc` trigram with a Latin
    corpus, so it carries no proof of zero hits and must survive.
    """
    full = lexical_recall_terms(query)
    gated = set(corpus_gated_recall_terms(query, ["en"]))
    dropped = [term for term in full if term not in gated]
    assert dropped == [term for term in dropped if _is_all_cjk(term)]


def test_the_whole_sentence_head_term_survives_the_gate():
    """The priority head is off limits: the gate is a tail filter only."""
    full = lexical_recall_terms(LONG_CHINESE_QUERY)
    gated = corpus_gated_recall_terms(LONG_CHINESE_QUERY, ["en"])
    assert gated[0] == full[0]
    assert _is_all_cjk(full[0]) is False        # it carries the Latin identifiers
    sentence_only = "时序收敛的方法论是什么"
    # A head term that IS all-CJK still survives — head membership, not shape,
    # is what protects it.
    assert corpus_gated_recall_terms(sentence_only, ["en"])[0] == sentence_only


def test_user_quoted_cjk_phrase_survives_on_an_english_corpus():
    """The quote contract outranks the cost argument.

    `"..."` is the one construct whose entire purpose is to demand an exact
    match; the gate must never be the thing that quietly deletes it.
    """
    query = 'set_db "时序收敛" behaviour'
    gated = corpus_gated_recall_terms(query, ["en"])
    assert "时序收敛" in gated
    assert gated.index("时序收敛") == 0
    # ... while the same string arriving as ordinary prose is dropped.
    unquoted = corpus_gated_recall_terms("set_db 时序收敛 behaviour", ["en"])
    assert "时序收敛" not in unquoted[1:]


def test_quoted_phrase_survives_even_when_the_whole_query_is_cjk():
    query = '"时序收敛" 的关键步骤'
    assert corpus_gated_recall_terms(query, ["en"])[0] == "时序收敛"


# ── both backends must probe the SAME terms ───────────────────────────


def test_sqlite_match_expression_applies_the_same_gate():
    expression = sqlite_fts_match_expression(LONG_CHINESE_QUERY, ["en"])
    gated = corpus_gated_recall_terms(LONG_CHINESE_QUERY, ["en"])
    assert expression == " OR ".join(
        '"' + term.replace('"', '""') + '"' for term in gated
    )
    assert sqlite_fts_match_expression(LONG_CHINESE_QUERY) == " OR ".join(
        '"' + term.replace('"', '""') + '"'
        for term in lexical_recall_terms(LONG_CHINESE_QUERY)
    )


def test_sqlite_adapter_forwards_the_corpus_langs_it_was_given(monkeypatch):
    """Plumbing guard: a kwarg the adapter accepts but ignores is worse than
    no kwarg at all — the caller would believe a filter is in effect."""
    from app.repositories.sqlite import knowledge_store

    seen: list[object] = []

    def _spy(query, corpus_langs=None):
        seen.append(corpus_langs)
        return ""

    monkeypatch.setattr(knowledge_store, "sqlite_fts_match_expression", _spy)
    knowledge_store.KnowledgeStore.chunk_fts_search(
        object(), "nb", LONG_CHINESE_QUERY, 12, corpus_langs=["en"])
    knowledge_store.KnowledgeStore.fts_search(
        object(), "nb", LONG_CHINESE_QUERY, 12, corpus_langs=["zh", "en"])
    knowledge_store.KnowledgeStore.chunk_fts_search(
        object(), "nb", LONG_CHINESE_QUERY, 12)
    assert seen == [["en"], ["zh", "en"], None]


def test_postgres_union_probes_exactly_the_gated_terms():
    """PostgreSQL parity: one term set, one definition, no adapter drift."""
    from app.repositories.postgres import knowledge_store

    captured: list[list[str]] = []

    def _fake_rows_for_terms(db, notebook_id, terms, per_term_limit,
                             allowed_source_ids=None, *, allow_knn=False):
        captured.append(list(terms))
        return []

    knowledge_store._lexical_candidate_union(
        object(),
        "nb",
        LONG_CHINESE_QUERY,
        30,
        candidate_rows_for_terms=_fake_rows_for_terms,
        candidate_documents=lambda db, ids: [],
        output_id="chunk_id",
        text_field="text",
        corpus_langs=["en"],
    )
    knowledge_store._lexical_candidate_union(
        object(),
        "nb",
        LONG_CHINESE_QUERY,
        30,
        candidate_rows_for_terms=_fake_rows_for_terms,
        candidate_documents=lambda db, ids: [],
        output_id="chunk_id",
        text_field="text",
    )
    assert captured[0] == corpus_gated_recall_terms(LONG_CHINESE_QUERY, ["en"])
    assert captured[1] == lexical_recall_terms(LONG_CHINESE_QUERY)
    assert len(captured[0]) < len(captured[1])


# ── SQLite adapter: no recall is actually lost ────────────────────────


def _trigram_fts_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE VIRTUAL TABLE t USING fts5(text, tokenize='trigram')")
    db.executemany("INSERT INTO t (text) VALUES (?)", [
        ("set_db configures the engine before place_opt_design runs",),
        ("timing closure needs placement optimization and clock tree synthesis",),
        ("static timing analysis models setup and hold constraints",),
    ])
    return db


def test_gated_terms_return_the_same_hits_on_a_latin_corpus():
    """Zero recall loss, demonstrated rather than argued."""
    db = _trigram_fts_db()

    def _hits(expression):
        if not expression:
            return []
        return [
            row["text"] for row in db.execute(
                "SELECT text, bm25(t) AS rank FROM t WHERE t MATCH ? ORDER BY rank",
                (expression,),
            ).fetchall()
        ]

    ungated = _hits(sqlite_fts_match_expression(LONG_CHINESE_QUERY))
    gated = _hits(sqlite_fts_match_expression(LONG_CHINESE_QUERY, ["en"]))
    assert gated == ungated
    assert gated, "the Latin terms must still find the corpus"


# ── service wiring + kill switch ──────────────────────────────────────


@pytest.fixture
def repo(tmp_path, monkeypatch):
    from app.core.config import Settings
    from app.services.sqlite_repository import SQLiteRepository

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'gate.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


def _seed_english_chunks(repo, notebook_id):
    from app.services.sqlite_repository import _now

    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,file_name,"
            "file_path,file_size,file_hash,summary,doc_type,parse_status,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("src-gate", notebook_id, "Doc", "document", "s.md", "/tmp/s.md", 0,
             "h", "", "", "extracted", now, now))
        for index, text in enumerate([
            "set_db configures the engine before place_opt_design runs",
            "timing closure needs placement optimization",
        ]):
            db.execute(
                "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,"
                "element_ids,created_at) VALUES (?,?,?,?,?,?,?)",
                (f"c-gate-{index}", notebook_id, "src-gate", text, "", "[]", now))


def test_kg_search_endpoint_is_gated_too(repo, monkeypatch):
    """`/notebooks/{id}/kg/search` probes the same `knowledge_objects` trigram
    surface, so a long Chinese query against an English notebook pays the
    identical guaranteed-empty probe crowd there. Zero recall loss by the same
    proof: those terms cannot match this corpus either way.
    """
    from app.models.schemas import NotebookCreate

    notebook = repo.create_notebook(NotebookCreate(name="nb-kgsearch"))
    _seed_english_chunks(repo, notebook.id)

    seen: list[object] = []
    knowledge = repo._runtime.knowledge_query.knowledge
    original = knowledge.fts_search

    def _spy(db, notebook_id, q, k=30, **kwargs):
        seen.append(kwargs.get("corpus_langs"))
        return original(db, notebook_id, q, k, **kwargs)

    monkeypatch.setattr(knowledge, "fts_search", _spy)
    repo.kg_search(notebook.id, LONG_CHINESE_QUERY, 12)
    assert seen == [["en"]]


def test_retrieval_passes_the_probed_langs_to_the_chunk_adapter(repo, monkeypatch):
    from app.models.schemas import NotebookCreate

    notebook = repo.create_notebook(NotebookCreate(name="nb-gate"))
    _seed_english_chunks(repo, notebook.id)
    repo.backfill_chunk_fts(notebook.id)

    seen: list[object] = []
    candidates = repo.retrieval.candidates
    original = candidates.knowledge.chunk_fts_search

    def _spy(db, notebook_id, q, k=30, **kwargs):
        seen.append(kwargs.get("corpus_langs"))
        return original(db, notebook_id, q, k, **kwargs)

    monkeypatch.setattr(candidates.knowledge, "chunk_fts_search", _spy)
    candidates._keyword_chunk_candidates(notebook.id, "set_db 时序收敛")
    assert seen == [["en"]]


def test_kill_switch_stops_answering_the_corpus_language(repo, monkeypatch):
    """`LEXICAL_LANGUAGE_GATE_ENABLED=0` is expressed by not answering at all."""
    from app.models.schemas import NotebookCreate

    notebook = repo.create_notebook(NotebookCreate(name="nb-off"))
    _seed_english_chunks(repo, notebook.id)
    candidates = repo.retrieval.candidates
    assert candidates._lexical_corpus_langs(notebook.id) == ["en"]

    monkeypatch.setattr(
        candidates.settings, "lexical_language_gate_enabled", False)
    assert candidates._lexical_corpus_langs(notebook.id) is None


def test_source_scoped_runs_are_never_gated(repo):
    """A scoped run's lexical arm is its ONLY candidate generator.

    `_retrieve_chunks` deliberately skips the notebook-wide ANN when sources
    are selected, so filtering its terms on a misjudged notebook-level sample
    would not cost a share of a wider pool — it would return no evidence at
    all. The exemption is therefore about correctness, not caution.
    (codex #428 round-2 P1)
    """
    from app.models.schemas import NotebookCreate

    notebook = repo.create_notebook(NotebookCreate(name="nb-scoped"))
    _seed_english_chunks(repo, notebook.id)
    candidates = repo.retrieval.candidates
    # The sampled hint says English...
    assert candidates._notebook_langs(notebook.id) == ["en"]
    # ... yet a scoped run must not be filtered by it.
    assert candidates._lexical_corpus_langs(
        notebook.id, source_scoped=True) is None
    assert candidates._lexical_corpus_langs(
        notebook.id, source_scoped=False) == ["en"]


def test_scoped_chinese_source_still_retrieves_in_a_misjudged_notebook(repo):
    """End-to-end shape of the hole: an unsampled Chinese source, selected."""
    from app.models.schemas import NotebookCreate
    from app.services.sqlite_repository import _now

    notebook = repo.create_notebook(NotebookCreate(name="nb-mixed"))
    _seed_english_chunks(repo, notebook.id)
    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,file_name,"
            "file_path,file_size,file_hash,summary,doc_type,parse_status,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("src-zh", notebook.id, "中文手册", "document", "zh.md", "/tmp/zh.md",
             0, "hz", "", "", "extracted", now, now))
        db.execute(
            "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,"
            "element_ids,created_at) VALUES (?,?,?,?,?,?,?)",
            ("c-zh-0", notebook.id, "src-zh",
             "时序收敛需要布局优化与时钟树综合之间的反复迭代。", "", "[]", now))
    repo.backfill_chunk_fts(notebook.id)

    candidates = repo.retrieval.candidates
    # The scoped probe is unfiltered, so the CJK terms survive and hit.
    scoped_langs = candidates._lexical_corpus_langs(
        notebook.id, source_scoped=True)
    with repo._connect() as db:
        hits = candidates.knowledge.chunk_fts_search(
            db, notebook.id, "时序收敛的方法", k=10,
            allowed_source_ids=["src-zh"], corpus_langs=scoped_langs,
        )
    assert [hit["chunk_id"] for hit in hits] == ["c-zh-0"]

    # Had the notebook-level hint been applied, every term would have gone.
    misjudged = corpus_gated_recall_terms("时序收敛的方法", ["en"])
    assert all(not _is_all_cjk(term) for term in misjudged[1:])


def test_a_failing_language_probe_degrades_to_no_filtering(repo, monkeypatch):
    """The probe runs outside the lexical arm's fail-open `except`, so it owns
    its own: an unreadable hint must mean "unknown corpus", not a dead run."""
    from app.models.schemas import NotebookCreate

    notebook = repo.create_notebook(NotebookCreate(name="nb-probe-boom"))
    candidates = repo.retrieval.candidates

    def _boom(_notebook_id):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(candidates, "_notebook_langs", _boom)
    assert candidates._lexical_corpus_langs(notebook.id) is None
    # ... and the terms are then left completely alone.
    assert corpus_gated_recall_terms(
        LONG_CHINESE_QUERY, candidates._lexical_corpus_langs(notebook.id)
    ) == lexical_recall_terms(LONG_CHINESE_QUERY)
