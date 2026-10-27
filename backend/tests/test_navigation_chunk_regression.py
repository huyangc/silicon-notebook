"""Regression for the 2026-10-21 production failure: tables of contents and
lists of figures / tables crowding the body out of the top of retrieval
(plan ``docs/superpowers/plans/2026-10-21-section-navigation-retrieval.md``
§0, T3).

Production shape reproduced here, end to end through the real build and
retrieval path:

* two revisions (2.0 and 3.0) of a UCIe-like specification, parsed MinerU
  style (flat headings, no breadcrumbs, captions as headings, the table of
  contents a table whose cells split words: ``Voltage T | ransfer Function``);
* the body is intact: ``5.7.2.1 Loss and Crosstalk Mask`` and ``5.7.3 Standard
  Package`` with the Table 5-18 values in the same block;
* the embedder gives the navigation pages the HIGHEST vector similarity for
  the production sub-queries (they gather every title in one block, the body
  sections dilute the same keywords with their own vocabulary), and the
  fixture asserts that shape before relying on it.

The navigation flags are computed by the real chunk build
(``_chunk_and_embed_source``), the sub-queries are the production ones, and
``RETRIEVAL_NAVIGATION_FILTER_ENABLED=false`` brings the navigation pages back
to the top -- so the assertions exercise the filter, not the fixture.
"""
from __future__ import annotations

import json
import math
import re
import uuid

import pytest

from app.core.ask_retrieval_policy import ask_retrieval_limits
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository, _now
from tests.model_testkit import bind_all_embedding_clients, bind_chat_client

#: The production sub-queries (diagnosis 2026-10-21, plan appendix Q1-Q3).
Q1 = "UCIe Advanced Package Standard Package 插损 insertion loss 规格"
Q2 = "UCIe Advanced Package reach channel bandwidth density per direction"
Q3 = "UCIe insertion loss mask standard package"
QUERIES = (Q1, Q2, Q3)
TOP_K = 8


# --------------------------------------------------------------- embedder
#: One vector component per keyword. The query words of Q1-Q3 come first; the
#: rest is the body's own vocabulary, which a navigation page (titles only)
#: does not carry -- that is what makes the navigation pages win on cosine.
VOCAB = (
    "ucie", "advanced", "standard", "package", "insertion", "loss", "mask",
    "crosstalk", "channel", "reach", "bandwidth", "density", "direction",
    "performance", "targets", "voltage", "transfer", "function",
    # body vocabulary
    "transmitter", "receiver", "shall", "compliance", "measured", "termination",
    "lane", "bump", "eye", "jitter", "specified", "fixture", "defined",
)
_TOKEN = re.compile(r"[a-z0-9]+")


class KeywordEmbedder:
    """Deterministic keyword-count embedder: component ``i`` is how often
    ``VOCAB[i]`` occurs as a token, plus one constant component so no vector is
    zero. A word MinerU split across table cells (``T | ransfer``) does not
    count, as in a real embedding of that text it would be noise."""

    dim = len(VOCAB) + 1

    def _vec(self, text: str) -> list[float]:
        counts = {word: 0 for word in VOCAB}
        for token in _TOKEN.findall(text.casefold()):
            if token in counts:
                counts[token] += 1
        return [float(counts[word]) for word in VOCAB] + [0.5]

    def embed_texts(self, texts):
        return [self._vec(text) for text in texts]

    def embed_query(self, text):
        return self._vec(text)


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


# ---------------------------------------------------------------- document
#: (number, title, body paragraphs). A section without body is a heading
#: only (``5``, ``5.7``, ``5.7.2``), as MinerU emits them.
_TX = (
    "The transmitter shall drive each lane at the specified swing and the "
    "receiver shall meet the eye and jitter limits measured at the bump with a "
    "compliance fixture; termination is defined in the electrical chapter. "
)
SECTIONS = (
    ("1", "Introduction", ["UCIe is an open die-to-die interconnect. " + _TX]),
    ("1.1", "UCIe Components", ["The stack has protocol, adapter and physical layers. " + _TX]),
    ("1.2", "UCIe Configurations", ["Single module and multi module configurations. " + _TX]),
    ("1.3", "Advanced and Standard Package Options",
     ["Two package classes are supported with different bump pitch. " + _TX]),
    ("1.4", "UCIe Key Performance Targets", [
        "The key performance targets of UCIe are listed in Table 1-1 for the "
        "advanced package and in Table 1-2 for the standard package. " + _TX,
    ]),
    ("2", "Protocol Layer", []),
    ("2.1", "PCIe Mode Operation", ["Flits are carried in PCIe mode. " + _TX]),
    ("2.2", "CXL Mode Operation", ["CXL traffic is mapped onto flits. " + _TX]),
    ("3", "Die-to-Die Adapter", []),
    ("3.1", "Link Initialization Flow", ["The adapter trains the link in stages. " + _TX]),
    ("3.2", "Retry Buffer Management", ["The retry buffer holds unacknowledged flits. " + _TX]),
    ("4", "Logical Physical Layer", []),
    ("4.1", "Lane Reversal and Repair", ["Lanes can be reversed and repaired. " + _TX]),
    ("4.2", "Data Transmission Framing", ["Data is framed per module. " + _TX]),
    ("5", "Electrical Layer", []),
    ("5.1", "Interoperability Requirements", ["Interoperation across vendors. " + _TX]),
    ("5.7", "Channel Compliance", []),
    ("5.7.1", "Voltage Transfer Function", [
        "The voltage transfer function VTF of the channel is the ratio of the "
        "received voltage to the source voltage across frequency. " + _TX,
    ]),
    ("5.7.2", "Advanced Package Channel", []),
    ("5.7.2.1", "Loss and Crosstalk Mask", [
        "Loss and crosstalk are specified by a mask defined by L(fN) and XT(fN) "
        "for the advanced package channel; the channel reach is 2 mm. " + _TX,
    ]),
    ("5.7.3", "Standard Package", [
        "The standard package channel reach is 25 mm; its loss and crosstalk "
        "limits are given in Table 5-18. " + _TX,
        "Table 5-18. Standard Package Channel Requirements\n"
        "| Parameter | Requirement |\n"
        "| VTF Loss (dB) | L(0) > -4.5 L(fN) > -7.5 |\n"
        "| VTF Crosstalk (dB) | XT(fN) < -15 |",
    ]),
)
#: Caption headings MinerU emits inside the body: (after section number, caption).
FIGURES = (
    ("1.1", "Figure 1-1. UCIe Components Overview Diagram"),
    ("1.4", "Figure 1-2. Bandwidth Density per Direction by Package"),
    ("2.1", "Figure 2-1. Protocol Layer Stack Diagram"),
    ("3.1", "Figure 3-1. Adapter Link State Machine"),
    ("4.1", "Figure 4-1. Lane Reversal Mapping Example"),
    ("5.1", "Figure 5-1. Electrical Layer Block Diagram"),
    ("5.7.1", "Figure 5-20. Voltage Transfer Function Test Setup"),
    ("5.7.2.1", "Figure 5-21. Advanced Package Loss and Crosstalk Mask"),
    ("5.7.3", "Figure 5-22. Standard Package Channel Reach Diagram"),
)
TABLES = (
    ("1.4", "Table 1-1. UCIe Advanced Package Key Performance Targets"),
    ("1.4", "Table 1-2. UCIe Standard Package Key Performance Targets"),
    ("2.1", "Table 2-1. PCIe Mode Flit Format Encodings"),
    ("3.2", "Table 3-1. Adapter Retry Timeout Values"),
    ("4.1", "Table 4-1. Lane Repair Encoding Rules"),
    ("5.1", "Table 5-1. Interoperability Requirement Matrix"),
    ("5.7.2.1", "Table 5-17. Advanced Package Loss and Crosstalk Mask Values"),
)
_TABLE_ROWS = {
    "Table 1-1": "| Bandwidth density | 1317 GB/s/mm | measured at the bump |\n"
                 "| Latency target | 2 ns | transmitter to receiver |",
    "Table 1-2": "| Bandwidth density | 224 GB/s/mm | measured at the bump |\n"
                 "| Latency target | 2 ns | transmitter to receiver |",
    "Table 5-17": "| L(fN) | > -5 dB | XT(fN) | < -24 dB |",
}


def _split_like_mineru(title: str) -> str:
    """MinerU reads the TOC as a table and splits a wrapped title across a
    cell boundary."""
    return title.replace("Voltage Transfer", "Voltage T | ransfer")


def _toc_entries(sections=SECTIONS) -> list[tuple[str, str, int]]:
    entries = []
    for index, (number, title, _body) in enumerate(sections):
        entries.append((number, _split_like_mineru(title), 9 + index * 11))
    return entries


def spec_elements(revision: str, sections=SECTIONS) -> list[tuple[str, str]]:
    """``(element_type, text)`` in document order. ``sections`` defaults to
    :data:`SECTIONS`; the section-navigation end-to-end suite passes an
    extended copy (same shape, more subsections)."""
    out: list[tuple[str, str]] = [("heading", "Contents")]
    for number, title, page in _toc_entries(sections):
        out.append(("paragraph", f"| {number} | {title} {'.' * 12} | {page} |"))
    out.append(("heading", "Tables"))
    for index, (_after, caption) in enumerate(TABLES):
        out.append(("paragraph", f"{caption} {'.' * 12} {14 + index * 23}"))
    out.append(("heading", "Figures"))
    for index, (_after, caption) in enumerate(FIGURES):
        out.append(("paragraph", f"{_split_like_mineru(caption)} {'.' * 12} {12 + index * 21}"))
    for number, title, body in sections:
        # an unnumbered heading (``number == ""``) is the bare title
        out.append(("heading", f"{number} {title}".strip()))
        for paragraph in body:
            out.append(("paragraph", paragraph.replace("UCIe is", f"UCIe {revision} is")))
        for after, caption in FIGURES:
            if after == number:
                out.append(("heading", caption))
                out.append(("paragraph", "The figure shows the arrangement described above."))
        for after, caption in TABLES:
            if after == number:
                out.append(("heading", caption))
                key = caption.split(".")[0]
                out.append(("paragraph", _TABLE_ROWS.get(
                    key, "| Field | Encoding |\n| reserved | 0 |")))
    return out


def seed_spec(repo, notebook_id: str, revision: str, sections=SECTIONS,
              source_id: str = "") -> str:
    """Write heading + paragraph source elements, then build and embed the
    chunks through the real path (``_chunk_and_embed_source``), which also
    computes the navigation flags."""
    source_id = source_id or f"src-{uuid.uuid4().hex[:8]}"
    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,"
            "file_size,file_hash,summary,doc_type,parse_status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (source_id, notebook_id, f"UCIe Specification {revision}", "document",
             f"ucie-{revision}.pdf", f"/tmp/ucie-{revision}.pdf", 0, f"h-{source_id}",
             "", "", "extracted", now, now))
        for index, (element_type, text) in enumerate(
                spec_elements(revision, sections), 1):
            db.execute(
                "INSERT INTO source_elements (id,source_id,element_type,location_label,"
                "text,metadata,created_at) VALUES (?,?,?,?,?,?,?)",
                (f"el-{source_id}-{index:04d}", source_id, element_type, f"p{index}",
                 text, json.dumps({}), now))
    repo._runtime.source_chunking.chunk_and_embed_source(source_id)
    return source_id


# ----------------------------------------------------------------- fixture
def make_spec_repo(tmp_path, monkeypatch):
    """A scrubbed SQLite repository with the keyword embedder bound (a plain
    function so the section-navigation end-to-end suite builds the same one)."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'nav-regression.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", str(KeywordEmbedder.dim))
    for key in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
                "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL", "REASONING_LLM_MODEL",
                "REWRITE_LLM_API_KEY", "REWRITE_LLM_BASE_URL", "REWRITE_LLM_MODEL"):
        monkeypatch.setenv(key, "")
    repository = SQLiteRepository(Settings(_env_file=None))
    bind_all_embedding_clients(repository, KeywordEmbedder())
    return repository


@pytest.fixture
def repo(tmp_path, monkeypatch):
    return make_spec_repo(tmp_path, monkeypatch)


class Library:
    def __init__(self, repo):
        self.repo = repo
        self.notebook = repo.create_notebook(NotebookCreate(name="UCIE"))
        self.sources = {rev: seed_spec(repo, self.notebook.id, rev) for rev in ("2.0", "3.0")}
        with repo._connect() as db:
            rows = db.execute(
                "SELECT id, source_id, section_path, text, element_ids, navigation "
                "FROM chunks WHERE notebook_id=? ORDER BY rowid",
                (self.notebook.id,)).fetchall()
        self.rows = [dict(row) for row in rows]
        self.navigation_ids = {row["id"] for row in self.rows if row["navigation"]}
        self.navigation_elements = {
            element_id for row in self.rows if row["navigation"]
            for element_id in json.loads(row["element_ids"] or "[]")
        }
        self.body_57_ids = {
            row["id"] for row in self.rows
            if row["section_path"].startswith("5.7.") and not row["navigation"]
        }

    def section(self, chunk_id: str) -> str:
        return next(row["section_path"] for row in self.rows if row["id"] == chunk_id)


@pytest.fixture
def library(repo):
    return Library(repo)


def _candidates(repo, notebook_id, query):
    scored, ids, matrix = repo.retrieval.retrieve_chunk_candidates(notebook_id, query)
    selected = repo.retrieval.select_chunk_candidates(
        scored, ids, matrix, TOP_K, repo.settings.chunk_mmr_lambda,
        active_notebook_id=notebook_id,
    )
    return scored, selected


def _filter_off(repo, monkeypatch):
    settings = repo.retrieval.candidates.settings
    assert settings is repo.settings
    monkeypatch.setattr(settings, "retrieval_navigation_filter_enabled", False)


# ------------------------------------------------- the build flagged them
def test_the_build_path_flags_every_navigation_page_and_no_body_section(library):
    flagged = {(row["source_id"], row["section_path"]) for row in library.rows
               if row["navigation"]}
    for source_id in library.sources.values():
        sections = {path for sid, path in flagged if sid == source_id}
        assert sections == {"Contents", "Tables", "Figures"}
        # every chunk of the three navigation sections is flagged
        for row in library.rows:
            if row["source_id"] == source_id and row["section_path"] in sections:
                assert row["navigation"] == 1, row["text"][:80]
    # the body is intact and unflagged, Table 5-18 values in the 5.7.3 block
    standard = [row for row in library.rows if row["section_path"] == "5.7.3 Standard Package"]
    assert len(standard) == 2
    assert all("L(0) > -4.5 L(fN) > -7.5" in row["text"] for row in standard)
    assert all(not row["navigation"] for row in standard)
    mask = [row for row in library.rows if row["section_path"] == "5.7.2.1 Loss and Crosstalk Mask"]
    assert mask and all(not row["navigation"] for row in mask)
    assert any("ransfer" in row["text"] and " | " in row["text"]
               for row in library.rows if row["section_path"] == "Contents")
    with library.repo._connect() as db:
        scanned = db.execute(
            "SELECT COUNT(*) FROM sources WHERE notebook_id=? AND navigation_scanned_at IS NOT NULL",
            (library.notebook.id,)).fetchone()[0]
    assert scanned == 2


@pytest.mark.parametrize("query", QUERIES, ids=["Q1", "Q2", "Q3"])
def test_fixture_reproduces_production_navigation_pages_win_on_vectors(library, query):
    """Shape check, independent of the filter: for every production query
    the chunk with the highest vector similarity is a navigation page."""
    embedder = KeywordEmbedder()
    q = embedder.embed_query(query)
    ranked = sorted(library.rows, key=lambda row: -_cosine(q, embedder._vec(row["text"])))
    assert ranked[0]["id"] in library.navigation_ids


# ------------------------------------------------------- chunk candidates
@pytest.mark.parametrize("query", QUERIES, ids=["Q1", "Q2", "Q3"])
def test_production_queries_return_no_navigation_page_and_reach_chapter_5(
        library, query):
    repo, notebook_id = library.repo, library.notebook.id
    scored, selected = _candidates(repo, notebook_id, query)
    assert scored and selected
    assert not library.navigation_ids & {chunk.chunk_id for chunk in scored}
    assert not library.navigation_ids & {chunk.chunk_id for chunk in selected}
    assert len(selected) <= TOP_K
    assert library.body_57_ids & {chunk.chunk_id for chunk in selected}, [
        library.section(chunk.chunk_id) for chunk in selected]


@pytest.mark.parametrize("query", QUERIES, ids=["Q1", "Q2", "Q3"])
def test_with_the_read_switch_off_navigation_pages_come_back_to_the_top(
        library, query, monkeypatch):
    """Proves the assertions above exercise the filter: the stored flags are
    unchanged, only the read side is switched off."""
    repo, notebook_id = library.repo, library.notebook.id
    _filter_off(repo, monkeypatch)
    scored, selected = _candidates(repo, notebook_id, query)
    assert scored[0].chunk_id in library.navigation_ids, [
        library.section(chunk.chunk_id) for chunk in scored[:TOP_K]]
    in_top = [chunk for chunk in selected if chunk.chunk_id in library.navigation_ids]
    assert len(in_top) >= 2


# ---------------------------------------------------------- element channel
@pytest.mark.parametrize("query", QUERIES, ids=["Q1", "Q2", "Q3"])
def test_element_channel_returns_no_navigation_page_element(library, query, monkeypatch):
    repo, notebook_id = library.repo, library.notebook.id
    found = repo.retrieval.retrieve_elements(notebook_id, query, limit=TOP_K)
    assert found
    assert not library.navigation_elements & {element.element_id for element in found}

    _filter_off(repo, monkeypatch)
    restored = repo.retrieval.retrieve_elements(notebook_id, query, limit=TOP_K)
    assert library.navigation_elements & {element.element_id for element in restored}


# ------------------------------------------------------- reasoning smoke
class _ScriptedModel:
    """Plan: the production sub-queries; reflect: answer after the first round."""

    configured = True

    def __init__(self):
        self.reflects = 0

    def chat_json(self, messages, schema_hint, **kwargs):
        if "sub_queries" in schema_hint:
            return json.dumps({"sub_queries": [{"query": q} for q in QUERIES]})
        self.reflects += 1
        return json.dumps({"next_action": "answer", "sufficient": True, "reason": "enough"})


@pytest.mark.parametrize("filter_on", [True, False], ids=["filter-on", "filter-off"])
def test_reasoning_retrieval_final_candidates_hold_no_navigation_page(
        library, monkeypatch, filter_on):
    from app.services.reasoning_retrieval import ReasoningRetriever

    repo, notebook_id = library.repo, library.notebook.id
    if not filter_on:
        _filter_off(repo, monkeypatch)
    model = _ScriptedModel()
    bind_chat_client(repo, "reasoning_agent", model)
    retriever = ReasoningRetriever.from_repository(repo, repo.settings)
    result = retriever.run(
        notebook_id,
        "UCIe 2.0 和 3.0 的 Advanced Package 与 Standard Package 插损规格分别是多少",
        "", limits=ask_retrieval_limits("standard"),
    )
    assert model.reflects >= 1
    assert any(step.step_type == "search_chunks" for step in result.trace)
    final = {chunk.chunk_id for chunk in result.chunks}
    assert final
    if filter_on:
        assert not library.navigation_ids & final
        assert library.body_57_ids & final
        assert not library.navigation_elements & {e.element_id for e in result.elements}
    else:
        assert library.navigation_ids & final
