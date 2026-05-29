from __future__ import annotations

import io
import tempfile
import zipfile
from pathlib import Path

from app.core.config import Settings
from app.models.schemas import (
    ArticleCreate,
    AskRequest,
    CaseSearchRequest,
    ChecklistRequest,
    Evidence,
    FeedbackRequest,
    KnowledgeUpdate,
    MergeRequest,
    NotebookCreate,
    NotebookUpdate,
    SourceElement,
)
from app.services.demo_repository import DEMO_NOTEBOOK_ID
from app.services.extraction import run_extraction
from app.services.parsers import mineru_content_list_to_elements
from app.services.repository import UploadedSourceFile
from app.services.retrieval import score_knowledge
from app.services.sqlite_repository import SQLiteRepository


class _NoLLM:
    """Stub LLM client (unconfigured) to force the deterministic heuristic path."""

    configured = False


def check_heuristic_extraction() -> None:
    """E1: sentence-level heuristics — no empty-glossary noise, multi-candidate,
    definition-based glossary, risk-clause split, noise element types skipped."""

    def el(index: int, text: str, element_type: str = "paragraph") -> SourceElement:
        return SourceElement(
            id=f"e{index}",
            source_id="s",
            element_type=element_type,
            location_label=f"L{index}",
            text=text,
            metadata={},
        )

    elements = [
        el(1, "Wirebond Review Guideline", "heading"),
        el(2, "Quiet ground: the dedicated return path for sensitive analog currents."),
        el(
            3,
            "The ESD path must not share the sensitive input return otherwise noise "
            "couples into the AFE. Designers should keep the bond loop short.",
        ),
        el(4, "Pin | Net | Note ; 1 | GND | quiet return", "table"),
    ]
    records = run_extraction(_NoLLM(), elements, "t")

    glossary = [r for r in records if r.candidate_type == "glossary"]
    assert len(glossary) == 1, "only the definition line should yield a glossary term"
    assert glossary[0].payload["term"].lower().startswith("quiet ground")
    rules = [r for r in records if r.candidate_type == "rule"]
    assert len(rules) >= 2, "a 2-sentence directive paragraph should yield >=2 rules"
    assert any(r.payload.get("risk_if_ignored") for r in rules), "risk clause should split out"
    # The plain heading produced no candidate; the table element was skipped.
    assert all(
        not any(ev.element_id in {"e1", "e4"} for ev in r.evidence) for r in records
    ), "headings/tables must not spawn heuristic candidates"


def _ev(element_id: str, quoted_span: str) -> Evidence:
    return Evidence(
        source_id="s",
        source_title="t",
        element_id=element_id,
        element_type="paragraph",
        location_label="p",
        quoted_span=quoted_span,
        confidence=0.7,
    )


def check_csv_xlsx_parsing() -> None:
    """T4: CSV (stdlib) and XLSX (openpyxl) parse into table_row elements."""
    from app.services.parsers import parse_csv, parse_xlsx

    with tempfile.TemporaryDirectory(prefix="sn-csv-") as tmp:
        csv_path = Path(tmp) / "rules.csv"
        csv_path.write_text("Pin,Net,Note\n1,GND,quiet return\n2,VIN,sensitive input\n", encoding="utf-8")
        csv_elements = parse_csv("src-csv", csv_path)
        assert len(csv_elements) == 3 and all(e.element_type == "table_row" for e in csv_elements)
        assert "GND" in csv_elements[1].text

        try:
            from openpyxl import Workbook
        except ImportError:
            return
        xlsx_path = Path(tmp) / "rules.xlsx"
        wb = Workbook()
        ws = wb.active
        ws.append(["Pin", "Net", "Note"])
        ws.append([1, "GND", "quiet return"])
        wb.save(str(xlsx_path))
        xlsx_elements = parse_xlsx("src-xlsx", xlsx_path)
        assert xlsx_elements and xlsx_elements[0].element_type == "table_row"
        assert "GND" in " ".join(e.text for e in xlsx_elements)


def check_mineru_mapping() -> None:
    """MinerU content_list -> SourceElements: formulas keep LaTeX, tables flatten."""
    content_list = [
        {"type": "title", "text": "Bondwire coupling", "text_level": 1, "page_idx": 0},
        {"type": "text", "text": "Return path matters.", "text_level": 0, "page_idx": 0},
        {
            "type": "equation",
            "text": "$$\nV = I \\cdot R\n$$",
            "text_format": "latex",
            "page_idx": 1,
        },
        {
            "type": "table",
            "table_body": "<table><tr><td>Pin</td><td>Net</td></tr><tr><td>1</td><td>GND</td></tr></table>",
            "table_caption": ["Pinout"],
            "page_idx": 1,
        },
    ]
    elements = mineru_content_list_to_elements("src-x", content_list)
    types = {element.element_type for element in elements}
    assert "heading" in types and "paragraph" in types
    formula = next(element for element in elements if element.element_type == "formula")
    assert "V = I \\cdot R" in formula.text and "$$" not in formula.text
    assert formula.location_label == "PDF p.2 formula 1"
    table = next(element for element in elements if element.element_type == "table")
    assert "Pinout" in table.text and "GND" in table.text
    assert table.metadata.get("table_html", "").startswith("<table>")


def check_score_knowledge() -> None:
    """score_knowledge: keyword baseline unchanged, vector adds semantic recall."""
    objects = [
        {
            "id": "k1",
            "payload": {"title": "quiet ground return path", "statement": "keep return quiet"},
            "evidence": [_ev("e1", "quiet ground")],
        },
        {
            "id": "k2",
            "payload": {"title": "thermal via array", "statement": "use thermal vias"},
            "evidence": [_ev("e2", "thermal via")],
        },
    ]

    keyword_only = score_knowledge("quiet ground", objects, "rule")
    assert keyword_only and keyword_only[0].object_id == "k1"

    element_vectors = {"e1": [1.0, 0.0], "e2": [0.0, 1.0]}
    # Query shares no tokens with either object, but its vector matches e2.
    semantic = score_knowledge("xyzzy plugh", objects, "rule", [0.0, 1.0], element_vectors)
    assert semantic and semantic[0].object_id == "k2", "vector recall should surface k2"

    # query_vector=None must behave exactly like the keyword-only baseline.
    none_vector = score_knowledge("quiet ground", objects, "rule", None, element_vectors)
    assert [item.object_id for item in none_vector] == [item.object_id for item in keyword_only]


def check_cjk_keyword_recall() -> None:
    """WS1: Chinese keyword retrieval works without verbatim phrase equality."""
    from app.services.retrieval import _tokens, keyword_score

    toks = _tokens("低噪声封装")
    assert {"低噪", "噪声", "声封", "封装"} <= set(toks), toks
    assert keyword_score("低噪声 封装", "本节讨论低噪声模拟前端的封装注意事项") > 0

    rules = [
        {"id": "ko-cjk", "payload": {"title": "低噪声封装规则",
            "statement": "低噪声模拟前端封装应保证安静地回流路径。"}, "evidence": [], "status": "approved"},
        {"id": "ko-other", "payload": {"title": "数字时钟布线",
            "statement": "高速数字时钟走线需要包地处理。"}, "evidence": [], "status": "approved"},
    ]
    scored = score_knowledge("低噪声 封装 回流", rules, "rule", None, None)
    ids = [s.object_id for s in scored]
    assert ids and ids[0] == "ko-cjk", ids
    assert "ko-other" not in ids  # dropped by relevance floor
    print("[smoke] cjk keyword recall ok")


def check_hybrid_floor_and_weight() -> None:
    """WS1: relevance floor drops noise; type weight kept separate from relevance."""
    objs = [
        {"id": "hit", "payload": {"title": "quiet ground return path"}, "evidence": [], "status": "approved"},
        {"id": "noise", "payload": {"title": "thermal vias for power module layout"}, "evidence": [], "status": "approved"},
    ]
    scored = score_knowledge("quiet ground return", objs, "rule", None, None)
    ids = [s.object_id for s in scored]
    assert "hit" in ids and "noise" not in ids, ids
    assert scored[0].weight == 1.0  # authority weight, not folded into relevance
    assert abs(scored[0].score - scored[0].relevance) < 1e-9  # no scenario boost
    print("[smoke] hybrid floor + weight ok")


def check_fuzzy_evidence_binding() -> None:
    """WS2: paraphrased / re-punctuated spans still bind (no needs_review)."""
    from app.models.schemas import SourceElement
    from app.services.extraction import bind_evidence

    elements = [
        SourceElement(id="el-1", source_id="src-1", element_type="paragraph", location_label="p.1",
            text="低噪声模拟前端封装应保证安静地回流路径，避免噪声耦合。")
    ]
    span = "低噪声模拟前端封装，应保证安静的回流路径"  # not a substring
    ev = bind_evidence(span, elements, "src", 0.7)
    assert ev is not None and ev.element_id == "el-1"
    print("[smoke] fuzzy evidence binding ok")


def check_extraction_windowing() -> None:
    """WS2: large docs are covered in full, not just the first window."""
    from app.models.schemas import SourceElement
    from app.services.extraction import _element_windows

    elements = [
        SourceElement(id=f"el-{i}", source_id="s", element_type="paragraph",
            location_label=f"p.{i}", text="x" * 1500)
        for i in range(12)
    ]
    windows = _element_windows(elements)
    assert len(windows) > 1
    covered = {e.id for window in windows for e in window}
    assert covered == {e.id for e in elements}  # full coverage incl. last element
    print("[smoke] extraction windowing ok")


def check_payload_embedding_retrieval() -> None:
    """WS4: a rule whose own payload vector matches the query beats one whose
    only (unrelated) signal is an evidence-element vector."""
    query_vector = [1.0, 0.0]
    knowledge_vectors = {"A": [1.0, 0.0], "B": [0.0, 1.0]}
    element_vectors = {"elB": [0.0, 1.0]}
    objs = [
        {"id": "A", "payload": {"title": "alpha"}, "evidence": [], "status": "approved"},
        {"id": "B", "payload": {"title": "beta"},
            "evidence": [type("E", (), {"element_id": "elB", "quoted_span": "z"})()], "status": "approved"},
    ]
    scored = score_knowledge("zzz", objs, "rule", query_vector, element_vectors, knowledge_vectors)
    assert scored and scored[0].object_id == "A", [s.object_id for s in scored]
    print("[smoke] payload-level embedding retrieval ok")


def check_structured_scenario_boost() -> None:
    """WS3: a scenario field matching applies_to boosts that rule above an
    otherwise identical rule without the tag."""
    objs = [
        {"id": "tagged", "payload": {"title": "return path coupling", "applies_to": ["wirebond"]},
            "evidence": [], "status": "approved"},
        {"id": "untagged", "payload": {"title": "return path coupling"}, "evidence": [], "status": "approved"},
    ]
    scenario = {"package_type": "Wirebond"}
    scored = score_knowledge("return path coupling", objs, "rule", None, None, None, scenario)
    order = [s.object_id for s in scored]
    assert order and order[0] == "tagged", order
    print("[smoke] structured scenario boost ok")


def check_json_fences() -> None:
    """chat_json strips ```json fences some models wrap responses in."""
    from app.core.llm import strip_json_fences

    assert strip_json_fences('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert strip_json_fences('```\n{"b": 2}\n```') == '{"b": 2}'
    assert strip_json_fences('{"c": 3}') == '{"c": 3}'


def check_llm_interaction_logging() -> None:
    """LLM interaction logger: writes parseable JSONL, clips long text, and
    never raises even when the target path is unwritable."""
    import json as _json

    from app.core.config import Settings
    from app.core.llm_logging import LLMInteractionLogger

    with tempfile.TemporaryDirectory(prefix="silicon-notebook-llmlog-") as temp_dir:
        log_path = Path(temp_dir) / "logs" / "llm.jsonl"
        logger = LLMInteractionLogger(
            Settings(llm_log_path=str(log_path), llm_log_max_chars=20)
        )
        clipped = logger.clip("x" * 100)
        assert clipped.startswith("x" * 20) and "chars]" in clipped, clipped
        logger.log(
            {
                "ts": "2026-05-29T00:00:00",
                "id": "llm-test",
                "kind": "chat",
                "model": "test-model",
                "status": "ok",
                "latency_ms": 12,
                "usage": {"total_tokens": 7},
                "response": {"content": "{}"},
            }
        )
        line = log_path.read_text(encoding="utf-8").strip().splitlines()[-1]
        parsed = _json.loads(line)
        for field in ("ts", "id", "kind", "model", "status", "latency_ms"):
            assert field in parsed, field

        # A logging failure must not propagate: parent path is a file, not a dir.
        bad = LLMInteractionLogger(Settings(llm_log_path=str(log_path)))
        bad.path = log_path / "nope.jsonl"
        bad.log({"kind": "embed", "model": "m", "status": "ok", "latency_ms": 1, "dims": 3})

        # Disabled logger writes nothing.
        off_path = Path(temp_dir) / "off.jsonl"
        off = LLMInteractionLogger(
            Settings(llm_log_path=str(off_path), llm_log_enabled=False)
        )
        off.log({"kind": "chat", "model": "m", "status": "ok", "latency_ms": 1})
        assert not off_path.exists()
    print("[smoke] llm interaction logging ok")


def check_event_logging() -> None:
    """EventLogger: writes parseable JSONL with ts/channel, honors disable, and
    never raises when the target path is unwritable."""
    import json as _json

    from app.core.config import Settings
    from app.core.event_logging import EventLogger

    with tempfile.TemporaryDirectory(prefix="silicon-notebook-eventlog-") as temp_dir:
        logger = EventLogger(Settings(event_log_dir=temp_dir), channel="events")
        logger.emit({"kind": "pipeline", "stage": "parse", "status": "done", "latency_ms": 5})
        line = (Path(temp_dir) / "events.jsonl").read_text(encoding="utf-8").strip().splitlines()[-1]
        parsed = _json.loads(line)
        assert parsed["channel"] == "events" and "ts" in parsed, parsed
        assert parsed["stage"] == "parse"

        # Logging failure must not propagate (parent is a file, not a dir).
        bad = EventLogger(Settings(event_log_dir=temp_dir), channel="events")
        bad.path = Path(temp_dir) / "events.jsonl" / "nope.jsonl"
        bad.emit({"kind": "status", "status": "ok"})

        # Disabled logger writes nothing to its channel file.
        off = EventLogger(Settings(event_log_dir=temp_dir, event_log_enabled=False), channel="off")
        off.emit({"kind": "x", "status": "ok"})
        assert not (Path(temp_dir) / "off.jsonl").exists()
    print("[smoke] event logging ok")


def check_pipeline_event_logging() -> None:
    """process_source emits pipeline stage events, and a failing source records
    its real error in error_message (regression for the _set_source_status bug)."""
    import json as _json

    from app.models.schemas import NotebookCreate
    from app.services.repository import UploadedSourceFile

    with tempfile.TemporaryDirectory(prefix="silicon-notebook-pipe-") as temp_dir:
        root = Path(temp_dir)
        log_dir = root / "logs"
        repo = SQLiteRepository(
            Settings(
                database_url=f"sqlite:///{root / 'db.sqlite'}",
                storage_dir=str(root / "storage"),
                event_log_dir=str(log_dir),
                mineru_mode="off",
                openai_compat_base_url="",
                openai_compat_api_key="",
                openai_compat_model="",
                openai_compat_embedding_model="",
            )
        )
        notebook = repo.create_notebook(NotebookCreate(name="Pipe", purpose="p", primary_domain="d"))
        uploaded = repo.upload_sources(
            notebook.id,
            [UploadedSourceFile(file_name="s.md", content_type="text/markdown",
                                content=b"# H\n\nrequire a quiet ground return path.\n")],
        )
        assert uploaded[0].parse_status == "extracted"

        events = [
            _json.loads(line)
            for line in (log_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        pipeline = [e for e in events if e.get("kind") == "pipeline"]
        stages = {e["stage"] for e in pipeline}
        assert {"parse", "embed", "extract"} <= stages, stages
        assert any(e["stage"] == "pipeline" and e["status"] == "done" for e in pipeline)
        assert any(e.get("kind") == "status" for e in events)

        # Regression: a failure stores the real exception in error_message
        # (previously the bug wrote summary into that column).
        src = repo.get_source(uploaded[0].id)
        broken = Path(src.file_path)
        broken.unlink()  # make parse fail on manual reparse
        result = repo.parse_source(uploaded[0].id)
        assert result.parse_status == "failed"
        # error_message must carry the real exception, not the summary string.
        assert result.error_message, "failed source must record an error_message"
        assert result.error_message != "Parsing failed; see source error."
    print("[smoke] pipeline event logging + error_message ok")


def main() -> None:
    check_csv_xlsx_parsing()
    check_mineru_mapping()
    check_score_knowledge()
    check_heuristic_extraction()
    check_json_fences()
    check_llm_interaction_logging()
    check_event_logging()
    check_pipeline_event_logging()
    check_cjk_keyword_recall()
    check_hybrid_floor_and_weight()
    check_fuzzy_evidence_binding()
    check_extraction_windowing()
    check_payload_embedding_retrieval()
    check_structured_scenario_boost()
    with tempfile.TemporaryDirectory(prefix="silicon-notebook-smoke-") as temp_dir:
        root = Path(temp_dir)
        repository = SQLiteRepository(
            Settings(
                database_url=f"sqlite:///{root / 'silicon_notebook.db'}",
                storage_dir=str(root / "storage"),
                # Keep the smoke hermetic regardless of a configured local .env:
                # never call MinerU or any external LLM/embedding API.
                mineru_mode="off",
                openai_compat_base_url="",
                openai_compat_api_key="",
                openai_compat_model="",
                openai_compat_embedding_model="",
            )
        )
        demo = repository.get_notebook(DEMO_NOTEBOOK_ID)
        assert demo.counts["rules"] > 0, "seed demo should approve extracted rules"
        assert repository.list_rules(DEMO_NOTEBOOK_ID), "seed demo should populate knowledge_objects"

        notebook = repository.create_notebook(
            NotebookCreate(
                name="Smoke notebook",
                purpose="Validate local persistence and parsing.",
                primary_domain="Semiconductor",
            )
        )

        # §6.1/§6.2: templates + rich creation fields.
        templates = repository.list_notebook_templates()
        assert len(templates) >= 6 and any(t.id == "rule" for t in templates)
        templated = repository.create_notebook(NotebookCreate(name="From template", template="rule"))
        assert templated.taxonomy and templated.target_users, "template should seed rich fields"
        updated_nb = repository.update_notebook(
            templated.id, NotebookUpdate(expected_questions=["q1", "q2"], access_scope="team")
        )
        assert updated_nb.expected_questions == ["q1", "q2"] and updated_nb.access_scope == "team"

        uploaded = repository.upload_sources(
            notebook.id,
            [
                UploadedSourceFile(
                    file_name="mixed_markdown.md",
                    content_type="text/markdown",
                    content=(
                        "# Wirebond review\n\n"
                        "低噪声模拟前端需要检查 quiet ground return path.\n\n"
                        "- ESD path must not share sensitive input return.\n"
                    ).encode("utf-8"),
                ),
                UploadedSourceFile(
                    file_name="review_notes.docx",
                    content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    content=_docx_bytes("DOCX parser smoke paragraph for package review."),
                ),
                UploadedSourceFile(
                    file_name="slides.pptx",
                    content_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
                    content=_pptx_bytes("PPTX parser smoke slide for pin assignment."),
                ),
                UploadedSourceFile(
                    file_name="empty.pdf",
                    content_type="application/pdf",
                    content=_pdf_bytes(),
                ),
            ],
        )

        assert len(uploaded) == 4
        # Synchronous (no scheduler) path runs the full pipeline to completion.
        assert all(source.parse_status == "extracted" for source in uploaded)

        markdown = next(source for source in uploaded if source.file_name.endswith(".md"))
        assert len(repository.source_elements(markdown.id)) >= 3

        docx = next(source for source in uploaded if source.file_name.endswith(".docx"))
        assert repository.source_elements(docx.id)[0].text.startswith("DOCX parser smoke")

        pptx = next(source for source in uploaded if source.file_name.endswith(".pptx"))
        pptx_elements = repository.source_elements(pptx.id)
        assert "PPTX parser smoke" in pptx_elements[0].text
        # Per-shape granularity plus speaker notes parsing.
        assert any(el.element_type == "slide_text" and "Second shape" in el.text for el in pptx_elements)
        assert any(el.element_type == "speaker_notes" for el in pptx_elements)

        hits = repository.search_notebook(notebook.id, "quiet ground")
        assert any(hit.scope == "Element" for hit in hits.hits)

        # Extraction ran during parse; candidates should exist from heuristics.
        candidates = repository.list_candidates(notebook.id)
        assert candidates, "expected extraction candidates from uploaded sources"

        rule_candidate = next(
            (c for c in candidates if c.candidate_type == "rule"), candidates[0]
        )
        approved = repository.approve_candidate(rule_candidate.id)
        assert approved.status == "approved"

        # Approved knowledge should be reflected in notebook counts.
        refreshed = repository.get_notebook(notebook.id)
        assert sum(refreshed.counts.get(k, 0) for k in ("rules", "cases", "checklist_items")) >= 1

        # Approved candidate must no longer appear in the review queue.
        assert all(c.id != rule_candidate.id for c in repository.list_candidates(notebook.id))

        # Ask should ground its answer in approved knowledge and return an answer id.
        statement = str(rule_candidate.payload.get("statement") or rule_candidate.payload.get("title") or "")
        answer = repository.ask(notebook.id, AskRequest(question=statement or "quiet ground return path"))
        assert answer.answer_id
        assert answer.llm_mode in ("deterministic", "configured")

        # --- Tier 2: knowledge status lifecycle + multi-type browsing ---
        rules_now = repository.list_rules(notebook.id)
        assert rules_now, "approved rule should appear in list_rules"
        rule_id = rules_now[0].id
        assert rules_now[0].status == "approved"
        # Explain Rule (§6.10): traces the rule back to its origin evidence.
        explanation = repository.explain_rule(notebook.id, rule_id)
        assert explanation.rule.id == rule_id
        assert isinstance(explanation.origin, list) and isinstance(explanation.related_cases, list)
        # Deprecate -> must drop out of answers.
        repository.update_knowledge(rule_id, KnowledgeUpdate(status="deprecated", owner="curator-a"))
        dep = next(r for r in repository.list_rules(notebook.id) if r.id == rule_id)
        assert dep.status == "deprecated" and dep.owner == "curator-a"
        ans_dep = repository.ask(
            notebook.id, AskRequest(question=statement or "quiet ground return path")
        )
        assert all(r.id != rule_id for r in ans_dep.related_rules), "deprecated rule leaked into answer"
        # Reviewed -> usable again, last_reviewed stamped.
        repository.update_knowledge(rule_id, KnowledgeUpdate(status="reviewed"))
        rev = next(r for r in repository.list_rules(notebook.id) if r.id == rule_id)
        assert rev.status == "reviewed" and rev.last_reviewed
        # Invalid status rejected.
        try:
            repository.update_knowledge(rule_id, KnowledgeUpdate(status="bogus"))
            raise AssertionError("invalid status should raise ValueError")
        except ValueError:
            pass
        # Method/Risk/Glossary browse endpoints exist and return lists.
        assert isinstance(repository.list_methods(notebook.id), list)
        assert isinstance(repository.list_risks(notebook.id), list)
        assert isinstance(repository.list_glossary(notebook.id), list)

        # --- Tier 2b: duplicates / conflicts / merge ---
        for candidate in repository.list_candidates(notebook.id):
            repository.approve_candidate(candidate.id)
        assert isinstance(repository.find_duplicates(notebook.id, "rule"), list)
        assert isinstance(repository.find_conflicts(notebook.id), list)
        live_rules = [r for r in repository.list_rules(notebook.id) if r.status != "deprecated"]
        if len(live_rules) >= 2:
            src_id, tgt_id = live_rules[0].id, live_rules[1].id
            tgt_ev_before = len(live_rules[1].evidence)
            merged = repository.merge_knowledge(src_id, MergeRequest(into_id=tgt_id))
            assert len(merged.evidence) >= tgt_ev_before
            src_after = next(
                (r for r in repository.list_rules(notebook.id) if r.id == src_id), None
            )
            assert src_after is not None and src_after.status == "deprecated"

        cases = repository.case_search(notebook.id, CaseSearchRequest(query="noise"))
        assert isinstance(cases, list)
        # With an approved rule, checklist falls back to rules and yields items.
        checklist_items = repository.checklist(
            notebook.id, ChecklistRequest(scenario=statement or "ESD path")
        )
        assert checklist_items and all(item.question for item in checklist_items)

        # Feedback round-trip on the saved answer.
        feedback = repository.submit_feedback(
            answer.answer_id, FeedbackRequest(rating="useful", comment="grounded and actionable")
        )
        assert feedback.answer_id == answer.answer_id
        assert feedback.comment == "grounded and actionable"

        # §16: analytics aggregates feedback / candidates / knowledge / sources.
        analytics = repository.notebook_analytics(notebook.id)
        assert analytics.feedback_useful >= 1
        assert analytics.usefulness_rate == 1.0  # only the one useful vote so far
        assert analytics.knowledge_counts.get("rule", 0) >= 1
        assert sum(analytics.source_status_counts.values()) >= 1

        # Article research must derive from the article's own text, not a hardcoded brief.
        article = repository.create_article(
            notebook.id,
            ArticleCreate(
                title="Thermal via placement for power dies",
                abstract=(
                    "Dense thermal via arrays reduce junction temperature. "
                    "Via pitch must balance routing congestion against heat spreading."
                ),
            ),
        )
        brief = repository.research_article(article.id)
        joined = " ".join([brief.core_contribution] + brief.claims).lower()
        assert "bondwire" not in joined, "research_article leaked hardcoded bondwire text"
        assert "thermal" in joined or "via" in joined
        with repository._connect() as db:
            claim_row = db.execute(
                "SELECT relation_type, implication FROM article_claims WHERE article_id = ? LIMIT 1",
                (article.id,),
            ).fetchone()
            derived_count = db.execute(
                "SELECT COUNT(*) AS count FROM derived_rule_candidates WHERE article_id = ?",
                (article.id,),
            ).fetchone()
        assert claim_row is not None
        assert "thermal" in " ".join(brief.derived_rule_candidates).lower() or int(derived_count["count"]) >= 1

        # Derived Rule Candidate queue (§7.5): list + approve into the rule library.
        derived = repository.list_derived_rules(notebook.id)
        assert isinstance(derived, list)
        draft = next((d for d in derived if d.status == "draft"), None)
        if draft is not None:
            rules_before = len(repository.list_rules(notebook.id))
            approved_rule = repository.approve_derived_rule(draft.id)
            assert approved_rule.status == "approved"
            assert len(repository.list_rules(notebook.id)) == rules_before + 1
            assert any(d.status == "approved" for d in repository.list_derived_rules(notebook.id))

        repository.delete_article(article.id)
        assert all(item.id != article.id for item in repository.list_articles(notebook.id))
        with repository._connect() as db:
            deleted_claim_count = db.execute(
                "SELECT COUNT(*) AS count FROM article_claims WHERE article_id = ?",
                (article.id,),
            ).fetchone()
            deleted_derived_count = db.execute(
                "SELECT COUNT(*) AS count FROM derived_rule_candidates WHERE article_id = ?",
                (article.id,),
            ).fetchone()
        assert int(deleted_claim_count["count"]) == 0
        assert int(deleted_derived_count["count"]) == 0

        old_rule_id = approved.id
        reparsed = repository.parse_source(markdown.id)
        assert reparsed.parse_status == "extracted"
        assert all(rule.id != old_rule_id for rule in repository.list_rules(notebook.id))

        # Async scheduler path: upload returns queued; processing runs out of band.
        scheduled: list[str] = []
        queued = repository.upload_sources(
            notebook.id,
            [
                UploadedSourceFile(
                    file_name="deferred.md",
                    content_type="text/markdown",
                    content=b"# Deferred\n\nrequire post-upload background parsing.\n",
                )
            ],
            scheduler=scheduled.append,
        )
        assert queued[0].parse_status == "queued"
        assert repository.source_elements(queued[0].id) == []
        assert scheduled == [queued[0].id]
        processed = repository.process_source(scheduled[0])
        assert processed.parse_status == "extracted"
        assert repository.source_elements(queued[0].id)
        processed_path = Path(repository.get_source(processed.id).file_path)
        assert processed_path.exists()
        repository.delete_source(processed.id)
        assert all(source.id != processed.id for source in repository.list_sources(notebook.id))
        assert not processed_path.exists()
        try:
            repository.source_elements(processed.id)
            raise AssertionError("deleted source should no longer expose elements")
        except KeyError:
            pass

        restarted = SQLiteRepository(
            Settings(
                database_url=f"sqlite:///{root / 'silicon_notebook.db'}",
                storage_dir=str(root / "storage"),
                # Keep the smoke hermetic regardless of a configured local .env:
                # never call MinerU or any external LLM/embedding API.
                mineru_mode="off",
                openai_compat_base_url="",
                openai_compat_api_key="",
                openai_compat_model="",
                openai_compat_embedding_model="",
            )
        )
        assert restarted.get_notebook(notebook.id).name == "Smoke notebook"
        assert restarted.source_elements(markdown.id)

    print("backend smoke ok: sqlite persistence, extraction, retrieval, article, feedback")


def _docx_bytes(text: str) -> bytes:
    from docx import Document

    buffer = io.BytesIO()
    document = Document()
    document.add_paragraph(text)
    document.save(buffer)
    return buffer.getvalue()


def _pptx_bytes(text: str) -> bytes:
    buffer = io.BytesIO()
    slide_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        f"<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>{text}</a:t></a:r></a:p>"
        "</p:txBody></p:sp>"
        "<p:sp><p:txBody><a:p><a:r><a:t>Second shape risk note</a:t></a:r></a:p>"
        "</p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
    )
    notes_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<p:notes xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        "<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r>"
        "<a:t>Speaker note: confirm pin map before signoff.</a:t>"
        "</a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:notes>"
    )
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("ppt/slides/slide1.xml", slide_xml)
        archive.writestr("ppt/notesSlides/notesSlide1.xml", notes_xml)
    return buffer.getvalue()


def _pdf_bytes() -> bytes:
    from pypdf import PdfWriter

    buffer = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(buffer)
    return buffer.getvalue()


if __name__ == "__main__":
    main()
