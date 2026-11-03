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

import pytest

START_SENTINEL = "LANE_PARTITION_JSON_START"
END_SENTINEL = "LANE_PARTITION_JSON_END"
_COLLECTED_MODULES = {}


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_collection_modifyitems(items):
    global _COLLECTED_MODULES

    # Marker/shard deselection happens after parametrization. Even a module
    # absent from the selected shard has generated ids that must be discarded.
    _COLLECTED_MODULES = {
        module.obj.__name__: module.obj
        for item in items
        if (module := item.getparent(pytest.Module)) is not None
    }
    yield


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


def _collect_commands(commands):
    """Keep independent pytest sessions, sharing imported application libraries.

    This child process never executes a test. Reusing its interpreter avoids
    importing the application six times, while each command still
    reparses options and runs pytest's real collector, marker selection and
    lane plugin. Test modules must be reimported: pytest caches generated ids
    back into their parametrization marks, and reusing those marks escapes
    non-ASCII ids a second time. Release each completed session's module
    bindings and frozen collection before starting the next one; no test
    fixtures, parametrization state or mutable test worlds are reused.
    """
    import gc
    import io
    import os
    import sys
    from contextlib import redirect_stderr, redirect_stdout

    collections = []
    for job in commands:
        command = job["command"]
        if command[:3] != [sys.executable, "-m", "pytest"] or "--collect-only" not in command:
            raise ValueError("partition batch accepts only collect-only pytest commands")
        output = io.StringIO()
        os.chdir(job["cwd"])
        with redirect_stdout(output), redirect_stderr(output):
            status = pytest.main(command[3:])
        transcript = output.getvalue()
        if status != 0:
            raise AssertionError(f"collect-only subprocess failed (exit {status}):\n{transcript[-4000:]}")
        if START_SENTINEL not in transcript or END_SENTINEL not in transcript:
            raise AssertionError("lane_partition_plugin dump not found in collect-only output")
        dump = transcript.split(START_SENTINEL, 1)[1].split(END_SENTINEL, 1)[0]
        collections.append(json.loads(dump))
        plugin = sys.modules["tests.architecture.lane_partition_plugin"]
        modules, plugin._COLLECTED_MODULES = plugin._COLLECTED_MODULES, {}
        for name, module in modules.items():
            parent_name, _, attribute = name.rpartition(".")
            parent = sys.modules.get(parent_name)
            if parent is not None and getattr(parent, attribute, None) is module:
                delattr(parent, attribute)
            if sys.modules.get(name) is module:
                del sys.modules[name]
        modules.clear()
        # conftest freezes resident objects once collection finishes. They
        # should survive one normal test session, not all six proof sessions.
        gc.unfreeze()
        gc.collect()
    return collections


if __name__ == "__main__":
    import sys

    print(START_SENTINEL)
    print(json.dumps(_collect_commands(json.load(sys.stdin))))
    print(END_SENTINEL)
