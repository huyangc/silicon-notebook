from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.domain.indexing_pipeline import (
    IndexingPipelineKgExtractionFailedError,
    IndexingPipelineKgLimits,
    IndexingPipelineUnavailableError,
)
from app.extension_sdk import (
    EXTENSION_API_VERSION,
    INDEXING_PIPELINE_POINT,
    ContributionDeclaration,
    ContributionKind,
    ExtensionContribution,
    ExtensionManifest,
    IndexingKgEdgeProposal,
    IndexingKgFragment,
    IndexingKgMessage,
    IndexingKgObjectProposal,
    IndexingKgPrompt,
    IndexingPipelineDescriptor,
)
from app.extensions import build_extension_registry
from app.extensions.indexing import IndexingPipelineHost
from app.models.notebooks import NotebookCreate
from app.services import kg_ingest
from app.services.source_ingestion import SourceIngestionService
from app.services.sqlite_repository import SQLiteRepository, _now
from tests.model_testkit import bind_chat_client


def _limits() -> IndexingPipelineKgLimits:
    return IndexingPipelineKgLimits(
        max_messages=4,
        max_prompt_chars=4096,
        max_schema_hint_chars=512,
        max_objects=8,
        max_edges=8,
        max_evidence_handles=4,
        max_steps_per_object=4,
        max_name_chars=120,
    )


def _element(text: str):
    return SimpleNamespace(
        file="paper.md",
        char_start=0,
        char_end=len(text),
        line_start=1,
        line_end=1,
        text=text,
        element_type="paragraph",
        location_label="p1",
        section_path="intro",
    )


class _Client:
    def __init__(self) -> None:
        self.calls = []

    def chat_json(self, messages, schema_hint, **kwargs):
        self.calls.append((messages, schema_hint, kwargs))
        return '{"objects":[]}'


class _Strategy:
    descriptor = IndexingPipelineDescriptor(
        pipeline_id="test.pipeline",
        label="Test KG strategy",
        description="test-only prompt builder and response mapper",
        version="v1",
        overrides_chunking=False,
        overrides_kg_extraction=True,
    )

    def build_kg_prompt(self, elements, context):
        assert elements[0].handle == "e0"
        assert "Concept" in context.object_types
        return IndexingKgPrompt(
            messages=(
                IndexingKgMessage("system", "extract a tiny graph"),
                IndexingKgMessage("user", elements[0].text),
            ),
            response_schema_hint='{"type":"object"}',
        )

    def map_kg_response(self, response, elements, _context):
        assert response == '{"objects":[]}'
        assert elements[0].handle == "e0"
        return IndexingKgFragment(
            objects=(
                IndexingKgObjectProposal(
                    local_id="concept",
                    object_type="Concept",
                    name="Pipeline Strategy",
                    evidence_handles=("e0",),
                ),
                IndexingKgObjectProposal(
                    local_id="claim",
                    object_type="Claim",
                    name="The strategy is core-admitted.",
                    evidence_handles=("e0",),
                ),
            ),
            edges=(
                IndexingKgEdgeProposal(
                    edge_type="supports",
                    source_local_id="concept",
                    target_local_id="claim",
                    evidence_handles=("e0",),
                ),
            ),
        )


@dataclass
class _Bundle:
    manifest: ExtensionManifest
    contribution: ExtensionContribution

    def register(self, registrar) -> None:
        registrar.add_contributor(self.contribution)


def _host(strategy) -> IndexingPipelineHost:
    declaration = ContributionDeclaration(
        "test-pipeline", INDEXING_PIPELINE_POINT, ContributionKind.CONTRIBUTOR
    )
    bundle = _Bundle(
        ExtensionManifest(
            id="test",
            version="1.0",
            api_version=EXTENSION_API_VERSION,
            display_name="Test",
            trust="builtin",
            contributions=(declaration,),
        ),
        ExtensionContribution(declaration, strategy),
    )
    return IndexingPipelineHost(build_extension_registry((bundle,)))


def test_plugin_kg_strategy_uses_window_handles_and_core_edge_admission():
    client = _Client()

    nodes, edges = kg_ingest._plugin_extract_window(
        client,
        _host(_Strategy()),
        "test.pipeline",
        [_element("Pipeline Strategy supports a grounded claim.")],
        "intro",
        "academic",
        0,
        ("Concept", "Claim"),
        _limits(),
    )

    assert [node.type for node in nodes] == ["Concept", "Claim"]
    assert edges[0].type == "supports"
    assert edges[0].source_id == nodes[0].id
    assert edges[0].target_id == nodes[1].id
    assert edges[0].evidence[0].quote == "Pipeline Strategy supports a grounded claim."
    assert client.calls[0][0][0]["role"] == "system"


class _BadHandleStrategy(_Strategy):
    def map_kg_response(self, response, elements, context):
        return IndexingKgFragment(
            objects=(
                IndexingKgObjectProposal(
                    local_id="concept",
                    object_type="Concept",
                    name="Ungrounded",
                    evidence_handles=("not-a-window-handle",),
                ),
            )
        )


class _FailsOnlyMarkedSourceStrategy(_Strategy):
    def map_kg_response(self, response, elements, context):
        if "FAIL_SECOND" in elements[0].text:
            return _BadHandleStrategy.map_kg_response(
                self, response, elements, context
            )
        return super().map_kg_response(response, elements, context)


def test_plugin_kg_strategy_rejects_non_window_evidence_handles():
    with pytest.raises(ValueError, match="evidence handle"):
        kg_ingest._plugin_extract_window(
            _Client(),
            _host(_BadHandleStrategy()),
            "test.pipeline",
            [_element("Only e0 is legal in this window.")],
            "intro",
            "academic",
            0,
            ("Concept",),
            _limits(),
        )


class _BadEdgeStrategy(_Strategy):
    def map_kg_response(self, response, elements, context):
        return IndexingKgFragment(
            objects=(
                IndexingKgObjectProposal(
                    local_id="concept",
                    object_type="Concept",
                    name="Concept",
                    evidence_handles=("e0",),
                ),
                IndexingKgObjectProposal(
                    local_id="claim",
                    object_type="Claim",
                    name="Claim",
                    evidence_handles=("e0",),
                ),
            ),
            edges=(
                IndexingKgEdgeProposal(
                    edge_type="defines",
                    source_local_id="concept",
                    target_local_id="claim",
                    evidence_handles=("e0",),
                ),
            ),
        )


def test_plugin_kg_strategy_rejects_edges_outside_core_schema():
    with pytest.raises(ValueError, match="endpoint pair"):
        kg_ingest._plugin_extract_window(
            _Client(),
            _host(_BadEdgeStrategy()),
            "test.pipeline",
            [_element("This edge has the wrong endpoint types.")],
            "intro",
            "academic",
            0,
            ("Concept", "Claim"),
            _limits(),
        )


class _OversizedPromptStrategy(_Strategy):
    def build_kg_prompt(self, elements, context):
        return IndexingKgPrompt(
            messages=(IndexingKgMessage("user", "x" * 4097),),
            response_schema_hint="",
        )


def test_plugin_kg_prompt_bound_is_enforced_before_the_core_model_call():
    client = _Client()

    with pytest.raises(ValueError, match="configured bounds"):
        kg_ingest._plugin_extract_window(
            client,
            _host(_OversizedPromptStrategy()),
            "test.pipeline",
            [_element("bounded input")],
            "intro",
            "academic",
            0,
            ("Concept",),
            _limits(),
        )

    assert client.calls == []


class _CustomObjectStrategy(_Strategy):
    def map_kg_response(self, response, elements, context):
        return IndexingKgFragment(
            objects=(
                IndexingKgObjectProposal(
                    local_id="finding",
                    object_type="ExperimentFinding",
                    name="Admin-defined finding",
                    evidence_handles=("e0",),
                ),
                IndexingKgObjectProposal(
                    local_id="concept",
                    object_type="Concept",
                    name="Known concept",
                    evidence_handles=("e0",),
                ),
            ),
            edges=(
                IndexingKgEdgeProposal(
                    edge_type="depends_on",
                    source_local_id="finding",
                    target_local_id="concept",
                    evidence_handles=("e0",),
                ),
            ),
        )


def test_effective_admin_object_type_is_admitted_but_edge_vocab_stays_core_owned():
    nodes, edges = kg_ingest._plugin_extract_window(
        _Client(),
        _host(_CustomObjectStrategy()),
        "test.pipeline",
        [_element("The finding depends on the known concept.")],
        "intro",
        "academic",
        0,
        ("Concept", "ExperimentFinding"),
        _limits(),
    )

    assert [node.type for node in nodes] == ["ExperimentFinding", "Concept"]
    assert [edge.type for edge in edges] == ["depends_on"]


@pytest.mark.parametrize(
    "state",
    [
        {
            "pipeline_id": "test.pipeline",
            "pipeline_version": "v1",
            "pipeline_generation": "desired-generation",
            "published_pipeline_id": "",
            "published_pipeline_version": "builtin.chunk.v1",
        },
        {
            "pipeline_id": "",
            "pipeline_version": "builtin.chunk.v1",
            "pipeline_generation": "desired-generation",
            "published_pipeline_id": "test.pipeline",
            "published_pipeline_version": "v1",
        },
    ],
)
def test_ordinary_kg_extraction_refuses_desired_published_identity_mixes(state):
    service = object.__new__(SourceIngestionService)
    service.notebooks = SimpleNamespace(indexing_pipeline_state=lambda _id: state)
    service.indexing_pipelines = _host(_Strategy())

    with pytest.raises(IndexingPipelineUnavailableError):
        service._kg_strategy_for_notebook("notebook")


def test_switch_worker_must_present_the_exact_desired_generation():
    state = {
        "pipeline_id": "test.pipeline",
        "pipeline_version": "v1",
        "pipeline_generation": "current-generation",
        "published_pipeline_id": "",
        "published_pipeline_version": "builtin.chunk.v1",
    }
    service = object.__new__(SourceIngestionService)
    service.notebooks = SimpleNamespace(indexing_pipeline_state=lambda _id: state)
    service.indexing_pipelines = _host(_Strategy())

    with pytest.raises(IndexingPipelineUnavailableError):
        service._kg_strategy_for_notebook(
            "notebook",
            authorized_pipeline_id="test.pipeline",
            authorized_pipeline_version="v1",
            authorized_pipeline_generation="stale-generation",
        )
    pipeline_id, version, strategy = service._kg_strategy_for_notebook(
        "notebook",
        authorized_pipeline_id="test.pipeline",
        authorized_pipeline_version="v1",
        authorized_pipeline_generation="current-generation",
    )
    assert (pipeline_id, version) == ("test.pipeline", "v1")
    assert strategy is service.indexing_pipelines


class _ProbeAndExtractClient:
    configured = True
    model = "test-kg"

    def chat_json(self, messages, _schema_hint, **_kwargs):
        if messages[0]["content"].startswith('Return {"ok":true}'):
            return '{"ok":true}'
        return '{"objects":[]}'


def _insert_source(repo, notebook_id: str, text: str) -> tuple[str, str]:
    source_id = f"src-{uuid4().hex[:10]}"
    element_id = f"el-{uuid4().hex[:10]}"
    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources "
            "(id,notebook_id,title,source_type,status,parse_status,file_name,"
            "file_path,file_size,file_hash,summary,doc_type,created_at,updated_at) "
            "VALUES (?,?,?,'markdown','parsed','parsed',?,'',0,?,'',"
            "'academic_paper',?,?)",
            (
                source_id,
                notebook_id,
                "Existing source",
                f"{source_id}.md",
                source_id,
                now,
                now,
            ),
        )
        db.execute(
            "INSERT INTO source_elements "
            "(id,source_id,element_type,location_label,text,metadata,created_at) "
            "VALUES (?,?,'paragraph','p1',?,'{}',?)",
            (element_id, source_id, text, now),
        )
    return source_id, element_id


def test_plugin_kg_failure_keeps_old_graph_and_identity_and_fails_job(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'failure.db'}")
    monkeypatch.setenv(
        "SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage")
    )
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("MODEL_SERVICES_CONFIG", "")
    settings = Settings(_env_file=None)
    settings.paper_meta_enabled = False
    settings.kg_llm_max_retries = 0
    settings.kg_gleaning_enabled = False
    settings.kg_refine_enabled = False
    settings.kg_relink_enabled = False
    repo = SQLiteRepository(
        settings,
        indexing_pipeline_host=_host(_BadHandleStrategy()),
    )
    events = []

    def record_event(event, **_kwargs):
        events.append(event)

    repo._runtime.event_log.emit = record_event
    bind_chat_client(repo, "kg_extract", _ProbeAndExtractClient())
    notebook = repo.create_notebook(NotebookCreate(name="failure"))
    source_id, element_id = _insert_source(
        repo, notebook.id, "The old source graph remains readable."
    )
    repo.store_kg(
        notebook.id,
        source_id,
        [
            {
                "local_id": "old",
                "object_type": "concept",
                "payload": {"name": "Old published concept"},
                "evidence": [
                    {
                        "source_id": source_id,
                        "source_title": "Existing source",
                        "element_id": element_id,
                        "element_type": "paragraph",
                        "location_label": "p1",
                        "quoted_span": "old source graph",
                        "confidence": 1.0,
                    }
                ],
            }
        ],
        [],
    )
    with repo._connect() as db:
        source_before = dict(db.execute(
            "SELECT status,parse_status,error_message,updated_at "
            "FROM sources WHERE id=?", (source_id,),
        ).fetchone())
        runs_before = db.execute(
            "SELECT * FROM extraction_runs WHERE source_id=? ORDER BY rowid",
            (source_id,),
        ).fetchall()
    submitted = {}

    def capture(function, *args, **kwargs):
        submitted.update(function=function, args=args, kwargs=kwargs)
        return object()

    from app.services import background_jobs

    monkeypatch.setattr(background_jobs, "submit", capture)
    response = repo.set_indexing_pipeline(notebook.id, "test.pipeline")

    with pytest.raises(IndexingPipelineKgExtractionFailedError):
        submitted["function"](*submitted["args"])

    job = repo._runtime.kg_build_jobs.get(response["job_id"])
    assert (job["status"], job["error_code"]) == (
        "failed",
        "indexing_pipeline_kg_failed",
    )
    state = repo._runtime.notebook_store.indexing_pipeline_state(notebook.id)
    assert (state["published_pipeline_id"], state["published_pipeline_version"]) == (
        "",
        "builtin.chunk.v1",
    )
    assert state["pipeline_job_id"] == response["job_id"]
    with repo._connect() as db:
        objects = db.execute(
            "SELECT payload FROM knowledge_objects WHERE source_id=?",
            (source_id,),
        ).fetchall()
        run = db.execute(
            "SELECT status,error_message,indexing_pipeline_id,"
            "indexing_pipeline_version FROM extraction_runs WHERE source_id=? "
            "ORDER BY created_at DESC,rowid DESC LIMIT 1",
            (source_id,),
        ).fetchone()
        source_after = dict(db.execute(
            "SELECT status,parse_status,error_message,updated_at "
            "FROM sources WHERE id=?", (source_id,),
        ).fetchone())
        stage_count = int(db.execute(
            "SELECT COUNT(*) AS c FROM indexing_pipeline_stages WHERE job_id=?",
            (response["job_id"],),
        ).fetchone()["c"])
    assert [json.loads(row["payload"])["name"] for row in objects] == [
        "Old published concept"
    ]
    assert run is None
    assert runs_before == []
    assert source_after == source_before
    assert stage_count == 0
    strategy_event = next(
        event
        for event in events
        if event.get("kind") == "indexing_pipeline_kg_strategy"
    )
    assert set(strategy_event) == {
        "kind",
        "pipeline_id",
        "stage",
        "status",
        "window_count",
        "failed_count",
        "latency_ms",
    }
    assert strategy_event["status"] == "failed"
    assert "old source graph" not in repr(strategy_event)
    assert "not-a-window-handle" not in repr(strategy_event)


def test_plugin_kg_success_atomically_publishes_graph_facts_and_identity(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'success.db'}")
    monkeypatch.setenv(
        "SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage")
    )
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("MODEL_SERVICES_CONFIG", "")
    settings = Settings(_env_file=None)
    settings.paper_meta_enabled = False
    settings.kg_llm_max_retries = 0
    settings.kg_gleaning_enabled = False
    settings.kg_refine_enabled = False
    settings.kg_relink_enabled = False
    repo = SQLiteRepository(settings, indexing_pipeline_host=_host(_Strategy()))
    bind_chat_client(repo, "kg_extract", _ProbeAndExtractClient())
    notebook = repo.create_notebook(NotebookCreate(name="success"))
    source_id, _element_id = _insert_source(
        repo, notebook.id, "Pipeline Strategy supports a grounded claim."
    )
    hidden_id, hidden_element_id = _insert_source(
        repo, notebook.id, "Core-owned hidden memory."
    )
    with repo._write() as db:
        db.execute(
            "UPDATE sources SET source_type='memory' WHERE id=?", (hidden_id,)
        )
    repo._build_chunks_for_source(hidden_id)
    repo.store_kg(
        notebook.id,
        hidden_id,
        [
            {
                "local_id": "hidden",
                "object_type": "concept",
                "payload": {"name": "Hidden memory concept"},
                "evidence": [
                    {
                        "source_id": hidden_id,
                        "source_title": "Memory",
                        "element_id": hidden_element_id,
                        "element_type": "paragraph",
                        "location_label": "memory",
                        "quoted_span": "Core-owned hidden memory.",
                        "confidence": 1.0,
                    }
                ],
            }
        ],
        [],
    )
    with repo._write() as db:
        before_state = db.execute(
            "SELECT kg_mutation_seq,cluster_mutation_seq FROM unified_kg_state "
            "WHERE notebook_id=?", (notebook.id,),
        ).fetchone()
        db.execute(
            "INSERT INTO canonical_relations "
            "(notebook_id,canonical_src,edge_type,canonical_tgt,updated_at) "
            "VALUES (?,?,?,?,?)",
            (notebook.id, "old-a", "related_to", "old-b", _now()),
        )
        db.execute(
            "INSERT INTO kg_analysis_artifacts "
            "(notebook_id,kind,kg_mutation_seq,payload,created_at) "
            "VALUES (?,?,?,?,?)",
            (notebook.id, "source_profiles", 0, "{}", _now()),
        )
        # 批 3·W2(codex #671 R18):预置一份代际状态——published 指针、在飞
        # 认领与催收欠账都非零。staged 发布刚跨代清空三张派生表,必须同事务
        # 把这些归零(counter 除外),否则并发代际 rebuild 的翻转双 CAS 仍然
        # 匹配,会把行已被删光的代发布出去。
        db.execute(
            "UPDATE unified_kg_state SET cluster_generation=7, "
            "community_generation=3, derived_building_generation=9, "
            "derived_building_claimed_at=datetime('now'), "
            "derived_catchup_from=datetime('now'), "
            "derived_generation_counter=9 WHERE notebook_id=?",
            (notebook.id,),
        )
    submitted = {}

    def capture(function, *args, **kwargs):
        submitted.update(function=function, args=args, kwargs=kwargs)
        return object()

    from app.services import background_jobs

    monkeypatch.setattr(background_jobs, "submit", capture)
    response = repo.set_indexing_pipeline(notebook.id, "test.pipeline")
    submitted["function"](*submitted["args"])

    with repo._connect() as db:
        names = sorted(
            json.loads(row["payload"])["name"]
            for row in db.execute(
                "SELECT payload FROM knowledge_objects WHERE source_id=?",
                (source_id,),
            ).fetchall()
        )
        facts = int(db.execute(
            "SELECT COUNT(*) AS c FROM knowledge_source_facts WHERE source_id=?",
            (source_id,),
        ).fetchone()["c"])
        extraction = dict(db.execute(
            "SELECT status,indexing_pipeline_id,indexing_pipeline_version "
            "FROM extraction_runs WHERE source_id=? ORDER BY rowid DESC LIMIT 1",
            (source_id,),
        ).fetchone())
        stage_count = int(db.execute(
            "SELECT COUNT(*) AS c FROM indexing_pipeline_stages WHERE job_id=?",
            (response["job_id"],),
        ).fetchone()["c"])
        state = dict(db.execute(
            "SELECT dirty,kg_mutation_seq,cluster_mutation_seq,indexing_pipeline_id,"
            "indexing_pipeline_version FROM unified_kg_state WHERE notebook_id=?",
            (notebook.id,),
        ).fetchone())
        hidden_chunks = int(db.execute(
            "SELECT COUNT(*) AS c FROM chunks WHERE source_id=?", (hidden_id,)
        ).fetchone()["c"])
        hidden_objects = int(db.execute(
            "SELECT COUNT(*) AS c FROM knowledge_objects WHERE source_id=?",
            (hidden_id,),
        ).fetchone()["c"])
        derived_counts = {
            table: int(db.execute(
                f"SELECT COUNT(*) AS c FROM {table} WHERE notebook_id=?",
                (notebook.id,),
            ).fetchone()["c"])
            for table in ("canonical_relations", "kg_analysis_artifacts")
        }
        generational = dict(db.execute(
            "SELECT cluster_generation, community_generation, "
            "derived_building_generation, derived_building_claimed_at, "
            "derived_catchup_from, derived_generation_counter "
            "FROM unified_kg_state WHERE notebook_id=?",
            (notebook.id,),
        ).fetchone())
    # R18:发布同事务的代际重置——指针/在飞/催收归零,counter 不回卷;
    # 拿着发布前快照(P=7, G=9)的翻转在两个方向的 CAS 上都作废。
    assert generational == {
        "cluster_generation": 0, "community_generation": 0,
        "derived_building_generation": 0,
        "derived_building_claimed_at": None, "derived_catchup_from": None,
        "derived_generation_counter": 9,
    }, generational
    from app.repositories.sqlite.unified_kg_store import (
        UnifiedKgStore as _UKS,
    )
    with repo._write() as db:
        assert not _UKS.flip_cluster_generation(
            db, notebook.id, published_from=7, generation=9,
            catchup_from_ts=_now(), now=_now())
    assert names == ["Pipeline Strategy", "The strategy is core-admitted."]
    assert facts == 2
    assert extraction == {
        "status": "completed",
        "indexing_pipeline_id": "test.pipeline",
        "indexing_pipeline_version": "v1",
    }
    assert stage_count == 0
    assert state["dirty"] == 1
    assert int(state["kg_mutation_seq"]) >= 1
    assert int(state["kg_mutation_seq"]) > int(before_state["kg_mutation_seq"])
    assert int(state["cluster_mutation_seq"]) > int(
        before_state["cluster_mutation_seq"]
    )
    assert (state["indexing_pipeline_id"], state["indexing_pipeline_version"]) == (
        "test.pipeline", "v1"
    )
    assert hidden_chunks > 0
    assert hidden_objects == 1
    assert derived_counts == {"canonical_relations": 0, "kg_analysis_artifacts": 0}
    assert repo._runtime.kg_build_jobs.get(response["job_id"])["status"] == "succeeded"


def test_second_source_failure_leaves_all_live_products_byte_identical(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'two-source.db'}")
    monkeypatch.setenv(
        "SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage")
    )
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("MODEL_SERVICES_CONFIG", "")
    settings = Settings(_env_file=None)
    settings.paper_meta_enabled = False
    settings.kg_llm_max_retries = 0
    settings.kg_gleaning_enabled = False
    settings.kg_refine_enabled = False
    settings.kg_relink_enabled = False
    repo = SQLiteRepository(
        settings, indexing_pipeline_host=_host(_FailsOnlyMarkedSourceStrategy())
    )
    bind_chat_client(repo, "kg_extract", _ProbeAndExtractClient())
    notebook = repo.create_notebook(NotebookCreate(name="two-source"))
    first, first_element = _insert_source(repo, notebook.id, "GOOD_FIRST")
    second, second_element = _insert_source(repo, notebook.id, "FAIL_SECOND")
    for source_id, element_id, name in (
        (first, first_element, "Old first"),
        (second, second_element, "Old second"),
    ):
        repo._build_chunks_for_source(source_id)
        repo.store_kg(
            notebook.id,
            source_id,
            [
                {
                    "local_id": "old",
                    "object_type": "concept",
                    "payload": {"name": name},
                    "evidence": [
                        {
                            "source_id": source_id,
                            "source_title": name,
                            "element_id": element_id,
                            "element_type": "paragraph",
                            "location_label": "p1",
                            "quoted_span": name,
                            "confidence": 1.0,
                        }
                    ],
                }
            ],
            [],
        )

    def live_snapshot():
        with repo._connect() as db:
            return {
                table: [tuple(row) for row in db.execute(
                    f"SELECT * FROM {table} ORDER BY rowid"
                ).fetchall()]
                for table in (
                    "chunks",
                    "chunk_elements",
                    "chunk_embeddings",
                    "knowledge_objects",
                    "knowledge_object_sources",
                    "knowledge_relations",
                    "knowledge_source_facts",
                    "knowledge_source_fact_elements",
                    "extraction_runs",
                    "unified_kg_state",
                    "sources",
                )
            }

    before = live_snapshot()
    submitted = {}

    def capture(function, *args, **kwargs):
        submitted.update(function=function, args=args, kwargs=kwargs)
        return object()

    from app.services import background_jobs

    monkeypatch.setattr(background_jobs, "submit", capture)
    response = repo.set_indexing_pipeline(notebook.id, "test.pipeline")
    # Desired notebook intent/job authority are expected to change; the live
    # product identity and all source-derived rows must not.
    before["sources"] = live_snapshot()["sources"]
    before["unified_kg_state"] = live_snapshot()["unified_kg_state"]

    with pytest.raises(IndexingPipelineKgExtractionFailedError):
        submitted["function"](*submitted["args"])

    after = live_snapshot()
    assert {key: value for key, value in after.items() if key != "sources"} == {
        key: value for key, value in before.items() if key != "sources"
    }
    # The stage-only extraction path never mutates source state/timestamps.
    assert after["sources"] == before["sources"]
    with repo._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM indexing_pipeline_stages WHERE job_id=?",
            (response["job_id"],),
        ).fetchone()["c"] == 0


def test_zero_element_source_stages_empty_replacement_and_publishes(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'empty-source.db'}")
    monkeypatch.setenv(
        "SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage")
    )
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("MODEL_SERVICES_CONFIG", "")
    settings = Settings(_env_file=None)
    settings.paper_meta_enabled = False
    settings.kg_relink_enabled = False
    repo = SQLiteRepository(settings, indexing_pipeline_host=_host(_Strategy()))
    bind_chat_client(repo, "kg_extract", _ProbeAndExtractClient())
    notebook = repo.create_notebook(NotebookCreate(name="empty-source"))
    source_id, element_id = _insert_source(repo, notebook.id, "temporary")
    with repo._write() as db:
        db.execute("DELETE FROM source_elements WHERE id=?", (element_id,))
    repo.store_kg(
        notebook.id,
        source_id,
        [
            {
                "local_id": "legacy",
                "object_type": "concept",
                "payload": {"name": "Legacy ungrounded object"},
                "evidence": [],
            }
        ],
        [],
    )
    submitted = {}

    def capture(function, *args, **kwargs):
        submitted.update(function=function, args=args, kwargs=kwargs)
        return object()

    from app.services import background_jobs

    monkeypatch.setattr(background_jobs, "submit", capture)
    response = repo.set_indexing_pipeline(notebook.id, "test.pipeline")
    submitted["function"](*submitted["args"])

    with repo._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM knowledge_objects WHERE source_id=?",
            (source_id,),
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT COUNT(*) AS c FROM chunks WHERE source_id=?", (source_id,)
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT COUNT(*) AS c FROM extraction_runs WHERE source_id=?",
            (source_id,),
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT COUNT(*) AS c FROM indexing_pipeline_stages WHERE job_id=?",
            (response["job_id"],),
        ).fetchone()["c"] == 0
    state = repo._runtime.notebook_store.indexing_pipeline_state(notebook.id)
    assert (state["published_pipeline_id"], state["published_pipeline_version"]) == (
        "test.pipeline", "v1"
    )


def test_bounded_validity_scope_drops_oversized_plugin_annotations():
    """插件 mapper 的 validity_scope 套核心围栏(codex #602 R3 P2):列表条数复用
    max_steps_per_object、每个字符串复用 max_name_chars,越界丢标注保对象。"""
    from app.services.kg_ingest import _bounded_validity_scope

    limits = _limits()
    ok = {"region": ["a", "b"], "approximation": "small"}
    assert _bounded_validity_scope(dict(ok), limits) == ok
    long_text = "x" * (limits.max_name_chars + 1)
    assert _bounded_validity_scope({"region": [long_text]}, limits) == {}
    assert _bounded_validity_scope({"range": long_text}, limits) == {}
    too_many = {"assumptions": ["a"] * (limits.max_steps_per_object + 1)}
    assert _bounded_validity_scope(too_many, limits) == {}
