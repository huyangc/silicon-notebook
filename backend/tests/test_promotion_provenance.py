"""PR-E8 (ledger B-12): a promoted object carries the public library's own
provenance -- SQLite side.  The scenarios live in ``promotion_provenance_cases``
(PostgreSQL twin: ``postgres/test_promotion_provenance_pg.py``); the pure rule's
edge cases are pinned here on ``app.domain.promotion_provenance`` directly.

Acceptance (plan §5 PR-E8):
* a public library mounted on a personal notebook: the single-notebook ask and
  the global ask both recall and cite the promoted object, and the card opens
  the library's promotion source;
* deleting the promoter's private notebook leaves the promoted object usable;
* migration before/after equality is pinned in ``test_promotion_provenance_migration.py``.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.domain.promotion_provenance import (
    OriginElement,
    plan_promotion_evidence,
    promotion_element_id,
    promotion_origin_title,
    promotion_source_id,
)
from tests import global_citation_e2e_kit as kit
from tests import promotion_provenance_cases as cases


def _sql(statement: str) -> str:
    return statement


@pytest.fixture
def world(tmp_path, monkeypatch):
    repo = kit.make_repo(tmp_path, monkeypatch)
    try:
        yield cases.build(repo, _sql)
    finally:
        repo.close()


def test_approval_writes_library_owned_provenance(world):
    cases.approval_writes_library_owned_provenance(world)


def test_a_second_promotion_from_the_same_original_reuses_its_source(world):
    cases.a_second_promotion_from_the_same_original_reuses_its_source(world)


def test_a_merge_rewrites_only_the_incoming_entries(world):
    cases.a_merge_rewrites_only_the_incoming_entries(world)


def test_a_memory_promotion_is_titled_after_the_memory(world):
    cases.a_memory_promotion_is_titled_after_the_memory(world)


def test_deleting_the_promotion_source_deletes_the_objects_it_supports(world):
    cases.deleting_the_promotion_source_deletes_the_objects_it_supports(world)


def test_the_promotion_source_is_never_a_pipeline_target(world):
    cases.the_promotion_source_is_never_a_pipeline_target(world)


def test_the_command_catalog_and_library_profile_skip_the_promotion_source(world):
    cases.the_command_catalog_and_library_profile_skip_the_promotion_source(world)


def test_a_memory_original_is_dropped_by_the_approval_store(world):
    cases.a_memory_original_is_dropped_by_the_approval_store(world)


def test_deleting_the_promotion_source_keeps_a_native_object_merged_into(world):
    cases.deleting_the_promotion_source_keeps_a_native_object_merged_into(world)


def test_the_source_summary_says_whether_its_objects_are_still_in_the_graph(world):
    cases.the_source_summary_says_whether_its_objects_are_still_in_the_graph(world)


def test_the_answer_context_reads_the_entry_as_the_librarys_own(world):
    cases.the_answer_context_reads_the_entry_as_the_librarys_own(world)


def test_single_notebook_ask_through_a_mount_cites_the_promoted_object(world):
    cases.single_notebook_ask_through_a_mount_cites_the_promoted_object(world)


def test_global_ask_cites_the_promoted_object(world):
    cases.global_ask_cites_the_promoted_object(world)


def test_the_promoters_private_notebook_can_go(world):
    cases.the_promoters_private_notebook_can_go(world)


# ---------------------------------------------------------------------------
# re-parse is refused for a promotion source
# ---------------------------------------------------------------------------

@pytest.fixture
def client_world(tmp_path, monkeypatch):
    from app.api.deps import repository
    from app.core.config import get_settings
    from app.main import create_app

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'api.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    get_settings.cache_clear()
    repository.cache_clear()
    client = TestClient(create_app())
    repo = repository()
    try:
        yield client, cases.build(repo, _sql)
    finally:
        repo.close()
        repository.cache_clear()
        get_settings.cache_clear()


def test_reparse_routes_refuse_a_promotion_source(client_world, monkeypatch):
    from app.api import source_routes

    client, world = client_world
    submitted: list = []
    monkeypatch.setattr(
        source_routes.kg_scheduler, "submit_job",
        lambda fn, *args, **kwargs: submitted.append(args),
    )
    single = client.post(f"/api/sources/{world.promotion_source}/parse")
    assert single.status_code == 409, single.text
    assert single.json()["detail"] == "这份来源是收录到公共知识库的内容，不能重新解析。"
    batch = client.post(
        f"/api/notebooks/{world.base}/sources/reparse",
        json={"source_ids": [world.promotion_source]},
    )
    assert batch.status_code == 200, batch.text
    assert batch.json()["scheduled"] == []
    assert submitted == []
    # the promoted object is untouched
    (base_object,) = world.approved["base_object_ids"]
    assert world.evidence(base_object)[0]["source_id"] == world.promotion_source


def test_command_catalog_routes_answer_a_promotion_source_as_missing(client_world):
    client, world = client_world
    response = client.get(
        f"/api/notebooks/{world.base}/sources/{world.promotion_source}"
        "/command-catalog/preview"
    )
    assert response.status_code == 404, response.text


def test_promotion_sources_never_take_an_uploaded_document_slot(tmp_path, monkeypatch):
    """A public library owned by an ordinary user (an admin may publish one)
    keeps its owner's document limit: 15 uploads plus 6 promotion sources
    from other people's contributions, and the owner can still add one more
    (the limit is 20); the list's total still counts all 22, and its
    ``visible_document_count`` -- what the upload gate and the 「文档 X / 上限」
    indicator read -- says 16 (a filtered page carries none)."""
    from app.api import deps, source_routes
    from app.core.config import get_settings
    from app.main import create_app

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'cap.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("USER_UPLOAD_DOCUMENT_LIMIT", "20")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    get_settings.cache_clear()
    deps.repository.cache_clear()
    monkeypatch.setattr(source_routes.kg_scheduler, "submit_job", lambda fn, *a, **k: None)
    client = TestClient(create_app())
    try:
        client.post("/api/auth/register", json={"username": "c00123456", "password": "pw"})
        token = client.post(
            "/api/auth/login", json={"username": "c00123456", "password": "pw"}
        ).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        nb_id = client.post("/api/notebooks", json={"name": "n"}, headers=headers).json()["id"]

        def import_files(n, start=0):
            return client.post(
                f"/api/notebooks/{nb_id}/sources/import",
                json={"files": [{"file_name": f"f{start + i}.pdf"} for i in range(n)]},
                headers=headers,
            )

        assert import_files(15).status_code == 200
        repo = deps.repository()
        with repo._write() as db:
            for index in range(6):
                db.execute(
                    "INSERT INTO sources (id,notebook_id,title,source_type,status,"
                    "parse_status,created_at,updated_at) VALUES (?,?,?,'promotion',"
                    "'active','extracted','t','t')",
                    (f"src-promo-cap-{index}", nb_id, f"晋升自：原件{index}"),
                )
        assert repo.visible_document_count(nb_id) == 15
        response = import_files(1, start=15)
        assert response.status_code == 200, response.text
        listing = client.get(f"/api/notebooks/{nb_id}/sources", headers=headers).json()
        assert listing["total_count"] == 22
        assert listing["visible_document_count"] == 16
        filtered = client.get(
            f"/api/notebooks/{nb_id}/sources", params={"q": "f1"}, headers=headers
        ).json()
        assert filtered["visible_document_count"] is None
    finally:
        deps.repository.cache_clear()
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# the pure rule
# ---------------------------------------------------------------------------

def _plan(evidence, **kwargs):
    defaults = dict(own_source_ids=set(), source_notebooks={}, origin_elements={})
    defaults.update(kwargs)
    return plan_promotion_evidence("nb-base", evidence, **defaults)


def test_an_entry_with_neither_live_text_nor_a_quote_is_dropped():
    plan = _plan([
        {"source_id": "s-gone", "element_id": "el-gone", "quoted_span": "",
         "source_title": "T"},
        {"source_id": "s-gone", "element_id": "el-gone2", "quoted_span": "kept",
         "source_title": "T"},
    ])
    assert [entry["quoted_span"] for entry in plan.evidence] == ["kept"]
    (source,) = plan.sources
    assert source.title == "晋升自：T"
    assert plan.evidence[0]["origin_notebook_id"] == ""


def test_live_text_wins_only_for_the_element_of_the_named_source():
    plan = _plan(
        [{"source_id": "s-a", "element_id": "el-1", "quoted_span": "stored",
          "source_title": "A"}],
        source_notebooks={"s-a": "nb-private"},
        origin_elements={"el-1": OriginElement("s-other", "SOMEONE ELSE'S TEXT")},
    )
    (element,) = plan.elements
    assert element.text == "stored"
    plan = _plan(
        [{"source_id": "s-a", "element_id": "el-1", "quoted_span": "",
          "source_title": "A"}],
        source_notebooks={"s-a": "nb-private"},
        origin_elements={"el-1": OriginElement("s-a", "x" * 800)},
    )
    (entry,) = plan.evidence
    assert entry["quoted_span"] == "x" * 500
    assert entry["origin_notebook_id"] == "nb-private"
    source_id = promotion_source_id("nb-base", "s-a")
    assert entry["element_id"] == promotion_element_id(source_id, "el-1", "x" * 800)


def test_own_entries_and_non_dict_items_are_kept_in_place():
    own = {"source_id": "s-own", "element_id": "el-own", "quoted_span": "q"}
    plan = _plan(["legacy string", own], own_source_ids={"s-own"})
    assert plan.evidence == ["legacy string", own]
    assert plan.sources == () and plan.elements == ()


def test_the_rewrite_is_idempotent():
    first = _plan([{"source_id": "s-a", "element_id": "el-1", "quoted_span": "q",
                    "source_title": "A"}])
    again = _plan(first.evidence, own_source_ids={first.sources[0].id})
    assert again.evidence == first.evidence
    assert again.sources == ()


def test_a_memory_original_is_dropped_and_a_memory_card_keeps_its_quote():
    entries = [
        {"source_id": "s-mem", "element_id": "el-m", "quoted_span": "mem",
         "source_title": "M"},
        {"source_id": "s-a", "element_id": "el-1", "quoted_span": "card",
         "source_title": "A"},
    ]
    live = {"el-1": OriginElement("s-a", "LIVE"), "el-m": OriginElement("s-mem", "MEM")}
    generic = _plan(entries, origin_elements=live, memory_source_ids={"s-mem"})
    assert [entry["origin_source_id"] for entry in generic.evidence] == ["s-a"]
    assert (generic.rewritten, generic.dropped) == (1, 1)
    assert [row.text for row in generic.elements] == ["LIVE"]
    memory = _plan(entries, origin_elements=live, memory_source_ids={"s-mem"},
                   memory=("mem-1", "记忆"))
    assert [row.text for row in memory.elements] == ["card"]
    assert memory.sources[0].title == "晋升自个人记忆：记忆"


def test_the_card_title_is_the_original():
    assert promotion_origin_title("晋升自：原件") == "原件"
    assert promotion_origin_title("晋升自个人记忆：记忆") == "记忆"
    assert promotion_origin_title("改过名") == "改过名"
