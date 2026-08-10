import numpy as np

from app.migration.sqlite_to_postgres import (
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
