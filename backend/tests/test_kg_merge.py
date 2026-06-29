import time
from app.services.kg_merge import cluster_concepts, _norm

def _concept(oid, name): return {"object_id": oid, "name": name}

def test_name_seed_auto_merge():
    concepts = [_concept("o1", "MOSFET"), _concept("o2", "mosfet "), _concept("o3", "BJT")]
    vecs = {"o1": [1.0, 0], "o2": [1.0, 0], "o3": [0, 1.0]}
    res = cluster_concepts(concepts, vecs, confirmed=set(), rejected=set(), hi=0.9, lo=0.82)
    cmap = res["cluster_map"]
    assert cmap["o1"] == cmap["o2"] and cmap["o1"] != cmap["o3"]

def test_vector_threshold_and_pending():
    # Under new contract: vector-similar pairs (sim >= hi) go to auto_candidates, NOT auto-merged
    # in cluster_map. Exact-name-merged pairs are still unioned.
    concepts = [_concept("o1", "current mirror"), _concept("o2", "current-mirror circuit"), _concept("o3", "slew rate")]
    vecs = {"o1": [1.0, 0.0], "o2": [0.97, 0.24], "o3": [0.0, 1.0]}
    res = cluster_concepts(concepts, vecs, confirmed=set(), rejected=set(), hi=0.9, lo=0.82)
    # o1 and o2 are NOT auto-merged in cluster_map; they appear in auto_candidates instead
    assert res["cluster_map"]["o1"] != res["cluster_map"]["o2"]
    # The pair (o1, o2) should appear in auto_candidates (sim ~0.97 >= hi=0.9)
    cids_o1 = res["cluster_map"]["o1"]
    cids_o2 = res["cluster_map"]["o2"]
    auto_pairs = {frozenset((a, b)) for a, b, _ in res["auto_candidates"]}
    assert frozenset((cids_o1, cids_o2)) in auto_pairs
    # o3 is not merged with either o1 or o2 (unchanged semantics)
    assert all(res["cluster_map"]["o3"] != res["cluster_map"][o] for o in ("o1", "o2"))

def test_rejected_pair_not_merged_confirmed_forced():
    concepts = [_concept("o1", "A"), _concept("o2", "B")]
    vecs = {"o1": [1.0, 0.0], "o2": [0.99, 0.14]}
    res_r = cluster_concepts(concepts, vecs, confirmed=set(), rejected={frozenset(("A", "B"))}, hi=0.9, lo=0.82)
    assert res_r["cluster_map"]["o1"] != res_r["cluster_map"]["o2"]
    res_c = cluster_concepts([_concept("o1","A"), _concept("o2","B")], {"o1":[1.0,0.0],"o2":[0.0,1.0]},
                             confirmed={frozenset(("A","B"))}, rejected=set(), hi=0.9, lo=0.82)
    assert res_c["cluster_map"]["o1"] == res_c["cluster_map"]["o2"]

def test_perf_2000_reps_under_2s():
    concepts = [_concept(f"o{i}", f"concept {i}") for i in range(2000)]
    vecs = {f"o{i}": [float((i % 7) == k) for k in range(8)] for i in range(2000)}
    t = time.perf_counter()
    cluster_concepts(concepts, vecs, confirmed=set(), rejected=set(), hi=0.9, lo=0.82)
    assert time.perf_counter() - t < 2.0

def test_large_seed_set_still_uses_vector_candidates():
    concepts = [_concept(f"o{i}", f"concept {i}") for i in range(4500)]
    concepts.extend([
        _concept("mos_a", "voltage-controlled oscillator"),
        _concept("mos_b", "VCO"),
    ])
    vecs = {f"o{i}": [1.0 if (i % 16) == k else 0.0 for k in range(16)] for i in range(4500)}
    vecs["mos_a"] = [1.0] + [0.0] * 15
    vecs["mos_b"] = [0.99, 0.01] + [0.0] * 14

    res = cluster_concepts(concepts, vecs, confirmed=set(), rejected=set(), hi=0.94, lo=0.86)

    assert res["capped"] is False
    assert res["cluster_map"]["mos_a"] == res["cluster_map"]["mos_b"]


def test_pending_candidates_are_bounded_and_ranked():
    concepts = [_concept(f"o{i}", f"concept {i}") for i in range(200)]
    vecs = {f"o{i}": [1.0, i / 1000.0] for i in range(200)}

    res = cluster_concepts(
        concepts,
        vecs,
        confirmed=set(),
        rejected=set(),
        hi=0.9999,
        lo=0.90,
        top_k=3,
        max_pending=50,
    )

    assert len(res["pending"]) <= 50
    scores = [score for _a, _b, score in res["pending"]]
    assert scores == sorted(scores, reverse=True)


from app.services.kg_merge import derive_unified_graph

def test_derive_rewires_and_dedups_edges():
    cluster_map = {"o1": "K1", "o2": "K1"}
    nodes = [{"id":"o1","object_type":"concept","payload":{"name":"MOSFET"}},
             {"id":"o2","object_type":"concept","payload":{"name":"mosfet"}},
             {"id":"k1","object_type":"claim","payload":{"name":"claim A"}}]
    edges = [{"source_object_id":"k1","target_object_id":"o1","edge_type":"about"},
             {"source_object_id":"k1","target_object_id":"o2","edge_type":"about"}]
    g = derive_unified_graph(nodes, edges, cluster_map)
    concept_ids = {n["id"] for n in g["nodes"] if n["object_type"]=="concept"}
    assert concept_ids == {"K1"}                       # two MOSFET nodes -> one canonical
    about = [e for e in g["edges"] if e["edge_type"]=="about"]
    assert len(about) == 1 and about[0]["target_object_id"]=="K1"   # rewired + deduped


from app.services.kg_merge import limit_graph_by_degree


def _star_graph():
    # hub h connected to a,b,c; d,e isolated (degree 0)
    nodes = [{"id": i, "object_type": "concept"} for i in ["h", "a", "b", "c", "d", "e"]]
    edges = [{"source_object_id": "h", "target_object_id": t, "edge_type": "rel"}
             for t in ["a", "b", "c"]]
    return {"nodes": nodes, "edges": edges}


def test_limit_keeps_top_degree_and_internal_edges():
    g = limit_graph_by_degree(_star_graph(), 2)
    ids = {n["id"] for n in g["nodes"]}
    assert "h" in ids and len(ids) == 2          # hub (deg 3) + one of its neighbors
    # every kept edge has both endpoints in the kept set
    assert all(e["source_object_id"] in ids and e["target_object_id"] in ids for e in g["edges"])


def test_limit_excludes_isolated_nodes_first():
    g = limit_graph_by_degree(_star_graph(), 4)
    ids = {n["id"] for n in g["nodes"]}
    assert ids == {"h", "a", "b", "c"}           # degree-0 d/e ranked last, dropped
    assert len(g["edges"]) == 3


def test_limit_noop_when_limit_covers_all():
    full = _star_graph()
    g = limit_graph_by_degree(full, 99)
    assert len(g["nodes"]) == 6 and len(g["edges"]) == 3
    assert limit_graph_by_degree(full, None)["nodes"] is full["nodes"]


def test_discriminative_conflict_blocks_contrast_twins():
    from app.services.kg_merge import _discriminative_conflict
    assert _discriminative_conflict("voltage voltage feedback", "current voltage feedback")
    assert _discriminative_conflict("single balanced mixer", "double balanced mixer")
    assert _discriminative_conflict("drain", "source")
    assert _discriminative_conflict("NMOS", "PMOS")


def test_discriminative_conflict_keeps_subtypes_and_aliases():
    from app.services.kg_merge import _discriminative_conflict
    assert not _discriminative_conflict("current mirror", "wilson current mirror")
    assert not _discriminative_conflict("current mirror", "cascode current mirror")
    assert not _discriminative_conflict("VCO", "voltage controlled oscillator")
    assert not _discriminative_conflict("low pass filter", "low pass filter")


def test_ann_candidates_recall_vs_bruteforce():
    import numpy as np
    from app.services.kg_merge import _ann_candidates
    rng = np.random.default_rng(0)
    seeds = [f"s{i}" for i in range(200)]
    reps = {s: rng.standard_normal(32).astype("float32") for s in seeds}
    got = {(a, b) for a, b, _ in _ann_candidates(seeds, reps, k=5, lo=0.5)}
    M = np.asarray([reps[s] for s in seeds], dtype="float32")
    M /= (np.linalg.norm(M, axis=1, keepdims=True) + 1e-8)
    sims = M @ M.T
    brute = set()
    for i in range(len(seeds)):
        order = np.argsort(-sims[i])
        cnt = 0
        for j in order:
            if j == i:
                continue
            if sims[i, j] < 0.5:
                break
            a, b = (i, j) if i < j else (j, i)
            brute.add((seeds[a], seeds[b]))
            cnt += 1
            if cnt >= 5:
                break
    if brute:
        recall = len(got & brute) / len(brute)
        assert recall >= 0.9, recall


def test_star_groups_breaks_chains():
    from app.services.kg_merge import _star_groups
    seeds = ["A", "B", "C"]
    members = {"A": [1, 2, 3], "B": [1], "C": [1]}   # A 质量最高 → A 当锚点
    edges = [("A", "B", 0.96), ("B", "C", 0.96)]      # A~B、B~C ≥hi; 无 A~C
    asn = _star_groups(seeds, members, edges, hi=0.94)
    assert asn["A"] == "A"
    assert asn["B"] == "A"
    assert asn["C"] == "C"


def test_star_groups_claims_direct_neighbors():
    from app.services.kg_merge import _star_groups
    seeds = ["X", "Y", "Z"]
    members = {"X": [1, 2], "Y": [1], "Z": [1]}
    edges = [("X", "Y", 0.97), ("X", "Z", 0.95)]
    asn = _star_groups(seeds, members, edges, hi=0.94)
    assert asn["Y"] == "X" and asn["Z"] == "X"


def test_cluster_concepts_exact_name_unions_vectors_become_candidates():
    from app.services.kg_merge import cluster_concepts
    concepts = [_concept("o1", "current mirror"), _concept("o2", "Current Mirror"),
                _concept("o3", "voltage controlled oscillator")]
    vecs = {"o1": [1.0, 0.0], "o2": [1.0, 0.0], "o3": [0.99, 0.01]}
    res = cluster_concepts(concepts, vecs, confirmed=set(), rejected=set())
    assert res["cluster_map"]["o1"] == res["cluster_map"]["o2"]   # exact-name merged
    assert res["cluster_map"]["o3"] != res["cluster_map"]["o1"]   # vector sim NOT auto-merged
    assert "auto_candidates" in res and "pending" in res


def test_cluster_concepts_guard_blocks_twin_candidate():
    from app.services.kg_merge import cluster_concepts
    concepts = [_concept("a", "single balanced mixer"), _concept("b", "double balanced mixer")]
    vecs = {"a": [1.0, 0.0], "b": [0.999, 0.001]}
    res = cluster_concepts(concepts, vecs, confirmed=set(), rejected=set())
    assert res["cluster_map"]["a"] != res["cluster_map"]["b"]
    allcand = {frozenset((a, b)) for a, b, _ in res["auto_candidates"] + res["pending"]}
    assert frozenset(("K-single balanced mixer", "K-double balanced mixer")) not in allcand
