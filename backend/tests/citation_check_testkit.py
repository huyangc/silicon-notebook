"""Shared seeding and race driver for the citation-check twin tests.

Used by the SQLite files under ``backend/tests`` and their PostgreSQL twins
under ``backend/tests/postgres``: one seeding routine per placeholder marker,
so both backends are driven through the SAME rows and the SAME race.
"""
from __future__ import annotations

from types import SimpleNamespace

NOW = "2026-09-29T00:00:00Z"

#: Texts whose digests must agree across the Python helper and PostgreSQL's
#: in-SQL hash. ``combining`` and ``precomposed`` render alike and must NOT
#: collide: no Unicode normalization happens on either side.
TWIN_TEXTS = {
    "cjk": "低温性能 · 增益随温度下降而上升",
    "emoji": "电池 🔋 与试剂 🧪 👩‍🔬",
    "crlf": "第一行\r\n第二行\r\n",
    "combining": "Café résumé",
    "precomposed": "Café résumé",
    "ascii": "plain ascii evidence",
    "empty": "",
}

RACE_SOURCE = "src-race"
RACE_ELEMENTS = ("el-src-race-0000", "el-src-race-0001")
RACE_TEXTS = ("首段 · 低温下增益上升 🔋", "次段 · 零下四十度仍满足指标\r\n")
RACE_CHUNK = "chunk-race"

#: mutation -> the verdict the terminal check must reach for the cited element.
RACE_EXPECTATIONS = {
    "none": None,
    "update": "changed",
    "delete": "source_gone",
    "reinsert": None,
    "sibling_update": "changed",
    "source_delete": "source_gone",
}


def seed_source(db, marker: str, *, notebook_id: str, source_id: str,
                elements: dict, chunk: tuple | None = None) -> None:
    """One source with ``elements`` (``{id: text}``) and optionally one chunk
    ``(chunk_id, element_ids, text)``. ``marker`` is ``?`` or ``%s``."""
    def values(count):
        return ",".join(marker for _ in range(count))

    db.execute(
        "INSERT INTO sources(id,notebook_id,title,source_type,created_at,updated_at) "
        f"VALUES({values(6)})",
        (source_id, notebook_id, "Original", "markdown", NOW, NOW),
    )
    for index, (element_id, text) in enumerate(elements.items()):
        db.execute(
            "INSERT INTO source_elements(id,source_id,element_type,location_label,"
            f"text,created_at) VALUES({values(6)})",
            (element_id, source_id, "paragraph", f"p{index}", text, NOW),
        )
    if chunk is not None:
        import json

        chunk_id, element_ids, text = chunk
        cast = "::jsonb" if marker == "%s" else ""
        db.execute(
            "INSERT INTO chunks(id,notebook_id,source_id,text,section_path,"
            f"element_ids,created_at) VALUES({marker},{marker},{marker},{marker},"
            f"{marker},{marker}{cast},{marker})",
            (chunk_id, notebook_id, source_id, text, "", json.dumps(list(element_ids)), NOW),
        )


def seed_race(database, marker: str, notebook_id: str) -> str:
    """The two-element passage the race tests cite; returns its text."""
    passage = " ".join(RACE_TEXTS)
    with database.write() as db:
        seed_source(
            db, marker, notebook_id=notebook_id, source_id=RACE_SOURCE,
            elements=dict(zip(RACE_ELEMENTS, RACE_TEXTS)),
            chunk=(RACE_CHUNK, RACE_ELEMENTS, passage),
        )
    return passage


def mutate(database, marker: str, mutation: str) -> None:
    """Apply one race mutation between the snapshot read and the terminal read."""
    cited, sibling = RACE_ELEMENTS
    with database.write() as db:
        if mutation == "update":
            db.execute(
                f"UPDATE source_elements SET text={marker} WHERE id={marker}",
                (RACE_TEXTS[0] + "（修订）", cited),
            )
        elif mutation == "sibling_update":
            db.execute(
                f"UPDATE source_elements SET text={marker} WHERE id={marker}",
                ("次段被改写", sibling),
            )
        elif mutation in {"delete", "reinsert"}:
            db.execute(f"DELETE FROM source_elements WHERE id={marker}", (cited,))
            if mutation == "reinsert":
                db.execute(
                    "INSERT INTO source_elements(id,source_id,element_type,"
                    "location_label,text,created_at) VALUES("
                    + ",".join(marker for _ in range(6)) + ")",
                    (cited, RACE_SOURCE, "paragraph", "p0", RACE_TEXTS[0], NOW),
                )
        elif mutation == "source_delete":
            db.execute(f"DELETE FROM sources WHERE id={marker}", (RACE_SOURCE,))


def passage_race(sources, database, marker: str, notebook_id: str, mutation: str):
    """Drive the REAL federated passage producer and the REAL terminal check.

    ``chunk_federation._report_evidence`` takes the retrieval-time snapshot out
    of the store (``passage_evidence_snapshot``), the run state folds it, the
    mutation lands, then ``GlobalCitationCheck`` re-reads the store. Returns
    the checked ``AskResponse``.
    """
    from app.models.ask import AskResponse, Citation
    from app.services import chunk_federation
    from app.services.global_ask import _RunState
    from app.services.global_citation_check import GlobalCitationCheck, apply_outcome

    passage = seed_race(database, marker, notebook_id)
    state = _RunState((notebook_id,))
    plan = SimpleNamespace(
        on_evidence=state.record_evidence,
        on_evidence_groups=state.record_evidence_groups,
        notebook_timeout_seconds=10.0, cancel=None,
    )
    candidates = SimpleNamespace(sources=sources, event_log=None)
    collected = {RACE_CHUNK: SimpleNamespace(text=passage, element_ids=RACE_ELEMENTS)}
    chunk_federation._report_evidence(candidates, plan, collected, 0.0)
    assert set(state.evidence) == set(RACE_ELEMENTS)
    assert all(value is not None for value in state.evidence.values())
    mutate(database, marker, mutation)
    response = AskResponse(
        conclusion="结论", answer="答案", grounded=True, evidence_level="grounded",
        citations=[Citation(
            label="Original", source_id=RACE_SOURCE, element_id=RACE_ELEMENTS[0],
            location_label="p0", quoted_span=RACE_TEXTS[0], notebook_id=notebook_id,
        )],
    )
    outcome = GlobalCitationCheck(sources, notebook_timeout_seconds=10.0).run(
        response, evidence=state.evidence_snapshot(),
        siblings=state.sibling_snapshot(),
        source_ceiling={notebook_id: frozenset({RACE_SOURCE})},
    )
    apply_outcome(response, outcome)
    return response


class _CountingConnection:
    """Delegates to a real connection and records every statement."""

    def __init__(self, inner, statements: list):
        self._inner = inner
        self._statements = statements
        self._entered = None

    def __enter__(self):
        self._entered = self._inner.__enter__()
        return self

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)

    def execute(self, sql, *args, **kwargs):
        self._statements.append(sql)
        return (self._entered or self._inner).execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._entered or self._inner, name)


class count_statements:
    """``with count_statements(database) as statements:`` -- every statement
    any read issues through ``database.connect()`` inside the block."""

    def __init__(self, database):
        self.database = database
        self.statements: list = []

    def __enter__(self):
        self._connect = self.database.connect
        self.database.connect = lambda *a, **k: _CountingConnection(
            self._connect(*a, **k), self.statements,
        )
        return self.statements

    def __exit__(self, *exc):
        self.database.connect = self._connect
        return False


def seed_libraries(database, marker: str, notebook_ids) -> dict:
    """One source + one element per library; returns ``{notebook: element_id}``."""
    elements = {}
    with database.write() as db:
        for notebook_id in notebook_ids:
            element_id = f"el-{notebook_id}"
            seed_source(db, marker, notebook_id=notebook_id, source_id=f"src-{notebook_id}",
                        elements={element_id: f"正文 {notebook_id} 🔋"})
            elements[notebook_id] = element_id
    return elements


def terminal_check_statements(sources, database, marker: str, notebook_ids) -> tuple:
    """Run the terminal check over one citation per library; ``(statements, outcome)``."""
    from app.models.ask import AskResponse, Citation
    from app.services.global_citation_check import GlobalCitationCheck

    elements = seed_libraries(database, marker, notebook_ids)
    evidence = dict(sources.evidence_fingerprints(list(elements.values())))
    response = AskResponse(conclusion="结论", answer="答案", citations=[
        Citation(label="l", source_id=f"src-{nb}", element_id=element_id,
                 location_label="p0", quoted_span="q", notebook_id=nb)
        for nb, element_id in elements.items()
    ])
    with count_statements(database) as statements:
        outcome = GlobalCitationCheck(sources, notebook_timeout_seconds=10.0).run(
            response, evidence=evidence, siblings={},
            source_ceiling={nb: frozenset({f"src-{nb}"}) for nb in notebook_ids},
        )
    return list(statements), outcome


def dangling_response(live_id: str, source_id: str):
    """Citations/anchors mixing a live element with dead ones (J2 tests)."""
    from app.models.ask import AnswerAnchor, AskResponse, Citation

    def citation(element_id):
        return Citation(label="l", source_id=source_id, element_id=element_id,
                        location_label="p0", quoted_span="q")

    return AskResponse(
        conclusion="A B [k2] C [k1, k3].", answer="A [k1] B [k2] C [k1, k3].",
        citations=[citation(live_id), citation("el-dead")],
        anchors=[
            AnswerAnchor(key="k1", object_id="el-dead", object_type="element",
                         label="dead", source_id=source_id, element_id="el-dead"),
            AnswerAnchor(key="k2", object_id="obj-1", object_type="claim",
                         label="claim", source_id=source_id, element_id="el-dead-2"),
            AnswerAnchor(key="k3", object_id=live_id, object_type="element",
                         label="live", source_id=source_id, element_id=live_id),
        ],
    )


def assert_dangling_dropped(response, live_id: str) -> None:
    assert response.answer == "A B [k2] C [k3]."
    assert response.conclusion == "A B [k2] C [k3]."
    assert [row.element_id for row in response.citations] == [live_id]
    assert [(row.key, row.element_id) for row in response.anchors] == [
        ("k2", ""), ("k3", live_id),
    ]
    assert "verification" not in response.model_dump_json()
    assert response.citation_check is None
