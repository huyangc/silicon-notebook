import json

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'follow.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    for key in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
                "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL",
                "REASONING_LLM_MODEL"):
        monkeypatch.setenv(key, "")
    result = SQLiteRepository(Settings(_env_file=None))
    bind_all_embedding_clients(result, FakeEmbedder(dim=16))
    return result


def _seed_chain(repo, *, edge_type="derived_from", object_type="claim",
                scopes=None, second_evidence=True):
    nb = repo.create_notebook(NotebookCreate(name=f"{edge_type}-chain"))
    scopes = scopes or ({}, {}, {})
    objects = []
    for local_id, name, scope in zip(("A", "B", "C"),
                                     ("Premise A", "Bridge B", "Conclusion C"),
                                     scopes):
        payload = {"name": name, "section_path": local_id}
        if scope:
            payload["validity_scope"] = scope
        objects.append({"local_id": local_id, "object_type": object_type,
                        "payload": payload, "evidence": []})
    relations = [
        {"source_local_id": "A", "target_local_id": "B", "edge_type": edge_type,
         "evidence": [{"quote": "Premise A leads to Bridge B", "confidence": 1.0}]},
        {"source_local_id": "B", "target_local_id": "C", "edge_type": edge_type,
         "evidence": ([{"quote": "Bridge B leads to Conclusion C", "confidence": 1.0}]
                      if second_evidence else [])},
    ]
    repo.store_kg(nb.id, None, objects, relations)
    with repo._connect() as db:
        ids = {json.loads(r["payload"])["name"]: r["id"] for r in db.execute(
            "SELECT id,payload FROM knowledge_objects WHERE notebook_id=?", (nb.id,))}
        relation_ids = [r["id"] for r in db.execute(
            "SELECT id FROM knowledge_relations WHERE notebook_id=? ORDER BY rowid", (nb.id,))]
    return nb, ids, relation_ids


def test_follow_chain_uses_facade_keeps_direction_and_does_not_persist(repo):
    nb, ids, _ = _seed_chain(repo)
    with repo._connect() as db:
        before = db.execute(
            "SELECT COUNT(*) c FROM knowledge_relations WHERE notebook_id=?", (nb.id,)
        ).fetchone()["c"]

    result = repo.retrieval.follow_chain(
        nb.id, ids["Premise A"], edge_type="derived_from", direction="out")

    assert len(result.inferences) == 1
    chain = result.inferences[0]
    assert (chain.source_id, chain.via_id, chain.target_id) == (
        ids["Premise A"], ids["Bridge B"], ids["Conclusion C"])
    assert chain.inferred_edge_type == "derived_from"
    assert [h.source_name for h in chain.hops] == ["Premise A", "Bridge B"]
    assert [h.target_name for h in chain.hops] == ["Bridge B", "Conclusion C"]
    assert {n.object_id for n in result.nodes} == set(ids.values())
    with repo._connect() as db:
        after = db.execute(
            "SELECT COUNT(*) c FROM knowledge_relations WHERE notebook_id=?", (nb.id,)
        ).fetchone()["c"]
    assert after == before == 2


def test_follow_chain_incoming_normalizes_to_stored_source_target(repo):
    nb, ids, _ = _seed_chain(repo)
    result = repo._follow_chain(
        nb.id, ids["Conclusion C"], edge_type="derived_from",
        target_object_id=ids["Premise A"], direction="in")
    assert len(result.inferences) == 1
    chain = result.inferences[0]
    assert (chain.source_id, chain.via_id, chain.target_id) == (
        ids["Premise A"], ids["Bridge B"], ids["Conclusion C"])


@pytest.mark.parametrize("edge_type", ["supports", "depends_on", "contrasts_with", "about"])
def test_follow_chain_rejects_non_transitive_types(repo, edge_type):
    nb, ids, _ = _seed_chain(repo, edge_type=edge_type)
    assert repo._follow_chain(nb.id, ids["Premise A"], edge_type=edge_type).inferences == []


def test_follow_chain_requires_quote_on_every_hop(repo):
    nb, ids, _ = _seed_chain(repo, second_evidence=False)
    assert repo._follow_chain(
        nb.id, ids["Premise A"], edge_type="derived_from").inferences == []


def test_follow_chain_does_not_repackage_an_existing_direct_edge(repo):
    nb, ids, _ = _seed_chain(repo)
    repo.add_relations(nb.id, None, [{
        "source_object_id": ids["Premise A"],
        "target_object_id": ids["Conclusion C"],
        "edge_type": "derived_from",
        "evidence": [{"quote": "Premise A directly yields Conclusion C"}],
    }])
    assert repo._follow_chain(
        nb.id, ids["Premise A"], edge_type="derived_from").inferences == []


def test_follow_chain_excludes_rejected_edge_and_deprecated_node(repo):
    nb, ids, relation_ids = _seed_chain(repo)
    with repo._write() as db:
        db.execute("UPDATE knowledge_relations SET review_status='rejected' WHERE id=?",
                   (relation_ids[1],))
    assert repo._follow_chain(
        nb.id, ids["Premise A"], edge_type="derived_from").inferences == []

    with repo._write() as db:
        db.execute("UPDATE knowledge_relations SET review_status='pending' WHERE id=?",
                   (relation_ids[1],))
        db.execute("UPDATE knowledge_objects SET status='deprecated' WHERE id=?",
                   (ids["Bridge B"],))
    assert repo._follow_chain(
        nb.id, ids["Premise A"], edge_type="derived_from").inferences == []


def test_follow_chain_rejects_incompatible_validity_scope(repo):
    nb, ids, _ = _seed_chain(repo, scopes=(
        {"region": ["saturation"]},
        {"region": ["saturation"]},
        {"region": ["triode"]},
    ))
    assert repo._follow_chain(
        nb.id, ids["Premise A"], edge_type="derived_from").inferences == []


def test_follow_chain_rejects_start_in_unmounted_base(repo):
    """起点授权门按挂载判定:base 已发布(tier='base')但 active 从未挂载它,
    follow_chain 不得把 base 的对象当作合法起点——即便对象本身存在且可达。"""
    active = repo.create_notebook(NotebookCreate(name="active"))
    base, base_ids, _ = _seed_chain(repo)
    repo.mark_notebook_base(base.id)
    assert repo._follow_chain(
        active.id, base_ids["Premise A"], edge_type="derived_from").inferences == []


def test_follow_chain_can_read_base_but_not_unrelated_personal(repo):
    active = repo.create_notebook(NotebookCreate(name="active"))
    base, base_ids, _ = _seed_chain(repo)
    repo.mark_notebook_base(base.id)
    repo.replace_notebook_bases(active.id, [base.id], "user-local")
    other, other_ids, _ = _seed_chain(repo)

    base_result = repo._follow_chain(
        active.id, base_ids["Premise A"], edge_type="derived_from")
    assert len(base_result.inferences) == 1
    assert all(h.tier == "base" for h in base_result.inferences[0].hops)
    assert repo._follow_chain(
        active.id, other_ids["Premise A"], edge_type="derived_from").inferences == []


def test_base_chain_trust_is_higher_than_personal(repo):
    personal, personal_ids, _ = _seed_chain(repo)
    active = repo.create_notebook(NotebookCreate(name="active"))
    base, base_ids, relation_ids = _seed_chain(repo)
    repo.mark_notebook_base(base.id)
    repo.replace_notebook_bases(active.id, [base.id], "user-local")
    with repo._write() as db:
        db.execute(
            f"UPDATE knowledge_relations SET review_status='verified' "
            f"WHERE id IN ({','.join('?' for _ in relation_ids)})", relation_ids)

    personal_trust = repo._follow_chain(
        personal.id, personal_ids["Premise A"], edge_type="derived_from"
    ).inferences[0].chain_trust
    base_trust = repo._follow_chain(
        active.id, base_ids["Premise A"], edge_type="derived_from"
    ).inferences[0].chain_trust
    assert base_trust > personal_trust


def test_direct_edge_beyond_frontier_limit_still_suppresses_inference(repo):
    nb, ids, _ = _seed_chain(repo)
    renamed = {
        ids["Premise A"]: "ko-A",
        ids["Bridge B"]: "ko-B",
        ids["Conclusion C"]: "ko-Z",
    }
    with repo._write() as db:
        for old, new in renamed.items():
            db.execute("UPDATE knowledge_objects SET id=? WHERE id=?", (new, old))
            db.execute("UPDATE knowledge_relations SET source_object_id=? "
                       "WHERE source_object_id=?", (new, old))
            db.execute("UPDATE knowledge_relations SET target_object_id=? "
                       "WHERE target_object_id=?", (new, old))

    decoy_ids = []
    for index in range(7):
        old = repo._test_insert_object(
            nb.id, "claim", {"name": f"Decoy {index}", "section_path": "D"})
        new = f"ko-D{index}"
        with repo._write() as db:
            db.execute("UPDATE knowledge_objects SET id=? WHERE id=?", (new, old))
        decoy_ids.append(new)
    repo.add_relations(nb.id, None, [{
        "source_object_id": "ko-A", "target_object_id": decoy,
        "edge_type": "derived_from", "evidence": [{"quote": f"A to {decoy}"}],
    } for decoy in decoy_ids])

    # B plus seven D* targets fill LIMIT 8; Z sorts after them. The valid A-B-Z
    # path is still found because B is in the frontier.
    assert len(repo._follow_chain(
        nb.id, "ko-A", edge_type="derived_from", max_fan_out=8).inferences) == 1

    # Direct A-Z is the ninth outgoing edge and therefore absent from edge_rows,
    # but the separately indexed exact-direct guard must still see it.
    repo.add_relations(nb.id, None, [{
        "source_object_id": "ko-A", "target_object_id": "ko-Z",
        "edge_type": "derived_from", "evidence": [{"quote": "direct A to Z"}],
    }])
    assert repo._follow_chain(
        nb.id, "ko-A", edge_type="derived_from", max_fan_out=8).inferences == []


def test_truncated_supernode_direct_guard_fails_closed(repo):
    nb, ids, relation_ids = _seed_chain(repo)
    with repo._write() as db:
        db.execute("UPDATE knowledge_relations SET review_status='verified' WHERE id=?",
                   (relation_ids[0],))
    decoys = []
    for index in range(64):
        decoy = repo._test_insert_object(
            nb.id, "claim", {"name": f"High-degree decoy {index}"})
        decoys.append({
            "source_object_id": ids["Premise A"], "target_object_id": decoy,
            "edge_type": "derived_from", "evidence": [{"quote": "bounded decoy"}],
        })
    repo.add_relations(nb.id, None, decoys)

    # fan_out=8 => raw endpoint budget=64; 65 outgoing rows means absence of a
    # direct A→C edge cannot be proven inside the budget, so no inference ships.
    assert repo._follow_chain(
        nb.id, ids["Premise A"], edge_type="derived_from",
        max_fan_out=8).inferences == []


def _promote_first_hop_evidence(repo, nb, relation_ids):
    """The first hop's relation lives in ``nb`` but its evidence points at a
    private library's source and IMAGE element (asset, caption, knowhow
    locator): the shape a promoted relation leaves (ledger B-11)."""
    private = repo.create_notebook(NotebookCreate(name="private"))
    metadata = {"asset_id": "asset-priv", "caption": "PRIVATE CAPTION",
                "knowhow": {"table_id": "kt-priv", "row_id": "row-priv"}}
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id, notebook_id, title, source_type, created_at, updated_at) "
            "VALUES ('s-priv', ?, 'Private Current Name', 'markdown', "
            "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')", (private.id,))
        db.execute(
            "INSERT INTO source_elements(id,source_id,element_type,location_label,text,"
            "metadata,created_at) VALUES ('el-priv','s-priv','image','p1',"
            "'PRIVATE CURRENT TEXT',?,'2026-01-01T00:00:00Z')", (json.dumps(metadata),))
        db.execute(
            "UPDATE knowledge_relations SET evidence=? WHERE id=?",
            (json.dumps([{
                "quote": "Premise A leads to Bridge B", "confidence": 1.0,
                "source_id": "s-priv", "element_id": "el-priv",
                "source_title": "Stored Title",
            }]), relation_ids[0]))


PRIVATE_MARKS = ("Private Current Name", "PRIVATE CURRENT TEXT", "PRIVATE CAPTION",
                 "asset-priv", "kt-priv", "el-priv", "s-priv")


def test_promoted_hop_anchor_is_a_pointer_free_snapshot_without_a_scope(repo):
    """PR-E2·E2-4(台账 B-11,推导链一面):无 scope 的运行里,关系在本库、证据
    指向另一库来源与元素的一跳,锚点只有存储的标题与引文,没有来源 / 元素
    指针,于是不会按全局 id 现读那一库来源的现名、也不挂那张私有图。

    变异锚点:``GraphRetrievalService._snapshot_foreign_relation_evidence``
    不改写外库条目 → 锚点标题变成私有现名并挂上私有图,红。"""
    from app.services.kg.follow_chain import render_follow_chain_context

    nb, ids, relation_ids = _seed_chain(repo)
    _promote_first_hop_evidence(repo, nb, relation_ids)
    result = repo.retrieval.follow_chain(
        nb.id, ids["Premise A"], edge_type="derived_from", direction="out")
    assert len(result.inferences) == 1
    _block, id_map = render_follow_chain_context(
        result.inferences, active_notebook_id=nb.id)
    first = id_map["k2001"]
    assert (first["source_id"], first["element_id"]) == ("", "")
    assert first["source_title"] == "Stored Title"
    anchors = repo._parse_answer_anchors("claim [k2001]", id_map)
    repo._runtime.evidence_context_component.attach_citation_images(
        [(anchor, (anchor.element_id,)) for anchor in anchors])
    rendered = json.dumps(
        [{k: str(v) for k, v in first.items()},
         [anchor.model_dump() for anchor in anchors]], ensure_ascii=False)
    for mark in PRIVATE_MARKS:
        assert mark not in rendered, mark
    assert anchors[0].source_title == "Stored Title"
    assert not anchors[0].images


def test_promoted_hop_never_reaches_a_scoped_run(repo):
    """Under a source ceiling the promoted hop has no in-ceiling evidence and
    the chain is dropped; nothing of the private library is rendered."""
    from app.services.kg.follow_chain import render_follow_chain_context
    from app.services.source_scope import source_scope_context

    nb, ids, relation_ids = _seed_chain(repo)
    _promote_first_hop_evidence(repo, nb, relation_ids)
    with source_scope_context(
        nb.id, {"mode": "include", "source_ids": ["s-other"], "narrowed": True}, None,
    ):
        result = repo.retrieval.follow_chain(
            nb.id, ids["Premise A"], edge_type="derived_from", direction="out")
    block, id_map = render_follow_chain_context(
        result.inferences, active_notebook_id=nb.id)
    rendered = block + json.dumps(
        {k: {f: str(v) for f, v in value.items()} for k, value in id_map.items()},
        ensure_ascii=False)
    for mark in PRIVATE_MARKS:
        assert mark not in rendered, mark
