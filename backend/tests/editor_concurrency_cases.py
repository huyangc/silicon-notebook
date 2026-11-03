"""Two people writing one notebook at the same time, over HTTP, shared by both
backends (share-edit P1-T10, spec §5).

``test_editor_concurrency.py`` runs every case on SQLite and
``postgres/test_editor_concurrency_pg.py`` runs the same cases on PostgreSQL.
One world: the owner's notebook with a named editor who has a display name
(张三) and an owner without one (so the fallback to the username shows), one
uploaded source, and one pending conflict-review candidate.

* D8: a source being parsed answers a second single re-parse with 409 and is
  skipped (``skipped_busy``) by the batch re-parse;
* the knowledge-graph 409 names the initiator when it is someone else -- the
  durable analysis job's ``created_by``, or the maintenance slot's claimer;
* re-parse and delete are two independent writers: deleting is not held up by
  a parse in flight and the parse then answers 404;
* the document-limit refusal speaks of the notebook's limit (the owner's,
  which an editor's upload counts against);
* a conflict candidate already decided answers the second reviewer 409.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient

_STAMP = "2026-10-29T00:00:00+00:00"


@dataclass
class World:
    client: TestClient
    postgres: bool
    notebook: str
    source: str
    headers: dict[str, dict] = field(default_factory=dict)
    users: dict[str, str] = field(default_factory=dict)
    usernames: dict[str, str] = field(default_factory=dict)

    def sql(self, text: str) -> str:
        return text.replace("?", "%s") if self.postgres else text

    def write(self, text: str, params: tuple = ()) -> None:
        with _repository()._runtime.database.write() as db:
            db.execute(self.sql(text), params)

    def count(self, text: str, params: tuple = ()) -> int:
        with _repository()._runtime.database.connect() as db:
            row = db.execute(self.sql(text), params).fetchone()
        return int(row["c"])

    def url(self, suffix: str) -> str:
        return f"/api/notebooks/{self.notebook}{suffix}"


def _repository():
    from app.api.deps import repository

    return repository()


def _login(client: TestClient, username: str) -> dict:
    client.post("/api/auth/register", json={"username": username, "password": "pw123456"})
    token = client.post(
        "/api/auth/login", json={"username": username, "password": "pw123456"}
    ).json()["token"]
    return {"Authorization": f"Bearer {token}"}


@contextmanager
def _no_background():
    """Background work off: an upload's own parse must not hold its source
    busy, and a claimed maintenance slot must stay ``running``."""
    from app.services import background_jobs
    from app.services.kg import scheduler as kg_scheduler

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(kg_scheduler, "submit_job", lambda *args, **kwargs: None)
        patch.setattr(background_jobs, "submit", lambda *args, **kwargs: None)
        yield patch


def build_world(client: TestClient, *, postgres: bool) -> World:
    usernames = {name: f"c{20261300 + index:08d}" for index, name in enumerate(("owner", "editor"))}
    headers = {name: _login(client, username) for name, username in usernames.items()}
    users = {name: client.get("/api/me", headers=h).json()["id"] for name, h in headers.items()}
    notebook = client.post(
        "/api/notebooks", json={"name": "协作库"}, headers=headers["owner"]
    ).json()["id"]
    _repository()._runtime.groups.set_user_editor_grant(
        notebook, owner_id=users["owner"], target_user_id=users["editor"]
    )
    world = World(client=client, postgres=postgres, notebook=notebook, source="",
                  headers=headers, users=users, usernames=usernames)
    world.write("UPDATE users SET display_name=? WHERE id=?", ("张三", users["editor"]))
    with _no_background():
        uploaded = client.post(
            world.url("/sources"),
            files=[("files", ("doc.md", b"# doc\n\ntext", "text/markdown"))],
            headers=headers["owner"],
        )
    assert uploaded.status_code == 200, uploaded.text
    world.source = uploaded.json()[0]["id"]
    return world


@contextmanager
def _parsing(source_id: str):
    """Hold the source's parse lock, exactly what ``process_source`` holds from
    replacing elements to building chunks: the source is "being parsed"."""
    ingestion = _repository()._runtime.source_ingestion
    with ingestion.try_hold_source_chunk_lock(source_id, timeout=0) as held:
        assert held
        yield


# ---------------------------------------------------------------- cases


def reparse_single_source_busy_409(world: World) -> None:
    client = world.client
    answered: dict = {}
    with _no_background(), _parsing(world.source):
        # In a thread with a deadline: without the refusal the parse would
        # wait on the very lock this test holds, and the case must fail, not
        # hang.
        request = threading.Thread(target=lambda: answered.update(busy=client.post(
            f"/api/sources/{world.source}/parse", headers=world.headers["editor"]
        )))
        request.start()
        request.join(timeout=15)
        waited = request.is_alive()
    request.join()
    assert not waited, "the second parse waited for the first instead of answering 409"
    busy = answered["busy"]
    assert busy.status_code == 409, busy.text
    assert busy.json()["detail"] == "这份来源正在解析，请等它完成"
    # idle again: the same request parses
    with _no_background():
        done = client.post(f"/api/sources/{world.source}/parse", headers=world.headers["editor"])
    assert done.status_code == 200, done.text


def batch_reparse_reports_skipped_busy(world: World) -> None:
    client = world.client
    with _no_background():
        second = client.post(
            world.url("/sources"),
            files=[("files", ("two.md", b"# two\n\nother text", "text/markdown"))],
            headers=world.headers["editor"],
        ).json()[0]["id"]
    submitted: list[str] = []
    with _no_background() as patch, _parsing(world.source):
        from app.services.kg import scheduler as kg_scheduler

        patch.setattr(
            kg_scheduler, "submit_job", lambda fn, source_id, **kwargs: submitted.append(source_id)
        )
        response = client.post(
            world.url("/sources/reparse"),
            json={"source_ids": [world.source, second]},
            headers=world.headers["editor"],
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scheduled"] == [second] == submitted
    assert body["skipped_busy"] == [world.source]


def kg_busy_409_names_other_initiator_by_display_name(world: World) -> None:
    client = world.client
    # the durable analysis job, started by the editor (display name 张三)
    world.write(
        "INSERT INTO kg_build_jobs (id,notebook_id,created_by,mode,status,stage,"
        "total_sources,completed_sources,failed_sources,error_code,error_message,"
        "created_at,updated_at) "
        "VALUES (?,?,?,'incremental','running','probing',0,0,0,'','',?,?)",
        ("kgj-editor", world.notebook, world.users["editor"], _STAMP, _STAMP),
    )
    with _no_background():
        by_owner = client.post(world.url("/kg/delete"), headers=world.headers["owner"])
        by_editor = client.post(world.url("/kg/delete"), headers=world.headers["editor"])
    assert (by_owner.status_code, by_owner.json()["detail"]) == (
        409, "张三发起的知识图谱分析正在运行"
    )
    # your own job is not narrated with your own name
    assert (by_editor.status_code, by_editor.json()["detail"]) == (
        409, "当前笔记本已有知识图谱分析任务正在运行"
    )
    world.write("UPDATE kg_build_jobs SET status='succeeded' WHERE id=?", ("kgj-editor",))
    # the maintenance slot, claimed by the owner (no display name: the username)
    repo = _repository()
    with _no_background() as patch:
        patch.setattr(repo._runtime.models, "configured", lambda workload: True)
        started = client.post(world.url("/kg/relink"), headers=world.headers["owner"])
        assert started.status_code == 200, started.text
        relink = client.post(world.url("/kg/relink"), headers=world.headers["editor"])
        build = client.post(world.url("/kg/build"), headers=world.headers["editor"])
        own = client.post(world.url("/kg/relink"), headers=world.headers["owner"])
    owner_name = world.usernames["owner"]
    assert (relink.status_code, relink.json()["detail"]) == (
        409, f"{owner_name}发起的补上关联正在运行，请等它完成"
    )
    assert (build.status_code, build.json()["detail"]) == (
        409, f"{owner_name}发起的补上关联正在运行，请等它完成"
    )
    assert (own.status_code, own.json()["detail"]) == (
        409, "当前笔记本正在补上关联，请等它完成"
    )
    repo._runtime.knowledge_lifecycle.kg_maintenance.settle(
        world.notebook, started.json()["job_id"], "succeeded"
    )


def reparse_vs_delete_two_writers(world: World) -> None:
    """Deleting is not held up by a parse in flight (it takes the row lock,
    not the parse lock); afterwards the parse answers 404, the source's
    answer once it is gone."""
    client = world.client
    with _no_background(), _parsing(world.source):
        deleted = client.delete(f"/api/sources/{world.source}", headers=world.headers["owner"])
        assert deleted.status_code == 204, deleted.text
        assert world.count(
            "SELECT COUNT(*) AS c FROM sources WHERE id=?", (world.source,)
        ) == 0
    with _no_background():
        parsed = client.post(f"/api/sources/{world.source}/parse", headers=world.headers["editor"])
    assert parsed.status_code == 404


def capacity_message_wording(world: World) -> None:
    """An editor's upload counts against the owner's limit, and the refusal
    says so: the notebook's limit, not "your" limit."""
    from app.api.source_routes import document_capacity_message

    assert document_capacity_message(3, 3, 1) == (
        "已达到这本笔记本的文档上限：最多 3 篇，当前已有 3 篇，无法再添加 1 篇。"
    )
    repo = _repository()
    owner_limit = {world.users["owner"]: 1}
    with _no_background() as patch:
        patch.setattr(
            repo, "effective_document_limit", lambda user_id: owner_limit.get(user_id, 99)
        )
        refused = world.client.post(
            world.url("/sources"),
            files=[("files", ("over.md", b"# over\n\nlimit", "text/markdown"))],
            headers=world.headers["editor"],
        )
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"] == document_capacity_message(1, 1, 1)


def conflict_review_second_reviewer_gets_409(world: World) -> None:
    for candidate in ("kc-one", "kc-two"):
        world.write(
            "INSERT INTO kg_conflict_candidates "
            "(id,notebook_id,kind,left_ref,right_ref,winner_ref,rationale,status,"
            "created_at,updated_at) VALUES (?,?,'node','a','b',NULL,'',"
            "'pending',?,?)",
            (candidate, world.notebook, _STAMP, _STAMP),
        )
    client = world.client
    first = client.post(world.url("/kg/conflicts/kc-one/reject"), headers=world.headers["editor"])
    assert first.status_code == 200, first.text
    for action in ("reject", "confirm"):
        again = client.post(
            world.url(f"/kg/conflicts/kc-one/{action}"), headers=world.headers["owner"]
        )
        assert (again.status_code, again.json()["detail"]) == (409, "这一项已被处理"), action
    # a pending one that cannot be applied keeps its own (different) answer
    unapplied = client.post(world.url("/kg/conflicts/kc-two/confirm"), headers=world.headers["owner"])
    assert unapplied.status_code == 409
    assert unapplied.json()["detail"] != "这一项已被处理"


CASES: dict[str, Callable[[World], Any]] = {
    "reparse_single_source_busy_409": reparse_single_source_busy_409,
    "batch_reparse_reports_skipped_busy": batch_reparse_reports_skipped_busy,
    "kg_busy_409_names_other_initiator_by_display_name": (
        kg_busy_409_names_other_initiator_by_display_name
    ),
    "reparse_vs_delete_two_writers": reparse_vs_delete_two_writers,
    "capacity_message_wording": capacity_message_wording,
    "conflict_review_second_reviewer_gets_409": conflict_review_second_reviewer_gets_409,
}
