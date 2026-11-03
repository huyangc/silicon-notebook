"""Real-scale owner projection equivalence and indexed pulse gating."""
import re

import pytest

from app.repositories.postgres import access_sql
from tests.postgres.test_access_level_explain_pins import _CALLERS
from tests.postgres.test_access_level_explain_pins import pg_access_world as pg_access_world
from tests.postgres.test_mount_sql_viewer_pg import _plan

pytestmark = pytest.mark.postgres_integration


def _shape(plan):
    return {key: plan[key] for key in ("Node Type", "Relation Name", "Index Name", "Join Type", "Scan Direction")
            if key in plan} | {"Plans": [_shape(child) for child in plan.get("Plans", [])]}


def test_owner_if_and_pulse_wanted_plans(pg_access_world):
    database, scale = pg_access_world
    with database.connect() as db:
        for label, caller in _CALLERS:
            for boolean, owner, params in (
                (access_sql.NOTEBOOK_EDIT_SQL, access_sql.NOTEBOOK_EDIT_OWNER_SQL, access_sql.edit_access_params(caller)),
                (access_sql.NOTEBOOK_ADMIN_SQL, access_sql.NOTEBOOK_ADMIN_OWNER_SQL, access_sql.admin_access_params(caller)),
            ):
                plans = [db.execute("EXPLAIN (FORMAT JSON) " + query, ("nbP0", *params)).fetchone()["QUERY PLAN"][0]["Plan"]
                         for query in (boolean, owner)]
                assert _shape(plans[0]) == _shape(plans[1]), (scale, label)
            params = access_sql.collab_pulse_wanted_params("nbP0", caller)
            for mode in ("custom", "generic"):
                plan = _plan(db, access_sql.NOTEBOOK_COLLAB_PULSE_WANTED_SQL, params, mode=mode)
                assert not re.search(r"Seq Scan on (notebook_grants|notebook_members|group_members)\b", plan), (scale, label, plan)
            plan = _plan(db, access_sql.NOTEBOOK_COLLAB_PULSE_WANTED_SQL, params, seqscan=False)
            assert not re.search(r"Seq Scan on (notebook_grants|notebook_members|group_members)\b", plan), (scale, label, plan)
