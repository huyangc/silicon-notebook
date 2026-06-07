from app.services.kg_merge import cluster_objects, cluster_concepts


def _obj(oid, name):
    return {"object_id": oid, "name": name}


def test_cluster_objects_groups_by_custom_seed_and_prefix():
    objs = [_obj("o1", "Foo Bar"), _obj("o2", "foo  bar"), _obj("o3", "Other")]
    res = cluster_objects(objs, {}, set(), set(),
                          seed_fn=lambda c: " ".join(c["name"].lower().split()),
                          conflict_fn=None, id_prefix="KX-")
    cm = res["cluster_map"]
    assert cm["o1"] == cm["o2"]                       # merged by normalized name
    assert cm["o3"] != cm["o1"]                       # distinct
    assert all(v.startswith("KX-") for v in cm.values())   # id_prefix honored
    assert set(res["canonical_names"].values()) <= {"Foo Bar", "Other"}  # display name kept


def test_cluster_concepts_wrapper_preserves_behavior():
    # concept wrapper keeps _ALIASES normalization + K- prefix
    objs = [_obj("o1", "VCO"), _obj("o2", "voltage controlled oscillator"),
            _obj("o3", "low noise amplifier")]
    res = cluster_concepts(objs, {}, set(), set())
    cm = res["cluster_map"]
    assert cm["o1"] == cm["o2"]                       # vco alias -> full name, merged
    assert cm["o1"].startswith("K-")
    assert cm["o3"] != cm["o1"]


def test_cluster_objects_confirmed_same_name_fold_no_crash():
    # a confirmed pair whose two seeds collapse to one (size-1 frozenset after a
    # caller's normalization) must be skipped harmlessly, not raise ValueError.
    objs = [_obj("o1", "x"), _obj("o2", "y")]
    res = cluster_objects(objs, {}, {frozenset({"x"})}, set(),
                          seed_fn=lambda c: c["name"], id_prefix="K-")
    assert "o1" in res["cluster_map"] and "o2" in res["cluster_map"]
