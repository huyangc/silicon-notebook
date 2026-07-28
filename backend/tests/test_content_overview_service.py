from app.services.knowhow.api import cell_content_hash
from app.services.content_overview import ContentOverviewService


class FakeMemoryStore:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def notebook_content_overview(self, user_id, notebook_id, limit=3):
        self.calls.append((user_id, notebook_id, limit))
        return self.result


class FakeKnowhowStore:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def knowhow_table_health_inputs(self, notebook_id):
        self.calls.append(notebook_id)
        return self.rows


def test_content_overview_uses_viewer_memory_and_canonical_code_freshness():
    memory = FakeMemoryStore({
        "total": 4,
        "confirmed": 2,
        "candidate": 1,
        "recent": [{
            "id": "m1",
            "title": "Stable memory",
            "status": "confirmed",
            "updated_at": "2026-07-20T08:00:00+00:00",
        }],
    })
    knowhow = FakeKnowhowStore([{
        "id": "t1",
        "notebook_id": "nb1",
        "title": "Bring-up",
        "description": "",
        "created_at": "2026-07-19T00:00:00+00:00",
        "updated_at": "2026-07-19T01:00:00+00:00",
        "row_count": 2,
        "projection_pending": 1,
        "projection_failed": 1,
        "row_activity_at": "2026-07-20T06:00:00+00:00",
        "cell_activity_at": "2026-07-20T07:00:00+00:00",
        "code_inputs": [
            {
                "saved_hash": cell_content_hash("unchanged"),
                "current_content_md": "unchanged",
                "updated_at": "2026-07-20T05:00:00+00:00",
            },
            {
                "saved_hash": cell_content_hash("old"),
                "current_content_md": "![plot](asset://img)\nnew",
                "updated_at": "2026-07-20T09:00:00+00:00",
            },
            {
                "saved_hash": cell_content_hash("![plot](asset://img)\nnew"),
                "current_content_md": "![plot](asset://img)\nnew",
                "updated_at": "2026-07-20T08:00:00+00:00",
            },
        ],
    }])

    result = ContentOverviewService(memory, knowhow).get("nb1", "viewer1")

    assert memory.calls == [("viewer1", "nb1", 3)]
    assert knowhow.calls == ["nb1"]
    assert result.memory.total == 4
    assert result.memory.confirmed == 2
    assert result.memory.candidate == 1
    assert [item.id for item in result.memory.recent] == ["m1"]
    assert result.knowhow.table_count == 1
    assert result.knowhow.row_count == 2
    assert result.knowhow.projection_pending == 1
    assert result.knowhow.projection_failed == 1
    assert result.knowhow.stale_code_count == 1
    assert result.knowhow.recent_tables[0].last_activity_at == "2026-07-20T09:00:00+00:00"


def test_content_overview_limits_recent_tables_and_returns_typed_empty_sections():
    rows = [{
        "id": f"t{index}",
        "notebook_id": "nb1",
        "title": f"T{index}",
        "description": "",
        "created_at": f"2026-07-{10 + index:02d}T00:00:00+00:00",
        "updated_at": f"2026-07-{10 + index:02d}T00:00:00+00:00",
        "row_count": index,
        "projection_pending": 0,
        "projection_failed": 0,
        "row_activity_at": "",
        "cell_activity_at": "",
        "code_inputs": [],
    } for index in range(5)]
    empty_memory = FakeMemoryStore({
        "total": 0,
        "confirmed": 0,
        "candidate": 0,
        "recent": [],
    })

    populated = ContentOverviewService(empty_memory, FakeKnowhowStore(rows)).get("nb1", "u1")
    empty = ContentOverviewService(empty_memory, FakeKnowhowStore([])).get("nb1", "u1")

    assert [table.id for table in populated.knowhow.recent_tables] == ["t4", "t3", "t2"]
    assert empty.memory.recent == []
    assert empty.knowhow.table_count == 0
    assert empty.knowhow.recent_tables == []
