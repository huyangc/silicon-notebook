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
    concepts = [_concept("o1", "current mirror"), _concept("o2", "current-mirror circuit"), _concept("o3", "slew rate")]
    vecs = {"o1": [1.0, 0.0], "o2": [0.97, 0.24], "o3": [0.0, 1.0]}
    res = cluster_concepts(concepts, vecs, confirmed=set(), rejected=set(), hi=0.9, lo=0.82)
    assert res["cluster_map"]["o1"] == res["cluster_map"]["o2"]
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
