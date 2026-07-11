"""Scale-index SQL belongs to IndexProjectionStore, not its orchestrator."""
from __future__ import annotations

from contextlib import contextmanager

from app.repositories.sqlite.index_projection_store import IndexProjectionStore
from app.services.scale_index_builder import ScaleIndexBuilder


class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _RecordingConnection:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def execute(self, sql, params):
        self.calls.append((sql, params))
        return _Cursor(self.rows)


def _projection_store():
    return IndexProjectionStore(
        object(),
        connect=lambda: None,
        in_batches=lambda values: [values],
        ent_chunk_map=lambda *args: {},
        mention_extra_edges=lambda *args: [],
        vector_matrix=lambda *args, **kwargs: ([], []),
    )


def test_relation_ids_for_source_batch_preserves_query_params_and_projection():
    rows = [{"id": "rel-2"}, {"id": "rel-1"}]
    db = _RecordingConnection(rows)

    result = _projection_store().relation_ids_for_source_batch(
        db, "nb-1", ["source-a", "source-b"]
    )

    assert result == ["rel-2", "rel-1"]
    assert db.calls == [
        (
            "SELECT id FROM knowledge_relations "
            "WHERE notebook_id=? AND source_id IN (?,?)",
            ("nb-1", "source-a", "source-b"),
        )
    ]


def test_active_object_graph_rows_preserves_query_params_and_projection():
    rows = [
        {"id": "o2", "object_type": "claim", "name": None},
        {"id": "o1", "object_type": "concept", "name": "MOSFET"},
    ]
    db = _RecordingConnection(rows)

    result = _projection_store().active_object_graph_rows(db, "nb-1")

    assert result is rows
    assert db.calls == [
        (
            "SELECT id, object_type, json_extract(payload,'$.name') AS name "
            "FROM knowledge_objects "
            "WHERE notebook_id=? AND status!='deprecated'",
            ("nb-1",),
        )
    ]


class _DelegatingProjections:
    def __init__(self):
        self.db = object()
        self.calls = []
        self.batches = []
        self.object_rows = [
            {"id": "o1", "object_type": "concept", "name": "MOSFET"},
            {"id": "o2", "object_type": "claim", "name": None},
        ]

    @contextmanager
    def connect(self):
        yield self.db

    def in_batches(self, values):
        assert list(values) == ["s1", "s2", "s3"]
        yield ["s1", "s2"]
        yield ["s3"]

    def relation_ids_for_source_batch(self, db, notebook_id, source_ids):
        self.calls.append(("relations", db, notebook_id, list(source_ids)))
        self.batches.append(list(source_ids))
        return [f"rel-{source_ids[-1]}"]

    def active_object_graph_rows(self, db, notebook_id):
        self.calls.append(("objects", db, notebook_id))
        return self.object_rows


def _builder(projections):
    return ScaleIndexBuilder(
        settings=object(),
        projections=projections,
        artifacts=object(),
        event_log=object(),
        get_notebook=lambda notebook_id: notebook_id,
        version=lambda notebook_id: [],
        load_scale=lambda notebook_id: None,
        full_viz_graph=lambda notebook_id: {},
        relations_for_notebook=lambda notebook_id: [],
        cluster_map=lambda notebook_id: {},
        incremental_fuse_source=lambda notebook_id, source_id: None,
        invalidate_scale_cache=lambda notebook_id: None,
        cache_viz=lambda notebook_id, value: None,
        building=set(),
        building_lock=object(),
        notify_index_done=lambda notebook_id: None,
        now=lambda: "now",
    )


def test_delta_relation_ids_delegate_each_batch_on_one_connection():
    projections = _DelegatingProjections()

    result = _builder(projections)._delta_relation_ids("nb-1", ["s1", "s2", "s3"])

    assert result == ["rel-s2", "rel-s3"]
    assert projections.calls == [
        ("relations", projections.db, "nb-1", ["s1", "s2"]),
        ("relations", projections.db, "nb-1", ["s3"]),
    ]


def test_lite_graph_delegates_object_projection_on_builder_connection():
    projections = _DelegatingProjections()

    result = _builder(projections)._derive_object_graph_lite("nb-1")

    assert projections.calls == [("objects", projections.db, "nb-1")]
    assert result["nodes"] == [
        {"id": "o1", "object_type": "concept", "payload": {"name": "MOSFET"}},
        {"id": "o2", "object_type": "claim", "payload": {"name": ""}},
    ]
