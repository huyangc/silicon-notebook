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
