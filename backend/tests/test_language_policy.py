"""Language-policy tests (bilingual query + source-language preservation + answer language).

Covers the 3-layer language policy:
  ① query side: sub_queries in the question's own language (single multilingual embed),
     keywords bilingual (cheap FTS carries the 2nd language);
  ② intermediate/structural content: preserve source/original language (never translate names);
  ③ final answer: in the question's language.
"""
import json as _json

import pytest

from app.services.query_rewrite import expand_query, ExpandedQuery, SubQuerySpec
from tests.model_testkit import bind_all_embedding_clients
from tests.model_testkit import bind_chat_client


class _FakeLLM:
    def __init__(self, payload, configured=True, raise_exc=False):
        self.configured = configured
        self._p = payload
        self._raise = raise_exc

    def chat_json(self, messages, schema_hint, **kw):
        if self._raise:
            raise RuntimeError("boom")
        return _json.dumps(self._p)


# ── ① query-side rename: query_en -> query ─────────────────────────────

def test_expanded_query_has_query_field_not_query_en():
    ex = ExpandedQuery(query="q", sub_queries=[SubQuerySpec("a")])
    assert ex.query == "q"
    assert not hasattr(ex, "query_en")


def test_fallback_populates_query_from_question():
    """Unconfigured client → fallback carries the (normalized) question in .query."""
    class Off:
        configured = False
    ex = expand_query(Off(), "gpt4 对比")
    assert ex.query == "gpt4 对比"           # fallback query = original question
    assert [s.query for s in ex.sub_queries] == ["gpt 4 对比"]


def test_parse_populates_query_from_new_key():
    llm = _FakeLLM({"query": "diff between DeepSeek-V3 and V2",
                    "sub_queries": [{"query": "DeepSeek-V3 改进"},
                                    {"query": "DeepSeek-V2 架构"}],
                    "high_level_keywords": ["输出电阻", "output resistance"],
                    "low_level_keywords": ["cascode", "共源共栅"]})
    ex = expand_query(llm, "deepseekv3相比deepseekv2有什么改进")
    assert ex.query == "diff between DeepSeek-V3 and V2"
    assert [s.query for s in ex.sub_queries] == ["DeepSeek-V3 改进", "DeepSeek-V2 架构"]
    assert ex.high_level_keywords == ["输出电阻", "output resistance"]
    assert ex.low_level_keywords == ["cascode", "共源共栅"]


def test_parse_missing_query_key_falls_back_to_question():
    ex = expand_query(_FakeLLM({"sub_queries": [{"query": "x"}]}), "原始问题")
    assert ex.query == "原始问题"


# ── ② expand_query accepts corpus_langs and forwards it ────────────────

def test_expand_query_accepts_corpus_langs_param():
    captured = {}

    class _Fake:
        configured = True
        def chat_json(self, messages, schema_hint, **kw):
            captured["prompt"] = messages[-1]["content"]
            return '{"query":"q","sub_queries":[{"query":"a"}]}'

    expand_query(_Fake(), "q", corpus_langs=["zh", "en"])
    assert "zh" in captured["prompt"] and "en" in captured["prompt"]


# ── ② expand_query_prompt text: no forced English, bilingual keywords ──

def test_prompt_drops_forced_english():
    from app.services.prompts import expand_query_prompt
    p = expand_query_prompt("q", corpus_langs=["zh", "en"])
    assert "ENGLISH document corpus" not in p
    assert "ENGLISH search queries" not in p
    assert "query_en" not in p


def test_prompt_query_field_says_own_language():
    from app.services.prompts import expand_query_prompt
    p = expand_query_prompt("q")
    assert "own language" in p.lower()


def test_prompt_subqueries_in_question_language():
    from app.services.prompts import expand_query_prompt
    p = expand_query_prompt("q")
    # sub_queries wording must reference the question's language, not force English
    assert "question's language" in p.lower()


def test_prompt_bilingual_keywords_when_two_langs():
    from app.services.prompts import expand_query_prompt
    p = expand_query_prompt("q", corpus_langs=["zh", "en"])
    assert "zh" in p and "en" in p
    assert "BOTH" in p  # instruct to include both forms


def test_prompt_single_lang_does_not_force_chinese():
    from app.services.prompts import expand_query_prompt
    p = expand_query_prompt("q", corpus_langs=["en"])
    assert "zh" not in p  # only English corpus → no Chinese-forcing


def test_prompt_default_none_is_bilingual():
    from app.services.prompts import expand_query_prompt
    p = expand_query_prompt("q")  # corpus_langs=None ⇒ both
    assert "zh" in p and "en" in p


def test_expand_schema_hint_uses_query_not_query_en():
    from app.services.prompts import EXPAND_SCHEMA_HINT
    assert '"query"' in EXPAND_SCHEMA_HINT
    assert "query_en" not in EXPAND_SCHEMA_HINT


# ── ③ _notebook_langs probe: sampled + cached ─────────────────────────

@pytest.fixture
def repo(tmp_path, monkeypatch):
    from app.core.config import Settings
    from app.services.sqlite_repository import SQLiteRepository
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


def _seed_chunk_texts(repo, notebook_id, texts):
    import uuid
    from app.services.sqlite_repository import _now
    sid = f"src-{uuid.uuid4().hex[:8]}"
    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,"
            "file_size,file_hash,summary,doc_type,parse_status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, notebook_id, "Doc", "document", "s.md", "/tmp/s.md", 0, "h", "",
             "", "extracted", now, now))
        for i, t in enumerate(texts):
            db.execute(
                "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,"
                "element_ids,created_at) VALUES (?,?,?,?,?,?,?)",
                (f"c-{sid}-{i}", notebook_id, sid, t, "", "[]", now))


def test_notebook_langs_english_only(repo):
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="nb-en"))
    _seed_chunk_texts(repo, nb.id, ["Low noise amplifier design", "cascode output resistance"])
    assert repo._notebook_langs(nb.id) == ["en"]


def test_notebook_langs_includes_zh_when_cjk_present(repo):
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="nb-zh"))
    _seed_chunk_texts(repo, nb.id, ["低噪声放大器设计", "cascode 共源共栅"])
    langs = repo._notebook_langs(nb.id)
    assert "zh" in langs


def test_notebook_langs_empty_defaults_to_en(repo):
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="nb-empty"))
    assert repo._notebook_langs(nb.id) == ["en"]


def test_notebook_langs_is_cached(repo, monkeypatch):
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="nb-cache"))
    _seed_chunk_texts(repo, nb.id, ["Low noise amplifier"])
    first = repo._notebook_langs(nb.id)
    # After the first call, further calls must NOT re-open a read connection.
    calls = {"n": 0}
    orig_connect = repo._connect

    def _spy():
        calls["n"] += 1
        return orig_connect()

    monkeypatch.setattr(repo, "_connect", _spy)
    second = repo._notebook_langs(nb.id)
    assert second == first
    assert calls["n"] == 0  # served from cache, no DB access


def test_notebook_langs_invalidated_on_new_language_chunk(repo):
    """A previously-English notebook that gains a Chinese chunk must re-reflect zh
    after the cache is invalidated at a chunk-write settle point (backfill /
    _mark_unified_kg_dirty)."""
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="nb-grows"))
    _seed_chunk_texts(repo, nb.id, ["Low noise amplifier design"])
    assert repo._notebook_langs(nb.id) == ["en"]        # warms the cache
    _seed_chunk_texts(repo, nb.id, ["低噪声放大器设计"])   # add zh content
    repo.backfill_chunk_fts(nb.id)                       # settle point -> invalidates hint
    assert "zh" in repo._notebook_langs(nb.id)


def test_notebook_langs_invalidated_on_mark_dirty(repo):
    """_mark_unified_kg_dirty — the single content-mutation funnel — also clears
    the language hint (so real source-add / re-chunk / re-embed re-reflect)."""
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="nb-dirty"))
    _seed_chunk_texts(repo, nb.id, ["Low noise amplifier design"])
    assert repo._notebook_langs(nb.id) == ["en"]        # warms the cache
    _seed_chunk_texts(repo, nb.id, ["低噪声放大器设计"])
    repo._mark_unified_kg_dirty(nb.id)                  # content-mutation funnel
    assert "zh" in repo._notebook_langs(nb.id)


def test_notebook_langs_samples_spread_not_first_50(repo):
    """2nd-language content appended AFTER many first-language chunks must still be
    caught — sampling must include a tail spread, not just the first rows."""
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="nb-spread"))
    texts = [f"English chunk number {i}" for i in range(60)] + ["末尾追加的中文段落"]
    _seed_chunk_texts(repo, nb.id, texts)
    assert "zh" in repo._notebook_langs(nb.id)


# ── ① [MAIN] bilingual keywords reach the CHUNK FTS ───────────────────

def test_keyword_chunk_candidates_matches_second_language(repo):
    """The chunk-keyword union runs ONE chunk_fts_search over the bilingual keyword
    string and returns a chunk that matches ONLY the 2nd-language keyword."""
    from app.models.schemas import NotebookCreate
    from app.services.retrieval import RetrievedChunk
    nb = repo.create_notebook(NotebookCreate(name="nb-kw"))
    # English notebook; ONE chunk carries only the Chinese term (no English overlap).
    _seed_chunk_texts(repo, nb.id, ["cascode output resistance analysis",
                                    "低噪声放大器 的设计要点"])
    repo.backfill_chunk_fts(nb.id)
    # bilingual keyword string carries the Chinese 2nd-language term
    hits = repo.retrieval.candidates._keyword_chunk_candidates(nb.id, "低噪声放大器", recall=50)
    assert hits, "keyword union must surface the 2nd-language chunk"
    assert any("低噪声放大器" in h.text for h in hits)
    assert all(isinstance(h, RetrievedChunk) for h in hits)
    assert all(h.relevance > 0 for h in hits)  # keyword hits carry keyword score


def test_keyword_chunk_candidates_empty_keywords_no_hits(repo):
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="nb-kw2"))
    _seed_chunk_texts(repo, nb.id, ["anything"])
    repo.backfill_chunk_fts(nb.id)
    assert repo.retrieval.candidates._keyword_chunk_candidates(nb.id, "", recall=50) == []
    assert repo.retrieval.candidates._keyword_chunk_candidates(nb.id, "   ", recall=50) == []


def test_ask_chunk_unions_bilingual_keyword_hit_once(repo, monkeypatch):
    """End-to-end wiring: ask_chunk merges the bilingual-keyword chunk into the
    candidate set, and calls the keyword-union exactly ONCE per query (one FTS)."""
    from app.models.schemas import NotebookCreate, AskRequest
    from app.services.embedding import FakeEmbedder
    import app.services.query_rewrite as qr

    bind_all_embedding_clients(repo, FakeEmbedder(dim=repo.settings.embed_dim))
    nb = repo.create_notebook(NotebookCreate(name="nb-e2e"))
    _seed_chunk_texts(repo, nb.id, ["cascode output resistance",
                                    "低噪声放大器 LNA 设计"])
    repo.backfill_chunk_fts(nb.id)

    # English-only sub_queries (won't lexically match the Chinese chunk); the
    # bilingual keyword carries the Chinese term into the FTS union.
    monkeypatch.setattr(qr, "expand_query", lambda *a, **k: qr.ExpandedQuery(
        query="LNA design", low_level_keywords=["低噪声放大器"], high_level_keywords=[],
        sub_queries=[qr.SubQuerySpec("cascode resistance"),
                     qr.SubQuerySpec("amplifier design")]))

    calls = {"n": 0}
    orig = repo.retrieval.candidates._keyword_chunk_candidates

    def _spy(nid, kws, recall=0):
        calls["n"] += 1
        return orig(nid, kws, recall)

    monkeypatch.setattr(repo.retrieval.candidates, "_keyword_chunk_candidates", _spy)

    class _FakeLLM:
        configured = True
        def chat_json(self, messages, schema_hint, **kw):
            return '{"answer":"ok","grounded":true}'

    bind_chat_client(repo, "ask_answer", _FakeLLM())
    captured = {}

    import app.services.retrieval as _ret
    orig_quota = _ret.quota_fuse
    def _cap_quota(collected, per_query, top_n, relevance=lambda h: h.relevance):
        captured["collected_ids"] = set(collected.keys())
        return orig_quota(collected, per_query, top_n, relevance)
    monkeypatch.setattr(_ret, "quota_fuse", _cap_quota)

    repo.ask_chunk(nb.id, AskRequest(question="what about the LNA?"))
    assert calls["n"] == 1            # ONE keyword-union FTS call per query
    assert captured.get("collected_ids")
    # the Chinese chunk (only reachable via the bilingual keyword) is in candidates
    with repo._connect() as db:
        row = db.execute("SELECT id FROM chunks WHERE notebook_id=? AND text LIKE ?",
                         (nb.id, "%低噪声放大器%")).fetchone()
    assert row["id"] in captured["collected_ids"]


# ── ② source-language preservation clause (text only) ─────────────────

def test_kg_extract_prompt_preserves_source_language():
    from app.services.kg.extract import _prompt
    p = _prompt("[0] text", "sec", "textbook")
    low = p.lower()
    assert "original language" in low
    assert "do not translate" in low or "not translate" in low


def test_gleaning_prompt_preserves_source_language():
    from app.services.prompts import gleaning_prompt
    p = gleaning_prompt("sec", "textbook").lower()
    assert "original language" in p
    assert "translate" in p


def test_concept_description_prompt_preserves_source_language():
    from app.services.prompts import concept_description_prompt
    p = concept_description_prompt("LNA", "snippets").lower()
    assert "original language" in p or "language of the source" in p


def test_community_report_prompt_preserves_source_language():
    from app.services.prompts import community_report_prompt
    p = community_report_prompt("members", "rels").lower()
    assert "original language" in p or "language of the source" in p


def test_evidence_refine_prompt_preserves_source_language():
    from app.services.prompts import evidence_refine_prompt
    p = evidence_refine_prompt("q", "ev").lower()
    assert "original language" in p or "language of the source" in p


def test_schema_induction_prompt_preserves_source_language_keeps_ascii_keys():
    from app.services.prompts import schema_induction_prompt
    p = schema_induction_prompt(["foo"], "sample")
    low = p.lower()
    assert "original language" in low or "language of the source" in low
    # keys stay snake_case ASCII
    assert "snake_case" in p


def test_notebook_description_prompt_uses_dominant_source_language():
    from app.services.prompts import notebook_description_prompt
    p = notebook_description_prompt("srcs")
    assert "(Chinese ok)" not in p
    assert "dominant language of the sources" in p.lower()


def test_notebook_meta_prompt_uses_dominant_source_language():
    from app.services.prompts import notebook_meta_prompt
    p = notebook_meta_prompt("srcs")
    assert "(Chinese ok)" not in p
    assert "dominant language of the sources" in p.lower()


def test_concept_merge_review_prompt_merges_translations():
    from app.services.concept_merge_review import _prompt
    p = _prompt([{"id": "1", "canonical_a": "LNA", "canonical_b": "低噪声放大器"}])
    low = p.lower()
    assert "translation" in low  # translations of each other should merge
    assert "verbatim" in low or "never translate" in low
    assert "original language" in low


# ── ③ final answer language ───────────────────────────────────────────

def test_answer_prompt_answers_in_question_language():
    # global_reduce_prompt 随退役的 global 模式一起删除后,「用提问语言作答」
    # 这条语言政策由 answer_prompt 规则 4(L1 片段 answer.style_language)承载。
    from app.services.prompts import answer_prompt
    p = answer_prompt("q", "k1: [concept] X").lower()
    assert "answer in the question's language" in p
