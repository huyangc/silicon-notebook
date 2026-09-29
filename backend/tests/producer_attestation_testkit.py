"""Shared drivers for the PR-D producer race tests (D3, D4, D7).

Three non-federated citation producers register the evidence they cite through
``services.evidence_attestation``:

* document overview (``document_source_overview.prepare_source_overview``) and
  collection enumeration (``CollectionEnumerationService.enumerate_elements``)
  READ the element text themselves and register it with ``attest_read``;
* table analysis (``SpreadsheetAnalysisService``) cites a row element it never
  read and registers it with ``attest_pointers``.

Each driver runs the REAL producer against a REAL repository inside the shape a
global run installs (peer source scope, run plan, attestation seat), then the
REAL terminal check (``GlobalCitationCheck``) re-reads the store after one
mutation. The SQLite file ``tests/test_producer_attestation_races.py`` and the
PostgreSQL twin ``tests/postgres/test_producer_attestation_races_pg.py`` drive
the same rows through the same functions; ``marker`` is ``?`` or ``%s``.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

NOW = "2026-09-29T00:00:00Z"

SOURCE = "src-attest"
CITED, SIBLING = "el-attest-0000", "el-attest-0001"
#: Long enough that every producer's excerpt is a strict prefix: the snapshot
#: must be the digest of the whole stored body, never of what the card quotes.
TEXTS = {
    CITED: "首段 · 低温下增益随温度下降而上升 🔋，零下四十度仍满足指标；"
           "第二句继续说明测试条件与样本数量。\r\n第三行。",
    SIBLING: "次段 · 常温基线与对照组的测量方法，另附误差分析与重复实验记录。",
}

#: mutation (between retrieval and the terminal read) -> the cited verdict.
RACE_EXPECTATIONS = {
    "none": None,
    "update": "changed",
    "delete": "source_gone",
    "reinsert": None,
}

_SPREADSHEET_QUESTION = "分析这个 Excel 的数据质量和缺失情况"


def seed(database, marker: str, notebook_id: str) -> None:
    """One parsed source holding the two ``formula`` elements above."""
    values = lambda count: ",".join(marker for _ in range(count))  # noqa: E731
    with database.write() as db:
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,status,parse_status,"
            f"created_at,updated_at) VALUES({values(8)})",
            (SOURCE, notebook_id, "Original", "markdown", "extracted", "parsed", NOW, NOW),
        )
        for index, (element_id, text) in enumerate(TEXTS.items()):
            db.execute(
                "INSERT INTO source_elements(id,source_id,element_type,location_label,"
                f"text,metadata,created_at) VALUES({values(7)})",
                (element_id, SOURCE, "formula", f"p{index}", text, "{}", NOW),
            )


def mutate(database, marker: str, mutation: str) -> None:
    """Apply one race mutation to the cited element."""
    with database.write() as db:
        if mutation == "update":
            db.execute(
                f"UPDATE source_elements SET text={marker} WHERE id={marker}",
                (TEXTS[CITED] + "（修订）", CITED),
            )
        elif mutation in {"delete", "reinsert"}:
            db.execute(f"DELETE FROM source_elements WHERE id={marker}", (CITED,))
            if mutation == "reinsert":
                db.execute(
                    "INSERT INTO source_elements(id,source_id,element_type,"
                    "location_label,text,metadata,created_at) VALUES("
                    + ",".join(marker for _ in range(7)) + ")",
                    (CITED, SOURCE, "formula", "p0", TEXTS[CITED], "{}", NOW),
                )


@contextmanager
def global_run(sources, notebook_id: str):
    """What ``GlobalAskService`` installs around a run: a subjectless (peer)
    source scope, the run plan whose ``on_evidence`` folds into ``_RunState``,
    and the attestation seat reading through the real store."""
    from app.services.evidence_attestation import evidence_attestation_seat
    from app.services.federated_run import FederatedRunPlan, federated_run_plan
    from app.services.global_ask import _RunState
    from app.services.source_scope import source_scope_context

    state = _RunState((notebook_id,))
    events: list = []
    plan = FederatedRunPlan(
        phase_timeout_seconds=30.0, notebook_timeout_seconds=10.0,
        executor=None, window=lambda: 1, cancel=None,
        on_library=lambda *_: None, on_evidence=state.record_evidence,
        on_evidence_groups=state.record_evidence_groups,
    )
    with source_scope_context(
        notebook_id, None, None,
        notebook_source_ceilings={notebook_id: [SOURCE]}, subjectless=True,
    ), federated_run_plan(plan), evidence_attestation_seat(sources, emit=events.append):
        yield SimpleNamespace(state=state, events=events)


def terminal_check(sources, run, notebook_id: str, citations):
    """The real terminal read over the run's accumulated evidence."""
    from app.models.ask import AskResponse
    from app.services.global_citation_check import GlobalCitationCheck, apply_outcome

    response = AskResponse(
        conclusion="结论", answer="答案", grounded=True, evidence_level="grounded",
        citations=list(citations),
    )
    outcome = GlobalCitationCheck(sources, notebook_timeout_seconds=10.0).run(
        response, evidence=run.state.evidence_snapshot(),
        siblings=run.state.sibling_snapshot(),
        source_ceiling={notebook_id: frozenset({SOURCE})},
    )
    apply_outcome(response, outcome)
    return response


# ---------------------------------------------------------------------------
# The three real producers
# ---------------------------------------------------------------------------

def overview_citations(sources, notebook_id: str) -> list:
    """D3: a budget small enough that each quote is a strict prefix."""
    from app.services.collection_enumeration import SourceItem
    from app.services.document_source_overview import prepare_source_overview

    item = SourceItem(
        source_id=SOURCE, source_title="Original", doc_type_label="", summary="",
        notebook_id=notebook_id, tier="personal",
    )
    overview = prepare_source_overview(
        sources, item, budget_chars=60, max_elements=2,
        active_notebook_id=notebook_id,
    )
    for citation in overview.citations:
        text = TEXTS[citation.element_id]
        assert citation.quoted_span and text.startswith(citation.quoted_span)
        assert citation.element_id != CITED or citation.quoted_span != text
    return list(overview.citations)


def enumeration_citations(repository, notebook_id: str, *, between=None) -> list:
    """D4: the executor lists the elements (excerpt cut to 12 characters) and
    the production minting (``collection_item_citations``) turns them into
    cards. ``between`` runs after the listing and before the minting -- the
    window in which the model has already seen the listed text."""
    from app.services.collection_enumeration import EnumerationBudget

    listed = repository.collection_enumeration.enumerate_elements(
        notebook_id, "formula",
        budget=EnumerationBudget(
            page_size=25, max_rows=100, max_pages=10, max_payload_chars=256_000,
            excerpt_chars=12,
        ),
    )
    for item in listed.items:
        assert item.text == TEXTS[item.element_id][:12]
    if between is not None:
        between()
    minted = repository._runtime.evidence_context_component.collection_item_citations(
        list(listed.items), active_notebook_id=notebook_id,
    )
    return [minted[item.element_id] for item in listed.items if item.element_id in minted]


class _OfflinePlanner:
    configured = False


def table_citations(tmp_path: Path, notebook_id: str) -> list:
    """D7: a workbook whose data rows are anchored to the two elements; the
    offline plan profiles the sheet and cites the first data row (``CITED``)."""
    from openpyxl import Workbook

    from app.core.config import Settings
    from app.repositories.analysis_artifacts import AnalysisArtifactStore
    from app.services.spreadsheet_analysis import SpreadsheetAnalysisService

    path = tmp_path / "attest.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Sales"
    sheet.append(["Region", "Amount"])
    sheet.append(["East", 10])
    sheet.append(["West", 20])
    workbook.save(path)
    settings = Settings(
        _env_file=None, storage_dir=str(tmp_path / "sheet-storage"),
        event_log_enabled=False, llm_log_enabled=False,
    )
    service = SpreadsheetAnalysisService(
        artifacts=AnalysisArtifactStore(Path(settings.storage_dir), retention_days=30),
        settings=settings,
        event_log=SimpleNamespace(
            emit=lambda _event: None,
            logger=SimpleNamespace(warning=lambda *a, **k: None,
                                   exception=lambda *a, **k: None),
        ),
        now=lambda: NOW,
    )
    assert service.compile_source(
        SimpleNamespace(
            id=SOURCE, notebook_id=notebook_id, title="Original", type="xlsx",
            file_name="attest.xlsx", file_path=str(path), file_hash="h", source_url="",
        ),
        notebook_name="Notebook", owner_id="owner",
        row_element_ids={("Sales", 2): CITED, ("Sales", 3): SIBLING},
    )
    results, _trace = service.analyze(
        notebook_id=notebook_id, source_ids=(SOURCE,),
        question=_SPREADSHEET_QUESTION, planner_client=_OfflinePlanner(),
    )
    return [row.citation for result in results for row in result.rows if row.citation]


def cited(citations, element_id: str = CITED):
    """The one card naming ``element_id`` (the producers mint one per element)."""
    [found] = [citation for citation in citations if citation.element_id == element_id]
    return found
