from __future__ import annotations

import pytest

from tests.test_notebook_delete_grace import http as http


@pytest.mark.parametrize("action", ["knowhow", "knowhow_import", "mount"])
def test_delete_winning_after_route_preflight_returns_404(http, monkeypatch, action):
    from app.api import knowhow_routes

    def gone(*args, **kwargs):
        raise KeyError(http["mine"])

    client, headers = http["client"], http["member"]
    path = f"/api/notebooks/{http['mine']}"
    if action == "knowhow":
        monkeypatch.setattr(knowhow_routes.knowhow_api, "create_table", gone)
        result = client.post(f"{path}/knowhow", json={"title": "Title", "columns": []}, headers=headers)
    elif action == "knowhow_import":
        monkeypatch.setattr(knowhow_routes.knowhow_api, "import_table", gone)
        result = client.post(
            f"{path}/knowhow/import", headers=headers,
            data={"title": "Title", "columns_json": "[]"},
            files={"file": ("table.csv", b"A\nValue", "text/csv")},
        )
    else:
        monkeypatch.setattr(http["repo"]._runtime.notebook_store, "replace_mounts", gone)
        result = client.put(f"{path}/bases", json={"base_notebook_ids": []}, headers=headers)
    assert result.status_code == 404
