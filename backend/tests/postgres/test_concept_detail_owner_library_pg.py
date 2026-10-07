"""PostgreSQL twin of ``tests/test_concept_detail_owner_library.py``'s
mixed-pointer case: an evidence item naming this library's source but an
element of another library never carries that element's id in the concept
detail's raw ``members[].evidence`` / ``attached[].evidence`` lists."""
from __future__ import annotations

import pytest

from tests.postgres import test_kg_viewer_scope_pg as kg_viewer_scope_pg
from tests.postgres.test_kg_viewer_scope_pg import repo  # noqa: F401  (fixture)
from tests.test_concept_detail_owner_library import (
    check_mixed_pointer_never_reaches_the_raw_lists,
)

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_kg_viewer_scope"),
]

_PG_EVIDENCE_SQL = (
    "SELECT evidence FROM knowledge_objects WHERE id=%s",
    "UPDATE knowledge_objects SET evidence=%s::jsonb WHERE id=%s",
)


@pytest.mark.parametrize("b_memory", [True, False])
def test_pg_mixed_pointer_never_reaches_the_raw_lists(repo, b_memory):  # noqa: F811
    check_mixed_pointer_never_reaches_the_raw_lists(
        repo, kg_viewer_scope_pg, b_memory, update_evidence_sql=_PG_EVIDENCE_SQL,
    )
