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
