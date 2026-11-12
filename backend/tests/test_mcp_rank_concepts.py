"""Concept rankings through the official MCP client; local SQLite, no models."""
from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.api.deps import repository
from app.api.mcp_server import TOOL_TIERS
from app.api.mcp_tools._shared import TOTAL_TEXT_LIMIT, _serialized_size
from app.api.mcp_tools.refs import decode_ref
from app.repositories.read_budget import ReadBudgetExceeded
from app.services.concept_ranking import (
    ConceptRankComputation,
    ConceptRankEvidence,
    ConceptRankPage,
    ConceptRankRow,
    LibraryRankCoverage,
    LibraryRanking,
)
from app.services.concept_ranking_paging import rank_fingerprint, rank_next_cursor
from app.services.source_scope import memory_channel_allowed
from tests import test_memory_mcp as mcp_world
from tests.test_memory_mcp import OfficialMcpClient, _error, _error_code, _payload

NOW = "2026-11-12T00:00:00+00:00"


@pytest.fixture
def mcp_env(tmp_path, tmp_path_factory, monkeypatch):
    root, ids = mcp_world._mcp_template(tmp_path_factory).materialize(tmp_path)
    return mcp_world._open_mcp_world(root, ids, monkeypatch)


def _seed(repo, nb, count=25):
    with repo._write() as db:
        for index in range(count):
            sid, oid, eid = f"rank-src-{index:02}", f"rank-obj-{index:02}", f"rank-el-{index:02}"
            db.execute(
                "INSERT INTO sources(id,notebook_id,title,source_type,status,parse_status,created_at,updated_at) "
                "VALUES (?,?,?,'markdown','ready','parsed',?,?)", (sid, nb, sid, NOW, NOW),
            )
            db.execute(
                "INSERT INTO source_elements(id,source_id,element_type,location_label,text,metadata,created_at) "
                "VALUES (?,?,'paragraph','第 1 段',?,'{}',?)", (eid, sid, f"证据 {index}", NOW),
            )
            db.execute(
                "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,payload,evidence,source_id,created_at,updated_at) "
                "VALUES (?,?,'concept','approved',?,?,?,?,?)",
                (oid, nb, json.dumps({"name": f"概念 {index:02}"}),
                 json.dumps([{"source_id": sid, "element_id": eid}]), sid, NOW, NOW),
            )
        repo._runtime.unified_kg.mark_dirty(db, nb, NOW)


@pytest.mark.anyio
async def test_read_tier_sorted_rows_and_resolvable_refs(mcp_env, monkeypatch):
    repo, nb = repository(), mcp_env["notebook"].id
    _seed(repo, nb, 3)
    monkeypatch.setattr(repo.retrieval, "retrieve_scored", Mock(side_effect=AssertionError("no query")))
    assert TOOL_TIERS["rank_concepts"] == "read"
    async with OfficialMcpClient(mcp_env["app"], mcp_env["restricted"].token) as client:
        assert "rank_concepts" in {tool.name for tool in (await client.session.list_tools()).tools}
        page = _payload(await client.call("rank_concepts"))
        assert [row["name"] for row in page["rows"]] == [f"概念 {i:02}" for i in range(3)]
        assert [row["rank"] for row in page["rows"]] == [1, 2, 3]
        assert all(row["source_count"] == row["object_count"] == 1 for row in page["rows"])
        assert page["coverage"]["exact"] and page["next_cursor"] == ""
        for row in page["rows"]:
            locator = page["refs"][row["refs"][0]]
            result = _payload(await client.call("read_reference", {"ref": locator}))
            assert decode_ref(locator)[1]["e"] in json.dumps(result)


@pytest.mark.anyio
@pytest.mark.parametrize("arguments", [{"limit": 0}, {"limit": 21}, {"query": "长" * 201}])
async def test_invalid_parameters_are_rejected(mcp_env, arguments, monkeypatch):
    from app.api.mcp_tools import _shared

    recorded = Mock()
    monkeypatch.setattr(_shared, "_record_agent_call", recorded)
    async with OfficialMcpClient(mcp_env["app"], mcp_env["restricted"].token) as client:
        assert _error_code(await client.call("rank_concepts", arguments)) == "invalid_argument"
    recorded.assert_not_called()


@pytest.mark.anyio
async def test_three_ten_row_pages_and_bound_cursor(mcp_env):
    repo, nb = repository(), mcp_env["notebook"].id
    _seed(repo, nb)
    async with OfficialMcpClient(mcp_env["app"], mcp_env["token_a"].token) as client:
        first = _payload(await client.call("rank_concepts", {"limit": 10}))
        second = _payload(await client.call("rank_concepts", {"limit": 10, "cursor": first["next_cursor"]}))
        third = _payload(await client.call("rank_concepts", {"limit": 10, "cursor": second["next_cursor"]}))
        rows = first["rows"] + second["rows"] + third["rows"]
        assert [r["rank"] for r in rows] == list(range(1, 26))
        assert [r["name"] for r in rows] == [f"概念 {i:02}" for i in range(25)]
        assert third["rows"][0]["rank"] == 21 and third["next_cursor"] == ""
        changed = await client.call("rank_concepts", {"limit": 20, "cursor": first["next_cursor"]})
        assert _error_code(changed) == "invalid_argument" and "limit" in _error(changed)
        raw = json.loads(base64.urlsafe_b64decode(first["next_cursor"] + "=="))
        for update in ({"v": 1}, {"p": True}, {"p": 1.5}, {"p": 8}, {"l": True}):
            bad = base64.urlsafe_b64encode(json.dumps({**raw, **update}).encode()).decode().rstrip("=")
            assert _error_code(await client.call("rank_concepts", {"limit": 10, "cursor": bad})) == "invalid_argument"
        assert _error_code(await client.call("rank_concepts", {"cursor": "c2:p1"})) == "invalid_argument"
        with repo._write() as db:
            repo._runtime.unified_kg.mark_dirty(db, nb, NOW)
        stale = await client.call("rank_concepts", {"limit": 10, "cursor": first["next_cursor"]})
        assert _error_code(stale) == "invalid_argument" and "排行已变化" in _error(stale)


@pytest.mark.anyio
async def test_large_notebook_without_precomputation_returns_backend_failure(mcp_env, monkeypatch):
    repo, nb = repository(), mcp_env["notebook"].id
    _seed(repo, nb, 3)
    monkeypatch.setattr(repo._runtime.settings, "kg_rank_online_max_objects", 1)
    monkeypatch.setattr(repo._runtime.concept_ranking, "_producible", lambda nb: True)
    async with OfficialMcpClient(mcp_env["app"], mcp_env["restricted"].token) as client:
        page = _payload(await client.call("rank_concepts"))
    assert page["coverage"]["status"] == "unavailable"
    assert page["coverage"]["reason"] == "not_computed"
    assert page["rows"] == [] and page["next_cursor"] == ""
    assert page["ranking_failures"][0]["line"] == (
        "没能按提及次数排行：本笔记本还没有排行数据。"
    )
    assert "这次更新没有产出排行数据" in page["ranking_failures"][0]["fallback_line"]
    assert "下面的回答" not in json.dumps(page, ensure_ascii=False)


@pytest.mark.anyio
async def test_lost_notebook_access_is_rechecked_on_same_session(mcp_env):
    app, nb = mcp_env["app"], mcp_env["notebook"].id
    async with app.router.lifespan_context(app):
        async with OfficialMcpClient(app, mcp_env["bob_token"].token, manage_lifespan=False) as client:
            assert not (await client.call("rank_concepts")).isError
            mcp_env["sharing"].remove_member(nb, mcp_env["bob"].id)
            assert _error_code(await client.call("rank_concepts")) == "notebook_unreadable"


@pytest.mark.anyio
async def test_topic_pages_recall_once_and_reject_changed_candidates(mcp_env, monkeypatch):
    repo, nb = repository(), mcp_env["notebook"].id
    _seed(repo, nb, 26)
    def hits(indices):
        return [SimpleNamespace(object_id=f"rank-obj-{index:02}", object_type="concept") for index in indices]
    recall = Mock(side_effect=[hits(range(25)), hits(reversed(range(25))), hits([*range(24), 25])])
    monkeypatch.setattr(repo.retrieval, "retrieve_scored", recall)
    async with OfficialMcpClient(mcp_env["app"], mcp_env["restricted"].token) as client:
        first = _payload(await client.call("rank_concepts", {"query": "主题", "limit": 10}))
        second = _payload(await client.call("rank_concepts", {
            "query": "主题", "limit": 10, "cursor": first["next_cursor"],
        }))
        assert [row["rank"] for row in second["rows"]] == list(range(11, 21))
        stale = await client.call("rank_concepts", {
            "query": "主题", "limit": 10, "cursor": second["next_cursor"],
        })
        assert _error_code(stale) == "invalid_argument" and "排行已变化" in _error(stale)
    assert recall.call_count == 3


def _production_page(name_chars, query, *, page_number=0, page_size=20,
                     source_count=123456789, object_count=987654321):
    nb = "nb-0123456789"
    rows = tuple(ConceptRankRow(
        nb, f"concept-{index:032x}", "名" * name_chars, source_count, object_count, "",
        tuple(ConceptRankEvidence(f"el-{i:032x}", f"src-{i:032x}", nb)
              for i in range(index * 3, index * 3 + 3)), "personal",
    ) for index in range(25))
    coverage = LibraryRankCoverage(nb, "排行库", path="query" if query else "scope_full")
    computation = ConceptRankComputation(
        rank_fingerprint(query, "all", (nb,)), query, "all",
        (LibraryRanking(coverage, rows),), ((nb, 1, 1),),
        json.dumps(["", "0" * 64], separators=(",", ":")) if query else "",
    )
    return ConceptRankPage(computation, page_number, page_size,
                           rank_next_cursor(computation, page_number, page_size))


@pytest.mark.anyio
@pytest.mark.parametrize("query", ["", "题" * 200], ids=["whole_notebook", "long_query"])
async def test_production_ids_fit_default_page_and_long_query(mcp_env, monkeypatch, query):
    original = _production_page(40, query, source_count=123, object_count=456)
    monkeypatch.setattr(repository()._runtime.concept_ranking, "rank", Mock(return_value=original))
    async with OfficialMcpClient(mcp_env["app"], mcp_env["restricted"].token) as client:
        page = _payload(await client.call("rank_concepts", {"query": query}))
    assert _serialized_size(page) <= TOTAL_TEXT_LIMIT
    assert len(page["rows"]) == 20 and page["next_cursor"] == original.next_cursor
    assert all(r["source_count"] == 123 and r["object_count"] == 456 for r in page["rows"])
    if not query:
        assert all(len(r["refs"]) >= 2 for r in page["rows"])
    else:
        assert all(r["refs"] for r in page["rows"])
    assert all(isinstance(ref, str) for ref in page["refs"])


@pytest.mark.anyio
async def test_single_row_long_names_are_clipped_and_paging_can_advance(mcp_env, monkeypatch):
    pages = [_production_page(10000, "题" * 200, page_size=1, page_number=i) for i in range(2)]
    monkeypatch.setattr(repository()._runtime.concept_ranking, "rank", Mock(side_effect=pages))
    async with OfficialMcpClient(mcp_env["app"], mcp_env["restricted"].token) as client:
        first = _payload(await client.call("rank_concepts", {"query": "题" * 200, "limit": 1}))
        second = _payload(await client.call("rank_concepts", {
            "query": "题" * 200, "limit": 1, "cursor": first["next_cursor"],
        }))
    for page in (first, second):
        assert _serialized_size(page) <= TOTAL_TEXT_LIMIT
        assert page["rows"][0]["name"] == "名" * 200 + "…"
        assert page["rows"][0]["source_count"] == 123456789
        assert page["rows"][0]["object_count"] == 987654321
        assert page["truncation"]["omitted_characters"] == 9800
    assert first["rows"][0]["rank"] == 1 and second["rows"][0]["rank"] == 2


@pytest.mark.anyio
async def test_over_budget_page_is_refused_whole_then_limit_one_succeeds(mcp_env, monkeypatch):
    service = repository()._runtime.concept_ranking
    monkeypatch.setattr(service, "rank", Mock(return_value=_production_page(200, "题" * 200)))
    async with OfficialMcpClient(mcp_env["app"], mcp_env["restricted"].token) as client:
        refused = await client.call("rank_concepts", {"query": "题" * 200})
        assert _error_code(refused) == "internal" and "减小 limit" in _error(refused)
        monkeypatch.setattr(service, "rank", Mock(return_value=_production_page(200, "题" * 200, page_size=1)))
        page = _payload(await client.call("rank_concepts", {"query": "题" * 200, "limit": 1}))
        assert len(page["rows"]) == 1 and page["next_cursor"]


@pytest.mark.anyio
@pytest.mark.parametrize("error", [TimeoutError, ReadBudgetExceeded])
async def test_executor_timeout_is_unavailable(mcp_env, monkeypatch, error):
    monkeypatch.setattr(repository()._runtime.concept_ranking, "rank", Mock(side_effect=error("private")))
    async with OfficialMcpClient(mcp_env["app"], mcp_env["restricted"].token) as client:
        result = await client.call("rank_concepts")
    assert _error_code(result) == "unavailable" and "稍后重试" in _error(result)
    assert "private" not in _error(result)


@pytest.mark.anyio
@pytest.mark.parametrize("allow_memory", [True, False])
async def test_hidden_projection_concepts_never_count_name_or_cite(mcp_env, monkeypatch, allow_memory):
    from app.api.mcp_tools import sources

    repo, nb = repository(), mcp_env["notebook"].id
    _seed(repo, nb, 5)
    with repo._write() as db:
        for index, kind in ((1, "memory"), (2, "knowhow"), (3, "memory"), (4, "knowhow")):
            db.execute("UPDATE sources SET source_type=? WHERE id=?", (kind, f"rank-src-{index:02}"))
            db.execute("UPDATE knowledge_objects SET payload=? WHERE id=?",
                       (json.dumps({"name": "概念 00" if index < 3 else "隐藏名字"}), f"rank-obj-{index:02}"))
            if index < 3:
                db.execute(
                    "INSERT INTO concept_clusters(id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,created_at,generation) "
                    "VALUES (?,?,?,?,?,'concept',?,0)",
                    (f"cc-{index}", nb, "rank-obj-00", f"rank-obj-{index:02}", "概念 00", NOW),
                )
        repo._runtime.unified_kg.mark_dirty(db, nb, NOW)
    def retrieve(*args, **kwargs):
        assert memory_channel_allowed() is allow_memory
        return [SimpleNamespace(object_id=f"rank-obj-{i:02}", object_type="concept") for i in range(5)]
    if not allow_memory:
        monkeypatch.setattr(sources, "_memory_viewer", lambda principal: "")
    monkeypatch.setattr(repo.retrieval, "retrieve_scored", retrieve)
    async with OfficialMcpClient(mcp_env["app"], mcp_env["restricted"].token) as client:
        for query in ("", "主题"):
            page = _payload(await client.call("rank_concepts", {"query": query}))
            assert len(page["rows"]) == 1, page
            row = page["rows"][0]
            assert row["name"] == "概念 00" and row["source_count"] == row["object_count"] == 1
            assert {decode_ref(ref)[1]["s"] for ref in page["refs"]} == {"rank-src-00"}
