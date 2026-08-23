"""Collect-only pytest plugin used by the G1/G2 lane partition self-guard.

``test_verification_lane_markers_partition_every_architecture_contract_test``
in ``test_test_architecture_policy.py`` needs, for a fixed set of candidate
files, both the full universe of collected test ids *and* each item's own
marker names, so it can evaluate the real G1/G2 ``-m`` expressions in-process
and prove the split has no gap or overlap. Loading this plugin with
``--collect-only`` avoids running one ``--collect-only`` subprocess per ``-m``
expression (previously 3: unfiltered, G1-filtered, G2-filtered) — one
unfiltered collection run captures everything needed, and the marker
expressions are evaluated afterward with pytest's own
``_pytest.mark.expression`` compiler against each item's marker set.

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
            "markers": sorted({marker.name for marker in item.iter_markers()}),
        }
        for item in session.items
    ]
    print(START_SENTINEL)
    print(json.dumps(items))
    print(END_SENTINEL)
