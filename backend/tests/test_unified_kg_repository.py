import json
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.services.vector_index import decode_vector
from app.models.schemas import NotebookCreate
from tests.model_testkit import bind_all_embedding_clients
from tests.model_testkit import bind_chat_client

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))               # inject; no real model loads (lazy)
    return r

def test_store_kg_batch_embeds_nodes(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    objs = [
        {"local_id": "C1", "object_type": "concept",
         "payload": {"name": "MOSFET", "section_path": "1"}, "evidence": []},
        {"local_id": "C2", "object_type": "concept",
         "payload": {"name": "current mirror", "section_path": "1"}, "evidence": []},
    ]
    repo.store_kg(nb.id, None, objs, [])
    with repo._connect() as db:
        rows = db.execute("SELECT object_id, vector FROM knowledge_embeddings WHERE notebook_id=?", (nb.id,)).fetchall()
    assert len(rows) == 2                      # both nodes embedded
    # New writes are BLOB (float32 tobytes), not JSON text — decode_vector
    # dual-reads either format; here it must hit the bytes branch.
    assert isinstance(rows[0]["vector"], (bytes, bytearray))
    assert decode_vector(rows[0]["vector"]).size == 16

def test_cluster_and_candidate_crud(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.write_clusters(nb.id, [
        {"canonical_id": "K1", "member_object_id": "o1", "canonical_name": "MOSFET"},
        {"canonical_id": "K1", "member_object_id": "o2", "canonical_name": "MOSFET"},
    ])
    assert repo.cluster_map(nb.id) == {"o1": "K1", "o2": "K1"}
    repo.write_merge_candidate(nb.id, "K1", "K2", 0.85)
    pend = repo.pending_merges(nb.id)
    assert len(pend) == 1 and pend[0]["status"] == "pending"
    repo.set_merge_decision(nb.id, pend[0]["id"], "rejected")
    assert repo.pending_merges(nb.id) == []
    assert repo.decided_pairs(nb.id) == {("K1", "K2"): "rejected"}
    repo.set_merge_decision(nb.id, pend[0]["id"], "confirmed")
    assert repo.decided_pairs(nb.id) == {("K1", "K2"): "confirmed"}


def test_merge_decision_settles_legacy_duplicate_canonical_pair_rows(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.write_merge_candidate(nb.id, "K-left", "K-right", 0.91)
    repo.write_merge_candidate(nb.id, "K-right", "K-left", 0.89)
    repo.write_merge_candidate(nb.id, "K-left", "K-other", 0.88)
    target = next(
        row for row in repo.pending_merges(nb.id)
        if frozenset((row["canonical_a"], row["canonical_b"]))
        == frozenset(("K-left", "K-right"))
    )

    repo.reject_merge(nb.id, target["id"])

    remaining = repo.pending_merges(nb.id)
    assert len(remaining) == 1
    assert frozenset((remaining[0]["canonical_a"], remaining[0]["canonical_b"])) == frozenset(
        ("K-left", "K-other")
    )
    with repo._connect() as db:
        statuses = db.execute(
            "SELECT status FROM concept_merge_candidates WHERE notebook_id=? AND "
            "((canonical_a='K-left' AND canonical_b='K-right') OR "
            "(canonical_a='K-right' AND canonical_b='K-left'))",
            (nb.id,),
        ).fetchall()
    assert [row["status"] for row in statuses] == ["rejected", "rejected"]

def test_set_merge_decision_rejects_bad_status(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.write_merge_candidate(nb.id, "K1", "K2", 0.85)
    cid = repo.pending_merges(nb.id)[0]["id"]
    with pytest.raises(ValueError):
        repo.set_merge_decision(nb.id, cid, "maybe")

def test_rebuild_merges_same_concept_across_sources(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [{"local_id":"a","object_type":"concept","payload":{"name":"MOSFET","section_path":""},"evidence":[]}], [])
    repo.store_kg(nb.id, None, [{"local_id":"b","object_type":"concept","payload":{"name":"mosfet","section_path":""},"evidence":[]}], [])
    repo.rebuild_unified_kg(nb.id)
    cmap = repo.cluster_map(nb.id)
    assert len(set(cmap.values())) == 1 and len(cmap) == 2   # both MOSFET nodes one cluster

def test_rebuild_is_idempotent(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [{"local_id":"a","object_type":"concept","payload":{"name":"X","section_path":""},"evidence":[]}], [])
    repo.rebuild_unified_kg(nb.id); first = repo.cluster_map(nb.id)
    repo.rebuild_unified_kg(nb.id); assert repo.cluster_map(nb.id).keys() == first.keys()

def test_unified_graph_concept_level_cached(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id":"a","object_type":"concept","payload":{"name":"MOSFET","section_path":""},"evidence":[]},
        {"local_id":"b","object_type":"concept","payload":{"name":"current mirror","section_path":""},"evidence":[]},
    ], [{"source_local_id":"b","target_local_id":"a","edge_type":"depends_on","evidence":[]}])
    repo.rebuild_unified_kg(nb.id)
    g = repo.unified_graph(nb.id, level="concept")
    assert len(g["nodes"]) == 2 and len(g["edges"]) == 1
    assert g["total_nodes"] == 2 and g["truncated"] is False   # metadata for "widen range"
    # the full graph is cached (same object); unified_graph wraps it with metadata
    assert repo._unified_graph_full(nb.id, "concept") is repo._unified_cache[(nb.id,"concept")]


def test_unified_graph_limit_returns_core_subgraph(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    # hub h linked to a/b/c (degree 3,1,1,1); iso has no edge (degree 0)
    objs = [{"local_id": i, "object_type": "concept",
             "payload": {"name": i, "section_path": ""}, "evidence": []}
            for i in ["h", "a", "b", "c", "iso"]]
    rels = [{"source_local_id": "h", "target_local_id": t, "edge_type": "rel", "evidence": []}
            for t in ["a", "b", "c"]]
    repo.store_kg(nb.id, None, objs, rels)
    repo.rebuild_unified_kg(nb.id)
    g = repo.unified_graph(nb.id, level="concept", limit=2)
    assert g["total_nodes"] == 5 and g["truncated"] is True
    assert len(g["nodes"]) == 2                                  # hub + one neighbor
    ids = {n["id"] for n in g["nodes"]}
    assert all(e["source_object_id"] in ids and e["target_object_id"] in ids for e in g["edges"])
    gfull = repo.unified_graph(nb.id, level="concept", limit=99)
    assert gfull["truncated"] is False and len(gfull["nodes"]) == 5   # whole graph, isolated incl.

def test_store_kg_invalidates_unified_cache(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [{"local_id":"a","object_type":"concept","payload":{"name":"A","section_path":""},"evidence":[]}], [])
    repo.rebuild_unified_kg(nb.id)
    g1 = repo.unified_graph(nb.id, level="concept")
    assert (nb.id, "concept") in repo._unified_cache
    # a new store_kg must evict the cache
    repo.store_kg(nb.id, None, [{"local_id":"b","object_type":"concept","payload":{"name":"B","section_path":""},"evidence":[]}], [])
    assert (nb.id, "concept") not in repo._unified_cache

def test_concept_detail_lists_members_and_attached(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id":"a","object_type":"concept","payload":{"name":"MOSFET","section_path":""},
         "evidence":[{"source_id":"s","source_title":"D","element_id":"e","element_type":"p","location_label":"1","quoted_span":"MOSFET","confidence":1.0}]},
        {"local_id":"k","object_type":"claim","payload":{"name":"MOSFET has threshold","section_path":""},"evidence":[]},
    ], [{"source_local_id":"k","target_local_id":"a","edge_type":"about","evidence":[]}])
    repo.rebuild_unified_kg(nb.id)
    cid = list(repo.cluster_map(nb.id).values())[0]
    detail = repo.concept_detail(nb.id, cid)
    assert detail["canonical_name"] == "MOSFET"
    assert any(x["object_type"]=="claim" for x in detail["attached"])
    assert detail["evidence"]
    # R3·T-B2 pagination fields: default page (200) fits everything here.
    assert detail["member_total"] == 1
    assert detail["next_cursor"] is None

# R3·T-B2 (KG-4 application-side fix): `concept_cluster_detail_rows` used to
# return a hub cluster's entire member set (with full payload/evidence)
# unbounded. These lock the keyset-paginated replacement: page union equals
# the legacy unbounded read (kept as `limit=None`, the oracle), `member_total`
# uses the SAME predicate as the page query (design review B8 — a bare
# `COUNT(*) FROM concept_clusters` would count deprecated members), and
# `attached`/`evidence` are scoped to the page's own members (registered
# display-semantics change).

def _hub_cluster(repo, nb_id, count):
    """`count` concept objects with an identical (post-normalization) name,
    which cluster into a single canonical id on rebuild."""
    repo.store_kg(nb_id, None, [
        {"local_id": f"m{i}", "object_type": "concept",
         "payload": {"name": "HUB", "section_path": ""}, "evidence": []}
        for i in range(count)
    ], [])
    repo.rebuild_unified_kg(nb_id)
    from collections import Counter
    cmap = repo.cluster_map(nb_id)
    return Counter(cmap.values()).most_common(1)[0][0]

def test_concept_detail_pagination_union_matches_full_oracle(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    cid = _hub_cluster(repo, nb.id, 430)  # > one default page (200), > one 90-sized page too

    full = repo.concept_detail(nb.id, cid, limit=None)  # oracle: legacy unbounded read
    assert len(full["members"]) == 430
    assert full["member_total"] == 430
    assert full["next_cursor"] is None

    seen: list[str] = []
    after = ""
    pages = 0
    while True:
        page = repo.concept_detail(nb.id, cid, limit=90, after=after)
        pages += 1
        assert pages <= 6  # ceil(430/90) == 5; guards against an infinite loop on a bug
        page_ids = [m["id"] for m in page["members"]]
        assert page_ids == sorted(page_ids)  # keyset order within the page
        if seen:
            assert seen[-1] < page_ids[0]  # strictly increasing across pages too
        seen.extend(page_ids)
        # R3 PR-B P1-1: the COUNT is only re-run on the FIRST page (`after`
        # unset) — a hub cluster's total would otherwise be re-priced on
        # every "load more" page for no display benefit. Later pages answer
        # `None`; the frontend merge carries the first page's total forward.
        if pages == 1:
            assert page["member_total"] == 430
        else:
            assert page["member_total"] is None
        if not page["next_cursor"]:
            break
        assert page["next_cursor"] == page_ids[-1]
        after = page["next_cursor"]
    assert pages == 5  # 4 full pages of 90 + one page of 70
    assert seen == sorted(m["id"] for m in full["members"])  # union == oracle set, no dup/gap


def test_concept_detail_second_page_skips_the_member_count(repo, monkeypatch):
    """R3 PR-B P1-1 (store-level counter assertion): a "load more" request
    (non-empty `after`) must NOT re-issue `concept_cluster_member_total`'s
    COUNT — that query is O(cluster size), the same order of cost as the
    legacy unbounded read this pagination replaced, so re-running it every
    page would repay almost the whole cost this feature exists to cut.
    Mutation-checked: removing the `if not after:` guard around the COUNT
    call in `KnowledgeQueryService._concept_detail` turns this red (the
    counter reaches 2, and page 2's `member_total` is no longer `None`)."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    cid = _hub_cluster(repo, nb.id, 5)

    knowledge = repo._runtime.knowledge_query.knowledge
    original = knowledge.concept_cluster_member_total
    calls: list = []

    def _counting(db, notebook_id, canonical_id):
        calls.append((notebook_id, canonical_id))
        return original(db, notebook_id, canonical_id)

    monkeypatch.setattr(knowledge, "concept_cluster_member_total", _counting)

    first = repo.concept_detail(nb.id, cid, limit=2)
    assert len(calls) == 1
    assert first["member_total"] == 5
    assert first["next_cursor"]

    second = repo.concept_detail(nb.id, cid, limit=2, after=first["next_cursor"])
    assert len(calls) == 1  # no additional COUNT on the second page
    assert second["member_total"] is None

def test_concept_detail_next_cursor_boundary_cases(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    cid = _hub_cluster(repo, nb.id, 5)

    # Empty cluster (bogus canonical id): no members, no crash, no phantom cursor.
    empty = repo.concept_detail(nb.id, "bogus-canonical-id", limit=200)
    assert empty == {
        "canonical_id": "bogus-canonical-id", "canonical_name": "",
        "members": [], "attached": [], "evidence": [],
        "member_total": 0, "next_cursor": None,
    }

    # Exact full page (limit == member_total): every member fits, no next page.
    exact = repo.concept_detail(nb.id, cid, limit=5)
    assert len(exact["members"]) == 5 and exact["next_cursor"] is None

    # Last page reached via keyset walk: a short final page also has no next cursor.
    first = repo.concept_detail(nb.id, cid, limit=3)
    assert len(first["members"]) == 3 and first["next_cursor"] is not None
    last = repo.concept_detail(nb.id, cid, limit=3, after=first["next_cursor"])
    assert len(last["members"]) == 2 and last["next_cursor"] is None

def test_concept_detail_member_total_excludes_deprecated_members(repo):
    """Design review B8 (hard, mutation-checked): member_total must use the
    SAME predicate as the page query (JOIN knowledge_objects ...
    AND status != 'deprecated'). A bare COUNT(*) FROM concept_clusters would
    count a deprecated member and pagination would look like it never
    reaches the end."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    cid = _hub_cluster(repo, nb.id, 4)
    full = repo.concept_detail(nb.id, cid, limit=None)
    assert full["member_total"] == 4
    deprecated_id = full["members"][0]["id"]
    with repo._connect() as db:
        db.execute("UPDATE knowledge_objects SET status='deprecated' WHERE id=?", (deprecated_id,))
        db.commit()
    after = repo.concept_detail(nb.id, cid, limit=None)
    assert after["member_total"] == 3  # deprecated member dropped from both rows and total
    assert deprecated_id not in {m["id"] for m in after["members"]}

def test_concept_detail_attached_and_evidence_are_page_scoped(repo):
    """Registered display-semantics change (R3·T-B2): attached/evidence are
    computed over the CURRENT PAGE's members only, not the whole cluster.
    Paging through every page still surfaces the complete set."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "ma", "object_type": "concept", "payload": {"name": "HUB", "section_path": ""},
         "evidence": [{"source_id": "s", "source_title": "D", "element_id": "e", "element_type": "p",
                       "location_label": "1", "quoted_span": "tag-A", "confidence": 1.0}]},
        {"local_id": "mb", "object_type": "concept", "payload": {"name": "HUB", "section_path": ""},
         "evidence": [{"source_id": "s", "source_title": "D", "element_id": "e", "element_type": "p",
                       "location_label": "1", "quoted_span": "tag-B", "confidence": 1.0}]},
        {"local_id": "ka", "object_type": "claim", "payload": {"name": "claim-A", "section_path": ""}, "evidence": []},
        {"local_id": "kb", "object_type": "claim", "payload": {"name": "claim-B", "section_path": ""}, "evidence": []},
    ], [
        {"source_local_id": "ka", "target_local_id": "ma", "edge_type": "about", "evidence": []},
        {"source_local_id": "kb", "target_local_id": "mb", "edge_type": "about", "evidence": []},
    ])
    repo.rebuild_unified_kg(nb.id)
    from collections import Counter
    cmap = repo.cluster_map(nb.id)
    cid = Counter(cmap.values()).most_common(1)[0][0]  # the 2-member HUB cluster (claims are singletons)

    full = repo.concept_detail(nb.id, cid, limit=None)
    assert len(full["members"]) == 2
    # Map each member's generated id back to its own tag/claim via the raw
    # per-member evidence carried on `members[i]` (untouched by the flattened
    # `evidence` enrichment below it).
    tag_of = {m["id"]: m["evidence"][0]["quoted_span"] for m in full["members"]}
    claim_of_tag = {"tag-A": "claim-A", "tag-B": "claim-B"}

    page1 = repo.concept_detail(nb.id, cid, limit=1)
    first_id = page1["members"][0]["id"]
    first_tag = tag_of[first_id]
    assert [ev["quoted_span"] for ev in page1["evidence"]] == [first_tag]
    assert [a["payload"]["name"] for a in page1["attached"]] == [claim_of_tag[first_tag]]
    assert page1["next_cursor"] == first_id

    page2 = repo.concept_detail(nb.id, cid, limit=1, after=page1["next_cursor"])
    second_id = page2["members"][0]["id"]
    second_tag = tag_of[second_id]
    assert second_tag != first_tag
    assert [ev["quoted_span"] for ev in page2["evidence"]] == [second_tag]
    assert [a["payload"]["name"] for a in page2["attached"]] == [claim_of_tag[second_tag]]
    assert page2["next_cursor"] is None


# R5 P1 (concept-detail dense-hub P1 fix): `concept_neighbor_rows`'s
# `member_ids` became PAGE-local under R3·T-B2 pagination, so a relation to a
# same-cluster member on a DIFFERENT page used to look like a genuine
# external neighbor to the "not in member_set" check — fully hydrated
# (payload/evidence) before the service layer's `object_type != "concept"`
# filter threw it away. For a dense hub (thousands/millions of members) that
# is an unbounded read. These lock the fix: same-cluster cross-page members
# never reach `attached`'s OUTPUT (already true, via the belt-and-suspenders
# `object_type` filter) AND never get HYDRATED (the actual fix), while the
# legacy unbounded (`limit=None`) read's behavior is unchanged.

def test_concept_detail_attached_excludes_cross_page_cluster_members(repo):
    """Dense-hub cross-page scenario: a same-cluster relation must never
    surface in `attached`, and the genuine external neighbor must still.
    Mutation-checked below (`test_concept_neighbor_rows_...`) that the
    exclusion is what does this, not the pre-existing `object_type` filter
    alone."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    hub_count = 6
    objs = [
        {"local_id": f"m{i}", "object_type": "concept",
         "payload": {"name": "HUB", "section_path": ""}, "evidence": []}
        for i in range(hub_count)
    ] + [
        {"local_id": "ext", "object_type": "claim",
         "payload": {"name": "external fact", "section_path": ""}, "evidence": []}
    ]
    # A path graph over every hub member: whichever physical member ends up
    # sorted LAST (the one `limit=hub_count-1` pushes to page 2 below) is
    # guaranteed to have at least one chain edge to a page-1 member, in
    # either direction -- so the cross-page case is exercised regardless of
    # which local_id the store assigns the highest generated id to.
    chain = [
        {"source_local_id": f"m{i}", "target_local_id": f"m{i+1}",
         "edge_type": "related_to", "evidence": []}
        for i in range(hub_count - 1)
    ]
    # "ext" is wired to EVERY hub member (not just one) for the same
    # sort-order-independence reason -- `attached` dedups by the other
    # object's id, so this still yields exactly one attached entry.
    ext_edges = [
        {"source_local_id": "ext", "target_local_id": f"m{i}",
         "edge_type": "about", "evidence": []}
        for i in range(hub_count)
    ]
    repo.store_kg(nb.id, None, objs, chain + ext_edges)
    repo.rebuild_unified_kg(nb.id)
    from collections import Counter
    cmap = repo.cluster_map(nb.id)
    cid = Counter(cmap.values()).most_common(1)[0][0]  # the hub_count-member HUB cluster

    full = repo.concept_detail(nb.id, cid, limit=None)  # oracle: legacy unbounded read
    full_ids = sorted(m["id"] for m in full["members"])
    assert len(full_ids) == hub_count
    # Full-oracle regression (item 5 of the fix plan): the unbounded read's
    # `member_set` covers the WHOLE cluster, so an in-cluster relation could
    # never look like an external neighbor even before this fix -- confirms
    # the fix changes nothing here.
    assert [a["payload"]["name"] for a in full["attached"]] == ["external fact"]

    last_id = full_ids[-1]
    page1 = repo.concept_detail(nb.id, cid, limit=hub_count - 1)
    assert [m["id"] for m in page1["members"]] == full_ids[:-1]
    assert page1["next_cursor"] == full_ids[-2]
    attached_ids_page1 = {a["id"] for a in page1["attached"]}
    assert last_id not in attached_ids_page1  # cross-page cluster-mate excluded
    assert [a["payload"]["name"] for a in page1["attached"]] == ["external fact"]


def test_concept_neighbor_rows_probes_and_excludes_same_cluster_members(repo):
    """Repo-layer contract (mutation-checked, see report): `concept_neighbor_rows`
    issues a same-cluster membership probe against `concept_clusters` BEFORE
    hydrating attached candidates, and the hydrated set (`by_other`) never
    contains a same-cluster id -- only the genuine external one."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    with repo._write() as db:
        for i in range(3):
            db.execute(
                "INSERT INTO knowledge_objects "
                "(id,notebook_id,object_type,status,payload,evidence,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (f"member-{i}", nb.id, "concept", "active", "{}", "[]",
                 "2024-01-01", "2024-01-01"),
            )
            db.execute(
                "INSERT INTO concept_clusters "
                "(id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (f"cc-{i}", nb.id, "canon-probe", f"member-{i}", "HUB", "concept",
                 "2024-01-01"),
            )
        db.execute(
            "INSERT INTO knowledge_objects "
            "(id,notebook_id,object_type,status,payload,evidence,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("ext-1", nb.id, "claim", "active", json.dumps({"name": "external"}), "[]",
             "2024-01-01", "2024-01-01"),
        )
        # member-0 (page-local) --related_to--> member-2 (same cluster, OTHER page)
        db.execute(
            "INSERT INTO knowledge_relations "
            "(id,notebook_id,source_object_id,target_object_id,edge_type,evidence,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("rel-cross-page", nb.id, "member-0", "member-2", "related_to", "[]",
             "2024-01-01"),
        )
        # ext-1 --about--> member-0 (genuine external neighbor)
        db.execute(
            "INSERT INTO knowledge_relations "
            "(id,notebook_id,source_object_id,target_object_id,edge_type,evidence,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("rel-ext", nb.id, "ext-1", "member-0", "about", "[]", "2024-01-01"),
        )

    knowledge = repo._runtime.knowledge_query.knowledge
    probes: list = []

    class Recorder:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, sql, parameters=()):
            text = " ".join(str(sql).split())
            if text.startswith("SELECT member_object_id FROM concept_clusters"):
                probes.append((text, list(parameters)))
            return self.connection.execute(sql, parameters)

    with repo._connect() as db:
        rel_edges, by_other = knowledge.concept_neighbor_rows(
            Recorder(db), nb.id, "canon-probe", ["member-0", "member-1"],
        )

    assert probes, "same-cluster membership probe must run"
    assert "member_object_id IN (" in probes[0][0]
    assert probes[0][1][:2] == [nb.id, "canon-probe"]
    assert "member-2" in probes[0][1][2:]

    assert "member-2" not in by_other  # same-cluster cross-page member never hydrated
    assert "ext-1" in by_other          # genuine external neighbor still hydrated


def test_concept_neighbor_rows_batches_over_900_candidates(repo):
    """SQLite parameter-cap correctness (item 5's ">900 候选" case): both the
    same-cluster exclusion probe AND the attached-candidate hydration must
    batch when the candidate count exceeds `batch_size` (default 900) --
    otherwise a dense hub's cross-page candidate count can exceed
    SQLITE_MAX_VARIABLE_NUMBER outright. 905 same-cluster candidates (must
    ALL be excluded) plus 905 genuinely-external candidates (must ALL be
    hydrated) push both the exclusion probe and the hydration read past one
    batch."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    same_cluster_count = 905
    external_count = 905
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowledge_objects "
            "(id,notebook_id,object_type,status,payload,evidence,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("page-0", nb.id, "concept", "active", "{}", "[]", "2024-01-01", "2024-01-01"),
        )
        db.execute(
            "INSERT INTO concept_clusters "
            "(id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("cc-page-0", nb.id, "canon-batch", "page-0", "HUB", "concept", "2024-01-01"),
        )
        db.executemany(
            "INSERT INTO knowledge_objects "
            "(id,notebook_id,object_type,status,payload,evidence,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [(f"sc-{i}", nb.id, "concept", "active", "{}", "[]", "2024-01-01", "2024-01-01")
             for i in range(same_cluster_count)],
        )
        db.executemany(
            "INSERT INTO concept_clusters "
            "(id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            [(f"cc-sc-{i}", nb.id, "canon-batch", f"sc-{i}", "HUB", "concept", "2024-01-01")
             for i in range(same_cluster_count)],
        )
        db.executemany(
            "INSERT INTO knowledge_relations "
            "(id,notebook_id,source_object_id,target_object_id,edge_type,evidence,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            [(f"rel-sc-{i}", nb.id, "page-0", f"sc-{i}", "related_to", "[]", "2024-01-01")
             for i in range(same_cluster_count)],
        )
        db.executemany(
            "INSERT INTO knowledge_objects "
            "(id,notebook_id,object_type,status,payload,evidence,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [(f"ext-{i}", nb.id, "claim", "active", "{}", "[]", "2024-01-01", "2024-01-01")
             for i in range(external_count)],
        )
        db.executemany(
            "INSERT INTO knowledge_relations "
            "(id,notebook_id,source_object_id,target_object_id,edge_type,evidence,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            [(f"rel-ext-{i}", nb.id, f"ext-{i}", "page-0", "about", "[]", "2024-01-01")
             for i in range(external_count)],
        )

    knowledge = repo._runtime.knowledge_query.knowledge
    probes: list = []
    hydrations: list = []

    class Recorder:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, sql, parameters=()):
            text = " ".join(str(sql).split())
            if text.startswith("SELECT member_object_id FROM concept_clusters"):
                probes.append(list(parameters))
            elif text.startswith("SELECT id, object_type, payload, evidence FROM knowledge_objects"):
                hydrations.append(list(parameters))
            return self.connection.execute(sql, parameters)

    with repo._connect() as db:
        rel_edges, by_other = knowledge.concept_neighbor_rows(
            Recorder(db), nb.id, "canon-batch", ["page-0"],
        )

    # Exclusion probe: 1810 total candidates (905 same-cluster + 905
    # external) batched at 900 -> [900, 900, 10].
    assert [len(p) - 2 for p in probes] == [900, 900, 10]
    probed_ids = {value for params in probes for value in params[2:]}
    assert probed_ids == {f"sc-{i}" for i in range(same_cluster_count)} | {
        f"ext-{i}" for i in range(external_count)
    }

    # Hydration: only the 905 survivors (external) are ever read, batched at
    # 900 -> [900, 5].
    assert [len(p) for p in hydrations] == [900, 5]
    hydrated_ids = {value for params in hydrations for value in params}
    assert hydrated_ids == {f"ext-{i}" for i in range(external_count)}

    assert len(by_other) == external_count
    assert set(by_other) == {f"ext-{i}" for i in range(external_count)}
    for i in range(same_cluster_count):
        assert f"sc-{i}" not in by_other


def test_confirm_merge_unions_clusters_on_rebuild(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [{"local_id":"a","object_type":"concept","payload":{"name":"current mirror","section_path":""},"evidence":[]}], [])
    repo.store_kg(nb.id, None, [{"local_id":"b","object_type":"concept","payload":{"name":"current source","section_path":""},"evidence":[]}], [])
    repo.rebuild_unified_kg(nb.id)
    cmap = repo.cluster_map(nb.id)
    a_cid, b_cid = cmap[list(cmap)[0]], cmap[list(cmap)[1]]
    assert a_cid != b_cid                                # distinct names -> separate clusters
    repo.write_merge_candidate(nb.id, a_cid, b_cid, 0.84)
    cand = repo.pending_merges(nb.id)[0]
    repo.confirm_merge(nb.id, cand["id"])
    repo.rebuild_unified_kg(nb.id)
    assert len(set(repo.cluster_map(nb.id).values())) == 1   # forced union held across rebuild


def test_rebuild_tolerates_mixed_dim_vectors(repo):
    import datetime
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [{"local_id":"a","object_type":"concept","payload":{"name":"A","section_path":""},"evidence":[]}], [])
    with repo._connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO knowledge_embeddings (object_id, notebook_id, vector, created_at) VALUES (?,?,?,?)",
            ("rogue", nb.id, json.dumps([0.1] * 999), datetime.datetime.now().isoformat()))
    assert repo.rebuild_unified_kg(nb.id) >= 1   # must NOT raise on mismatched-dim vector


def test_unified_kg_dirty_status_lifecycle(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    status = repo.unified_kg_status(nb.id)
    assert status["dirty"] is False
    assert status["clusters"] == 0

    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept", "payload": {"name": "MOSFET", "section_path": ""}, "evidence": []}
    ], [])
    status = repo.unified_kg_status(nb.id)
    assert status["dirty"] is True

    repo.rebuild_unified_kg(nb.id)
    status = repo.unified_kg_status(nb.id)
    assert status["dirty"] is False
    assert status["clusters"] == 1


def test_rebuild_applies_llm_confirmed_auto_candidate(repo):
    """LLM 兜底: auto_candidate 经 LLM 确认 merge 后, rebuild 应将两个概念合入同一簇。"""
    from app.services.sqlite_repository import _now
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    o1 = repo._test_insert_object(nb.id, "concept", {"name": "operational amplifier"})
    o2 = repo._test_insert_object(nb.id, "concept", {"name": "op amplifier circuit"})
    # 用16维向量(与 EMBED_DIM=16 匹配), 方向相同 -> 余弦相似度=1.0(≥hi=0.94)
    vec = [1.0] + [0.0] * 15
    with repo._write() as db:
        for oid in (o1, o2):
            db.execute(
                "INSERT OR REPLACE INTO knowledge_embeddings "
                "(object_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                (oid, nb.id, json.dumps(vec), _now()))

    class _LLM:
        configured = True
        def chat_json(self, messages, schema):
            return ('{"decisions":[{"candidate_id":"ac0","decision":"merge",'
                    '"canonical_name":"operational amplifier","confidence":0.99,'
                    '"rationale":"same concept"}]}')

    bind_chat_client(repo, "kg_merge_review", _LLM())
    repo.rebuild_unified_kg(nb.id)
    cmap = repo.cluster_map(nb.id)
    assert cmap.get(o1) == cmap.get(o2), (
        f"Expected same cluster after LLM-confirmed merge, got {cmap}")


class _CannedReviewLLM:
    """Mock LLM that returns a fixed (decision, confidence) for every candidate it
    is asked to review. Mirrors concept_merge_review's chat_json interface so
    review_pending_merges runs without a real model."""

    configured = True

    def __init__(self, decision: str, confidence: float):
        self._decision = decision
        self._confidence = confidence

    def chat_json(self, messages, schema):
        import re
        content = messages[0]["content"]
        ids = re.findall(r"id=(\S+)", content)
        decisions = [
            {
                "candidate_id": cid,
                "decision": self._decision,
                "canonical_name": "x",
                "confidence": self._confidence,
                "rationale": "canned",
            }
            for cid in ids
        ]
        return json.dumps({"decisions": decisions})


def _candidate_status(repo, nb_id, cid):
    with repo._connect() as db:
        row = db.execute(
            "SELECT status FROM concept_merge_candidates WHERE id=? AND notebook_id=?",
            (cid, nb_id)).fetchone()
    return row["status"]


def test_review_merge_below_confirm_threshold_becomes_deferred(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.write_merge_candidate(nb.id, "K1", "K2", 0.8)
    cid = repo.pending_merges(nb.id)[0]["id"]
    bind_chat_client(repo, "kg_merge_review", _CannedReviewLLM("merge", 0.88))
    out = repo.review_pending_merges(nb.id, confirm_threshold=0.90, separate_threshold=0.80)
    assert out == {"reviewed": 1, "confirmed": 0, "rejected": 0, "unsure": 1}
    assert _candidate_status(repo, nb.id, cid) == "deferred"


def test_review_merge_at_confirm_threshold_is_confirmed(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.write_merge_candidate(nb.id, "K1", "K2", 0.8)
    cid = repo.pending_merges(nb.id)[0]["id"]
    bind_chat_client(repo, "kg_merge_review", _CannedReviewLLM("merge", 0.92))
    out = repo.review_pending_merges(nb.id, confirm_threshold=0.90, separate_threshold=0.80)
    assert out == {"reviewed": 1, "confirmed": 1, "rejected": 0, "unsure": 0}
    assert _candidate_status(repo, nb.id, cid) == "confirmed"


def test_review_keep_separate_at_threshold_is_rejected(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.write_merge_candidate(nb.id, "K1", "K2", 0.8)
    cid = repo.pending_merges(nb.id)[0]["id"]
    bind_chat_client(repo, "kg_merge_review", _CannedReviewLLM("keep_separate", 0.85))
    out = repo.review_pending_merges(nb.id, confirm_threshold=0.90, separate_threshold=0.80)
    assert out == {"reviewed": 1, "confirmed": 0, "rejected": 1, "unsure": 0}
    assert _candidate_status(repo, nb.id, cid) == "rejected"


def test_review_keep_separate_below_threshold_becomes_deferred(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.write_merge_candidate(nb.id, "K1", "K2", 0.8)
    cid = repo.pending_merges(nb.id)[0]["id"]
    bind_chat_client(repo, "kg_merge_review", _CannedReviewLLM("keep_separate", 0.70))
    out = repo.review_pending_merges(nb.id, confirm_threshold=0.90, separate_threshold=0.80)
    assert out == {"reviewed": 1, "confirmed": 0, "rejected": 0, "unsure": 1}
    assert _candidate_status(repo, nb.id, cid) == "deferred"


def test_review_defaults_drain_keep_separate_that_old_single_threshold_left_pending(repo):
    """Under the old single 0.95 threshold a 0.88 keep_separate stayed pending forever.
    With the asymmetric settings defaults (confirm 0.90 / separate 0.80) it now drains
    to rejected without any explicit threshold args."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    assert repo.settings.kg_merge_confirm_threshold == 0.90
    assert repo.settings.kg_merge_separate_threshold == 0.80
    repo.write_merge_candidate(nb.id, "K1", "K2", 0.8)
    cid = repo.pending_merges(nb.id)[0]["id"]
    bind_chat_client(repo, "kg_merge_review", _CannedReviewLLM("keep_separate", 0.88))
    out = repo.review_pending_merges(nb.id)  # no thresholds -> settings defaults
    assert out == {"reviewed": 1, "confirmed": 0, "rejected": 1, "unsure": 0}
    assert _candidate_status(repo, nb.id, cid) == "rejected"


def test_write_clusters_is_per_type_isolated(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.write_clusters(nb.id, [{"canonical_id": "K-a", "member_object_id": "o1",
                                 "canonical_name": "A"}], object_type="concept")
    repo.write_clusters(nb.id, [{"canonical_id": "KL-b", "member_object_id": "o2",
                                 "canonical_name": "B"}], object_type="claim")
    cm = repo.cluster_map(nb.id)
    assert cm.get("o1") == "K-a" and cm.get("o2") == "KL-b"   # both persist
    # rewriting concept clusters must NOT delete claim clusters
    repo.write_clusters(nb.id, [{"canonical_id": "K-a2", "member_object_id": "o1b",
                                 "canonical_name": "A2"}], object_type="concept")
    cm2 = repo.cluster_map(nb.id)
    assert "o2" in cm2 and cm2.get("o1") is None and cm2.get("o1b") == "K-a2"


def test_review_pending_merges_fail_open_on_bad_llm_json(repo):
    """A categorical confidence ("high") from the LLM must not 500 this endpoint.

    The KG route only catches KeyError; review_merge_candidates must be
    total so review_pending_merges returns a summary instead of raising ValueError.
    """
    from app.services.sqlite_repository import _now

    nb = repo.create_notebook(NotebookCreate(name="nb"))
    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO concept_merge_candidates "
            "(id,notebook_id,canonical_a,canonical_b,score,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?, 'pending', ?, ?)",
            ("cand-1", nb.id, "K-vco", "K-voltage controlled oscillator", 0.93, now, now),
        )

    class _BadConfLLM:
        configured = True

        def chat_json(self, messages, response_schema_hint):
            return json.dumps({"decisions": [
                {"candidate_id": "cand-1", "decision": "merge",
                 "canonical_name": "vco", "confidence": "high", "rationale": "r"},
            ]})

    bind_chat_client(repo, "kg_merge_review", _BadConfLLM())  # inject via setter (system default LLM)
    summary = repo.review_pending_merges(nb.id)  # must NOT raise / 500
    assert isinstance(summary, dict)
    # "high" confidence coerces to 0.0 -> below thresholds -> counted as unsure, not confirmed.
    assert summary["confirmed"] == 0
    assert summary["reviewed"] == 1
