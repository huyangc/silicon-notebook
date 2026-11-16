"""Shared cases for the compact relink reader's backend twins."""
import json


def projection_rows():
    evidence = [{"element_id": "e2", "quoted_span": "discarded evidence"},
                {"element_id": "e1"}, {"element_id": "e2"},
                {"element_id": ""}, {"element_id": None}, {}, "ignored", None]
    return [
        ("z-first", "nb", "s", "concept", "approved",
         json.dumps({"name": "概念 Ω", "unused": "long payload"}), json.dumps(evidence)),
        ("a-second", "nb", "s", "claim", "pending", '{}', '[]'),
        ("retired", "nb", "s", "claim", "deprecated", '{}', '[]'),
        ("foreign", "other", "s", "claim", "approved", '{}', '[]'),
        ("other-source", "nb", "other", "claim", "approved", '{}', '[]'),
        ("empty-source", "nb", "", "claim", "approved", '{"name":"empty"}', '[]'),
        ("deleted-source", "nb", "deleted", "claim", "approved", '{"name":null}', '[]'),
    ]


def assert_projection(store, db):
    assert store.relink_node_rows_for_source(db, "nb", "s") == [
        {"id": "z-first", "object_type": "concept", "name": "概念 Ω",
         "element_ids": {"e1", "e2"}},
        {"id": "a-second", "object_type": "claim", "name": "", "element_ids": set()},
    ]
    assert store.relink_node_rows_for_source(db, "nb", "") == [
        {"id": "empty-source", "object_type": "claim", "name": "empty", "element_ids": set()},
    ]
    assert store.relink_node_rows_for_source(db, "nb", "deleted") == [
        {"id": "deleted-source", "object_type": "claim", "name": None, "element_ids": set()},
    ]
    assert store.relink_node_rows_for_source(db, "nb", "absent") == []
