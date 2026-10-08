"""PR-E8 (ledger B-12) scenarios shared by SQLite (``test_promotion_provenance.py``)
and PostgreSQL (``postgres/test_promotion_provenance_pg.py``).

The world: a public library ``base``; the promoter's private notebook
``private`` (mounting ``base``) with one document ``s-priv`` titled
``ORIGIN_TITLE`` whose first element is quoted by a claim; that claim is
proposed and approved into ``base``.  A third notebook ``mine`` mounts ``base``
and asks.  Every check talks to the real repository; ``sql`` adapts the
placeholder style of the few raw statements (``?`` -> ``%s`` on PostgreSQL).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from app.domain.promotion_provenance import (
    PROMOTION_SOURCE_TYPE,
    memory_origin_key,
    promotion_source_id,
    source_origin_key,
)
from app.models.ask import AskRequest
from app.models.notebooks import NotebookCreate

NOW = "2026-10-01T00:00:00+00:00"
TERM = "低温增益"
ORIGIN_TITLE = "私有原件"
LIVE_TEXT = f"{TERM} 实验记录 当前原文"
STORED_QUOTE = f"{TERM} 实验记录"
CLAIM = f"{TERM}论断"


@dataclass
class World:
    repo: Any
    sql: Callable[[str], str]
    user_id: str
    base: str
    private: str
    mine: str
    object_id: str
    proposal_id: str
    approved: dict

    @property
    def promotion_source(self) -> str:
        return promotion_source_id(self.base, source_origin_key("s-priv"))

    def rows(self, statement: str, params=()) -> list[dict]:
        with self.repo._runtime.database.connect() as db:
            return [dict(row) for row in db.execute(self.sql(statement), params).fetchall()]

    def write(self, statement: str, params=()) -> None:
        with self.repo._runtime.database.write() as db:
            db.execute(self.sql(statement), params)

    def evidence(self, object_id: str) -> list:
        (row,) = self.rows("SELECT evidence FROM knowledge_objects WHERE id=?", (object_id,))
        value = row["evidence"]
        return json.loads(value) if isinstance(value, str) else value


def seed_document(repo, sql, notebook_id: str, source_id: str, title: str, texts) -> list[str]:
    with repo._runtime.database.write() as db:
        db.execute(
            sql("INSERT INTO sources (id,notebook_id,title,source_type,status,"
                "parse_status,created_at,updated_at) VALUES (?,?,?,'markdown','parsed',"
                "'parsed',?,?)"),
            (source_id, notebook_id, title, NOW, NOW),
        )
        for index, text in enumerate(texts):
            db.execute(
                sql("INSERT INTO source_elements (id,source_id,element_type,"
                    "location_label,text,metadata,created_at) VALUES (?,?,'paragraph',?,?,"
                    "'{}',?)"),
                (f"{source_id}-{index:03}", source_id, f"第{index + 1}节", text, NOW),
            )
    return [f"{source_id}-{index:03}" for index in range(len(texts))]


def evidence(source_id: str, element_id: str, quote: str, title: str = ORIGIN_TITLE) -> dict:
    return {"source_id": source_id, "source_title": title, "element_id": element_id,
            "element_type": "paragraph", "location_label": "第1节",
            "quoted_span": quote, "confidence": 1.0}


def object_id_named(repo, sql, notebook_id: str, name: str) -> str:
    with repo._runtime.database.connect() as db:
        for row in db.execute(
            sql("SELECT id,payload FROM knowledge_objects WHERE notebook_id=?"),
            (notebook_id,),
        ).fetchall():
            payload = row["payload"]
            payload = json.loads(payload) if isinstance(payload, str) else payload
            if payload.get("name") == name:
                return str(row["id"])
    raise KeyError(name)


def build(repo, sql, *, approve: bool = True) -> World:
    user_id = repo.current_user().id
    base = repo.create_notebook(NotebookCreate(name="公共库")).id
    repo.mark_notebook_base(base)
    private = repo.create_notebook(NotebookCreate(name="推广者私有库")).id
    mine = repo.create_notebook(NotebookCreate(name="我的笔记本")).id
    elements = seed_document(repo, sql, private, "s-priv", ORIGIN_TITLE, [
        LIVE_TEXT, f"{TERM} 结论",
    ])
    repo.store_kg(private, "s-priv", [
        {"local_id": "A", "object_type": "claim",
         "payload": {"name": CLAIM, "section_path": "1"},
         "evidence": [evidence("s-priv", elements[0], STORED_QUOTE)]},
    ], [])
    repo.replace_notebook_bases(private, [base], user_id)
    repo.replace_notebook_bases(mine, [base], user_id)
    object_id = object_id_named(repo, sql, private, CLAIM)
    proposal = repo.propose_promotion(private, object_id)
    approved = repo.approve_promotion(proposal["id"]) if approve else {}
    return World(repo, sql, user_id, base, private, mine, object_id,
                 proposal["id"], approved)


# ---------------------------------------------------------------------------
# write side
# ---------------------------------------------------------------------------

def approval_writes_library_owned_provenance(world: World) -> None:
    """The base object's evidence names the public library's own promotion
    source and element; the original stays only as display keys; the element
    text is the original element's CURRENT text; the reverse index follows."""
    (base_object,) = world.approved["base_object_ids"]
    (entry,) = world.evidence(base_object)
    assert entry["source_id"] == world.promotion_source
    assert entry["origin_source_id"] == "s-priv"
    assert entry["origin_notebook_id"] == world.private
    assert entry["source_title"] == ORIGIN_TITLE
    assert entry["quoted_span"] == STORED_QUOTE
    (source,) = world.rows(
        "SELECT notebook_id,title,source_type,status,parse_status FROM sources "
        "WHERE id=?", (world.promotion_source,))
    assert source == {
        "notebook_id": world.base, "title": f"晋升自：{ORIGIN_TITLE}",
        "source_type": PROMOTION_SOURCE_TYPE, "status": "active",
        # terminal: the browser never polls it as "still processing"
        "parse_status": "extracted",
    }
    (element,) = world.rows(
        "SELECT id,source_id,element_type,text FROM source_elements WHERE source_id=?",
        (world.promotion_source,))
    assert element["id"] == entry["element_id"]
    assert element["text"] == LIVE_TEXT
    assert element["element_type"] == "paragraph"
    index = world.rows(
        "SELECT source_id FROM knowledge_object_sources WHERE object_id=?", (base_object,))
    assert [row["source_id"] for row in index] == [world.promotion_source]
    # the source shows up in the public library's own (visible) listing
    listed = {source.id: source for source in world.repo.list_sources(world.base)}
    assert listed[world.promotion_source].type == PROMOTION_SOURCE_TYPE


def a_second_promotion_from_the_same_original_reuses_its_source(world: World) -> None:
    repo, sql = world.repo, world.sql
    repo.store_kg(world.private, "s-priv", [
        {"local_id": "A", "object_type": "claim",
         "payload": {"name": CLAIM, "section_path": "1"},
         "evidence": [evidence("s-priv", "s-priv-000", STORED_QUOTE)]},
        {"local_id": "B", "object_type": "claim",
         "payload": {"name": f"{TERM}第二论断", "section_path": "2"},
         "evidence": [evidence("s-priv", "s-priv-000", STORED_QUOTE),
                      evidence("s-priv", "s-priv-001", f"{TERM} 结论")]},
    ], [])
    second = object_id_named(repo, sql, world.private, f"{TERM}第二论断")
    proposal = repo.propose_promotion(world.private, second)
    (base_object,) = repo.approve_promotion(proposal["id"])["base_object_ids"]
    entries = world.evidence(base_object)
    assert {entry["source_id"] for entry in entries} == {world.promotion_source}
    sources = world.rows(
        "SELECT id FROM sources WHERE notebook_id=? AND source_type=?",
        (world.base, PROMOTION_SOURCE_TYPE))
    assert [row["id"] for row in sources] == [world.promotion_source]
    elements = world.rows(
        "SELECT id FROM source_elements WHERE source_id=?", (world.promotion_source,))
    # the element of s-priv-000 is shared with the first promotion
    assert len(elements) == 2
    assert {entry["element_id"] for entry in entries} == {row["id"] for row in elements}


def _merge_into_native(world: World):
    """A native public object (its own document ``s-pub``) that a promotion
    from ``s-priv`` is merged into; returns ``(object id, native entry,
    evidence before)``."""
    repo, sql = world.repo, world.sql
    native = seed_document(repo, sql, world.base, "s-pub", "公共原件", [f"{TERM} 公共原文"])
    native_entry = evidence("s-pub", native[0], f"{TERM} 公共原文", title="公共原件")
    repo.store_kg(world.base, "s-pub", [
        {"local_id": "P", "object_type": "claim",
         "payload": {"name": f"{TERM}合并论断", "section_path": "1"},
         "evidence": [native_entry]},
    ], [])
    public_object = object_id_named(repo, sql, world.base, f"{TERM}合并论断")
    before = world.evidence(public_object)
    repo.store_kg(world.private, "s-priv", [
        {"local_id": "M", "object_type": "claim",
         "payload": {"name": f"{TERM}合并论断", "section_path": "1"},
         "evidence": [evidence("s-priv", "s-priv-001", f"{TERM} 结论")]},
    ], [])
    incoming = object_id_named(repo, sql, world.private, f"{TERM}合并论断")
    proposal = repo.propose_promotion(world.private, incoming)
    result = repo.approve_promotion(proposal["id"])
    assert result["merged_into"] == public_object
    return public_object, native_entry, before


def a_merge_rewrites_only_the_incoming_entries(world: World) -> None:
    """Promoting onto an existing public object (same seed) keeps that
    object's own evidence byte for byte and rewrites only the new entries."""
    public_object, _native, before = _merge_into_native(world)
    after = world.evidence(public_object)
    assert after[0] == before[0]
    assert after[1]["source_id"] == world.promotion_source
    assert after[1]["origin_source_id"] == "s-priv"
    index = {row["source_id"] for row in world.rows(
        "SELECT source_id FROM knowledge_object_sources WHERE object_id=?",
        (public_object,))}
    assert index == {"s-pub", world.promotion_source}


def a_memory_promotion_is_titled_after_the_memory(world: World) -> None:
    """A Memory promotion publishes one "晋升自个人记忆：<title>" source; its
    cards (the Memory's verified citations) become its elements."""
    from app.models.schemas import AskResponse, Citation
    from tests.answer_owner_testkit import save_owned_answer

    repo = world.repo
    answer_id = save_owned_answer(
        repo, world.private, world.user_id, "低温增益是什么？",
        AskResponse(
            conclusion=LIVE_TEXT, answer=LIVE_TEXT,
            citations=[Citation(label="k1", source_id="s-priv",
                                element_id="s-priv-000", location_label="第1节",
                                quoted_span="client supplied")],
        ),
    )
    memory = repo.create_memory_from_answer(
        world.private, world.user_id, answer_id, "增益记忆", LIVE_TEXT, [],
    )
    proposal = repo.propose_memory_promotion(memory.id, world.user_id)
    # the cited element changes after the member proposed: the approval
    # publishes the card the member approved, never the element's live text
    world.write("UPDATE source_elements SET text=? WHERE id='s-priv-000'",
                ("CHANGED LIVE TEXT",))
    result = repo.approve_promotion(proposal["id"])
    expected = promotion_source_id(world.base, memory_origin_key(memory.id))
    (source,) = world.rows("SELECT title,source_type FROM sources WHERE id=?", (expected,))
    assert source == {"title": "晋升自个人记忆：增益记忆",
                      "source_type": PROMOTION_SOURCE_TYPE}
    for base_object in result["base_object_ids"]:
        entries = world.evidence(base_object)
        assert entries and {entry["source_id"] for entry in entries} == {expected}
        assert all(entry["origin_source_id"] == "s-priv" for entry in entries)
    (element,) = world.rows("SELECT text FROM source_elements WHERE source_id=?", (expected,))
    assert element["text"] == LIVE_TEXT


def a_memory_original_is_dropped_by_the_approval_store(world: World) -> None:
    """M1, fail closed: an entry whose original is a member's Memory source is
    dropped by the approval-side planner -- neither its element's text nor
    its stored excerpt is published (the approval paths refuse such objects
    anyway; this is the store's own floor)."""
    repo = world.repo
    world.write(
        "INSERT INTO memory_items (id,notebook_id,created_by,origin,status,title,"
        "content_md,created_at,updated_at) VALUES ('mem-floor',?,?,'ask_answer',"
        "'confirmed','私人记忆','x',?,?)", (world.private, world.user_id, NOW, NOW))
    world.write(
        "INSERT INTO sources (id,notebook_id,title,source_type,memory_id,created_at,"
        "updated_at) VALUES ('s-pmem',?,'私人记忆','memory','mem-floor',?,?)",
        (world.private, NOW, NOW))
    world.write(
        "INSERT INTO source_elements (id,source_id,element_type,location_label,text,"
        "metadata,created_at) VALUES ('el-pmem','s-pmem','paragraph','p1',"
        "'PRIVATE MEMORY TEXT','{}',?)", (NOW,))
    if type(repo).__name__ == "PostgresRepository":
        from app.repositories.postgres import promotion_provenance_store as store
    else:
        from app.repositories.sqlite import promotion_provenance_store as store
    with repo._runtime.database.connect() as db:
        plan = store.plan_for_library(db, world.base, [
            evidence("s-pmem", "el-pmem", "memory excerpt"),
            evidence("s-priv", "s-priv-000", STORED_QUOTE),
        ])
    assert [entry["origin_source_id"] for entry in plan.evidence] == ["s-priv"]
    assert (plan.rewritten, plan.dropped) == (1, 1)
    assert all("PRIVATE MEMORY" not in row.text and row.text != "memory excerpt"
               for row in plan.elements)


def deleting_the_promotion_source_deletes_the_objects_it_supports(world: World) -> None:
    (base_object,) = world.approved["base_object_ids"]
    world.repo.delete_source(world.promotion_source)
    assert world.rows("SELECT id FROM knowledge_objects WHERE id=?", (base_object,)) == []
    assert world.rows(
        "SELECT id FROM source_elements WHERE source_id=?", (world.promotion_source,)) == []


def deleting_the_promotion_source_keeps_a_native_object_merged_into(world: World) -> None:
    """A native public object a promotion was merged into survives the
    deletion of the promotion source: it loses exactly that source's entries
    (and their reverse-index rows); only an object left without evidence --
    the approved copy -- is deleted."""
    public_object, native_entry, _before = _merge_into_native(world)
    (promoted_copy,) = world.approved["base_object_ids"]
    world.repo.delete_source(world.promotion_source)
    assert world.evidence(public_object) == [native_entry]
    index = {row["source_id"] for row in world.rows(
        "SELECT source_id FROM knowledge_object_sources WHERE object_id=?",
        (public_object,))}
    assert index == {"s-pub"}
    assert world.rows("SELECT id FROM knowledge_objects WHERE id=?", (promoted_copy,)) == []
    assert world.rows("SELECT id FROM sources WHERE id=?", (world.promotion_source,)) == []


def the_promotion_source_is_never_a_pipeline_target(world: World) -> None:
    """Visible, but never handed to KG extraction (incremental or rebuild),
    never pending, no paper metadata, no missing-chunk checkup item."""
    repo = world.repo
    runtime = repo._runtime
    with runtime.database.connect() as db:
        build_rows = runtime.knowledge.source_build_state_page(
            db, world.base, None, "", 100)
        build_ids, _with_kg = runtime.knowledge.source_build_rows(db, world.base)
        pending = runtime.queries.pending_kg_source_count(db, world.base)
        visible_pending = runtime.queries.visible_pending_kg_source_count(db, world.base)
        missing_chunks = runtime.queries.sources_missing_chunks(db, world.base)
    assert world.promotion_source not in {str(row["id"]) for row in build_rows}
    assert world.promotion_source not in set(build_ids)
    assert (pending, visible_pending) == (0, 0)
    assert world.promotion_source not in missing_chunks
    targets = repo.maintenance.kg_target_source_rows_page(world.base)
    assert world.promotion_source not in {row["source_id"] for row in targets}
    assert repo.maintenance.count_sources_missing_kg(world.base) == 0
    assert world.promotion_source not in repo.maintenance.paper_metadata_source_ids_page(
        world.base)
    summary = {source.id: source for source in repo.list_sources(world.base)}[
        world.promotion_source]
    assert summary.paper_meta_status is None
    # the offline re-extraction / chunk-build roster and the element-vector
    # backfill leave it out, and extraction itself refuses it before clearing
    # anything (the one gate every extraction caller passes)
    assert world.promotion_source not in repo.maintenance.user_source_ids_page(world.base)
    assert world.promotion_source not in repo.maintenance.missing_element_vector_source_ids(
        world.base)
    from app.domain.promotion_source import PromotionSourceNotExtractable

    (base_object,) = world.approved["base_object_ids"]
    try:
        repo.extract_source(world.promotion_source)
    except PromotionSourceNotExtractable:
        pass
    else:
        raise AssertionError("extract_source accepted a promotion source")
    assert world.rows("SELECT id FROM knowledge_objects WHERE id=?", (base_object,))
    # it never takes an uploaded-document slot, while the list still counts it
    assert repo.visible_document_count(world.base) == 0
    assert repo.list_sources_page(world.base).total_count == 1


def the_command_catalog_and_library_profile_skip_the_promotion_source(
    world: World,
) -> None:
    """Two more document pipelines leave it out: the command catalog answers
    it like a missing source (its parse status is terminal, so the parsed
    gate alone would let it through), and the library profile's corpus
    statistics -- documents, parse-status counts, the live-evidence set --
    describe the uploaded documents only. The public library here holds the
    promotion source and nothing else."""
    repo = world.repo
    try:
        repo.command_catalog.preview(world.base, world.promotion_source)
    except KeyError:
        pass
    else:
        raise AssertionError("the command catalog read a promotion source")
    stats = repo._runtime.agent_profile_jobs.corpus_stats(world.base)
    assert stats.documents == 0
    assert world.promotion_source not in stats.visible_ids
    assert (stats.documents_parse_failed, stats.documents_not_parsed) == (0, 0)


def the_source_summary_says_whether_its_objects_are_still_in_the_graph(
    world: World,
) -> None:
    """The badge signal: a promotion source's ``kg_extracted`` is "some
    object's evidence still cites it" (single and page summaries alike)."""
    repo = world.repo

    def summaries():
        listed = {source.id: source for source in repo.list_sources(world.base)}
        return (listed[world.promotion_source].kg_extracted,
                repo.get_source(world.promotion_source).kg_extracted)

    assert summaries() == (True, True)
    (base_object,) = world.approved["base_object_ids"]
    world.write("DELETE FROM knowledge_object_sources WHERE object_id=?", (base_object,))
    world.write("DELETE FROM knowledge_objects WHERE id=?", (base_object,))
    assert summaries() == (False, False)


def the_answer_context_reads_the_entry_as_the_librarys_own(world: World) -> None:
    """The E2-4 snapshot path (an entry owned by another library) no longer
    fires for a rewritten entry: it keeps its pointers into the public
    library, is named after the original, and its source is one of the
    library's visible sources (E7b ``visible_source_owners``)."""
    from app.models.common import Evidence
    from app.services.retrieval import RetrievedKnowledge

    (base_object,) = world.approved["base_object_ids"]
    (entry,) = world.evidence(base_object)
    hit = RetrievedKnowledge(
        object_id=base_object, object_type="claim", payload={"name": CLAIM},
        evidence=[Evidence(**{key: entry[key] for key in (
            "source_id", "source_title", "element_id", "element_type",
            "location_label", "quoted_span", "confidence")})],
    )
    _block, id_map = world.repo._answer_context(world.base, [hit])
    (value,) = id_map.values()
    assert value["source_id"] == world.promotion_source
    assert value["element_id"] == entry["element_id"]
    assert value["source_title"] == ORIGIN_TITLE
    assert not value.get("source_foreign")
    owners = world.repo._runtime.source_store.visible_source_owners(
        [world.promotion_source])
    assert owners == {world.promotion_source: world.base}


# ---------------------------------------------------------------------------
# acceptance: recalled and cited under every ceiling
# ---------------------------------------------------------------------------

def _references(response) -> list:
    return [*response.citations, *response.anchors]


def _promotion_references(world: World, response) -> list:
    return [ref for ref in _references(response)
            if ref.source_id == world.promotion_source]


def single_notebook_ask_through_a_mount_cites_the_promoted_object(world: World) -> None:
    """``mine`` mounts ``base``: the mount opens only base's VISIBLE sources.
    The promoted object is recalled and cited, its card opens base's promotion
    source and is named after the original; nothing points into ``private``."""
    from tests import global_citation_e2e_kit as kit

    agent = kit.ScriptedAgent(plan_query=TERM)
    answerer = kit.bind_models(world.repo, agent)
    response = world.repo._runtime.ask_service().ask(
        world.mine, AskRequest(question=f"{TERM}的结论", mode="reasoning"),
        user_id=world.user_id,
    )
    assert any(CLAIM in prompt for prompt in answerer.prompts), (
        "the promoted object reached the answer model")
    cited = _promotion_references(world, response)
    assert cited, [ref.model_dump() for ref in _references(response)]
    assert all(ref.notebook_id == world.base for ref in cited)
    labels = [getattr(ref, "label", "") or getattr(ref, "source_title", "")
              for ref in cited]
    assert any(ORIGIN_TITLE in label for label in labels), labels
    assert not any(label.startswith("晋升自") for label in labels), labels
    for ref in _references(response):
        assert ref.source_id != "s-priv"
        assert not str(ref.element_id or "").startswith("s-priv")


def global_ask_cites_the_promoted_object(world: World) -> None:
    from tests import global_citation_e2e_kit as kit

    agent = kit.ScriptedAgent(plan_query=TERM)
    kit.bind_models(world.repo, agent)
    result = kit.global_answer(world.repo, [world.base], f"{TERM}的结论")
    assert result.status == "done", result.error
    cited = _promotion_references(world, result.answer)
    assert cited, [ref.model_dump() for ref in _references(result.answer)]
    assert all(ref.notebook_id == world.base for ref in cited)
    labels = [getattr(ref, "label", "") or getattr(ref, "source_title", "")
              for ref in cited]
    assert any(ORIGIN_TITLE in label for label in labels), labels
    assert not any(label.startswith("晋升自") for label in labels), labels
    assert "citation_check" not in result.answer.model_dump(mode="json")


def the_promoters_private_notebook_can_go(world: World) -> None:
    """Deleting the promoter's private notebook leaves the promoted object
    whole: its evidence, source and element live in the public library."""
    (base_object,) = world.approved["base_object_ids"]
    before = world.evidence(base_object)
    world.repo.delete_notebook(world.private)
    assert world.rows("SELECT id FROM notebooks WHERE id=?", (world.private,)) == []
    assert world.evidence(base_object) == before
    single_notebook_ask_through_a_mount_cites_the_promoted_object(world)
