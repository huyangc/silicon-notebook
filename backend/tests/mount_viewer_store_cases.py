"""M3 store-level matrix: the seven participant-set call sites, one check each.

``test_mount_viewer_store.py`` runs these on SQLite and
``postgres/test_mount_viewer_store_pg.py`` on PostgreSQL, over real tables, with
the world of ``test_mount_sql_contract`` (E6-1) plus a few knowledge-graph
rows.  The E6-1 matrix pins the SQL fragments; this one pins that each store
method really consumes the viewer-scoped fragment and binds ``(viewer, notebook)``
in the right order:

* the owner mounts a private library ``nb-b`` on a shared notebook ``nb-a``:
  effective for the owner, NOT for member ``u-plain``, effective again for a
  member who can read ``nb-b`` on their own (member, user grant, group grant);
* ``tier='base'`` and ``everyone`` libraries are effective for everybody,
  the empty viewer (``""`` / ``None``) included;
* a notebook whose ``created_by`` is NULL: only public / ``everyone`` libraries;
* the empty viewer keeps only public / ``everyone`` libraries.

A check returns the list of mismatches (empty = pass), so one test per call
site goes red when exactly that call site falls back to the old fragment.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable, Sequence

from tests.test_mount_sql_contract import (
    MOUNT_WORLD_VIEWERS,
    OLD_MOUNT_ORDERED,
    expected_mounts_for_viewer,
    seed_mount_world,
)

#: Extra rows on top of the E6-1 world: two mounter notebooks whose mounted
#: library carries a knowledge graph and is open to everybody (a public base /
#: an ``everyone`` grant), so the "is effective for all" arm is observable
#: through the KG-gate call sites.  ``nb-q`` / ``nb-r`` are u-owner's.
EXTRA_NOTEBOOKS = (
    ("nb-q", "u-owner", "personal", "draft"),
    ("nb-pk", "u-carol", "base", "draft"),
    ("nb-r", "u-owner", "personal", "draft"),
    ("nb-ek", "u-carol", "personal", "draft"),
)
EXTRA_BASES = (("nb-q", "nb-pk", "u-owner"), ("nb-r", "nb-ek", "u-owner"))
#: (id, notebook): one usable knowledge object each.  ``nb-b`` / ``nb-x`` hold
#: private graphs; ``nb-pk`` / ``nb-ek`` open ones; ``ko-a`` / ``ko-c`` are the
#: mounting notebooks' own.  Public libs of the E6-1 world (``nb-p``, ``nb-e``)
#: stay graph-less on purpose: the private-library cells depend on it.
KG_OBJECTS = (
    ("ko-a", "nb-a"),
    ("ko-c", "nb-c"),
    ("ko-b", "nb-b"),
    ("ko-x", "nb-x"),
    ("ko-pk", "nb-pk"),
    ("ko-ek", "nb-ek"),
)
KG_LIBRARIES = frozenset(notebook for _id, notebook in KG_OBJECTS)
EXTRA_MOUNTED = {
    "nb-q": ("nb-pk",),
    "nb-r": ("nb-ek",),
}
MOUNTERS = ("nb-a", "nb-c", "nb-n", "nb-q", "nb-r")


def seed_store_world(
    execute: Callable[[str, Sequence[object]], object], placeholder: str, now: object
) -> None:
    seed_mount_world(execute, placeholder, now)

    def run(sql: str, params: Sequence[object]) -> None:
        execute(sql.replace("?", placeholder), params)

    for notebook_id, owner, tier, status in EXTRA_NOTEBOOKS:
        run(
            "INSERT INTO notebooks "
            "(id,name,purpose,primary_domain,status,created_by,created_at,updated_at,tier) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (notebook_id, notebook_id, "", "Semiconductor", status, owner, now, now, tier),
        )
    run(
        "INSERT INTO notebook_grants "
        "(id,notebook_id,principal_type,principal_id,role,created_by,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("gr-ek-everyone", "nb-ek", "everyone", "", "viewer", "u-carol", now),
    )
    for notebook_id, base_id, created_by in EXTRA_BASES:
        run(
            "INSERT INTO notebook_bases "
            "(notebook_id,base_notebook_id,created_at,created_by) VALUES (?,?,?,?)",
            (notebook_id, base_id, now, created_by),
        )
    for object_id, notebook_id in KG_OBJECTS:
        run(
            "INSERT INTO knowledge_objects "
            "(id,notebook_id,object_type,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (object_id, notebook_id, "claim", "approved", now, now),
        )


def effective(notebook_id: str, viewer: str | None) -> frozenset:
    """The libraries mounted on ``notebook_id`` that are effective for ``viewer``."""
    if notebook_id in EXTRA_MOUNTED:
        return frozenset(EXTRA_MOUNTED[notebook_id])
    return expected_mounts_for_viewer(notebook_id, viewer)


def ordered(notebook_id: str, viewer: str | None) -> list[str]:
    """``effective`` in ``MOUNT_ORDER`` (public libraries first, then by name)."""
    keep = effective(notebook_id, viewer)
    if notebook_id in EXTRA_MOUNTED:
        return list(EXTRA_MOUNTED[notebook_id])
    return [base for base in OLD_MOUNT_ORDERED[notebook_id] if base in keep]


def _cells():
    for notebook_id in MOUNTERS:
        for viewer in MOUNT_WORLD_VIEWERS:
            yield notebook_id, viewer, f"{notebook_id} viewer={viewer!r}"


def has_kg(notebook_id: str, viewer: str | None) -> bool:
    return bool(effective(notebook_id, viewer) & KG_LIBRARIES)


# ---------------------------------------------------------------- the checks
# ``stores`` is a SimpleNamespace of callables ``(notebook_id, viewer) -> ...``
# built by each backend's test module (see ``namespace_of`` below for the shape).


def resolve_participants_failures(stores: Any) -> list[str]:
    failures = []
    for notebook_id, viewer, case in _cells():
        pairs = stores.resolve_participants(notebook_id, viewer)
        got = [nid for nid, _tier in pairs]
        want = [notebook_id, *ordered(notebook_id, viewer)]
        if got != want:
            failures.append(f"{case}: {got} != {want}")
        # The three delegating readers agree with it (one definition point).
        ids = stores.participant_ids(notebook_id, viewer)
        if ids != got:
            failures.append(f"{case} participant_ids: {ids} != {got}")
        id_list, tiers = stores.participant_tiers(notebook_id, viewer)
        if id_list != got or dict(pairs) != tiers:
            failures.append(f"{case} participant_tiers: {id_list} {tiers}")
        via_store = stores.participant_notebook_ids(notebook_id, viewer)
        if via_store != got:
            failures.append(f"{case} participant_notebook_ids: {via_store} != {got}")
    return failures


def participant_rows_failures(stores: Any) -> list[str]:
    failures = []
    for notebook_id, viewer, case in _cells():
        active, bases = stores.participant_rows(notebook_id, viewer)
        if active is None or active["id"] != notebook_id:
            failures.append(f"{case}: active row {active}")
        got = [row["id"] for row in bases]
        want = ordered(notebook_id, viewer)
        if got != want:
            failures.append(f"{case}: {got} != {want}")
    return failures


def usable_base_kg_failures(stores: Any) -> list[str]:
    failures = []
    for notebook_id, viewer, case in _cells():
        got = bool(stores.usable_base_kg(notebook_id, viewer))
        if got != has_kg(notebook_id, viewer):
            failures.append(f"{case}: {got} != {has_kg(notebook_id, viewer)}")
    return failures


def mounted_bases_row_failures(stores: Any) -> list[str]:
    failures = []
    for notebook_id, viewer, case in _cells():
        rows = stores.mounted_bases_row(notebook_id, viewer)
        got = [(row["id"], bool(row["has_kg"])) for row in rows]
        want = [
            (base, base in KG_LIBRARIES) for base in ordered(notebook_id, viewer)
        ]
        if got != want:
            failures.append(f"{case}: {got} != {want}")
    return failures


def mounted_base_ids_failures(stores: Any) -> list[str]:
    failures = []
    for notebook_id, viewer, case in _cells():
        got = stores.mounted_base_ids(notebook_id, viewer)
        want = ordered(notebook_id, viewer)
        if got != want:
            failures.append(f"{case}: {got} != {want}")
    return failures


def any_mounted_has_kg_failures(stores: Any) -> list[str]:
    failures = []
    for notebook_id, viewer, case in _cells():
        want = has_kg(notebook_id, viewer)
        for name in ("any_mounted_has_kg_on", "any_mounted_has_kg",
                     "any_mounted_has_kg_compat"):
            got = bool(getattr(stores, name)(notebook_id, viewer))
            if got != want:
                failures.append(f"{case} {name}: {got} != {want}")
    return failures


#: (object, owning library, the notebook that asks): ``follow_start_row`` lets a
#: walk start from the asking notebook's own objects or from an effective mount.
FOLLOW_CASES = (
    ("ko-a", "nb-a", "nb-a"),
    ("ko-b", "nb-b", "nb-a"),
    ("ko-b", "nb-b", "nb-c"),
    ("ko-x", "nb-x", "nb-c"),
    ("ko-c", "nb-c", "nb-c"),
    ("ko-pk", "nb-pk", "nb-q"),
    ("ko-ek", "nb-ek", "nb-r"),
    # Mounted on neither: never a legal start, whoever asks.
    ("ko-x", "nb-x", "nb-a"),
)


def follow_start_row_failures(stores: Any) -> list[str]:
    failures = []
    for object_id, library, asking in FOLLOW_CASES:
        for viewer in MOUNT_WORLD_VIEWERS:
            row = stores.follow_start(object_id, asking, viewer)
            got = row is not None
            want = library == asking or library in effective(asking, viewer)
            if got != want:
                failures.append(
                    f"{object_id} from {asking} viewer={viewer!r}: {got} != {want}"
                )
            if row is not None and row["notebook_id"] != library:
                failures.append(f"{object_id} from {asking}: row of {row['notebook_id']}")
    return failures


SITE_CHECKS = {
    "resolve_participants": resolve_participants_failures,
    "participant_rows": participant_rows_failures,
    "notebook_has_usable_base_kg": usable_base_kg_failures,
    "mounted_bases_row": mounted_bases_row_failures,
    "mounted_base_ids": mounted_base_ids_failures,
    "any_mounted_has_kg_on": any_mounted_has_kg_failures,
    "follow_start_row": follow_start_row_failures,
}


def required_keyword_failures(raw: SimpleNamespace) -> list[str]:
    """Every call site refuses a call without ``viewer_id`` (TypeError, never a
    silent fall back to the viewer-independent set).  ``raw`` holds callables
    ``(notebook_id) -> None`` that call the store method WITHOUT the keyword."""
    failures = []
    for name, call in vars(raw).items():
        try:
            call("nb-a")
        except TypeError:
            continue
        failures.append(f"{name} accepted a call without viewer_id")
    return failures
