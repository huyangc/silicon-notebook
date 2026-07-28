from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path


def _audit_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "audit_kg_edge_contract.py"
    spec = importlib.util.spec_from_file_location("audit_kg_edge_contract", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_audit_is_aggregate_only_and_classifies_invalid_pairs(tmp_path):
    database = tmp_path / "audit.db"
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        "CREATE TABLE knowledge_objects (id TEXT PRIMARY KEY, object_type TEXT);"
        "CREATE TABLE knowledge_relations ("
        " id TEXT PRIMARY KEY, notebook_id TEXT, source_object_id TEXT,"
        " target_object_id TEXT, edge_type TEXT, evidence TEXT, review_status TEXT);"
    )
    connection.executemany(
        "INSERT INTO knowledge_objects VALUES (?, ?)",
        [("concept", "concept"), ("claim", "claim"), ("custom", "voltage")],
    )
    connection.executemany(
        "INSERT INTO knowledge_relations VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("valid", "nb", "claim", "concept", "about", "[]", "pending"),
            (
                "invalid", "nb", "concept", "claim", "about",
                json.dumps([{"basis": "relink:name-match", "quote": "secret"}]),
                "verified",
            ),
            ("extension", "nb", "claim", "custom", "depends_on", "[]", "pending"),
        ],
    )
    report = _audit_module().audit(connection, "nb", sample_limit=2)
    connection.close()

    assert report["read_only"] is True
    assert (report["valid"], report["extension"], report["invalid"]) == (1, 1, 1)
    invalid_group = next(group for group in report["groups"] if group["validity"] == "invalid")
    assert invalid_group["origin"] == "deterministic_relink"
    assert invalid_group["review_status"] == "verified"
    assert invalid_group["sample_relation_ids"] == ["invalid"]
    assert "secret" not in json.dumps(report)
