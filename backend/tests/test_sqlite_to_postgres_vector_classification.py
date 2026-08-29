import pytest
import numpy as np

from app.migration.sqlite_to_postgres import (
    SqliteToPostgresMigrationError,
    _PostgresColumn,
    _transform_sqlite_value,
)
from app.services.vector_index import decode_vector, encode_vector


def test_chunk_question_vector_is_classified_for_snapshot_import():
    column = _PostgresColumn(
        name="vector",
        data_type="bytea",
        udt_name="bytea",
        nullable=False,
        identity=False,
    )
    expected = np.asarray([0.25, -0.5, 0.75], dtype=np.float32)

    transformed = _transform_sqlite_value(
        table="chunk_questions",
        column=column,
        value=encode_vector(expected),
    )

    assert isinstance(transformed, bytes)
    np.testing.assert_array_equal(decode_vector(transformed), expected)


# extension_runtime_toggles.enabled (SQLite v63 / PostgreSQL v41) is the
# FIRST business-table column ever classified as PostgreSQL `boolean` (see
# 0041_extension_runtime_toggles.sql's header for why this table breaks the
# usual "SQLite INTEGER flag -> PostgreSQL bigint" convention). The `bool`
# branch of ``_transform_sqlite_value`` therefore went from dead code (no
# `udt_name == "bool"` column existed anywhere) to a real, business-table-
# selected path in this same change, and had no direct test of its own.
@pytest.mark.parametrize(("raw", "expected"), [(0, False), (1, True), (False, False), (True, True)])
def test_extension_toggle_enabled_boolean_accepts_sqlite_zero_one_flags(raw, expected):
    column = _PostgresColumn(
        name="enabled",
        data_type="boolean",
        udt_name="bool",
        nullable=False,
        identity=False,
    )

    transformed = _transform_sqlite_value(
        table="extension_runtime_toggles",
        column=column,
        value=raw,
    )

    assert transformed is expected


def test_extension_toggle_enabled_boolean_rejects_a_value_outside_zero_or_one():
    """SQLite's twin `enabled INTEGER` column carries no
    `CHECK (enabled IN (0,1))` (this repository does not add one to any of
    its other INTEGER flag columns either), so a stray value like 2 can
    reach this column. It must hard-fail here rather than being silently
    coerced to `True`/`False` by Python's truthiness."""
    column = _PostgresColumn(
        name="enabled",
        data_type="boolean",
        udt_name="bool",
        nullable=False,
        identity=False,
    )

    with pytest.raises(SqliteToPostgresMigrationError, match="invalid boolean value"):
        _transform_sqlite_value(
            table="extension_runtime_toggles",
            column=column,
            value=2,
        )
