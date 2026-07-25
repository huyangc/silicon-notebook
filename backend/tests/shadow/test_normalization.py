from __future__ import annotations

import hashlib
import json
import math
import struct
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from psycopg.types.json import Jsonb

from app.migration.shadow.manifest import MANIFEST
from app.migration.shadow.transform import PostgresColumn
from app.migration.shadow.verifier import (
    TableVerification,
    canonical_json_bytes,
    canonical_replication_key,
    canonical_row_bytes,
    vector_cosine,
    vector_summary,
)


def _spec(name: str):
    return next(item for item in MANIFEST.replicated if item.name == name)


def test_composite_replication_key_uses_manifest_order_not_mapping_order():
    spec = _spec("community_members")
    encoded = canonical_replication_key(
        spec,
        {"canonical_id": "c", "notebook_id": "n", "level": 2},
    )
    decoded = json.loads(encoded)
    assert [item[0] for item in decoded] == [
        "notebook_id",
        "level",
        "canonical_id",
    ]
    assert encoded == canonical_replication_key(
        spec,
        (("notebook_id", "n"), ("level", 2), ("canonical_id", "c")),
    )


def test_composite_replication_key_rejects_wrong_shape_and_duplicates():
    spec = _spec("community_members")
    with pytest.raises(ValueError, match="shape"):
        canonical_replication_key(spec, (("level", 2), ("notebook_id", "n")))
    with pytest.raises(ValueError, match="shape"):
        canonical_replication_key(
            spec,
            (("notebook_id", "n"), ("level", 2), ("level", 3)),
        )


def test_json_normalization_sorts_keys_preserves_null_and_normalizes_numbers():
    left = canonical_json_bytes({"z": None, "a": [Decimal("1.00"), True, "中文"]})
    right = canonical_json_bytes({"a": [1, True, "中文"], "z": None})
    assert left == right
    assert b'"null"' in left
    assert "中文".encode() in left


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, Decimal("NaN")])
def test_json_normalization_rejects_non_finite_at_any_depth(value):
    with pytest.raises(ValueError, match="non-finite"):
        canonical_json_bytes({"outer": [value]})


def test_json_wrapper_and_text_have_same_canonical_row(tmp_path):
    spec = _spec("object_schemas")
    columns = (PostgresColumn("fields", "jsonb", False),)
    source, _ = canonical_row_bytes(
        spec,
        columns,
        (Jsonb({"b": 2, "a": None}),),
        storage_root=tmp_path,
    )
    target, _ = canonical_row_bytes(
        spec,
        columns,
        ('{"a": null, "b": 2.0}',),
        storage_root=tmp_path,
    )
    assert source == target


def test_timestamp_normalization_compares_absolute_instants(tmp_path):
    spec = _spec("app_settings")
    columns = (PostgresColumn("updated_at", "timestamp with time zone", False),)
    utc = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=timezone.utc)
    offset = utc.astimezone(timezone(timedelta(hours=8)))
    left, _ = canonical_row_bytes(spec, columns, (utc,), storage_root=tmp_path)
    right, _ = canonical_row_bytes(spec, columns, (offset,), storage_root=tmp_path)
    assert left == right


def test_boolean_text_and_bytea_remain_distinct_and_bytea_is_hash_only(tmp_path):
    spec = _spec("knowledge_embeddings")
    payload = struct.pack("<3f", 1.0, 2.0, 3.0)
    columns = (
        PostgresColumn("enabled", "boolean", False),
        PostgresColumn("label", "text", False),
        PostgresColumn("vector", "bytea", False),
    )
    encoded, _ = canonical_row_bytes(
        spec,
        columns,
        (True, "true", payload),
        storage_root=tmp_path,
    )
    text = encoded.decode()
    assert '["bool",true]' in text
    assert '["text","true"]' in text
    assert payload.hex() not in text
    assert str(len(payload)) in text


def test_paths_are_relative_to_storage_root_and_escape_is_redacted(tmp_path):
    root = tmp_path / "storage"
    root.mkdir()
    nested = root / "notebooks" / "n" / "source.md"
    nested.parent.mkdir(parents=True)
    nested.write_text("safe", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    spec = _spec("sources")
    columns = (PostgresColumn("file_path", "text", False),)

    relative, relative_error = canonical_row_bytes(
        spec, columns, ("notebooks/n/source.md",), storage_root=root
    )
    absolute, absolute_error = canonical_row_bytes(
        spec, columns, (str(nested),), storage_root=root
    )
    escaped, escaped_error = canonical_row_bytes(
        spec, columns, (str(outside),), storage_root=root
    )

    assert relative == absolute
    assert not relative_error and not absolute_error
    assert escaped_error
    assert str(outside) not in escaped.decode()
    assert "path-invalid" in escaped.decode()


def test_vector_summary_dimension_norm_hash_and_cosine_tolerance():
    left = struct.pack("<4f", 1.0, 2.0, 3.0, 4.0)
    right = struct.pack("<4f", 1.0, 2.0, 3.0, 4.000001)
    summary = vector_summary(left)
    assert summary.valid
    assert (summary.length, summary.dimension) == (16, 4)
    assert summary.norm == pytest.approx(math.sqrt(30.0))
    assert len(summary.sha256) == 64
    assert vector_cosine(left, right) > 0.99999


@pytest.mark.parametrize(
    "payload",
    [b"abc", struct.pack("<f", math.nan), struct.pack("<f", math.inf)],
)
def test_vector_summary_rejects_bad_length_and_non_finite(payload):
    assert not vector_summary(payload).valid


def test_vector_cosine_rejects_dimension_mismatch_and_non_finite():
    with pytest.raises(ValueError, match="dimensions"):
        vector_cosine(struct.pack("<f", 1.0), struct.pack("<2f", 1.0, 2.0))
    with pytest.raises(ValueError, match="non-finite"):
        vector_cosine(struct.pack("<f", math.nan), struct.pack("<f", 1.0))


def test_empty_table_has_complete_verification_coverage():
    table = TableVerification(
        "empty",
        0,
        0,
        0,
        0,
        0,
        0,
        hashlib.sha256().hexdigest(),
        hashlib.sha256().hexdigest(),
    )
    assert table.coverage == 1.0
