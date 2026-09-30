"""HTTP contract of the member's own exit and the Memory export (E5-2, M5).

* ``GET  /api/notebooks/{id}/membership/exit-disclosure`` -> ``{"memory_count": N}``
* ``DELETE /api/notebooks/{id}/membership[?acknowledged_memory_count=A]``,
  contract v2 (C = what the exit deletes now, claimed under the lock):
  C = 0 and A absent or 0 -> 204; A != C (also C = 0 with A > 0) -> 409
  ``exit_disclosure_required`` with C; A = C > 0 -> 200
  ``{"deleted_memory_count": d}``; memories saved during the exit -> 409
  ``exit_incomplete`` with d and r; a failed purge -> 503 ``exit_incomplete``.
* ``GET  /api/notebooks/{id}/memories/export`` -> Markdown attachment.
* ``POST /api/memories/transfer`` stays the other way out (confirmed only).
"""
from __future__ import annotations

from urllib.parse import quote

from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'memory-exit.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.main import create_app

    return TestClient(create_app())


def _register(client: TestClient, username: str) -> tuple[dict, str]:
    response = client.post(
        "/api/auth/register", json={"username": username, "password": "pw"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return {"Authorization": f"Bearer {body['token']}"}, body["user"]["id"]


def _world(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_headers, owner_id = _register(client, "e00100101")
    reader_headers, reader_id = _register(client, "r00100102")
    stranger_headers, _ = _register(client, "s00100103")
    notebook_id = client.post(
        "/api/notebooks", headers=owner_headers, json={"name": "共享 电路/手册"}
    ).json()["id"]
    own_notebook = client.post(
        "/api/notebooks", headers=reader_headers, json={"name": "Reader own"}
    ).json()["id"]
    from app.api.deps import repository

    repo = repository()
    repo.add_member(notebook_id, reader_id)
    service = repo._runtime.memory_service
    confirmed = service.confirm(
        service.create_candidate(
            notebook_id, reader_id, None, "exit-confirmed", "Reader confirmed",
            "Reader confirmed body.", ["loop"], "reason", {}, [],
        ).id,
        reader_id,
        {"extract_kg": False},
    )
    candidate = service.create_candidate(
        notebook_id, reader_id, None, "exit-candidate", "Reader candidate",
        "Reader candidate body.", [], "reason", {}, [],
    )
    service.confirm(
        service.create_candidate(
            notebook_id, owner_id, None, "owner-memory", "Owner private",
            "Owner private body.", [], "reason", {}, [],
        ).id,
        owner_id,
        {"extract_kg": False},
    )
    return {
        "client": client, "repo": repo, "notebook": notebook_id,
        "own_notebook": own_notebook, "owner": owner_headers, "owner_id": owner_id,
        "reader": reader_headers, "reader_id": reader_id,
        "stranger": stranger_headers, "confirmed": confirmed.id,
        "candidate": candidate.id,
    }


def test_exit_disclosure_counts_the_callers_own_memory_of_every_status(
    tmp_path, monkeypatch
):
    w = _world(tmp_path, monkeypatch)
    client, notebook = w["client"], w["notebook"]
    url = f"/api/notebooks/{notebook}/membership/exit-disclosure"
    response = client.get(url, headers=w["reader"])
    assert response.status_code == 200
    assert response.json() == {"memory_count": 2}
    # The owner keeps the notebook: leaving deletes nothing of theirs.
    assert client.get(url, headers=w["owner"]).json() == {"memory_count": 0}
    # Read-gated like the notebook itself.
    assert client.get(url, headers=w["stranger"]).status_code == 404


def test_exit_needs_the_exact_acknowledgement_then_deletes_and_leaves(
    tmp_path, monkeypatch
):
    w = _world(tmp_path, monkeypatch)
    client, notebook = w["client"], w["notebook"]
    url = f"/api/notebooks/{notebook}/membership"
    expected = {"detail": {"code": "exit_disclosure_required", "memory_count": 2}}
    for query in ("", "?acknowledged_memory_count=1", "?acknowledged_memory_count=3"):
        refused = client.delete(url + query, headers=w["reader"])
        assert refused.status_code == 409
        assert refused.json() == expected
    assert client.get(f"/api/notebooks/{notebook}", headers=w["reader"]).status_code == 200
    left = client.delete(url + "?acknowledged_memory_count=2", headers=w["reader"])
    assert left.status_code == 200
    assert left.json() == {"deleted_memory_count": 2}
    assert client.get(f"/api/notebooks/{notebook}", headers=w["reader"]).status_code == 404
    with w["repo"]._connect() as db:
        remaining = db.execute(
            "SELECT COUNT(*) AS c FROM memory_items WHERE notebook_id=? AND created_by=?",
            (notebook, w["reader_id"]),
        ).fetchone()["c"]
        others = db.execute(
            "SELECT COUNT(*) AS c FROM memory_items WHERE notebook_id=?", (notebook,)
        ).fetchone()["c"]
    assert remaining == 0
    assert others == 1  # the owner's Memory is untouched


def test_exit_without_memory_needs_no_acknowledgement(tmp_path, monkeypatch):
    w = _world(tmp_path, monkeypatch)
    client = w["client"]
    headers, user_id = _register(client, "m00100104")
    w["repo"].add_member(w["notebook"], user_id)
    response = client.delete(f"/api/notebooks/{w['notebook']}/membership", headers=headers)
    assert response.status_code == 204
    assert response.content == b""
    # Idempotent, exactly as before: leaving again is a no-op 204.
    again = client.delete(f"/api/notebooks/{w['notebook']}/membership", headers=headers)
    assert again.status_code == 204


def test_a_failed_purge_reports_what_it_deleted_and_keeps_the_membership(
    tmp_path, monkeypatch
):
    """503 ``exit_incomplete``: the server says how many it deleted before
    failing and how many remain; the caller is still a member."""
    w = _world(tmp_path, monkeypatch)
    store = w["repo"]._runtime.memory_service.store
    original = store.bulk_delete_memories
    calls: list[int] = []

    def fail_on_second_page(user_id, memory_ids):
        calls.append(len(memory_ids))
        if len(calls) > 1:
            raise RuntimeError("injected")
        return original(user_id, memory_ids)

    from app.services import memory_service as memory_service_module

    monkeypatch.setattr(memory_service_module, "_PURGE_PAGE", 1)
    monkeypatch.setattr(store, "bulk_delete_memories", fail_on_second_page)
    response = w["client"].delete(
        f"/api/notebooks/{w['notebook']}/membership?acknowledged_memory_count=2",
        headers=w["reader"],
    )
    assert response.status_code == 503
    assert response.json() == {
        "detail": {"code": "exit_incomplete", "deleted_memory_count": 1, "memory_count": 1}
    }
    assert w["repo"].is_member(w["notebook"], w["reader_id"])
    # The retry starts from the disclosure again, which tells the truth.
    assert w["client"].get(
        f"/api/notebooks/{w['notebook']}/membership/exit-disclosure", headers=w["reader"]
    ).json() == {"memory_count": 1}


def test_a_failed_purge_before_any_delete_says_nothing_was_deleted(
    tmp_path, monkeypatch
):
    w = _world(tmp_path, monkeypatch)
    store = w["repo"]._runtime.memory_service.store

    def failing(*_args, **_kwargs):
        raise RuntimeError("injected")

    monkeypatch.setattr(store, "bulk_delete_memories", failing)
    response = w["client"].delete(
        f"/api/notebooks/{w['notebook']}/membership?acknowledged_memory_count=2",
        headers=w["reader"],
    )
    assert response.status_code == 503
    assert response.json() == {
        "detail": {"code": "exit_incomplete", "deleted_memory_count": 0, "memory_count": 2}
    }
    assert w["repo"].is_member(w["notebook"], w["reader_id"])


def test_memory_saved_during_the_exit_is_an_incomplete_exit_with_both_numbers(
    tmp_path, monkeypatch
):
    """409 ``exit_incomplete``: the acknowledged two are gone, one saved
    meanwhile remains, the caller is still a member and is told both."""
    w = _world(tmp_path, monkeypatch)
    service = w["repo"]._runtime.memory_service
    original = service.store.bulk_delete_memories

    def delete_then_save(user_id, memory_ids):
        deleted = original(user_id, memory_ids)
        service.create_candidate(
            w["notebook"], w["reader_id"], None, "exit-late", "Late",
            "Saved while the exit ran.", [], "reason", {}, [],
        )
        return deleted

    monkeypatch.setattr(service.store, "bulk_delete_memories", delete_then_save)
    response = w["client"].delete(
        f"/api/notebooks/{w['notebook']}/membership?acknowledged_memory_count=2",
        headers=w["reader"],
    )
    assert response.status_code == 409
    assert response.json() == {
        "detail": {"code": "exit_incomplete", "deleted_memory_count": 2, "memory_count": 1}
    }
    assert w["repo"].is_member(w["notebook"], w["reader_id"])


def test_an_acknowledgement_is_refused_when_nothing_would_be_deleted(
    tmp_path, monkeypatch
):
    """Spec P2-1: the caller acknowledged 2, then meanwhile gained a grant
    (or lost the membership): the exit would delete 0, so the
    acknowledgement is refused with 0 and nothing changes."""
    w = _world(tmp_path, monkeypatch)
    client, notebook = w["client"], w["notebook"]
    url = f"/api/notebooks/{notebook}/membership"
    with w["repo"]._runtime.database.write() as db:
        db.execute(
            "INSERT INTO notebook_grants "
            "(id,notebook_id,principal_type,principal_id,role,created_by,created_at) "
            "VALUES ('gnt-exit-route',?,'user',?,'reader',?,'2026-09-29T00:00:00+00:00')",
            (notebook, w["reader_id"], w["owner_id"]),
        )
    refused = client.delete(url + "?acknowledged_memory_count=2", headers=w["reader"])
    assert refused.status_code == 409
    assert refused.json() == {
        "detail": {"code": "exit_disclosure_required", "memory_count": 0}
    }
    assert w["repo"].is_member(notebook, w["reader_id"])
    # With 0 acknowledged (or none) the exit ends the membership and keeps
    # every Memory: the grant still reads them.
    assert client.delete(url, headers=w["reader"]).status_code == 204
    assert not w["repo"].is_member(notebook, w["reader_id"])
    assert client.get(
        f"/api/notebooks/{notebook}/memories", headers=w["reader"]
    ).json()["total_count"] == 2
    # No longer a member at all: a stale acknowledgement is refused the same way.
    stale = client.delete(url + "?acknowledged_memory_count=2", headers=w["reader"])
    assert stale.status_code == 409
    assert stale.json() == {
        "detail": {"code": "exit_disclosure_required", "memory_count": 0}
    }


def test_export_is_a_markdown_attachment_of_only_the_callers_memory(
    tmp_path, monkeypatch
):
    w = _world(tmp_path, monkeypatch)
    client, notebook = w["client"], w["notebook"]
    response = client.get(
        f"/api/notebooks/{notebook}/memories/export", headers=w["reader"]
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/markdown; charset=utf-8"
    disposition = response.headers["content-disposition"]
    day = w["repo"]._runtime.memory_service.now()[:10].replace("-", "")
    # "/" in the title is not a path separator in the saved file name.
    filename = f"共享 电路_手册-记忆-{day}.md"
    assert disposition == (
        f'attachment; filename="memory-{day}.md"; '
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )
    body = response.content.decode("utf-8")
    assert "## 1. Reader confirmed" in body
    assert "## 2. Reader candidate" in body
    assert "- 状态：候选（尚未确认）" in body
    assert "- 标签：loop" in body
    assert "Owner private" not in body
    assert body.rstrip().endswith("共导出 2 条记忆。")
    assert client.get(
        f"/api/notebooks/{notebook}/memories/export", headers=w["stranger"]
    ).status_code == 404


def test_download_file_names_are_readable_across_origins(tmp_path, monkeypatch):
    """A cross-origin frontend can only read ``Content-Disposition`` (the
    export's file name) if CORS exposes it."""
    w = _world(tmp_path, monkeypatch)
    response = w["client"].get(
        f"/api/notebooks/{w['notebook']}/memories/export",
        headers={**w["reader"], "Origin": "http://localhost:3000"},
    )
    assert response.status_code == 200
    exposed = {
        name.strip().lower()
        for name in response.headers["access-control-expose-headers"].split(",")
    }
    assert {"content-disposition", "x-user-message", "x-request-id"} <= exposed


def test_transfer_is_the_other_way_out_for_a_member_and_carries_confirmed_only(
    tmp_path, monkeypatch
):
    w = _world(tmp_path, monkeypatch)
    client = w["client"]
    response = client.post(
        "/api/memories/transfer",
        headers=w["reader"],
        json={
            "memory_ids": [w["confirmed"], w["candidate"]],
            "target_notebook_id": w["own_notebook"],
            "mode": "move",
        },
    )
    assert response.status_code == 200, response.text
    statuses = [item["status"] for item in response.json()["results"]]
    assert statuses == ["moved", "failed"]
    # The candidate stayed, so leaving still has exactly one Memory to delete.
    assert client.get(
        f"/api/notebooks/{w['notebook']}/membership/exit-disclosure",
        headers=w["reader"],
    ).json() == {"memory_count": 1}
    listed = client.get(
        f"/api/notebooks/{w['own_notebook']}/memories", headers=w["reader"]
    ).json()["items"]
    assert [item["title"] for item in listed] == ["Reader confirmed"]


def test_the_exit_runs_to_completion_whether_or_not_the_client_listens():
    """Item 2: the production frontend proxies ``/api`` through Next's
    rewrite, whose proxy gives up after 30 s (Next 15.5 ``proxyTimeout``
    default; ``next.config.mjs`` sets none). The route is a plain ``def``:
    Starlette runs it in the threadpool and a disconnect cannot cancel a
    running thread, so the purge finishes and the next disclosure reports
    what is left (contract v2: the client then reports "unknown" and
    re-reads)."""
    import inspect

    from app.api.notebook_routes import leave_notebook_route

    assert not inspect.iscoroutinefunction(leave_notebook_route)
