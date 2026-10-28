"""MCP ``find_sources`` / ``get_paper_cards`` / ``extract_from_sources`` and
``list_sources``' paper metadata (design 2026-10-28 §7.4), through the
official MCP client on SQLite (the shared ``mcp_env``: alice owns the
notebook, bob is a member). No real model: fakes behind the real scheduled
adapter (``paper_card_testkit.scheduled_paper_model``)."""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import anyio
import pytest

from app.api.deps import repository
from app.api.mcp_server import TOOL_TIERS
from app.api.mcp_tools import sources as source_tools
from app.services.paper_cards import PAPER_CARD_SCHEMA_HINT
from app.services.paper_extraction import EXTRACTION_SCHEMA_HINT
from app.services.paper_meta import PAPER_META_SCHEMA_HINT
from tests import paper_card_testkit as card_kit
from tests import test_memory_mcp as mcp_world
from tests.paper_topics_testkit import paper, seed_paper_topics
from tests.test_memory_mcp import (
    MCP_OUTPUT_BUDGET,
    OfficialMcpClient,
    _agent_token,
    _error,
    _error_code,
    _payload,
)
from tests.test_paper_extraction import FakeExtractionModel

PAPER_TOOLS = ("find_sources", "get_paper_cards", "extract_from_sources")
NOW = "2026-10-01T00:00:00+00:00"


@pytest.fixture
def mcp_env(tmp_path, tmp_path_factory, monkeypatch):
    """The shared MCP world of ``test_memory_mcp`` (same seed, fresh copy)."""
    root, ids = mcp_world._mcp_template(tmp_path_factory).materialize(tmp_path)
    return mcp_world._open_mcp_world(root, ids, monkeypatch)


def _size(payload: dict) -> int:
    return len(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8"))


def _assert_refs_resolve(payload: dict) -> None:
    """Every ref index a page carries points into its ``refs`` table."""
    refs = payload["refs"]
    seen: list[int] = []

    def walk(value, key=""):
        if isinstance(value, dict):
            for child_key, child in value.items():
                walk(child, child_key)
        elif isinstance(value, list):
            for child in value:
                walk(child, key)
        elif key in ("ref", "refs") and isinstance(value, int):
            seen.append(value)

    walk(payload["items"])
    assert all(0 <= index < len(refs) for index in seen), (seen, len(refs))
    assert sorted(set(seen)) == list(range(len(refs))), "an unused or missing ref"


def _seed_projection(repo, notebook_id: str, source_id: str, owner_id: str, text: str) -> None:
    """A Memory projection source owned by ``owner_id`` with one element and
    one chunk -- searchable like any document unless the scope excludes it."""
    memory_id = f"mem-{source_id}"
    element_id = f"el-{source_id}-0001"
    with repo._write() as db:
        db.execute(
            "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,"
            "title,content_md,created_at,updated_at) "
            "VALUES (?,?,?,'ask_answer','confirmed',?,?,?,?)",
            (memory_id, notebook_id, owner_id, "私人", text, NOW, NOW),
        )
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,status,"
            "parse_status,memory_id,created_at,updated_at) "
            "VALUES (?,?,?,'memory','ready','parsed',?,?,?)",
            (source_id, notebook_id, text, memory_id, NOW, NOW),
        )
        db.execute(
            "INSERT INTO source_elements(id,source_id,element_type,location_label,"
            "text,metadata,created_at) VALUES (?,?,'paragraph','第 1 段',?,'{}',?)",
            (element_id, source_id, text, NOW),
        )
        db.execute(
            "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,"
            "element_ids,created_at) VALUES (?,?,?,?,?,?,?)",
            (f"chunk-{source_id}", notebook_id, source_id, text, "1",
             json.dumps([element_id]), NOW),
        )
    repo._runtime.knowledge_query.backfill_chunk_fts(notebook_id)


def _card_papers(repo, notebook_id: str, count: int, *, meta: bool = True, filler: int = 3) -> list[str]:
    ids = []
    for index in range(count):
        source_id = f"card-{index}"
        card_kit.seed_source(repo, notebook_id, source_id,
                             doc_type="academic_paper", filler=filler)
        if meta:
            card_kit.set_paper_meta(repo, notebook_id, source_id, is_paper=True)
        ids.append(source_id)
    return ids


# --------------------------------------------------------------- catalog


def test_paper_tools_are_read_tier_tools():
    for name in PAPER_TOOLS:
        assert TOOL_TIERS[name] == "read", name


@pytest.mark.anyio
async def test_a_read_only_token_lists_and_calls_the_paper_tools(mcp_env):
    async with OfficialMcpClient(mcp_env["app"], mcp_env["restricted"].token) as client:
        listed = {tool.name for tool in (await client.session.list_tools()).tools}
        assert set(PAPER_TOOLS) <= listed
        page = _payload(await client.call("find_sources", {"terms": ["nothing-here"]}))
    assert page["items"] == [] and page["refs"] == []
    assert page["counts"]["strong"] == 0


# ----------------------------------------------------------- list_sources


@pytest.mark.anyio
async def test_list_sources_carries_paper_metadata_from_the_page_read(mcp_env):
    repo = repository()
    notebook_id = mcp_env["notebook"].id
    seeded = seed_paper_topics(repo, notebook_id, keys=["dif-01"], embed=False)
    source_id = seeded["dif-01"]
    item = paper("dif-01")
    repo._runtime.source_store.upsert_paper_meta(source_id, notebook_id, {
        "is_paper": True, "paper_title": item["title"], "venue": "CVPR",
        "pub_year": 2023, "doi": None, "keywords": [],
        "authors": [{"position": i, "name": f"Author {i}", "affiliation": ""}
                    for i in range(5)],
        "model": "fixture", "raw_json": "{}",
    })
    card_kit.seed_source(repo, notebook_id, "plain-doc")
    async with OfficialMcpClient(mcp_env["app"], mcp_env["token_a"].token) as client:
        page = _payload(await client.call("list_sources", {}))
    rows = {row["source_id"]: row for row in page["items"]}
    assert rows[source_id]["authors"] == ["Author 0", "Author 1", "Author 2"]
    assert rows[source_id]["pub_year"] == 2023
    assert rows[source_id]["venue"] == "CVPR"
    assert rows["plain-doc"]["authors"] == []
    assert rows["plain-doc"]["pub_year"] is None and rows["plain-doc"]["venue"] == ""


# ---------------------------------------------------------- permissions


@pytest.mark.anyio
async def test_a_notebook_outside_the_whitelist_is_refused(mcp_env):
    async with OfficialMcpClient(mcp_env["app"], mcp_env["token_a"].token) as client:
        for name, arguments in (
            ("find_sources", {"terms": ["diffusion"]}),
            ("get_paper_cards", {"source_ids": ["x"]}),
            ("extract_from_sources", {"source_ids": ["x"], "fields": ["mAP"]}),
        ):
            result = await client.call(
                name, {**arguments, "notebook_id": mcp_env["other"].id}
            )
            assert _error_code(result) == "notebook_not_allowed", name


@pytest.mark.anyio
async def test_an_owner_who_lost_read_access_is_refused(mcp_env):
    notebook_id = mcp_env["notebook"].id
    app = mcp_env["app"]
    async with app.router.lifespan_context(app):
        async with OfficialMcpClient(
            app, mcp_env["bob_token"].token, manage_lifespan=False
        ) as client:
            assert not (await client.call("find_sources", {"terms": ["x"]})).isError
            mcp_env["sharing"].remove_member(notebook_id, mcp_env["bob"].id)
            for name, arguments in (
                ("find_sources", {"terms": ["x"]}),
                ("get_paper_cards", {"source_ids": ["x"]}),
                ("extract_from_sources", {"source_ids": ["x"], "fields": ["mAP"]}),
            ):
                result = await client.call(name, arguments)
                assert _error_code(result) == "notebook_unreadable", name


@pytest.mark.anyio
async def test_another_members_memory_never_reaches_find_or_extraction(mcp_env):
    """MCP has no run scope: the explicit set (visible ∪ the owner's hidden)
    is what keeps bob's Memory projection out of alice's results."""
    repo = repository()
    notebook_id = mcp_env["notebook"].id
    seed_paper_topics(repo, notebook_id, keys=["dif-01"], embed=False)
    _seed_projection(repo, notebook_id, "mem-bob", mcp_env["bob"].id,
                     "zebrafish quokka secret note of bob")
    _seed_projection(repo, notebook_id, "mem-alice", mcp_env["alice"].id,
                     "zebrafish quokka note of alice")
    finder = repo._runtime.source_finder
    real_find = finder.find
    scopes: list[set] = []

    def spy(*args, **kwargs):
        scopes.append(set(kwargs["allowed_source_ids"]))
        return real_find(*args, **kwargs)

    finder.find = spy
    fake = FakeExtractionModel()
    try:
        with card_kit.scheduled_paper_model(repo, fake):
            async with OfficialMcpClient(mcp_env["app"], mcp_env["token_a"].token) as client:
                found = _payload(await client.call("find_sources", {
                    "query": "zebrafish quokka diffusion",
                    "terms": ["quokka", "diffusion"], "kind": "all",
                }))
                extracted = _payload(await client.call("extract_from_sources", {
                    "source_ids": ["mem-bob"], "fields": ["secret note"],
                }))
    finally:
        del finder.find
    # The explicit set: alice's own projection in, bob's out.
    assert "mem-alice" in scopes[0] and "mem-bob" not in scopes[0]
    ids = [item["source_id"] for item in found["items"]]
    assert ids and "mem-bob" not in ids
    assert "bob" not in json.dumps(found, ensure_ascii=False)
    assert extracted["items"] == [{"source_id": "mem-bob", "status": "not_found"}]
    assert fake.prompts == []


# ---------------------------------------------------------- find_sources


@pytest.mark.anyio
async def test_find_sources_ranks_with_refs_coverage_and_meta_pending(mcp_env):
    repo = repository()
    notebook_id = mcp_env["notebook"].id
    seed_paper_topics(repo, notebook_id, keys=["dif-01", "dif-03"], embed=False)
    pending = seed_paper_topics(repo, notebook_id, keys=["dif-02"], embed=False,
                                with_meta=False)["dif-02"]
    async with OfficialMcpClient(mcp_env["app"], mcp_env["token_a"].token) as client:
        page = _payload(await client.call("find_sources", {
            "query": "diffusion image generation", "terms": ["diffusion"],
        }))
        ref_reads = [
            await client.call("read_reference", {"ref": ref}) for ref in page["refs"]
        ]
    assert page["items"], page
    assert page["content_is_untrusted_evidence"] is True
    statuses = {item["source_id"]: item["card_status"] for item in page["items"]}
    assert statuses[pending] == "meta_pending"
    assert page["counts"]["meta_pending_unchecked"] >= 1
    assert page["coverage"]["lexical_complete"] is False
    assert set(page["coverage"]) == {
        "lexical_complete", "semantic_recall_capped", "skipped_libraries", "lanes"}
    with_passage = [item for item in page["items"] if "best_passage" in item]
    assert with_passage and all("ref" in item["best_passage"] for item in with_passage)
    _assert_refs_resolve(page)
    for result in ref_reads:
        assert not _payload(result).get("text", "") == ""
    assert _size(page) <= MCP_OUTPUT_BUDGET


@pytest.mark.anyio
async def test_a_stale_cursor_tells_the_agent_to_restart(mcp_env):
    repo = repository()
    notebook_id = mcp_env["notebook"].id
    seeded = seed_paper_topics(repo, notebook_id, keys=["dif-01", "dif-02", "dif-03"],
                               embed=False)
    async with OfficialMcpClient(mcp_env["app"], mcp_env["token_a"].token) as client:
        first = _payload(await client.call("find_sources", {
            "terms": ["diffusion"], "limit": 1,
        }))
        assert first["next_cursor"]
        second = _payload(await client.call("find_sources", {
            "terms": ["diffusion"], "limit": 1, "cursor": first["next_cursor"],
        }))
        assert second["items"][0]["rank"] == 2
        item = paper("dif-01")
        repo._runtime.source_store.upsert_paper_meta(seeded["dif-01"], notebook_id, {
            "is_paper": True, "paper_title": item["title"], "venue": "ECCV",
            "pub_year": 2021, "doi": None, "keywords": [], "authors": [],
            "model": "fixture", "raw_json": "{}",
        })
        stale = await client.call("find_sources", {
            "terms": ["diffusion"], "limit": 1, "cursor": first["next_cursor"],
        })
        foreign = await client.call("find_sources", {
            "terms": ["other words"], "limit": 1, "cursor": first["next_cursor"],
        })
    assert _error_code(stale) == "invalid_argument"
    assert "第一页重新检索" in _error(stale)
    assert _error_code(foreign) == "invalid_argument"
    assert "cursor 无效" in _error(foreign)


def test_a_budget_cut_page_continues_at_the_first_undelivered_item():
    from app.api.mcp_tools.paper_pages import find_sources_page
    from app.services.source_finder import BestPassage, FoundSource

    long_title = "长标题" * 66
    items = tuple(
        FoundSource(
            rank=11 + index, source_id=f"src-{index:02d}", notebook_id="nb",
            title=long_title, pub_year=2024, venue="会议" * 40,
            authors=("作者甲", "作者乙", "作者丙"), tier="strong",
            reasons=("title", "passages:3"),
            best_passage=BestPassage(f"chunk-{index}", "片段" * 120, (f"el-{index}",)),
            card_status="ready",
        )
        for index in range(20)
    )
    cut_at: list[int] = []
    result = SimpleNamespace(
        items=items, strong=40, related=0, filtered_scope_total=40,
        meta_pending_excluded=0, meta_pending_unchecked=0,
        lexical_complete=True, semantic_recall_capped=False,
        skipped_libraries={}, lanes={"nb": "baseline"}, next_cursor="fs1.page-3",
    )

    def cursor_at(offset: int) -> str:
        cut_at.append(offset)
        return f"cursor@{offset}"

    page = find_sources_page("nb", result, cursor_at)
    assert _size(page) <= MCP_OUTPUT_BUDGET
    assert page["truncation"]["truncated"] is True
    delivered = len(page["items"])
    assert 1 <= delivered < 20
    assert page["next_cursor"] == f"cursor@{10 + delivered}"
    assert [item["rank"] for item in page["items"]] == list(range(11, 11 + delivered))
    _assert_refs_resolve(page)


# -------------------------------------------------------- get_paper_cards


@pytest.mark.anyio
async def test_three_cards_fit_the_budget_with_one_refs_table(mcp_env):
    repo = repository()
    notebook_id = mcp_env["notebook"].id
    ids = _card_papers(repo, notebook_id, 3)
    fake = card_kit.FakePaperModel()
    with card_kit.scheduled_paper_model(repo, fake):
        async with OfficialMcpClient(mcp_env["app"], mcp_env["token_a"].token) as client:
            page = _payload(await client.call("get_paper_cards", {"source_ids": ids}))
            again = _payload(await client.call("get_paper_cards", {"source_ids": ids}))
            metric_ref = page["refs"][page["items"][0]["metrics"][0]["ref"]]
            cited = _payload(await client.call("read_reference", {"ref": metric_ref}))
    assert [item["status"] for item in page["items"]] == ["generated"] * 3
    assert [item["status"] for item in again["items"]] == ["ready"] * 3
    assert fake.card_calls == 3
    assert page["detail"] == "full"
    assert _size(page) <= MCP_OUTPUT_BUDGET
    assert page["truncation"]["truncated"] is False
    first = page["items"][0]
    assert first["title"] == card_kit.TITLE
    assert first["metrics"][0]["value"] == "85.2"
    assert "85.20" in cited["text"]
    _assert_refs_resolve(page)
    # One table: a ref cited by several fields is listed once.
    assert len(page["refs"]) == len(set(page["refs"]))


def _heavy_card(prompt: str) -> dict:
    handles = [handle for handle, _kind in card_kit._HANDLE_RE.findall(prompt)]
    table = next(h for h, kind in card_kit._HANDLE_RE.findall(prompt) if kind == "table")
    text = "稀疏视觉主干中的门控小部件可以跳过冗余的标记并保持精度。" * 20
    return {
        "tldr": {"text": text, "anchors": handles},
        "problem": {"text": text, "anchors": handles},
        "method": {"text": text, "anchors": handles},
        "contributions": [{"text": text, "anchors": [h]} for h in handles[:5]],
        "metrics": [{
            "name": f"top-1 设定 {index}", "value": "85.2", "unit": "%",
            "setting": text, "compared_to": text, "anchor": table,
        } for index in range(12)],
        "datasets": [{"name": f"ImageNet-{index}", "anchors": [handles[index]]}
                     for index in range(8)],
    }


@pytest.mark.anyio
async def test_cards_over_budget_degrade_by_level_and_report_it(mcp_env):
    repo = repository()
    notebook_id = mcp_env["notebook"].id
    ids = _card_papers(repo, notebook_id, 3, filler=20)
    fake = card_kit.FakePaperModel()
    fake.reply = _heavy_card
    with card_kit.scheduled_paper_model(repo, fake):
        async with OfficialMcpClient(mcp_env["app"], mcp_env["token_a"].token) as client:
            page = _payload(await client.call("get_paper_cards", {"source_ids": ids}))
    assert [item["status"] for item in page["items"]] == ["generated"] * 3
    assert page["detail"] != "full"
    assert page["truncation"]["truncated"] is True
    assert page["truncation"]["omitted_items"] + page["truncation"]["omitted_characters"] > 0
    assert _size(page) <= MCP_OUTPUT_BUDGET
    assert all(item.get("tldr") or item.get("card_omitted") for item in page["items"])
    for item in page["items"]:
        for metric in item.get("metrics", []):
            assert metric["value"] == "85.2", "a metric value is never clipped"
    _assert_refs_resolve(page)


@pytest.mark.anyio
async def test_cards_beyond_the_call_budget_are_deferred_and_meta_pending_surfaces(mcp_env):
    repo = repository()
    notebook_id = mcp_env["notebook"].id
    ids = _card_papers(repo, notebook_id, 3, meta=False)
    fake = card_kit.FakePaperModel()
    with card_kit.scheduled_paper_model(repo, fake):
        async with OfficialMcpClient(mcp_env["app"], mcp_env["token_a"].token) as client:
            page = _payload(await client.call("get_paper_cards", {"source_ids": ids}))
            found = _payload(await client.call("find_sources", {
                "terms": ["doc"], "kind": "papers"}))
    statuses = [item["status"] for item in page["items"]]
    assert statuses[0] == "generated"
    assert "deferred" in statuses[1:]
    assert fake.meta_calls + fake.card_calls <= source_tools.PAPER_CARD_GENERATE_LIMIT
    pending = {item["source_id"]: item["card_status"] for item in found["items"]}
    assert "meta_pending" in pending.values()


@pytest.mark.anyio
async def test_a_rate_limited_model_defers_instead_of_failing(mcp_env):
    repo = repository()
    notebook_id = mcp_env["notebook"].id
    ids = _card_papers(repo, notebook_id, 1)
    fake = card_kit.FakePaperModel()

    def limited(_prompt):
        raise card_kit.StatusError(429)

    fake.on_card = limited
    with card_kit.scheduled_paper_model(repo, fake):
        async with OfficialMcpClient(mcp_env["app"], mcp_env["token_a"].token) as client:
            page = _payload(await client.call("get_paper_cards", {"source_ids": ids}))
    assert page["items"][0]["status"] == "deferred"
    assert page["items"][0]["reason"] == "model_unavailable"


@pytest.mark.anyio
async def test_paper_tool_arguments_are_validated_in_chinese(mcp_env):
    async with OfficialMcpClient(mcp_env["app"], mcp_env["token_a"].token) as client:
        too_many = await client.call("get_paper_cards", {"source_ids": ["a", "b", "c", "d"]})
        none = await client.call("get_paper_cards", {"source_ids": [" "]})
        fields = await client.call("extract_from_sources", {
            "source_ids": ["a"], "fields": ["a", "b", "c", "d", "e"]})
        empty = await client.call("find_sources", {})
        kind = await client.call("find_sources", {"terms": ["x"], "kind": "books"})
        terms = await client.call("find_sources", {"terms": [str(i) for i in range(7)]})
    for result in (too_many, none, fields, empty, kind, terms):
        assert _error_code(result) == "invalid_argument"
    assert "最多 3 篇" in _error(too_many)


# -------------------------------------------------- concurrency and cancel


@pytest.mark.anyio
async def test_a_third_concurrent_generating_call_of_one_owner_is_busy(mcp_env):
    repo = repository()
    notebook_id = mcp_env["notebook"].id
    ids = _card_papers(repo, notebook_id, 1)
    fake = card_kit.FakePaperModel()
    alice = mcp_env["alice"].id
    with card_kit.scheduled_paper_model(repo, fake):
        async with OfficialMcpClient(mcp_env["app"], mcp_env["token_b"].token) as client:
            with source_tools._owner_generation_slot(alice), \
                    source_tools._owner_generation_slot(alice):
                cards = await client.call("get_paper_cards", {"source_ids": ids})
                extract = await client.call("extract_from_sources", {
                    "source_ids": ids, "fields": ["top-1"]})
                # Searching calls no chat model: not counted.
                assert not (await client.call("find_sources", {"terms": ["x"]})).isError
            assert _error_code(cards) == "busy" and _error_code(extract) == "busy"
            assert fake.card_calls == 0
            with source_tools._owner_generation_slot(alice):
                assert not (await client.call(
                    "get_paper_cards", {"source_ids": ids})).isError
    assert source_tools._GENERATING == {}


@pytest.mark.anyio
async def test_a_cancelled_call_stops_the_service_before_its_next_model_call(mcp_env):
    from app.api.mcp_tool_host import capture_core_agent_tools
    from app.api.mcp_tools._shared import _MCP_PRINCIPAL

    repo = repository()
    notebook_id = mcp_env["notebook"].id
    ids = _card_papers(repo, notebook_id, 3)
    service = repo._runtime.paper_cards
    real = service.get_cards
    stops: list[threading.Event] = []

    def spy(*args, **kwargs):
        stops.append(kwargs["cancel_event"])
        return real(*args, **kwargs)

    started = threading.Event()
    fake = card_kit.FakePaperModel()

    def block_until_stopped(_prompt):
        started.set()
        assert stops[0].wait(10), "the stop event was never set"

    fake.on_card = block_until_stopped
    handlers = {tool.name: tool.handler for tool in capture_core_agent_tools(lambda: repo)}

    async def no_progress(*_args, **_kwargs):
        return None

    ctx = SimpleNamespace(report_progress=no_progress)
    marker = _MCP_PRINCIPAL.set(repo.refresh_agent_principal(mcp_env["token_a"].id))
    try:
        with card_kit.scheduled_paper_model(repo, fake, max_concurrency=1):
            service.get_cards = spy
            try:
                async with anyio.create_task_group() as group:
                    group.start_soon(lambda: handlers["get_paper_cards"](
                        source_ids=ids, ctx=ctx, notebook_id=notebook_id))
                    assert await anyio.to_thread.run_sync(started.wait, 10)
                    group.cancel_scope.cancel()
            finally:
                del service.get_cards
    finally:
        _MCP_PRINCIPAL.reset(marker)
    assert stops and stops[0].is_set()
    assert fake.card_calls == 1, "no model call after the cancel"
    assert source_tools._GENERATING == {}


# ---------------------------------------------------- extract_from_sources


@pytest.mark.anyio
async def test_extraction_returns_grounded_values_with_ref_indices(mcp_env):
    repo = repository()
    notebook_id = mcp_env["notebook"].id
    seeded = seed_paper_topics(repo, notebook_id, keys=["dif-01", "dif-02"], embed=False)
    fake = FakeExtractionModel()

    def first_number(prompt: str) -> dict:
        import re

        from tests.test_paper_extraction import excerpts_of

        for handle, text in excerpts_of(prompt).items():
            match = re.search(r"\d+\.\d+", text)
            if match:
                return {"values": [{"field_index": 0, "value": match.group(0),
                                    "anchor": handle}]}
        return {"values": []}

    fake.reply = first_number
    ids = [seeded["dif-01"], seeded["dif-02"]]
    with card_kit.scheduled_paper_model(repo, fake):
        async with OfficialMcpClient(mcp_env["app"], mcp_env["token_a"].token) as client:
            page = _payload(await client.call("extract_from_sources", {
                "source_ids": ids, "fields": [" FID  score ", "training cost"],
            }))
            value = page["items"][0]["values"][0]
            cited = _payload(await client.call(
                "read_reference", {"ref": page["refs"][value["ref"]]}))
    assert page["fields"] == ["FID score", "training cost"]
    assert [item["status"] for item in page["items"]] == ["extracted", "extracted"]
    assert value["field_index"] == 0 and value["value"] in cited["text"]
    assert page["items"][0]["values"][1] == {"field_index": 1, "missing": "not_stated"}
    assert page["items"][0]["title"] == paper("dif-01")["title"]
    assert all(hint == EXTRACTION_SCHEMA_HINT for hint in fake.hints)
    assert all(hint not in (PAPER_META_SCHEMA_HINT, PAPER_CARD_SCHEMA_HINT)
               for hint in fake.hints)
    _assert_refs_resolve(page)
    assert _size(page) <= MCP_OUTPUT_BUDGET


@pytest.mark.anyio
async def test_unknown_or_foreign_sources_are_not_found_without_a_reason(mcp_env):
    repo = repository()
    other_id = mcp_env["other"].id
    card_kit.seed_source(repo, other_id, "elsewhere", doc_type="academic_paper")
    token = _agent_token(mcp_env, ["read"])
    fake = card_kit.FakePaperModel()
    with card_kit.scheduled_paper_model(repo, fake):
        async with OfficialMcpClient(mcp_env["app"], token) as client:
            page = _payload(await client.call(
                "get_paper_cards", {"source_ids": ["elsewhere", "never-existed"]}))
    assert page["items"] == [
        {"source_id": "elsewhere", "status": "not_found"},
        {"source_id": "never-existed", "status": "not_found"},
    ]
    assert fake.meta_calls == fake.card_calls == 0
