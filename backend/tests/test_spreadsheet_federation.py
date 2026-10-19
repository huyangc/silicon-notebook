"""The workbook lane in a peer (global) run: one federated catalog, one plan.

``fangan_todo`` 「全局（对等）模式的表格分析臂只覆盖名义 active」 — user ruling
2026-10-19: federate, with the workbook total capped.  Every participant
library's workbooks are merged into ONE catalog and planned ONCE; the catalog
is filled library by library in rotation under the same byte budget a single
library's catalog already has, so neither the planner call count nor the
number of manifests read grows with the number of libraries; the participant
set is read through the chunk lane's own bounded entry
(``chunk_federation.federated_library_sources``), never through the
authorization-grade ``ask_engine_participant_notebooks`` seat.

The single-library lane is pinned byte for byte (planner prompt digest, the
exact manifest reads, the result) by ``test_single_library_lane_is_byte_identical``.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from openpyxl import Workbook

from app.core.config import Settings
from app.repositories.analysis_artifacts import AnalysisArtifactStore
from app.services.ask_service import AskService
from app.services.spreadsheet_analysis import SpreadsheetAnalysisService


_NOW = "2026-10-19T00:00:00+00:00"


class _EventLog:
    def __init__(self) -> None:
        self.events: list = []
        self.logger = SimpleNamespace(
            exception=lambda *args, **kwargs: None,
            warning=lambda *args, **kwargs: None,
        )

    def emit(self, event) -> None:
        self.events.append(event)


class _CountingArtifacts(AnalysisArtifactStore):
    """The real store, with every manifest read and directory listing counted."""

    def __init__(self, root: Path) -> None:
        super().__init__(root, retention_days=30)
        self.loads: list[tuple[str, str]] = []
        self.listings: list[str] = []

    def load_spreadsheet_manifest(self, notebook_id, source_id):
        self.loads.append((notebook_id, source_id))
        return super().load_spreadsheet_manifest(notebook_id, source_id)

    def spreadsheet_manifest_ids(self, notebook_id):
        self.listings.append(notebook_id)
        return super().spreadsheet_manifest_ids(notebook_id)


class _Planner:
    """A configured ``reasoning_agent`` that records every planner prompt."""

    configured = True

    def __init__(self, plan: dict | None = None) -> None:
        self.plan = plan or {}
        self.prompts: list[str] = []

    def chat_json(self, messages, *args, **kwargs) -> str:
        self.prompts.append(messages[0]["content"])
        return json.dumps(self.plan)


def _settings(tmp_path: Path, **overrides) -> Settings:
    return Settings(
        _env_file=None,
        storage_dir=str(tmp_path / "storage"),
        event_log_enabled=False,
        llm_log_enabled=False,
        **overrides,
    )


def _service(tmp_path: Path, **overrides):
    settings = _settings(tmp_path, **overrides)
    artifacts = _CountingArtifacts(Path(settings.storage_dir))
    return SpreadsheetAnalysisService(
        artifacts=artifacts, settings=settings, event_log=_EventLog(),
        now=lambda: _NOW,
    ), artifacts


def _compile(service, tmp_path: Path, notebook_id: str, source_id: str, *,
             title: str = "", header=("Region", "Amount"), rows=(("East", 10),)):
    path = tmp_path / f"{notebook_id}-{source_id}.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Sheet"
    sheet.append(list(header))
    for row in rows:
        sheet.append(list(row))
    workbook.save(path)
    source = SimpleNamespace(
        id=source_id, notebook_id=notebook_id, title=title or source_id,
        type="xlsx", file_name=f"{source_id}.xlsx", file_path=str(path),
        file_hash="h", source_url="",
    )
    assert service.compile_source(
        source, notebook_name=notebook_id, owner_id="owner", row_element_ids={},
    ) is True


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _NoSeat:
    """A retrieval double whose federated seat must never be read."""

    def workbook_libraries(self, notebook_id):  # pragma: no cover - must not run
        raise AssertionError("a single-library run must not read the peer seat")


def _ask(analysis, planner, *, participants, visible, tiers=None, retrieval=None):
    """A bare ``AskService`` carrying only the seats the workbook lane reads."""
    ask = object.__new__(AskService)
    ask.spreadsheet_analysis = analysis
    ask.ask_engine_hidden_sources = lambda notebook_id, user_id: ()
    ask.ask_engine_participant_notebooks = lambda notebook_id: participants
    ask.ask_engine_visible_sources = lambda notebook_id: visible.get(notebook_id, ())
    ask.evidence_context = SimpleNamespace(tier_map=lambda ids: {
        nb: (tiers or {}).get(nb, "personal") for nb in ids
    })
    ask.model_clients = SimpleNamespace(chat=lambda name: planner)
    ask.event_log = SimpleNamespace(logger=SimpleNamespace(warning=lambda *a: None))
    ask.retrieval = retrieval if retrieval is not None else _NoSeat()
    return ask


def _run_lane(ask, question, notebook_id="nb-1", scope=None):
    prepared = SimpleNamespace(notebook_id=notebook_id, user_id="u",
                               research_question=question)
    runtime = SimpleNamespace(scope=scope, cancellation=None, trace_sink=None)
    trace: list = []
    results = ask._spreadsheet_reasoning_results(prepared, runtime, trace)
    return results, trace


# ---------------------------------------------------------------------------
# Single-library lane: byte for byte
# ---------------------------------------------------------------------------

# Captured on the pre-federation base (ce7d6d81d) with this exact fixture.
_SINGLE_PROMPT_SHA = "99fd3f5daa8783179c4cf79590170b80af52171a6fbef3720683f401658198a0"
_SINGLE_RESULT_SHA = "d00bde19954f65d34e5647c7b095921b38087be96417bd1ebc1ac1651a56af22"


def test_single_library_lane_is_byte_identical(tmp_path):
    analysis, artifacts = _service(tmp_path)
    _compile(analysis, tmp_path, "nb-1", "src-own", title="本库报价",
             header=("Item", "Price"), rows=(("bolt", 3), ("nut", 1)))
    _compile(analysis, tmp_path, "base-1", "src-base", title="参考报价",
             header=("Item", "Price"), rows=(("bolt", 4),))
    planner = _Planner({"source_id": "src-base", "sheet": "Sheet",
                        "operation": "top", "sort_by": "Price", "order": "desc"})
    ask = _ask(analysis, planner, participants=("base-1",), visible={
        "nb-1": ("doc-1", "src-own"), "base-1": ("src-base", "doc-2"),
    }, tiers={"base-1": "base"})

    results, trace = _run_lane(ask, "分析这个 Excel 报价表里价格最高的项目")

    assert len(planner.prompts) == 1
    assert _digest(planner.prompts[0]) == _SINGLE_PROMPT_SHA, planner.prompts[0]
    assert artifacts.loads == [
        ("nb-1", "doc-1"), ("nb-1", "src-own"),
        ("base-1", "src-base"), ("base-1", "doc-2"),
    ]
    [result] = results
    dumped = json.dumps(result.model_dump(mode="json"), ensure_ascii=False,
                        sort_keys=True)
    assert _digest(dumped) == _SINGLE_RESULT_SHA, dumped
    assert (result.notebook_id, result.tier) == ("base-1", "base")
    [step] = trace
    assert step.detail == {
        "source_id": "src-base", "sheet": "Sheet", "range": step.detail["range"],
        "operation": "top", "scanned_rows": 1, "returned_rows": 1, "complete": True,
    }


# ---------------------------------------------------------------------------
# Peer lane: the harness
# ---------------------------------------------------------------------------

_ACTOR = "user-workbook-peer"


@contextlib.contextmanager
def _peer(ceilings: dict):
    """The installation shape ``global_run.global_ask_run`` freezes: nominal
    active = first id, participant override, per-library ceilings, subjectless."""
    from app.services.retrieval_participants import (
        ParticipantOverride, participant_override,
    )
    from app.services.retrieval_run import retrieval_run
    from app.services.source_scope import current_source_scope, source_scope_context

    ids = tuple(ceilings)
    with retrieval_run(run_kind="ask_reasoning", actor_id=_ACTOR):
        with source_scope_context(
            ids[0], None, None,
            notebook_source_ceilings={
                nb: set(sources) for nb, sources in ceilings.items()
            },
            subjectless=True,
        ):
            with participant_override(ParticipantOverride(
                notebook_ids=ids, tiers={}, attested_actor_id=_ACTOR,
            )):
                yield current_source_scope()


class _Seat:
    """``RetrievalService.workbook_libraries`` as a double: what the chunk lane
    would hand over, recorded."""

    def __init__(self, libraries) -> None:
        self.libraries = tuple(libraries)
        self.calls: list[str] = []

    def workbook_libraries(self, notebook_id):
        self.calls.append(notebook_id)
        return self.libraries


def _never(*_args, **_kwargs):
    raise AssertionError("the peer lane must not read the authorization-grade seats")


def _peer_ask(analysis, planner, seat, tiers=None):
    ask = _ask(analysis, planner, participants=(), visible={}, tiers=tiers,
               retrieval=seat)
    ask.ask_engine_participant_notebooks = _never
    ask.ask_engine_visible_sources = _never
    ask.ask_engine_hidden_sources = _never
    return ask


def _catalog(prompt: str) -> list:
    return json.loads(prompt.split("可用工作簿：", 1)[1])


def _head(question: str, count: int) -> int:
    digest = hashlib.blake2s(question.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % count


_COMPARE = "汇总比较 A 库和 B 库两张报价表的价格"


# ---------------------------------------------------------------------------
# Peer lane: behaviour
# ---------------------------------------------------------------------------

def test_both_libraries_enter_one_catalog_and_either_workbook_is_read(tmp_path):
    """The todo's own case: two libraries, one quote table each.  Both are in
    the ONE catalog the planner sees (one call), whichever it picks is the one
    executed, and its receipt carries the owning library and that library's
    tier -- including the nominal active, which has no "this library" status
    in a peer run and so keeps its id instead of the blank one."""
    analysis, artifacts = _service(tmp_path)
    _compile(analysis, tmp_path, "nb-a", "quote-a", title="A 报价",
             header=("Item", "Price"), rows=(("bolt", 3), ("nut", 1)))
    _compile(analysis, tmp_path, "nb-b", "quote-b", title="B 报价",
             header=("Item", "Price"), rows=(("bolt", 5),))
    tiers = {"nb-a": "personal", "nb-b": "base"}
    for chosen, owner in (("quote-a", "nb-a"), ("quote-b", "nb-b")):
        planner = _Planner({"source_id": chosen, "sheet": "Sheet",
                            "operation": "top", "sort_by": "Price"})
        seat = _Seat((("nb-a", ("doc-a", "quote-a")), ("nb-b", ("quote-b",))))
        artifacts.loads.clear()
        artifacts.listings.clear()
        with _peer({"nb-a": {"doc-a", "quote-a"}, "nb-b": {"quote-b"}}) as scope:
            results, trace = _run_lane(
                _peer_ask(analysis, planner, seat, tiers), _COMPARE, "nb-a", scope,
            )

        assert seat.calls == ["nb-a"]
        [prompt] = planner.prompts
        assert {entry["source_id"] for entry in _catalog(prompt)} == {
            "quote-a", "quote-b",
        }
        assert "目录说明" not in prompt
        # The non-workbook source is never opened: one listing per library.
        assert sorted(artifacts.loads) == [("nb-a", "quote-a"), ("nb-b", "quote-b")]
        assert sorted(artifacts.listings) == ["nb-a", "nb-b"]
        [result] = results
        assert result.source_id == chosen
        assert (result.notebook_id, result.tier) == (owner, tiers[owner])
        assert (result.rows[0].citation.notebook_id,
                result.rows[0].citation.tier) == (owner, tiers[owner])
        assert result.warnings == []
        [step] = trace
        assert step.detail["notebook_id"] == owner
        assert (step.detail["libraries"], step.detail["workbooks"],
                step.detail["listed"]) == (2, 2, 2)


def test_eight_libraries_cost_one_plan_and_no_more_reads_than_one_library(tmp_path):
    """Eight libraries x four workbooks under a small catalog budget.  One
    planner call; the catalog stays inside the single-library byte budget and
    lists exactly as many workbooks as ONE library's catalog of the same 32
    workbooks would; manifest reads stop at that capacity (+1, the read that
    found the budget spent) where the single-library lane reads all 32; the
    listed head is one workbook per library; the rest is disclosed by count."""
    budget = 1_024
    analysis, artifacts = _service(
        tmp_path, spreadsheet_analysis_planner_catalog_bytes=budget,
    )
    ids = [f"nb-{index}" for index in range(8)]
    for nb in ids:
        for position in range(4):
            _compile(analysis, tmp_path, nb, f"{nb}-wb{position}",
                     title=f"{nb} 报价 {position}", header=("Item", "Price"))
    libraries = tuple(
        (nb, tuple(f"{nb}-wb{position}" for position in range(4))) for nb in ids
    )
    planner = _Planner({"operation": "profile"})
    with _peer({nb: set(sources) for nb, sources in libraries}) as scope:
        results, _trace = _run_lane(
            _peer_ask(analysis, planner, _Seat(libraries)), _COMPARE, ids[0], scope,
        )

    [prompt] = planner.prompts
    catalog = _catalog(prompt)
    listed = len(catalog)
    assert 8 <= listed < 32
    assert len(json.dumps(catalog, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")) <= budget
    owners = [entry["source_id"].rsplit("-wb", 1)[0] for entry in catalog]
    assert len(set(owners[:8])) == 8, owners
    assert listed <= len(artifacts.loads) <= listed + 1
    assert "共有 32 份可读工作簿" in prompt
    assert f"列出其中 {listed} 份，另有 {32 - listed} 份未列出" in prompt
    [result] = results
    assert any(f"另有 {32 - listed} 份未列出" in warning for warning in result.warnings)

    # The single-library reference: the same 32 workbooks in ONE library.
    (tmp_path / "single").mkdir()
    single, single_artifacts = _service(
        tmp_path / "single", spreadsheet_analysis_planner_catalog_bytes=budget,
    )
    refs = []
    for nb in ids:
        for position in range(4):
            _compile(single, tmp_path / "single", "nb-one", f"{nb}-wb{position}",
                     title=f"{nb} 报价 {position}", header=("Item", "Price"))
            refs.append(("nb-one", f"{nb}-wb{position}"))
    single_planner = _Planner({"operation": "profile"})
    single.analyze(notebook_id="nb-one", source_ids=(), source_refs=tuple(refs),
                   question=_COMPARE, planner_client=single_planner)
    assert len(_catalog(single_planner.prompts[0])) == listed
    assert len(single_artifacts.loads) == 32


def test_rotation_head_follows_the_question_never_the_nominal_active(tmp_path):
    """No subject library: the order is libraries by id, rotated to a head
    derived from the question.  Reordering the participants or making another
    library the nominal active changes nothing; across questions every
    library takes the head."""
    analysis, _artifacts = _service(tmp_path)
    ids = ("nb-a", "nb-b", "nb-c")
    for nb in ids:
        _compile(analysis, tmp_path, nb, f"{nb}-quote", header=("Item", "Price"))
    heads = set()
    for index in range(24):
        question = f"汇总第 {index} 组报价表的价格"
        orders = []
        for participants in (ids, ("nb-c", "nb-a", "nb-b"), ("nb-b", "nb-c", "nb-a")):
            planner = _Planner({"operation": "profile"})
            seat = _Seat(tuple((nb, (f"{nb}-quote",)) for nb in participants))
            ceilings = {nb: {f"{nb}-quote"} for nb in participants}
            with _peer(ceilings) as scope:
                _run_lane(_peer_ask(analysis, planner, seat), question,
                          participants[0], scope)
            orders.append([
                entry["source_id"] for entry in _catalog(planner.prompts[0])
            ])
        assert orders[0] == orders[1] == orders[2], orders
        assert orders[0][0] == f"{sorted(ids)[_head(question, 3)]}-quote"
        heads.add(orders[0][0])
    assert heads == {f"{nb}-quote" for nb in ids}


def test_a_library_outside_the_run_or_a_source_outside_its_ceiling_never_enters(
    tmp_path,
):
    """Only libraries the run froze a ceiling for (the D0-5 narrowing, still
    narrowing-only) and only sources inside that ceiling: a library outside
    the selection (in production: one the asker cannot read, or never
    selected) and a deselected workbook are neither listed nor even read."""
    analysis, artifacts = _service(tmp_path)
    _compile(analysis, tmp_path, "nb-a", "quote-a", header=("Item", "Price"))
    _compile(analysis, tmp_path, "nb-a", "secret-a", header=("Item", "Price"))
    _compile(analysis, tmp_path, "nb-b", "quote-b", header=("Item", "Price"))
    _compile(analysis, tmp_path, "outsider", "quote-x", header=("Item", "Price"))
    planner = _Planner({"operation": "profile"})
    seat = _Seat((
        ("nb-a", ("quote-a", "secret-a")), ("nb-b", ("quote-b",)),
        ("outsider", ("quote-x",)),
    ))
    with _peer({"nb-a": {"quote-a"}, "nb-b": {"quote-b"}}) as scope:
        _results, trace = _run_lane(_peer_ask(analysis, planner, seat), _COMPARE,
                                    "nb-a", scope)

    assert {entry["source_id"] for entry in _catalog(planner.prompts[0])} == {
        "quote-a", "quote-b",
    }
    assert sorted(artifacts.loads) == [("nb-a", "quote-a"), ("nb-b", "quote-b")]
    assert "outsider" not in artifacts.listings
    assert trace[0].detail["libraries"] == 2


def test_no_table_intent_enumerates_no_library(tmp_path):
    """The cheap gate comes first: a global question without table intent
    reads neither the participant seat nor a single manifest."""
    analysis, artifacts = _service(tmp_path)
    _compile(analysis, tmp_path, "nb-a", "quote-a")
    planner = _Planner()
    seat = _Seat((("nb-a", ("quote-a",)),))
    with _peer({"nb-a": {"quote-a"}}) as scope:
        results, trace = _run_lane(_peer_ask(analysis, planner, seat),
                                   "这家公司成立于哪一年", "nb-a", scope)
    assert (results, trace, seat.calls, artifacts.loads, planner.prompts) == (
        [], [], [], [], [],
    )


def test_a_source_id_seen_in_two_libraries_is_listed_once(tmp_path):
    """The planner names a workbook by ``source_id`` alone, so an id already
    taken earlier in the rotation is skipped rather than listed twice."""
    analysis, artifacts = _service(tmp_path)
    _compile(analysis, tmp_path, "nb-a", "shared-id", header=("Item", "Price"))
    _compile(analysis, tmp_path, "nb-b", "shared-id", header=("Item", "Price"))
    planner = _Planner({"operation": "profile"})
    seat = _Seat((("nb-a", ("shared-id",)), ("nb-b", ("shared-id",))))
    with _peer({"nb-a": {"shared-id"}, "nb-b": {"shared-id"}}) as scope:
        [result], _trace = _run_lane(_peer_ask(analysis, planner, seat), _COMPARE,
                                     "nb-a", scope)
    first = sorted(("nb-a", "nb-b"))[_head(_COMPARE, 2)]
    assert [entry["source_id"] for entry in _catalog(planner.prompts[0])] == [
        "shared-id",
    ]
    assert artifacts.loads == [(first, "shared-id")]
    assert result.notebook_id == first


def test_one_library_peer_run_lists_what_the_single_library_lane_lists(tmp_path):
    """A global ask over ONE library must not lose a workbook to the cap: the
    catalog is the single-library catalog of that library, byte for byte
    (same visible order, same byte budget)."""
    analysis, _artifacts = _service(
        tmp_path, spreadsheet_analysis_planner_catalog_bytes=1_024,
    )
    sources = tuple(f"wb-{index:02}" for index in range(20))
    for source_id in sources:
        _compile(analysis, tmp_path, "nb-a", source_id, header=("Item", "Price"))
    single = _Planner({"operation": "profile"})
    analysis.analyze(notebook_id="nb-a", source_ids=sources, question=_COMPARE,
                     planner_client=single)
    peer = _Planner({"operation": "profile"})
    with _peer({"nb-a": set(sources)}) as scope:
        _run_lane(_peer_ask(analysis, peer, _Seat((("nb-a", sources),))),
                  _COMPARE, "nb-a", scope)
    single_catalog = single.prompts[0].split("可用工作簿：", 1)[1]
    peer_catalog = peer.prompts[0].split("可用工作簿：", 1)[1]
    assert peer_catalog == single_catalog


# ---------------------------------------------------------------------------
# End to end through the real global engine (backend neutral; the PostgreSQL
# twin is ``tests/postgres/test_spreadsheet_federation_pg.py``)
# ---------------------------------------------------------------------------

class _RecordingSheetAgent:
    """The ``reasoning_agent`` workload of ``global_citation_e2e_kit``, with
    every spreadsheet-planner prompt recorded and the plan aimed at one
    workbook chosen after the world is seeded."""

    configured = True
    model = "fake"

    def __init__(self) -> None:
        self.sheet_prompts: list[str] = []
        self.sheet_plan: dict = {"operation": "profile"}

    def chat_json(self, messages, schema_hint, **kwargs):
        if '"operation"' in schema_hint:
            self.sheet_prompts.append(messages[0]["content"])
            return json.dumps(self.sheet_plan)
        if "sub_queries" in schema_hint:
            return json.dumps({"sub_queries": [{"query": "报价"}]})
        if "next_action" in schema_hint:
            return json.dumps({"next_action": "answer", "sufficient": True})
        return json.dumps({})


def upload_quote(repo, notebook_id, tmp_path, name, rows) -> str:
    from app.repositories.ports import UploadedSourceFile

    path = tmp_path / f"{name}.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Quotes"
    sheet.append(["Item", "Price"])
    for row in rows:
        sheet.append(list(row))
    workbook.save(path)
    source = repo.upload_sources(notebook_id, [UploadedSourceFile(
        file_name=f"{name}.xlsx",
        content_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        content=path.read_bytes(),
    )], scheduler=lambda _source_id: None)[0]
    repo.process_source(source.id)
    return source.id


def build_quote_world(repo, tmp_path, owner, viewer) -> dict:
    """``owner`` keeps three libraries with one quote table each; ``viewer``
    is a read-only member of A and B only -- C is a library the viewer cannot
    read, and one the owner does not select."""
    from app.core.request_context import reset_request_user, set_request_user
    from app.models.notebooks import NotebookCreate

    marker = set_request_user(owner)
    try:
        world = {}
        for key, rows in (("a", [("bolt", 3)]), ("b", [("bolt", 5)]),
                          ("c", [("bolt", 9)])):
            notebook = repo.create_notebook(NotebookCreate(name=f"{key.upper()} 库"))
            world[key] = notebook.id
            world[f"src_{key}"] = upload_quote(
                repo, notebook.id, tmp_path, f"quote-{key}", rows,
            )
        repo._runtime.sharing.add_member(world["a"], viewer.id)
        repo._runtime.sharing.add_member(world["b"], viewer.id)
    finally:
        reset_request_user(marker)
    return world


def ask_globally(repo, user, notebook_ids, question):
    import threading
    import time

    from app.core.request_context import reset_request_user, set_request_user
    from app.models.global_ask import GlobalAskRequest

    marker = set_request_user(user)
    try:
        service = repo._runtime.global_ask_service()
        job = service.start(
            GlobalAskRequest(
                question=question, mode="reasoning",
                notebook_scope={"mode": "include", "notebook_ids": list(notebook_ids)},
            ),
            user_id=user.id,
        )
        deadline = time.monotonic() + 40
        while job.job_id in service._events and time.monotonic() < deadline:
            threading.Event().wait(0.02)
        return service.get_job(job.job_id, user_id=user.id)
    finally:
        reset_request_user(marker)


def assert_quote_federation(repo, tmp_path, owner, viewer) -> None:
    """The todo's case end to end, for the owner and for a read-only viewer:
    a global question over A and B lists BOTH quote tables in ONE planner
    call, either one is read (the plan aims at B's, then at A's), the card and
    the anchor carry the owning library, and C -- unselected for the owner,
    unreadable for the viewer -- never reaches the planner."""
    from tests import global_citation_e2e_kit as kit

    world = build_quote_world(repo, tmp_path, owner, viewer)
    agent = _RecordingSheetAgent()
    kit.bind_models(repo, agent)
    for user in (owner, viewer):
        for target in ("b", "a"):
            agent.sheet_prompts.clear()
            agent.sheet_plan = {
                "source_id": world[f"src_{target}"], "sheet": "Quotes",
                "operation": "top", "sort_by": "Price",
            }
            result = ask_globally(repo, user, [world["a"], world["b"]], _COMPARE)
            assert result.status == "done", result.error
            [prompt] = agent.sheet_prompts
            listed = {entry["source_id"] for entry in _catalog(prompt)}
            assert listed == {world["src_a"], world["src_b"]}, prompt
            assert world["src_c"] not in prompt
            [table] = [r for r in result.answer.result_sets if r.kind == "spreadsheet"]
            assert (table.source_id, table.notebook_id) == (
                world[f"src_{target}"], world[target],
            )
            [receipt] = [a for a in result.answer.anchors if a.key == "k6001"]
            assert (receipt.notebook_id, receipt.source_id) == (
                world[target], world[f"src_{target}"],
            )
            kit.assert_attributed(result)

    # The rollback switch of the chunk lane is the workbook lane's too: one
    # source, one rule -- only the nominal active (the first selected) stays.
    repo.settings.chunk_federation_enabled = False
    agent.sheet_prompts.clear()
    agent.sheet_plan = {"operation": "profile"}
    result = ask_globally(repo, owner, [world["a"], world["b"]], _COMPARE)
    assert result.status == "done", result.error
    [prompt] = agent.sheet_prompts
    assert {entry["source_id"] for entry in _catalog(prompt)} == {world["src_a"]}


def test_quote_tables_of_two_libraries_federate_end_to_end(tmp_path, monkeypatch):
    from tests import global_citation_e2e_kit as kit

    repo = kit.make_repo(tmp_path, monkeypatch)
    try:
        owner = repo.current_user()
        viewer = repo._runtime.identity.create_user("v00000003", "pw123456")
        assert_quote_federation(repo, tmp_path, owner, viewer)
    finally:
        repo.close()
