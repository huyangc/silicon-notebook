"""E4-4: the notebook-bound fragments and the ``viewer_id`` rule of
``memory_sql`` — same world and matrix as ``test_memory_sql_contract.py``.

* ``foreign_memory_in_notebook_excluded`` / ``memory_derived_in_notebook``
  decide row by row exactly like their unbound twins (rows and sources share
  a notebook — the write invariant — so the extra notebook binding only
  changes the plan, never the answer);
* ``memory_viewer_filter`` gives the three meanings of a reader's
  ``viewer_id``: ``None`` nothing, ``""`` every Memory-derived row out, a user
  id other members' rows out;
* ``own_memory_source`` selects exactly the viewer's own Memory sources;
* both backends render the same text (``%s`` vs ``?``).
"""
import pytest

from app.repositories.postgres import memory_sql as pg_memory_sql
from app.repositories.sqlite import memory_sql
from tests import memory_sql_cases as cases
from tests.test_memory_sql_contract import world  # noqa: F401  (fixture)

_VIEWERS = ("u-alice", "u-bob", "u-carol")


def _ids(database, sql: str, params: tuple = ()) -> set[str]:
    with database.connect() as db:
        return {row[0] for row in db.execute(sql, params).fetchall()}


def test_parameter_counts_and_alias_refusal():
    for alias in ("o", "ko", "x1"):
        assert memory_sql.foreign_memory_in_notebook_excluded(alias).count("?") == 1
        assert memory_sql.memory_derived_in_notebook(alias).count("?") == 0
    for alias in ("s", "src"):
        assert memory_sql.own_memory_source(alias).count("?") == 1
    for bad in cases.BAD_ALIASES["foreign"]:
        with pytest.raises(ValueError):
            memory_sql.foreign_memory_in_notebook_excluded(bad)
    for bad in cases.BAD_ALIASES["derived"]:
        with pytest.raises(ValueError):
            memory_sql.memory_derived_in_notebook(bad)
    for bad in cases.BAD_ALIASES["readable"]:
        with pytest.raises(ValueError):
            memory_sql.own_memory_source(bad)
    assert memory_sql.memory_viewer_filter("o", None) == ("", ())
    text, params = memory_sql.memory_viewer_filter("o", "")
    assert params == () and text.count("?") == 0 and text.startswith(" AND NOT EXISTS")
    text, params = memory_sql.memory_viewer_filter("o", "u-alice")
    assert params == ("u-alice",) and text.count("?") == 1


def test_both_backends_render_the_same_text():
    for alias in ("x1", "Outer_2"):
        for name in (
            "foreign_memory_in_notebook_excluded", "memory_derived_in_notebook",
            "own_memory_source",
        ):
            pg_text = getattr(pg_memory_sql, name)(alias)
            assert "?" not in pg_text
            assert pg_text.replace("%s", "?") == getattr(memory_sql, name)(alias), name
        for viewer in (None, "", "u-alice"):
            pg_text, pg_params = pg_memory_sql.memory_viewer_filter(alias, viewer)
            text, params = memory_sql.memory_viewer_filter(alias, viewer)
            assert pg_text.replace("%s", "?") == text and pg_params == params


@pytest.mark.parametrize("viewer", _VIEWERS)
def test_notebook_bound_foreign_exclusion_matches_the_matrix(world, viewer):  # noqa: F811
    objects = _ids(
        world,
        "SELECT o.id FROM knowledge_objects o "
        f"WHERE {memory_sql.foreign_memory_in_notebook_excluded('o')}",
        (viewer,),
    )
    relations = _ids(
        world,
        "SELECT r.id FROM knowledge_relations r "
        f"WHERE {memory_sql.foreign_memory_in_notebook_excluded('r')}",
        (viewer,),
    )
    assert objects == cases.FOREIGN_EXCLUDED_KEEPS_OBJECTS[viewer]
    assert relations == cases.FOREIGN_EXCLUDED_KEEPS_RELATIONS[viewer]


def test_notebook_bound_classifier_matches_the_matrix(world):  # noqa: F811
    derived = _ids(
        world,
        "SELECT o.id FROM knowledge_objects o "
        f"WHERE {memory_sql.memory_derived_in_notebook('o')}",
    )
    assert derived == cases.MEMORY_DERIVED_OBJECTS


@pytest.mark.parametrize("viewer", (None, "", *_VIEWERS))
def test_viewer_filter_gives_the_three_meanings(world, viewer):  # noqa: F811
    text, params = memory_sql.memory_viewer_filter("o", viewer)
    kept = _ids(world, f"SELECT o.id FROM knowledge_objects o WHERE 1=1{text}", params)
    if viewer is None:
        assert kept == cases.ALL_OBJECT_IDS
    elif viewer == "":
        assert kept == cases.ALL_OBJECT_IDS - cases.MEMORY_DERIVED_OBJECTS
    else:
        assert kept == cases.FOREIGN_EXCLUDED_KEEPS_OBJECTS[viewer]


@pytest.mark.parametrize("viewer, own", [
    ("u-alice", {"src-mem-alice"}), ("u-bob", {"src-mem-bob"}),
    ("u-carol", set()), ("", set()),
])
def test_own_memory_source_is_exactly_the_viewers(world, viewer, own):  # noqa: F811
    assert _ids(
        world, f"SELECT s.id FROM sources s WHERE {memory_sql.own_memory_source('s')}",
        (viewer,),
    ) == own
