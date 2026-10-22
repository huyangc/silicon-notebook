"""In-document navigation end to end (plan
``docs/superpowers/plans/2026-10-21-section-navigation-retrieval.md`` T8,
§3.7-§3.9): the REAL ``ReasoningRetriever`` (no knowledge graph, standard
effort) over the production-shaped specification of the PR-1 regression
(``test_navigation_chunk_regression``: two UCIe-like revisions parsed MinerU
style, chunks, embeddings and navigation flags built by the real
``_chunk_and_embed_source``, the keyword embedder that makes the navigation
pages the closest vectors), with a scripted reflect model that READS the
prompt it is given -- the ``sN`` it reads a section by is the one the hint
block or the previous listing printed, not one the test knows in advance.

The specification is the PR-1 one plus what in-document navigation needs:
``5.7.3`` gets two subsections (``5.7.3.1`` / ``5.7.3.2``) so its range is a
real subtree, and an unnumbered ``Implementation Notes`` heading occurs twice
(after ``5.1`` and after ``5.7.3.2``) -- the duplicate-title case.

What the unit suites already pin (outline folding, ranking, rendering in
``test_section_outline``; ports in ``test_section_outline_ports``; every gate
condition, skip code and seat rule in ``test_reasoning_section_nav``) is not
re-tested here; this suite proves the pieces meet through the real build,
retrieval, reflect and synthesis-order paths.

Hermetic: temporary SQLite, keyword embedder, scripted model, no port opened.
The chunk-graph concept walk (PPR) is off -- with no knowledge graph it would
pour every 5.7 passage into the first round, leaving the hint block nothing
uncovered to point at; the production Q1 failure was a first round that had
NOT reached 5.7.2.1.
"""
from __future__ import annotations

import itertools
import re

import pytest

from app.core.ask_retrieval_policy import ask_retrieval_limits
from app.models.schemas import NotebookCreate
from app.services.chunk_federation import reasoning_order_for
from app.services.reasoning_retrieval import ReasoningRetriever
from app.services.retrieval import passage_floor
from app.services.section_outline import HINT_BLOCK_TITLE, build_outline
from tests.model_testkit import bind_chat_client
from tests.test_navigation_chunk_regression import (
    _TX,
    Q1,
    SECTIONS,
    make_spec_repo,
    seed_spec,
)
from tests.test_reasoning_enumeration_tools import _skips, _steps, _ValidatingLLM
from tests.test_reasoning_section_nav import ANSWER, _browse, _read

QUESTION = "UCIe 2.0 和 3.0 的 Advanced Package 与 Standard Package 插损规格分别是多少"
T20 = "UCIe Specification 2.0"
T30 = "UCIe Specification 3.0"
MASK = "5.7.2.1 Loss and Crosstalk Mask"
STANDARD = "5.7.3 Standard Package"
NOTES_TITLE = "Implementation Notes"
NAVIGATION_LABELS = ("Contents", "Tables", "Figures")

_NOTES = ("", NOTES_TITLE, ["Implementers should consult the errata list of this revision."])
NAV_SECTIONS = tuple(
    item for section in SECTIONS
    for item in ((section, _NOTES) if section[0] == "5.1" else (section,))
) + (
    ("5.7.3.1", "Standard Package Loss Budget", [
        "The standard package loss budget splits the channel loss between "
        "the package trace and the connector. " + _TX]),
    ("5.7.3.2", "Standard Package Crosstalk Budget", [
        "The standard package crosstalk budget bounds near end and far end "
        "coupling. " + _TX]),
    _NOTES,
)


# ------------------------------------------------------------------ fixture
class Spec:
    """Both revisions seeded through the real build path, and each source's
    outline as the run will see it (the same port, the same projection)."""

    def __init__(self, repo):
        self.repo = repo
        self.notebook_id = repo.create_notebook(NotebookCreate(name="UCIE")).id
        self.sources = {
            title: seed_spec(repo, self.notebook_id, revision, NAV_SECTIONS,
                             source_id=f"src-ucie-{revision.replace('.', '')}")
            for title, revision in ((T20, "2.0"), (T30, "3.0"))}
        with repo._connect() as db:
            rows = db.execute(
                "SELECT id, source_id, section_path, text, navigation FROM chunks "
                "WHERE notebook_id=? ORDER BY rowid", (self.notebook_id,)).fetchall()
        self.rows = {row["id"]: dict(row) for row in rows}
        self.navigation_ids = {cid for cid, row in self.rows.items() if row["navigation"]}
        self.outlines = {
            title: build_outline(*repo.retrieval.section_outline(self.notebook_id, sid))
            for title, sid in self.sources.items()}

    def occurrences(self, title, label):
        return [occ for occ in self.outlines[title].occurrences if occ.label == label]

    def occurrence(self, title, label):
        (only,) = self.occurrences(title, label)
        return only

    def label_of(self, chunk_id):
        return self.rows[chunk_id]["section_path"]


@pytest.fixture
def spec(tmp_path, monkeypatch):
    """Row ids are minted by a counter (the ``_new_id`` seam) and the source
    ids are fixed: the two revisions carry identical body text, so equal
    scores between their copies are broken by id -- with random ids the
    first round would draw its passages from either revision run to run."""
    from app.services import sqlite_repository

    minted = itertools.count(1)
    monkeypatch.setattr(sqlite_repository, "_new_id",
                        lambda prefix: f"{prefix}-e2e{next(minted):06d}")
    repo = make_spec_repo(tmp_path, monkeypatch)
    repo.settings.graph_ppr_enabled = False
    return Spec(repo)


def test_the_fixture_has_the_shape_the_scenarios_rely_on(spec):
    """Read off the real build: navigation pages flagged and out of the
    outline; 5.7 / 5.7.2 virtual (headings without body); 5.7.3 a subtree
    holding Table 5-18; the notes title twice."""
    assert {spec.label_of(cid) for cid in spec.navigation_ids} == set(NAVIGATION_LABELS)
    for title in (T20, T30):
        outline = spec.outlines[title]
        assert not {occ.label for occ in outline.occurrences} & set(NAVIGATION_LABELS)
        assert spec.occurrence(title, "5.7").virtual
        assert spec.occurrence(title, "5.7.2").virtual
        standard = spec.occurrence(title, STANDARD)
        labels = [spec.label_of(cid) for cid in standard.range_chunk_ids]
        assert labels == [
            STANDARD, "Figure 5-22. Standard Package Channel Reach Diagram",
            "5.7.3.1 Standard Package Loss Budget",
            "5.7.3.2 Standard Package Crosstalk Budget", NOTES_TITLE]
        assert "L(0) > -4.5 L(fN) > -7.5" in spec.rows[standard.chunk_ids[0]]["text"]
        assert len(spec.occurrences(title, NOTES_TITLE)) == 2


# ---------------------------------------------------------- scripted model
class _PromptReadingLLM(_ValidatingLLM):
    """``_ValidatingLLM`` (the production shape checks run on every reply)
    whose scripted reflect entries may be callables: ``entry(prompt) -> dict``
    decides from the reflect prompt it is handed, like a model would."""

    def chat_json(self, messages, schema_hint, **kwargs):
        if ("sub_queries" not in schema_hint and self._reflects
                and callable(self._reflects[0])):
            self._reflects[0] = self._reflects[0](messages[-1]["content"])
        return super().chat_json(messages, schema_hint, **kwargs)


def _llm(*reflects):
    return _PromptReadingLLM(list(reflects) + [ANSWER],
                             plan={"sub_queries": [{"query": Q1}]})


def _run(spec, llm, effort="standard"):
    bind_chat_client(spec.repo, "reasoning_agent", llm)
    retriever = ReasoningRetriever.from_repository(spec.repo, spec.repo.settings)
    return retriever.run(spec.notebook_id, QUESTION, "",
                         limits=ask_retrieval_limits(effort))


_HINT_LINE = re.compile(r"^\[(s\d+)\]《(.+?)》 (.+)（(\d+) 段）$")


def _hint_lines(prompt):
    """``[(sid, source title, label, count)]`` of the hint block, or ``[]``."""
    if HINT_BLOCK_TITLE not in prompt:
        return []
    block = prompt[prompt.index(HINT_BLOCK_TITLE):].split("\n\n")[0].splitlines()[1:]
    parsed = [_HINT_LINE.match(line) for line in block]
    assert all(parsed), block
    return [(m[1], m[2], m[3], int(m[4])) for m in parsed]


def _listing(prompt):
    """The section listing block of a reflect prompt: header + entry lines."""
    start = prompt.index("（章节目录:")
    return prompt[start:].split("\n\n")[0].splitlines()


_LISTED = re.compile(r"^\s*\[(s\d+)\] (.+)（(\d+) 段）$")


def _listed_entries(prompt):
    return [(m[1], m[2]) for m in map(_LISTED.match, _listing(prompt)[1:]) if m]


def _seeded(result):
    """The passages the run's chunk searches brought in (read off the trace:
    a passage a later section read also returns keeps its place but takes the
    ``section_read`` marker too -- ``prefer_stronger_chunk_candidate`` unions
    it -- so the marker cannot tell them apart)."""
    return {cid for step in _steps(result, "search_chunks")
            for cid in step.detail.get("result_ids", ())}


# --------------------------------------------- 1. Q1 hint → read_section(sN)
def test_q1_hint_points_at_5721_and_the_read_survives_synthesis(spec):
    """Production Q1: the first round reaches 1.4, the 5.7 captions and 5.7.3.x
    but not the 5.7.2.1 body. The hint block -- vector ranking through the
    real ports and embedder -- lists 5.7.2.1 and no covered section; the model
    reads it by the id it finds there; the section's passages enter the
    candidates as ``section_read`` and hold seats at the front of the
    synthesis order, whole inside standard's ``chunk_context_chars``."""
    def read_the_mask_hint(prompt):
        return _read(next(sid for sid, source, label, _n in _hint_lines(prompt)
                          if (source, label) == (T20, MASK)))

    llm = _llm(read_the_mask_hint)
    result = _run(spec, llm)
    hints = _hint_lines(llm.reflect_prompts[0])
    seeded = _seeded(result)

    # the hint lists 5.7.2.1, whose body the first round did not reach
    assert (T20, MASK) in {(source, label) for _sid, source, label, _n in hints}
    mask = spec.occurrence(T20, MASK)
    assert not set(mask.chunk_ids) & seeded
    # covered sections are not listed: every hinted occurrence's own chunks are
    # outside the first round, and 1.4 (covered in a revision) is not hinted
    # for that revision
    for _sid, source, label, count in hints:
        candidates = spec.occurrences(source, label)
        assert any(not set(occ.chunk_ids) & seeded
                   and count == len(occ.range_chunk_ids) for occ in candidates), label
        assert label not in NAVIGATION_LABELS
    covered_14 = [title for title in (T20, T30)
                  if set(spec.occurrence(title, "1.4 UCIe Key Performance Targets")
                         .chunk_ids) & seeded]
    assert covered_14, "the Q1 first round reaches 1.4 (the production shape)"
    for title in covered_14:
        assert (title, "1.4 UCIe Key Performance Targets") not in {
            (source, label) for _sid, source, label, _n in hints}

    # the model read it by the id it parsed, and the read found passages
    (read,) = _steps(result, "read_section")
    assert read.detail["section"] == MASK and read.detail["found"] > 0
    # the range in document order, minus what the first round already held
    assert read.detail["result_ids"] == [
        cid for cid in mask.range_chunk_ids if cid not in seeded]
    by_id = {c.chunk_id: c for c in result.chunks}
    assert set(mask.range_chunk_ids) <= set(by_id)
    # every passage of the range carries the read provenance -- the caption
    # the first round had found too (the duplicate's markers are unioned)
    for chunk_id in mask.range_chunk_ids:
        assert by_id[chunk_id].section_read is True
        assert by_id[chunk_id].exact_lookup is False
    assert mask.chunk_ids[0] in read.detail["result_ids"]
    assert not spec.navigation_ids & set(by_id)

    # synthesis order: the read passages hold the section-read seats at the
    # front, and that prefix renders whole inside the standard partition
    limits = ask_retrieval_limits("standard")
    order = reasoning_order_for(spec.repo.settings, result.chunks, spec.notebook_id)
    front = [c.chunk_id for c in order.passages[:order.prefix]]
    assert set(read.detail["result_ids"]) <= set(front)
    floor = passage_floor(order.passages[:order.prefix], limits.chunk_context_chars,
                          heading_chars=0)
    assert 0 < floor < limits.chunk_context_chars // 2
    # the seats are what admits it: with them switched off the same front
    # holds only first-round passages and the 5.7.2.1 body sorts to the tail
    unseated = [c.chunk_id for c in reasoning_order_for(
        _without_section_seats(spec.repo.settings), result.chunks,
        spec.notebook_id).passages]
    assert mask.chunk_ids[0] in front
    assert mask.chunk_ids[0] not in unseated[:order.prefix]
    assert unseated.index(mask.chunk_ids[0]) >= len(unseated) - 2


def _without_section_seats(settings):
    from types import SimpleNamespace

    return SimpleNamespace(**{
        **{name: getattr(settings, name) for name in type(settings).model_fields},
        "reasoning_section_read_reserve": 0})


# ------------------------------- 2. browse under 5.7 → read by title / parent
def test_browse_under_57_then_read_standard_package_by_title_and_57_by_parent(spec):
    spec.repo.settings.reasoning_section_read_max_chunks = 6
    seen = {}

    def read_standard_by_title(prompt):
        seen["listing"] = _listing(prompt)
        return _read(STANDARD, source=T20)

    llm = _llm(_browse(T20, under="5.7"), read_standard_by_title, _read("5.7", source=T20))
    result = _run(spec, llm)

    # the listing in the next prompt is the 5.7 subtree, document order
    header, *lines = seen["listing"]
    assert header.startswith(f"（章节目录:《{T20}》 under 5.7,列出")
    titles = [_LISTED.match(line)[2].strip() for line in lines if _LISTED.match(line)]
    subtree = spec.occurrence(T20, "5.7")
    expected = [occ.title for occ in spec.outlines[T20].occurrences[
        subtree.index:subtree.range_end]]
    assert titles == expected
    assert titles[0] == "5.7" and MASK in titles and STANDARD in titles
    assert "1.4 UCIe Key Performance Targets" not in titles
    assert not set(titles) & set(NAVIGATION_LABELS)
    (browse,) = _steps(result, "browse_sections")
    assert browse.detail["under"] == "5.7" and browse.detail["listed"] == len(titles)

    first, second = _steps(result, "read_section")
    by_id = {c.chunk_id: c for c in result.chunks}
    # 5.7.3 by its title: its whole flat-document range -- Table 5-18 block,
    # the caption leaf, 5.7.3.1, 5.7.3.2 and the notes leaf after them
    standard = spec.occurrence(T20, STANDARD)
    assert first.detail["section"] == STANDARD and "truncated" not in first.detail
    assert set(standard.range_chunk_ids) <= set(by_id)
    seeded = _seeded(result)
    assert first.detail["result_ids"] == [
        cid for cid in standard.range_chunk_ids if cid not in seeded]
    table_518 = standard.chunk_ids[0]
    assert "L(0) > -4.5 L(fN) > -7.5" in by_id[table_518].text
    assert {spec.label_of(cid) for cid in standard.range_chunk_ids} >= {
        "5.7.3.1 Standard Package Loss Budget", "5.7.3.2 Standard Package Crosstalk Budget"}
    # 5.7 through the virtual parent: the 5.7.x range, capped at the first six
    # in document order, the cut disclosed
    assert second.detail["section"] == "5.7" and second.detail["truncated"] is True
    assert second.summary.endswith("(截取本节前 6 段)")
    head = subtree.range_chunk_ids[:6]
    assert len(subtree.range_chunk_ids) > 6
    assert set(head) <= set(by_id)
    assert second.detail["result_ids"] == [
        cid for cid in head
        if cid not in seeded and cid not in first.detail["result_ids"]]
    assert second.detail["found"] > 0
    assert [spec.label_of(cid) for cid in head][:3] == [
        "5.7.1 Voltage Transfer Function",
        "Figure 5-20. Voltage Transfer Function Test Setup", MASK]


# ------------------------------------------------- 3. duplicate headings
def test_a_duplicate_title_is_ambiguous_and_readable_by_its_id(spec):
    picked = {}

    def read_the_second_notes(prompt):
        ids = re.findall(rf"\[(s\d+)\] {NOTES_TITLE}", prompt)
        assert len(ids) == 2, ids
        picked["sid"] = ids[1]
        return _read(ids[1])

    llm = _llm(_read(NOTES_TITLE, source=T20), read_the_second_notes)
    result = _run(spec, llm)

    ambiguous = _skips(result)["section_ambiguous"]
    assert ambiguous.detail["matches"] == 2
    offered = re.findall(rf"\[(s\d+)\] {NOTES_TITLE}", ambiguous.summary)
    assert len(offered) == 2 and picked["sid"] == offered[1]
    (read,) = _steps(result, "read_section")
    first_notes, second_notes = spec.occurrences(T20, NOTES_TITLE)
    assert read.detail["section"] == NOTES_TITLE
    assert read.detail["result_ids"] == list(second_notes.chunk_ids)
    assert not set(first_notes.chunk_ids) & {c.chunk_id for c in result.chunks}


# --------------------------------------------- 4. browse ranked by meaning
def test_browse_query_insertion_loss_ranks_5721_first_in_the_source(spec):
    llm = _llm(_browse(T20, query="insertion loss"))
    result = _run(spec, llm)
    (browse,) = _steps(result, "browse_sections")
    assert browse.detail["query"] == "insertion loss"
    header, *_ = _listing(llm.reflect_prompts[1])
    assert header.startswith(f"（章节目录:《{T20}》 按「insertion loss」排序,列出")
    labels = [label for _sid, label in _listed_entries(llm.reflect_prompts[1])]
    assert MASK in labels[:3], labels[:6]
    assert not set(labels) & set(NAVIGATION_LABELS)
    # a listing is not evidence
    assert not [c for c in result.chunks if c.section_read]


# ------------------------------------- 5. navigation pages, filter on / off
@pytest.mark.parametrize("filter_on", [True, False], ids=["filter-on", "filter-off"])
def test_navigation_pages_never_reach_hints_listings_or_reads(spec, monkeypatch, filter_on):
    """With the read-side switch on, the Contents page is in no hint, no
    listing and no read; switched off it is an outline occurrence again,
    listed and readable -- so the filter-on assertions exercise the filter."""
    if not filter_on:
        monkeypatch.setattr(spec.repo.settings, "retrieval_navigation_filter_enabled", False)
    llm = _llm(_browse(T20), _read("Contents", source=T20))
    result = _run(spec, llm)
    hinted = {label for _sid, _source, label, _n in _hint_lines(llm.reflect_prompts[0])}
    listed = {label.strip() for _sid, label in _listed_entries(llm.reflect_prompts[1])}
    contents = {cid for cid in spec.navigation_ids if spec.label_of(cid) == "Contents"
                and spec.rows[cid]["source_id"] == spec.sources[T20]}
    if filter_on:
        assert hinted and not hinted & set(NAVIGATION_LABELS)
        assert listed and not listed & set(NAVIGATION_LABELS)
        assert _skips(result)["section_not_surfaced"].detail["section"] == "Contents"
        assert not _steps(result, "read_section")
        assert not spec.navigation_ids & {c.chunk_id for c in result.chunks}
    else:
        assert "Contents" in listed
        (read,) = _steps(result, "read_section")
        assert read.detail["section"] == "Contents"
        assert contents and contents <= {c.chunk_id for c in result.chunks}


# ------------------------------------------------------------ 6. gate off
def test_gate_off_leaves_the_prompts_without_any_navigation_trace(spec, monkeypatch):
    """Gate off: no hint block, neither action in rules, schema or ledger, and
    no section port call -- the prompts are the gate-on prompts with exactly
    the navigation additions taken out."""
    calls = []
    retrieval = spec.repo.retrieval
    for name in ("section_outline", "section_chunk_similarities", "section_chunks"):
        method = getattr(retrieval, name)
        monkeypatch.setattr(retrieval, name,
                            lambda *a, _m=method, _n=name: calls.append(_n) or _m(*a))

    on = _llm()
    _run(spec, on)
    assert calls and HINT_BLOCK_TITLE in on.reflect_prompts[0]
    calls.clear()

    spec.repo.settings.reasoning_section_nav_enabled = False
    off = _llm()
    result = _run(spec, off)
    assert calls == []
    for text in off.reflect_prompts + off.schema_hints:
        assert "browse_sections" not in text and "read_section" not in text
        assert HINT_BLOCK_TITLE not in text
    assert not [s for s in result.trace if s.step_type in ("browse_sections", "read_section")]
    assert len(off.reflect_prompts) == len(on.reflect_prompts) == 1
    assert _strip_navigation(on.reflect_prompts[0]) == off.reflect_prompts[0]
    assert off.plan_prompts == on.plan_prompts


def _strip_navigation(prompt):
    """The gate-on reflect prompt minus the navigation additions: the two
    action lines, the scope-word field and the hint block."""
    lines = prompt.split("\n")
    lines = [line for line in lines
             if not line.startswith(("- browse_sections:", "- read_section:"))]
    text = "\n".join(lines).replace(
        "query, keywords and browse_sections.query.", "query and keywords.")
    start = text.index(HINT_BLOCK_TITLE)
    end = text.index("\n\n", start)
    return text[:start] + text[end + 2:]
