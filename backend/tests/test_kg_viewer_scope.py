"""PR-A·A5 (user rulings Q4 / M1): KG browse reads are filtered by the
VIEWER's readable sources — visible sources ∪ the viewer's own hidden sources.

Scenario: user A owns a shared notebook, user B is a member.  One visible
source (S) and A's confirmed Memory (MA) both yield a concept "Engram"; the
two cluster together and the cluster carries a fused description.  A's Memory
also yields a concept only it knows ("SecretProject", linked to Engram), a
claim that ``defines`` the visible Engram, and the text of one step of a
visible procedure.  Optionally B has a Memory of its own (MB), which makes
A's view filtered too (A must still see A's own Memory text).

PostgreSQL twin: tests/postgres/test_kg_viewer_scope_pg.py.
"""
from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.core.request_context import reset_request_user, set_request_user
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository
from tests.pre_isolation_graph import build_pre_isolation
from tests.world_templates import world_env, world_template

_SCENARIO_ENV = {"EVENT_LOG_ENABLED": "false", "LLM_LOG_ENABLED": "false"}
_SCENARIO_PATH_SETTINGS = {
    "database_url", "storage_dir", "event_log_dir", "llm_log_path", "diagnostics_dir",
}


def _scenario_settings():
    return Settings(event_log_enabled=False, llm_log_enabled=False).model_dump(
        mode="json", exclude=_SCENARIO_PATH_SETTINGS,
    )


# Capture collection-time defaults, as world_templates does for its build
# environment. A test changing any non-path setting keeps the original builder.
_SCENARIO_DEFAULTS = _scenario_settings()


@pytest.fixture
def repo(tmp_path, monkeypatch, request):
    # Only explicit scenario consumers receive a seeded copy. Assembly and
    # empty-notebook tests keep the original empty repository. Resolve the seed
    # before constructing the runtime, including when another fixture requests
    # repo before pytest gets to the scenario fixture itself.
    seed = (request.getfixturevalue("scenario_seed")
            if "scenario_seed" in request.fixturenames else None)
    if seed is not None and seed[1] is not None:
        environment = world_env(seed[0])
    else:
        environment = {
            "DATABASE_URL": f"sqlite:///{tmp_path / 't.db'}",
            "SILICON_NOTEBOOK_STORAGE_DIR": str(tmp_path / "s"),
        }
    for name, value in {**environment, **_SCENARIO_ENV}.items():
        monkeypatch.setenv(name, value)
    return SQLiteRepository(Settings())


def _seed_scenario_at(root, *, b_memory):
    seeded = SQLiteRepository(Settings())
    try:
        scenario = build_scenario(seeded, b_memory=b_memory)
        payload = {
            "nb": scenario.nb, "a": scenario.a.id, "b": scenario.b.id,
            "ids": vars(scenario.ids),
        }
        # Re-read every returned object from the persisted world. This catches
        # a future builder returning transient fields that copies would lose.
        assert _scenario_from_ids(seeded, payload) == scenario
        return payload
    finally:
        seeded.close()


def _seed_scenario_without_b(root):
    return _seed_scenario_at(root, b_memory=False)


def _seed_scenario_with_b(root):
    return _seed_scenario_at(root, b_memory=True)


def _reopen_scenario_at(root):
    return SQLiteRepository(Settings())


def _scenario_from_ids(repo, payload):
    users = {}
    identity = repo._runtime.identity
    with repo._connect() as db:
        for name in ("a", "b"):
            user = db.execute("SELECT * FROM users WHERE id=?", (payload[name],)).fetchone()
            profile = db.execute(
                "SELECT * FROM user_profiles WHERE user_id=?", (payload[name],),
            ).fetchone()
            users[name] = identity._user_profile(user, profile)
    return SimpleNamespace(
        nb=payload["nb"], **users, ids=SimpleNamespace(**payload["ids"]),
    )


@pytest.fixture
def scenario_seed(tmp_path, tmp_path_factory, request):
    parameters = getattr(getattr(request.node, "callspec", None), "params", {})
    b_memory = ("scenario_with_b_memory" in request.fixturenames
                or parameters.get("b_memory", False))
    if _scenario_settings() != _SCENARIO_DEFAULTS:
        return b_memory, None
    builder = _seed_scenario_with_b if b_memory else _seed_scenario_without_b
    template = world_template(
        tmp_path_factory, f"kg-viewer-scope-b-memory-{b_memory}",
        builder, _reopen_scenario_at, env=_SCENARIO_ENV,
    )
    return template.materialize(tmp_path)


@pytest.fixture
def scenario(repo, scenario_seed):
    root_or_b_memory, payload = scenario_seed
    if payload is None:
        return build_scenario(repo, b_memory=root_or_b_memory)
    return _scenario_from_ids(repo, payload)


@pytest.fixture
def scenario_with_b_memory(scenario):
    return scenario


def _now(offset: int = 0) -> str:
    return (datetime.datetime(2026, 9, 1) + datetime.timedelta(seconds=offset)).isoformat()


def _source(db, nb, sid, *, memory_id=None, elements=()):
    db.execute(
        "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
        "file_name,file_path,file_size,file_hash,summary,doc_type,memory_id,"
        "created_at,updated_at) VALUES (?,?,?,?,'extracted','parsed','d.md','',0,'',"
        "'','',?,?,?)",
        (sid, nb, sid, "memory" if memory_id else "markdown", memory_id, _now(), _now()),
    )
    for index, (eid, text) in enumerate(elements):
        db.execute(
            "INSERT INTO source_elements (id,source_id,element_type,location_label,"
            "text,metadata,created_at) VALUES (?,?,'paragraph',?,?,'{}',?)",
            (eid, sid, f"p{index}", text, _now(index)),
        )


def _memory(db, nb, memory_id, owner):
    db.execute(
        "INSERT INTO memory_items (id,notebook_id,created_by,origin,status,title,"
        "content_md,created_at,updated_at) VALUES (?,?,?,'ask_answer','confirmed',"
        "'t','c',?,?)",
        (memory_id, nb, owner, _now(), _now()),
    )


def _ev(sid, eid):
    return {"source_id": sid, "source_title": sid, "element_id": eid,
            "element_type": "paragraph", "location_label": "p",
            "quoted_span": f"quote {eid}", "confidence": 1.0}


def _object_id(db, nb, name, source_id):
    return db.execute(
        "SELECT id FROM knowledge_objects WHERE notebook_id=? AND source_id=? "
        "AND json_extract(payload,'$.name')=?", (nb, source_id, name),
    ).fetchone()["id"]


def build_scenario(repo, *, b_memory: bool):
    a = repo._runtime.identity.create_user("a00000001", "pw123456")
    b = repo._runtime.identity.create_user("b00000002", "pw123456")
    token = set_request_user(a)
    try:
        nb = repo.create_notebook(NotebookCreate(name="shared")).id
        repo._runtime.sharing.add_member(nb, b.id)
    finally:
        reset_request_user(token)
    with repo._write() as db:
        _source(db, nb, "src-s", elements=[
            ("el-s-occ", "VISIBLE occurrence of Engram"),
            ("el-s-def", "VISIBLE definition of Engram"),
            ("el-s-step", "VISIBLE step text"),
        ])
        _memory(db, nb, "mem-a", a.id)
        _source(db, nb, "src-ma", memory_id="mem-a", elements=[
            ("el-ma-occ", "A-PRIVATE occurrence of Engram"),
            ("el-ma-def", "A-PRIVATE definition of Engram"),
            ("el-ma-step", "A-PRIVATE step text"),
            ("el-ma-secret", "A-PRIVATE secret project"),
        ])
        if b_memory:
            _memory(db, nb, "mem-b", b.id)
            _source(db, nb, "src-mb", memory_id="mem-b", elements=[
                ("el-mb", "B-PRIVATE note"),
            ])
    repo.store_kg(nb, "src-s", [
        {"local_id": "c", "object_type": "concept",
         "payload": {"name": "Engram", "section_path": "1"},
         "evidence": [_ev("src-s", "el-s-occ")]},
        {"local_id": "d", "object_type": "claim",
         "payload": {"name": "visible definer", "section_path": "1"},
         "evidence": [_ev("src-s", "el-s-def")]},
        {"local_id": "p", "object_type": "procedure",
         "payload": {"name": "Flow", "section_path": "2", "steps": [
             {"name": "s-visible", "element_id": "el-s-step", "quote": "q"},
             {"name": "s-private", "element_id": "el-ma-step", "quote": "q"},
         ]},
         "evidence": [_ev("src-s", "el-s-step")]},
    ], [])
    repo.store_kg(nb, "src-ma", [
        {"local_id": "c", "object_type": "concept",
         "payload": {"name": "Engram", "section_path": "1"},
         "evidence": [_ev("src-ma", "el-ma-occ")]},
        {"local_id": "d", "object_type": "claim",
         "payload": {"name": "private definer", "section_path": "1"},
         "evidence": [_ev("src-ma", "el-ma-def")]},
        {"local_id": "x", "object_type": "concept",
         "payload": {"name": "SecretProject", "section_path": "1"},
         "evidence": [_ev("src-ma", "el-ma-secret")]},
    ], [{"source_local_id": "x", "target_local_id": "c", "edge_type": "related_to",
         "evidence": []}])
    with repo._write() as db:
        ids = SimpleNamespace(
            engram_s=_object_id(db, nb, "Engram", "src-s"),
            engram_ma=_object_id(db, nb, "Engram", "src-ma"),
            secret=_object_id(db, nb, "SecretProject", "src-ma"),
            flow=_object_id(db, nb, "Flow", "src-s"),
            definer_s=_object_id(db, nb, "visible definer", "src-s"),
            definer_ma=_object_id(db, nb, "private definer", "src-ma"),
        )
        # r.id order: the private definer is scanned first.
        for rel_id, definer in (("rel-a", ids.definer_ma), ("rel-b", ids.definer_s)):
            db.execute(
                "INSERT INTO knowledge_relations (id,notebook_id,source_id,"
                "source_object_id,target_object_id,edge_type,evidence,created_at) "
                "VALUES (?,?,?,?,?,'defines','[]',?)",
                (rel_id, nb, "src-s", definer, ids.engram_s, _now()),
            )
    build_pre_isolation(repo, nb)
    cmap = repo.cluster_map(nb)
    ids.engram_canonical = cmap[ids.engram_s]
    assert cmap[ids.engram_ma] == ids.engram_canonical
    ids.secret_canonical = cmap[ids.secret]
    with repo._write() as db:
        db.execute(
            "UPDATE concept_clusters SET canonical_description='FUSED description' "
            "WHERE notebook_id=? AND canonical_id=?", (nb, ids.engram_canonical),
        )
    return SimpleNamespace(nb=nb, a=a, b=b, ids=ids)


def as_user(user, fn, *args, **kwargs):
    token = set_request_user(user)
    try:
        return fn(*args, **kwargs)
    finally:
        reset_request_user(token)


def _texts(ctx):
    return [o.get("element_text") for o in ctx["occurrences"]]


# ---------------------------------------------------------------- context
def test_member_sees_no_text_from_another_members_memory(scenario, repo):
    s = scenario
    ctx = as_user(s.b, repo.node_context, s.nb, s.ids.engram_s)
    # The fused description mixes A's Memory → strict predicate drops it; the
    # first defines evidence is A's → falls through to the visible definer.
    assert ctx["definition"] == "VISIBLE definition of Engram"
    assert ctx["definition_basis"] == "defines_evidence"
    assert ctx["definition_source_id"] == "src-s"
    assert _texts(ctx) == ["VISIBLE occurrence of Engram"]
    flow = as_user(s.b, repo.node_context, s.nb, s.ids.flow)
    # The store decides whether an unreadable step keeps its (visible
    # object's) name with blank text or is dropped; either way no text from
    # A's Memory survives and the readable step keeps its text.
    assert ("s-visible", "VISIBLE step text") in [
        (st["name"], st["element_text"]) for st in flow["steps"]]
    blob = repr(ctx) + repr(flow)
    assert "A-PRIVATE" not in blob and "FUSED" not in blob


def test_object_evidenced_only_by_another_members_memory_is_404(scenario, repo):
    s = scenario
    for oid in (s.ids.engram_ma, s.ids.secret, s.ids.definer_ma):
        with pytest.raises(KeyError):
            as_user(s.b, repo.node_context, s.nb, oid)
    # The owner still sees it (and here there is no foreign Memory at all, so
    # the owner's call takes the unfiltered path).
    own = as_user(s.a, repo.node_context, s.nb, s.ids.secret)
    assert _texts(own) == ["A-PRIVATE secret project"]


def test_owner_keeps_own_memory_text_when_the_notebook_also_holds_foreign_memory(
    scenario_with_b_memory, repo,
):
    s = scenario_with_b_memory
    ctx = as_user(s.a, repo.node_context, s.nb, s.ids.engram_ma)
    assert _texts(ctx) == ["A-PRIVATE occurrence of Engram"]
    # Every cluster member's source is readable to A → the fused description.
    visible = as_user(s.a, repo.node_context, s.nb, s.ids.engram_s)
    assert visible["definition"] == "FUSED description"
    assert visible["definition_basis"] == "cluster_description"
    flow = as_user(s.a, repo.node_context, s.nb, s.ids.flow)
    assert [st["element_text"] for st in flow["steps"]] == [
        "VISIBLE step text", "A-PRIVATE step text",
    ]
    # B, symmetric: A's Memory is foreign to B.
    with pytest.raises(KeyError):
        as_user(s.b, repo.node_context, s.nb, s.ids.engram_ma)


def test_short_circuit_passes_no_ceiling_and_returns_todays_bytes(scenario, repo, monkeypatch):
    s = scenario
    runtime = repo._runtime
    expected = runtime.knowledge.node_context(s.nb, s.ids.engram_s, check_access=False)
    seen = []
    original = runtime.knowledge.node_context

    def spy(*args, **kwargs):
        seen.append(kwargs.get("allowed_source_ids"))
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime.knowledge, "node_context", spy)
    # A owns the only Memory → nothing is foreign to A.
    got = as_user(s.a, repo.node_context, s.nb, s.ids.engram_s)
    assert seen == [None]
    assert got == expected
    assert got["definition"] == "FUSED description"


def test_filtered_path_hands_the_store_the_viewers_readable_set(scenario, repo, monkeypatch):
    s = scenario
    runtime = repo._runtime
    seen = []
    original = runtime.knowledge.node_context

    def spy(*args, **kwargs):
        seen.append(kwargs.get("allowed_source_ids"))
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime.knowledge, "node_context", spy)
    as_user(s.b, repo.node_context, s.nb, s.ids.engram_s)
    assert seen == [frozenset({"src-s"})]


# ---------------------------------------------------------- concept detail
def test_concept_detail_omits_hidden_members_and_their_text(scenario, repo):
    s = scenario
    detail = as_user(s.b, repo.concept_detail, s.nb, s.ids.engram_canonical)
    assert [m["id"] for m in detail["members"]] == [s.ids.engram_s]
    assert detail["member_total"] == 1
    assert detail["canonical_name"] == "Engram"
    assert all(e.get("source_id") == "src-s" for e in detail["evidence"])
    # The private definer claim is attached to the VISIBLE member by a defines
    # edge — it is still A's Memory and must not be attached for B.
    assert [a["payload"]["name"] for a in detail["attached"]] == ["visible definer"]
    for secret in ("A-PRIVATE", "private definer", "src-ma", "el-ma"):
        assert secret not in repr(detail)
    with pytest.raises(KeyError):
        as_user(s.b, repo.concept_detail, s.nb, s.ids.secret_canonical)
    own = as_user(s.a, repo.concept_detail, s.nb, s.ids.engram_canonical)
    assert {m["id"] for m in own["members"]} == {s.ids.engram_s, s.ids.engram_ma}
    assert own["member_total"] == 2


def test_concept_detail_keyset_pages_stay_full_around_hidden_members(scenario, repo):
    s = scenario
    # Pad the Engram cluster with visible members so paging has work to do.
    with repo._write() as db:
        for index in range(5):
            _source(db, s.nb, f"src-v{index}", elements=[(f"el-v{index}", f"V{index}")])
    for index in range(5):
        repo.store_kg(s.nb, f"src-v{index}", [{
            "local_id": "c", "object_type": "concept",
            "payload": {"name": "Engram", "section_path": "1"},
            "evidence": [_ev(f"src-v{index}", f"el-v{index}")]}], [])
    build_pre_isolation(repo, s.nb)
    canonical = repo.cluster_map(s.nb)[s.ids.engram_s]
    seen, after, total = [], "", None
    while True:
        page = as_user(s.b, repo.concept_detail, s.nb, canonical, limit=2, after=after)
        if total is None:
            total = page["member_total"]
        seen.extend(m["id"] for m in page["members"])
        if page["next_cursor"] is None:
            break
        assert len(page["members"]) == 2
        after = page["next_cursor"]
    assert s.ids.engram_ma not in seen
    assert len(seen) == len(set(seen)) == 6 == total


def test_cluster_label_never_comes_from_a_hidden_member(scenario, repo):
    s = scenario
    with repo._write() as db:
        db.execute(
            "UPDATE concept_clusters SET canonical_name='A-PRIVATE Engram' "
            "WHERE notebook_id=? AND canonical_id=?", (s.nb, s.ids.engram_canonical),
        )
    member = as_user(s.b, repo.concept_detail, s.nb, s.ids.engram_canonical)
    assert member["canonical_name"] == "Engram"
    owner = as_user(s.a, repo.concept_detail, s.nb, s.ids.engram_canonical)
    assert owner["canonical_name"] == "A-PRIVATE Engram"


def reader_of(repo):
    """The one KgViewerScopeReader instance (owned by knowledge_query)."""
    return repo._runtime.knowledge_query.viewer_scope.__self__


def test_neighbourhood_filter_relabels_partly_hidden_clusters(scenario, repo):
    """Both hydration paths take a cluster label from one member's payload;
    a label baked from the hidden member must come back as a visible one."""
    s = scenario
    scope = as_user(s.b, reader_of(repo).for_notebook, s.nb)
    nodes = [
        {"id": s.ids.engram_canonical, "object_type": "concept",
         "payload": {"name": "A-PRIVATE baked label"}},
        {"id": s.ids.secret_canonical, "object_type": "concept",
         "payload": {"name": "SecretProject"}},
        {"id": s.ids.definer_s, "object_type": "claim", "payload": {"name": "visible definer"}},
        {"id": s.ids.definer_ma, "object_type": "claim", "payload": {"name": "private definer"}},
    ]
    edges = [
        {"source_object_id": s.ids.secret_canonical,
         "target_object_id": s.ids.engram_canonical, "edge_type": "x"},
        {"source_object_id": s.ids.definer_s,
         "target_object_id": s.ids.engram_canonical, "edge_type": "y"},
        {"source_object_id": s.ids.definer_ma,
         "target_object_id": s.ids.engram_canonical, "edge_type": "z"},
    ]
    kept_nodes, kept_edges, answers = as_user(
        s.b, scope.answer_neighbourhood, nodes, edges, (s.ids.engram_canonical,))
    assert [(n["id"], n["payload"]["name"]) for n in kept_nodes] == [
        (s.ids.engram_canonical, "Engram"), (s.ids.definer_s, "visible definer"),
    ]
    assert [e["edge_type"] for e in kept_edges] == ["y"]
    # The relabelled cluster is answered by the member its label came from.
    assert answers == {s.ids.engram_canonical: s.ids.engram_s}
    assert as_user(s.b, scope.answer_neighbourhood, nodes, edges, (s.ids.secret,)) is None


# --------------------------------------------------------------- neighbours
@pytest.fixture(params=["viz", "db"])
def neighbour_path(request, repo, monkeypatch):
    """Run a neighbours test over both hydration paths: the persisted viz
    artifact (built lazily for a small notebook) and the DB fallback (forced
    by making the artifact absent)."""
    if request.param == "db":
        monkeypatch.setattr(
            repo._runtime.scale_artifacts, "viz_index", lambda *_a, **_k: None)
    return request.param


def test_neighbour_hydration_omits_hidden_nodes(scenario, repo, neighbour_path):
    s = scenario
    owner_view = as_user(s.a, repo.kg_neighbors, s.nb, s.ids.engram_s)
    assert s.ids.secret_canonical in {n["id"] for n in owner_view["nodes"]}
    member_view = as_user(s.b, repo.kg_neighbors, s.nb, s.ids.engram_s)
    ids = {n["id"] for n in member_view["nodes"]}
    assert s.ids.secret_canonical not in ids
    assert all(s.ids.secret_canonical not in (e["source_object_id"], e["target_object_id"])
               for e in member_view["edges"])
    assert "SecretProject" not in repr(member_view)
    hidden_focus = as_user(s.b, repo.kg_neighbors, s.nb, s.ids.secret)
    assert hidden_focus["nodes"] == [] and hidden_focus["edges"] == []


def test_no_memory_notebook_builds_no_scope(repo, monkeypatch):
    """No Memory at all: the probe stops after one read."""
    a = repo._runtime.identity.create_user("a00000009", "pw123456")
    token = set_request_user(a)
    try:
        nb = repo.create_notebook(NotebookCreate(name="plain")).id
        reader = reader_of(repo)
        calls = []
        monkeypatch.setattr(reader.sources, "hidden_source_ids",
                            lambda *a, **k: calls.append(a) or [])
        assert reader.for_notebook(nb) is None
        assert calls == []
    finally:
        reset_request_user(token)
