"""Collect-only pytest plugin used by the G1/G2/PG partition self-guard.

The guard collects the full non-PostgreSQL test universe and the two actual
G1 shard selections. Native ids and marker names prove complete partitioning;
group names additionally let the guard check xdist's own effective scheduling
keys without depending on the sharding implementation's assignment function.
The PG launcher's full and two sharded commands receive the same proof.
All collections use ``-n0`` so xdist never appends a group suffix to
the ids (a parametrized id may itself contain ``@``).

The dump is wrapped in sentinel lines so the caller can find it inside
``--collect-only -q`` output without confusing it with the terminal
reporter's own collected-item listing.
"""

from __future__ import annotations

import json

START_SENTINEL = "LANE_PARTITION_JSON_START"
END_SENTINEL = "LANE_PARTITION_JSON_END"


def pytest_collection_finish(session) -> None:
    items = [
        {
            "nodeid": item.nodeid,
            "path": str(item.path.relative_to(session.config.invocation_params.dir)),
            "markers": sorted({marker.name for marker in item.iter_markers()}),
            "xdist_groups": sorted({
                str(marker.args[0] if marker.args else marker.kwargs.get("name", "default"))
                for marker in item.iter_markers("xdist_group")
            }),
        }
        for item in session.items
    ]
    print(START_SENTINEL)
    print(json.dumps(items))
    print(END_SENTINEL)
