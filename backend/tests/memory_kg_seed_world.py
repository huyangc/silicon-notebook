"""Shared world for the E4-2 tests: a shared notebook that holds another member's
Memory, seeded so that WITHOUT the graph-build exclusion every derived layer
would pick the Memory up (ruling M1).

* Shared sources ``s1``/``s2``/``s3``: concepts "Grouped-query attention (GQA)"
  (in s1 and s2, one cross-source cluster), "Multi-Query Attention (MQA)" (s1
  only), "KV cache" (s2), and a shared claim naming GQA and MQA (s3).
* A Memory source ``mem`` of member ``u-bob``, stored FIRST (lowest ordinals,
  so it would win any first-seen tie): a concept named exactly like the shared
  MQA (it would join MQA's cluster and make it a cross-source cluster), two
  concepts with private names (they would mint ``K-secret ...`` canonical ids),
  a claim that names GQA and MQA (it would add mention edges and a co-mention),
  and two relations among its own objects (they would put Memory objects into
  canonical relations and communities).

``SECRET`` is the marker every Memory-derived name carries; the shared world
never contains it. Backend-neutral: ``?`` placeholders are rewritten for the
PostgreSQL repository, so the SQLite tests and their PostgreSQL twin seed the
very same shape.
"""
from __future__ import annotations

import json

from app.models.schemas import NotebookCreate

NOW = "2026-09-30T00:00:00"
SECRET = "secret"
MEMORY_SOURCE = "mem"
SHARED_SOURCES = ("s1", "s2", "s3")
MEMORY_OWNER = "u-bob"


def is_postgres(repo) -> bool:
    return type(repo).__name__ == "PostgresRepository"


def sql(repo, statement: str) -> str:
    return statement.replace("?", "%s") if is_postgres(repo) else statement


def _objects(prefix: str, names, object_type: str = "concept") -> list[dict]:
    return [
        {"local_id": f"{prefix}-{index}", "object_type": object_type,
         "payload": {"name": name, "section_path": "1"}, "evidence": []}
        for index, name in enumerate(names)
    ]


def _edge(source: str, target: str) -> dict:
    return {"source_local_id": source, "target_local_id": target,
            "edge_type": "depends_on", "evidence": []}


def _source(repo, notebook_id: str, source_id: str, source_type: str,
            memory_id: str | None = None) -> None:
    with repo._write() as db:
        db.execute(
            sql(repo, "INSERT INTO sources (id,notebook_id,title,source_type,status,"
                      "memory_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)"),
            (source_id, notebook_id, source_id, source_type, "ready", memory_id,
             NOW, NOW),
        )


def add_memory_source(repo, notebook_id: str, source_id: str = MEMORY_SOURCE) -> None:
    """A confirmed Memory of ``u-bob`` and the Memory source derived from it."""
    memory_id = f"memitem-{source_id}"
    with repo._write() as db:
        db.execute(
            sql(repo, "INSERT INTO users(id,email,display_name,role,status,created_at,"
                      "updated_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT (id) DO NOTHING"),
            (MEMORY_OWNER, f"{MEMORY_OWNER}@example.test", MEMORY_OWNER, "user",
             "active", NOW, NOW),
        )
        db.execute(
            sql(repo, "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,"
                      "title,content_md,created_at,updated_at) "
                      "VALUES (?,?,?,?,?,?,?,?,?)"),
            (memory_id, notebook_id, MEMORY_OWNER, "ask_answer", "confirmed",
             "private", "x", NOW, NOW),
        )
    _source(repo, notebook_id, source_id, "memory", memory_id)


def seed(repo) -> str:
    """The world above; returns the notebook id. Nothing is rebuilt yet."""
    notebook = repo.create_notebook(NotebookCreate(name="shared with memory"))
    repo.settings.community_min_size = 2
    add_memory_source(repo, notebook.id)
    for source_id in SHARED_SOURCES:
        _source(repo, notebook.id, source_id, "md")
    repo.store_kg(
        notebook.id, MEMORY_SOURCE,
        _objects("m", ["Multi-Query Attention (MQA)", "SECRET-ALPHA plan",
                       "Secret Beta budget"])
        + _objects("mc", ["SECRET-ALPHA plan relies on GQA and MQA for the budget."],
                   "claim"),
        [_edge("m-1", "m-0"), _edge("m-2", "m-1")],
    )
    repo.store_kg(
        notebook.id, "s1",
        _objects("a", ["Grouped-query attention (GQA)", "Multi-Query Attention (MQA)"]),
        [_edge("a-0", "a-1")],
    )
    repo.store_kg(
        notebook.id, "s2",
        _objects("b", ["Grouped-query attention (GQA)", "KV cache"]),
        [_edge("b-0", "b-1")],
    )
    repo.store_kg(
        notebook.id, "s3",
        _objects("c", ["GQA uses fewer KV heads than MQA while keeping quality."],
                 "claim"),
        [],
    )
    return notebook.id


def memory_object_ids(repo, notebook_id: str) -> set[str]:
    with repo._connect() as db:
        rows = db.execute(
            sql(repo, "SELECT ko.id AS id FROM knowledge_objects ko "
                      "JOIN sources s ON s.id = ko.source_id "
                      "WHERE ko.notebook_id=? AND s.source_type='memory'"),
            (notebook_id,),
        ).fetchall()
    return {row["id"] for row in rows}


def object_ids_by_name(repo, notebook_id: str, source_id: str) -> dict[str, str]:
    name = ("payload ->> 'name'" if is_postgres(repo)
            else "json_extract(payload,'$.name')")
    with repo._connect() as db:
        rows = db.execute(
            sql(repo, f"SELECT id, {name} AS name FROM knowledge_objects "
                      "WHERE notebook_id=? AND source_id=?"),
            (notebook_id, source_id),
        ).fetchall()
    return {row["name"]: row["id"] for row in rows}


def rows(repo, statement: str, params: tuple) -> list:
    with repo._connect() as db:
        return [dict(row) for row in db.execute(sql(repo, statement), params).fetchall()]


def derived_layers(repo, notebook_id: str) -> dict:
    """Every derived layer of the notebook, as plain rows."""
    return {
        "clusters": rows(repo, "SELECT canonical_id, member_object_id, canonical_name, "
                               "canonical_description FROM concept_clusters "
                               "WHERE notebook_id=?", (notebook_id,)),
        "canonical_relations": rows(repo, "SELECT canonical_src, canonical_tgt, "
                                          "support_count, sample_relation_ids "
                                          "FROM canonical_relations WHERE notebook_id=?",
                                    (notebook_id,)),
        "mention_edges": rows(repo, "SELECT claim_object_id, concept_canonical_id "
                                    "FROM mention_edges WHERE notebook_id=?",
                              (notebook_id,)),
        "comentions": rows(repo, "SELECT canonical_a, canonical_b, bridge_claims "
                                 "FROM concept_comentions WHERE notebook_id=?",
                           (notebook_id,)),
        "community_members": rows(repo, "SELECT canonical_id, canonical_name "
                                        "FROM community_members WHERE notebook_id=?",
                                  (notebook_id,)),
        "communities": rows(repo, "SELECT member_ids FROM communities "
                                  "WHERE notebook_id=?", (notebook_id,)),
        "artifacts": rows(repo, "SELECT kind, payload FROM kg_analysis_artifacts "
                                "WHERE notebook_id=?", (notebook_id,)),
    }


def _text(value) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def assert_no_memory_in_derived_layers(repo, notebook_id: str) -> dict:
    """The acceptance of plan E4-2 on one rebuilt notebook; returns the layers
    for positive controls."""
    memory = memory_object_ids(repo, notebook_id)
    assert len(memory) == 4, memory
    layers = derived_layers(repo, notebook_id)
    clusters = layers["clusters"]
    # no Memory object in any cluster, no canonical id or name minted from one
    assert not {r["member_object_id"] for r in clusters} & memory, clusters
    for r in clusters:
        for column in ("canonical_id", "canonical_name", "canonical_description"):
            assert SECRET not in (r[column] or "").lower(), r
    canonical = {r["canonical_id"] for r in clusters}
    assert not canonical & memory
    for r in layers["canonical_relations"]:
        assert not {r["canonical_src"], r["canonical_tgt"]} & memory, r
        assert SECRET not in (r["canonical_src"] + r["canonical_tgt"]).lower(), r
    for r in layers["mention_edges"]:
        assert r["claim_object_id"] not in memory, r
        assert r["concept_canonical_id"] not in memory, r
    for r in layers["comentions"]:
        assert not {r["canonical_a"], r["canonical_b"]} & memory, r
    for r in layers["community_members"]:
        assert r["canonical_id"] not in memory, r
        assert SECRET not in (r["canonical_name"] or "").lower(), r
    for r in layers["communities"]:
        assert not set(json.loads(_text(r["member_ids"]))) & memory, r
    for r in layers["artifacts"]:
        blob = _text(r["payload"]).lower()
        assert SECRET not in blob, r["kind"]
        for object_id in memory:
            assert object_id.lower() not in blob, r["kind"]
    return layers


# ------------------------------------------------ legacy-shape checks
# The shapes a pre-isolation graph (or a cross-kind manual merge) left behind,
# injected directly after an isolated rebuild. Each helper is the whole test
# body, shared by the SQLite test and its PostgreSQL twin, so removing any one
# reader's exclusion on either backend turns a test red.

def _published_cluster_generation(repo, notebook_id: str) -> int:
    return int(rows(repo, "SELECT cluster_generation AS g FROM unified_kg_state "
                          "WHERE notebook_id=?", (notebook_id,))[0]["g"])


def _insert_cluster_row(repo, notebook_id: str, row_id: str, canonical_id: str,
                        member: str, name: str, generation: int) -> None:
    with repo._write() as db:
        db.execute(
            sql(repo, "INSERT INTO concept_clusters (id, notebook_id, canonical_id, "
                      "member_object_id, canonical_name, object_type, created_at, "
                      "generation) VALUES (?, ?, ?, ?, ?, 'concept', ?, ?)"),
            (row_id, notebook_id, canonical_id, member, name, NOW, generation),
        )


def _canonical_rows(repo, notebook_id: str) -> dict:
    return {
        (r["canonical_src"], r["edge_type"], r["canonical_tgt"]):
            (r["support_count"], _text(r["sample_relation_ids"]))
        for r in rows(repo, "SELECT canonical_src, edge_type, canonical_tgt, "
                            "support_count, sample_relation_ids FROM canonical_relations "
                            "WHERE notebook_id=?", (notebook_id,))
    }


def assert_legacy_memory_endpoints_stay_out(repo) -> None:
    """A SHARED relation whose endpoint is a Memory-derived object (what a
    cross-kind manual merge left behind) enters neither the canonical
    relations nor the community graph; the Memory source's own relations do
    not either. The shared relations keep their canonical rows and counts."""
    nb_id = seed(repo)
    repo.rebuild_unified_kg(nb_id, force=True)
    before = _canonical_rows(repo, nb_id)
    assert {key: value[0] for key, value in before.items()} == {
        ("K-grouped query attention", "depends_on", "K-multi query attention"): 1,
        ("K-grouped query attention", "depends_on", "K-kv cache"): 1,
    }
    memory = object_ids_by_name(repo, nb_id, MEMORY_SOURCE)
    shared = object_ids_by_name(repo, nb_id, "s2")
    with repo._write() as db:
        for rel_id, source, target in (
            ("rel-legacy-out", shared["KV cache"], memory["SECRET-ALPHA plan"]),
            ("rel-legacy-in", memory["Secret Beta budget"], shared["KV cache"]),
        ):
            db.execute(
                sql(repo, "INSERT INTO knowledge_relations (id, notebook_id, source_id, "
                          "source_object_id, target_object_id, edge_type, evidence, "
                          "created_at) VALUES (?, ?, 's2', ?, ?, 'depends_on', '[]', ?)"),
                (rel_id, nb_id, source, target, NOW),
            )
    repo.rebuild_canonical_relations(nb_id, force=True)
    assert _canonical_rows(repo, nb_id) == before
    repo.rebuild_communities(nb_id, force=True)
    ids = memory_object_ids(repo, nb_id)
    members = rows(repo, "SELECT canonical_id, canonical_name FROM community_members "
                         "WHERE notebook_id=?", (nb_id,))
    assert members, "control: the shared graph still forms a community"
    assert not {r["canonical_id"] for r in members} & ids, members
    for r in rows(repo, "SELECT member_ids FROM communities WHERE notebook_id=?", (nb_id,)):
        assert not set(json.loads(_text(r["member_ids"]))) & ids, r


def _bridge(repo, notebook_id: str):
    edges = {(r["claim_object_id"], r["concept_canonical_id"]) for r in rows(
        repo, "SELECT claim_object_id, concept_canonical_id FROM mention_edges "
              "WHERE notebook_id=?", (notebook_id,))}
    pairs = [(r["canonical_a"], r["canonical_b"]) for r in rows(
        repo, "SELECT canonical_a, canonical_b FROM concept_comentions "
              "WHERE notebook_id=?", (notebook_id,))]
    return edges, pairs


def assert_memory_builds_no_mention_bridge(repo) -> None:
    """A Memory claim naming shared concepts adds no mention edge and no
    co-mention; a Memory concept left as a member of a shared cluster (legacy)
    does not make a single-source shared concept a cross-source bridge target.
    The shared claim keeps its edge to the one genuinely cross-source concept
    (GQA: s1 + s2)."""
    nb_id = seed(repo)
    repo.rebuild_unified_kg(nb_id, force=True)
    shared_claim = object_ids_by_name(repo, nb_id, "s3")[
        "GQA uses fewer KV heads than MQA while keeping quality."]
    expected = ({(shared_claim, "K-grouped query attention")}, [])
    assert _bridge(repo, nb_id) == expected
    memory = object_ids_by_name(repo, nb_id, MEMORY_SOURCE)
    _insert_cluster_row(repo, nb_id, "cc-legacy", "K-multi query attention",
                        memory["Multi-Query Attention (MQA)"],
                        "Multi-Query Attention (MQA)",
                        _published_cluster_generation(repo, nb_id))
    repo.rebuild_mention_bridge(nb_id, force=True)
    assert _bridge(repo, nb_id) == expected
    with repo._connect() as db:
        clusters, claims = repo._runtime.unified_kg.mention_seed_rows(db, nb_id)
    assert MEMORY_SOURCE not in {r["src"] for r in clusters}
    assert [r["id"] for r in claims] == [shared_claim]


def assert_analysis_readers_skip_legacy_memory_rows(repo) -> None:
    """The three heavy analysis readers neither count, rank nor name a
    Memory-derived member of a published cluster (a shared cluster with one
    injected, and a cluster whose only member is one), and the edge
    provenance counts only the shared sources' relations -- the Memory
    source's are not even "endpoint unusable". The persisted artifacts agree."""
    nb_id = seed(repo)
    repo.rebuild_unified_kg(nb_id, force=True)
    histogram = repo.kg_cluster_size_histogram(nb_id)
    largest = repo.kg_largest_clusters(nb_id)
    provenance = repo.kg_relation_provenance_counts(nb_id)
    memory = object_ids_by_name(repo, nb_id, MEMORY_SOURCE)
    generation = _published_cluster_generation(repo, nb_id)
    _insert_cluster_row(repo, nb_id, "cc-legacy", "K-grouped query attention",
                        memory["SECRET-ALPHA plan"], "Grouped-query attention (GQA)",
                        generation)
    _insert_cluster_row(repo, nb_id, "cc-legacy-2", "K-secret beta budget",
                        memory["Secret Beta budget"], "Secret Beta budget", generation)
    assert repo.kg_cluster_size_histogram(nb_id) == histogram
    assert repo.kg_largest_clusters(nb_id) == largest
    head = {c["canonical_id"]: c["members"] for c in largest["clusters"]}
    assert head["K-grouped query attention"] == 2
    assert SECRET not in json.dumps(largest).lower()
    assert provenance["total_rows"] == 2, provenance
    assert provenance["counted"] == 2
    assert provenance["excluded"]["endpoint_unusable"] == 0
    all_relations = rows(repo, "SELECT COUNT(*) AS n FROM knowledge_relations "
                               "WHERE notebook_id=?", (nb_id,))[0]["n"]
    assert int(all_relations) == 4
    repo.rebuild_communities(nb_id, force=True)
    blob = json.dumps([_text(r["payload"]) for r in derived_layers(repo, nb_id)["artifacts"]]
                      ).lower()
    assert "cluster_size_histogram" in json.dumps(
        [r["kind"] for r in derived_layers(repo, nb_id)["artifacts"]])
    assert SECRET not in blob
    for object_id in memory_object_ids(repo, nb_id):
        assert object_id.lower() not in blob
