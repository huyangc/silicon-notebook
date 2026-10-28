"""Web export / import of single notebooks (``app.api.notebook_transfer_routes`` over
``app.services.notebook_transfer.jobs``) -- **both backends run the same checks**:
``tests/test_notebook_transfer_web.py`` (SQLite) and
``tests/postgres/test_notebook_transfer_web_pg.py`` (PostgreSQL) each build the app
over their database and hand ``build_world`` an ``insert`` adapter.

The world is the import suites' full world (files, assets, Knowhow, KG, vectors) owned
by a registered user, plus a second user. Jobs run inline on the request thread (the
registry's ``submit`` seam), except where a check drives a run on its own thread to
stop it mid-way; the re-embedding run an import hands off to runs in the real heavy
pool. The registry's clock is a fake the checks advance: nothing sleeps and no literal
date is compared with the real clock.

Not a test module (no ``test_`` prefix); pytest does not collect it.
"""
from __future__ import annotations

import dataclasses
import io
import logging
import os
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient

from app.services import notebook_reembed
from app.services.notebook_transfer import jobs as jobs_module
from app.services.notebook_transfer.import_ import NotebookImportError, archive_document_count
from tests import notebook_archive_cases as archive_cases
from tests import notebook_copy_characterization_cases as char_cases
from tests import notebook_import_cases as import_cases

NB = char_cases.NB
ENV = dict(import_cases.ENV)
"""Settings every app of these checks needs (set before it is built)."""
MIB = 1024 * 1024


class FakeTimer:
    """One arming of the registry's expiry timer (never fires by itself)."""

    def __init__(self, delay: float, action: Callable[[], Any]) -> None:
        self.delay, self.action, self.cancelled = delay, action, False

    def cancel(self) -> None:
        self.cancelled = True


class FakeClock:
    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclasses.dataclass
class World:
    client: TestClient
    repo: Any
    backend: Any
    tmp: Path
    monkeypatch: Any
    jobs: Any
    clock: FakeClock
    owner: dict
    owner_id: str
    other: dict
    other_id: str
    timers: list

    @property
    def storage(self) -> Path:
        return Path(self.repo.storage_dir)

    def sql(self, statement: str, params: tuple = ()) -> None:
        if not self.repo.settings.database_url.startswith("sqlite"):
            statement = statement.replace("?", "%s")
        with self.repo._runtime.database.write() as db:
            db.execute(statement, params)

    def transfer_files(self) -> list[str]:
        root = self.storage / "transfers"
        return sorted(
            str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()
        )


def inline_submit(fn: Callable, *args: Any, name: "str | None" = None, **kwargs: Any) -> None:
    fn(*args, **kwargs)


def configure_environment(monkeypatch, tmp_path: Path, database_url: str) -> None:
    monkeypatch.setenv("SILICON_NOTEBOOK_ENV_FILE", "")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("MODEL_SERVICES_CONFIG", "")
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)


def register(client: TestClient, username: str) -> tuple[dict, str]:
    response = client.post("/api/auth/register", json={"username": username, "password": "pw"})
    assert response.status_code == 200, response.text
    headers = {"Authorization": f"Bearer {response.json()['token']}"}
    return headers, client.get("/api/me", headers=headers).json()["id"]


def build_world(client: TestClient, repo, backend, tmp: Path, monkeypatch) -> World:
    owner, owner_id = register(client, "a10000001")
    other, other_id = register(client, "b10000002")
    archive_cases.seed(
        repo, backend, import_cases.full_world(), Path(repo.storage_dir), char_cases.FULL_FILES
    )
    jobs = repo._runtime.notebook_transfers
    clock = FakeClock()
    timers: list = []
    monkeypatch.setattr(jobs, "_submit", inline_submit)
    monkeypatch.setattr(jobs, "_clock", clock)

    def arm(delay: float, action: Callable[[], Any]) -> FakeTimer:
        timers.append(FakeTimer(delay, action))
        return timers[-1]

    monkeypatch.setattr(jobs, "_timer", arm)
    world = World(
        client=client, repo=repo, backend=backend, tmp=tmp, monkeypatch=monkeypatch,
        jobs=jobs, clock=clock, owner=owner, owner_id=owner_id, other=other,
        other_id=other_id, timers=timers,
    )
    world.sql("UPDATE notebooks SET created_by = ?, sync_origin = '' WHERE id = ?", (owner_id, NB))
    return world


# ------------------------------------------------------------------ helpers


def export(world: World, headers: "dict | None" = None, notebook_id: str = NB):
    return world.client.post(
        f"/api/notebooks/{notebook_id}/export", headers=headers or world.owner
    )


def exported(world: World, notebook_id: str = NB) -> dict:
    """A finished export of a notebook (its job); by default the world's."""
    response = export(world, notebook_id=notebook_id)
    assert response.status_code == 202, response.text
    job = world.client.get(
        f"/api/notebook-transfers/{response.json()['id']}", headers=world.owner
    ).json()
    assert job["state"] == "succeeded", job
    return job


def second_notebook(world: World, name: str = "第二本") -> str:
    """Another notebook of the owner (an export of it never supersedes the
    world notebook's)."""
    created = world.client.post("/api/notebooks", json={"name": name}, headers=world.owner)
    assert created.status_code == 200, created.text
    return created.json()["id"]


def ticket(world: World, job_id: str, headers: "dict | None" = None):
    return world.client.post(
        f"/api/notebook-transfers/{job_id}/download-ticket", headers=headers or world.owner
    )


def download(world: World, job_id: str) -> bytes:
    response = ticket(world, job_id)
    assert response.status_code == 200, response.text
    fetched = world.client.get(response.json()["url"])
    assert fetched.status_code == 200, fetched.text
    return fetched.content


def upload(world: World, body, headers: "dict | None" = None, *, name: str = "", **extra):
    return world.client.post(
        "/api/notebook-imports",
        params={"name": name} if name else None,
        content=body,
        headers={**(headers or world.owner), "Content-Type": "application/zip", **extra},
    )


def archive_bytes(world: World) -> bytes:
    return download(world, exported(world)["id"])


def assert_refused_download(response) -> None:
    assert response.status_code == 404, response.text
    assert response.json() == {"detail": "Not found"}
    assert response.headers["cache-control"] == "no-store"


def bind_models(world: World):
    """Every embedding workload bound to one fake (the import re-embeds what the
    archive's unbound identities make it drop)."""
    from tests.model_testkit import bind_embedding_client

    embedder = import_cases.CountingEmbedder()
    bind_embedding_client(world.repo, embedder, workload_ids=list(import_cases._ALL_SERVICES))
    return embedder


# ------------------------------------------------------------------ checks


def check_export_needs_the_owner_and_allows_a_mirror(world: World) -> None:
    """``notebook:export`` is owner-only (a reader and a stranger get 404, no job is
    made) and lets a sync mirror through."""
    world.repo._runtime.sharing.add_member(NB, world.other_id)
    assert world.client.get(f"/api/notebooks/{NB}", headers=world.other).status_code == 200
    assert export(world, world.other).status_code == 404
    assert world.client.get("/api/notebook-transfers", headers=world.other).json() == {"jobs": []}
    world.repo._runtime.sharing_store.set_notebook_sync_origin(NB, "prod-shanghai")
    assert world.repo._runtime.sharing.notebook_sync_origin(NB) == "prod-shanghai"
    response = export(world)
    assert response.status_code == 202, response.text
    job = response.json()
    assert (job["kind"], job["notebook_id"], job["state"]) == ("export", NB, "succeeded")
    assert job["downloadable"] and job["archive_bytes"] > 0
    assert "path" not in job["report"] and "stale_part_files" not in job["report"]


def check_jobs_of_another_user_are_not_found(world: World) -> None:
    job = exported(world)
    base = f"/api/notebook-transfers/{job['id']}"
    assert world.client.get(base, headers=world.other).status_code == 404
    assert ticket(world, job["id"], world.other).status_code == 404
    assert world.client.delete(base, headers=world.other).status_code == 404
    assert world.client.get("/api/notebook-transfers", headers=world.other).json() == {"jobs": []}
    # Untouched by those attempts.
    assert world.client.get(base, headers=world.owner).json()["state"] == "succeeded"
    assert [j["id"] for j in world.client.get(
        "/api/notebook-transfers", headers=world.owner).json()["jobs"]] == [job["id"]]
    for path in (base, f"{base}/download-ticket"):
        assert world.client.request(
            "GET" if path == base else "POST", path
        ).status_code == 401


def count(path: Path, **kwargs) -> int:
    return archive_document_count(path, max_inflated_bytes=1 << 40, **kwargs)


def check_document_count_trusts_no_label(world: World) -> None:
    """Every source row counts but a Knowhow table's own hidden source (named by
    a ``knowhow_tables`` row of the archive): a promotion source counts, and so
    does any document relabelled ``promotion`` or ``knowhow`` in a forged archive
    (checksums recomputed) -- relabelling buys a non-admin nothing."""
    with world.repo._runtime.database.write() as db:
        world.backend.insert(db, "sources", {
            "id": "src-promo-web", "notebook_id": NB, "title": "P", "source_type": "promotion",
            "status": "ready", "parse_status": "ready",
            "created_at": char_cases.NOW, "updated_at": char_cases.NOW,
        })
    path = world.tmp / "count.snbk"
    path.write_bytes(archive_bytes(world))
    tables = archive_cases.archive_rows(path)
    rows = tables["sources"]
    hidden = {t["hidden_source_id"] for t in tables["knowhow_tables"]}
    assert hidden and {"knowhow", "promotion"} <= {row["source_type"] for row in rows}
    honest = count(path)
    assert honest == len(rows) - len(hidden)
    assert honest == world.repo.visible_document_count(NB) + 1  # + the promotion source
    documents = [r["id"] for r in rows if r["source_type"] not in ("knowhow", "promotion")]
    assert documents
    for label in ("promotion", "knowhow"):
        forged = world.tmp / f"forged-{label}.snbk"
        import_cases.rebuild(path, forged, rows=lambda table, table_rows, label=label: [
            {**r, "source_type": label} if r["id"] in documents else r for r in table_rows
        ] if table == "sources" else table_rows)
        assert count(forged) == honest, label
        assert count(forged, stop_above=1) == 2, "stops right past the bound"
        world.sql(
            "UPDATE user_profiles SET upload_document_limit = ? WHERE user_id = ?",
            (honest - 1, world.other_id),
        )
        refused = upload(world, forged.read_bytes(), world.other)
        assert refused.status_code == 409, (label, refused.text)


def check_non_admins_are_held_to_the_document_limit(world: World) -> None:
    data = archive_bytes(world)
    path = world.tmp / "limit.snbk"
    path.write_bytes(data)
    documents = count(path)
    assert documents >= 2
    before = world.transfer_files()

    world.sql(
        "UPDATE user_profiles SET upload_document_limit = ? WHERE user_id = ?",
        (documents - 1, world.other_id),
    )
    refused = upload(world, data, world.other)
    assert refused.status_code == 409, refused.text
    assert str(documents - 1) in refused.json()["detail"] and "命令行" in refused.json()["detail"]
    assert refused.headers.get("x-user-message") == "1"
    assert world.client.get("/api/notebook-transfers", headers=world.other).json() == {"jobs": []}
    assert world.transfer_files() == before, "the refused upload left a file"

    # An administrator is not held to it.
    world.sql("UPDATE users SET role = 'admin' WHERE id = ?", (world.other_id,))
    admitted = upload(world, data, world.other)
    assert admitted.status_code == 202, admitted.text
    world.sql("UPDATE users SET role = 'user' WHERE id = ?", (world.other_id,))

    # At the limit exactly: admitted.
    world.sql(
        "UPDATE user_profiles SET upload_document_limit = ? WHERE user_id = ?",
        (documents, world.other_id),
    )
    admitted = upload(world, data, world.other)
    assert admitted.status_code == 202, admitted.text
    assert admitted.json()["state"] == "succeeded", admitted.json()


def check_413_for_export_and_upload(world: World) -> None:
    settings = world.repo.settings
    world.monkeypatch.setattr(settings, "notebook_transfer_web_max_mb", 1)
    assert world.client.get("/api/system/config", headers=world.owner).json()[
        "notebook_transfer_max_bytes"
    ] == MIB
    # A Memory source's file never travels: it does not count against the limit.
    with world.repo._runtime.database.write() as db:
        world.backend.insert(db, "sources", {
            "id": "src-memory-web", "notebook_id": NB, "title": "M", "source_type": "memory",
            "status": "ready", "parse_status": "ready", "file_size": 10 * MIB,
            "created_at": char_cases.NOW, "updated_at": char_cases.NOW,
        })
    world.sql("UPDATE sources SET file_size = 1 WHERE notebook_id = ? AND source_type <> 'memory'",
              (NB,))
    assert export(world).json()["state"] == "succeeded"
    # Export: the source files alone exceed the limit -> refused before a job.
    world.sql("UPDATE sources SET file_size = ? WHERE notebook_id = ? AND source_type <> 'memory'",
              (MIB, NB))
    jobs_before = world.client.get("/api/notebook-transfers", headers=world.owner).json()
    refused = export(world)
    assert refused.status_code == 413, refused.text
    assert "命令行" in refused.json()["detail"]
    assert world.client.get("/api/notebook-transfers", headers=world.owner).json() == jobs_before
    world.sql("UPDATE sources SET file_size = 1 WHERE notebook_id = ?", (NB,))
    # Export: the archive grows past it while it is written -> the job stops
    # there (it never reaches publication, where a final size check also stands).
    from app.services.notebook_transfer import export as export_module

    published: list = []
    real_publish = export_module._publish
    world.monkeypatch.setattr(
        export_module, "_publish", lambda *args: (published.append(args), real_publish(*args))
    )
    big = world.storage / "notebooks" / NB / "big.bin"
    big.parent.mkdir(parents=True, exist_ok=True)
    big.write_bytes(os.urandom(3 * MIB))
    job = export(world).json()
    assert (job["state"], job["error"]["code"]) == ("failed", "too_large"), job
    assert published == [], "the export ran to the end before it was stopped"
    # The last checkpoint it passed was still within the limit.
    assert 0 < job["progress"]["bytes"] <= MIB, job["progress"]
    assert "命令行" in job["error"]["message"]
    assert [f for f in world.transfer_files() if job["id"] in f and not f.startswith("jobs/")] == []
    big.unlink()

    # Upload: a declared length over the limit is refused before reading; a
    # streamed body is refused the moment it passes the limit.
    declared = upload(world, b"x", **{"Content-Length": str(MIB + 1)})
    assert declared.status_code == 413, declared.text

    def chunks():
        for _ in range(MIB // 65536 + 1):
            yield b"\0" * 65536

    streamed = upload(world, chunks())
    assert streamed.status_code == 413, streamed.text
    assert "命令行" in streamed.json()["detail"]
    assert [f for f in world.transfer_files() if f.startswith("imports/")] == []


def check_the_upload_streams_to_disk(world: World) -> None:
    """The body is never read whole: ``Request.body`` is never called, and while
    the request is still arriving (driven message by message through the ASGI
    app, as a server delivers it) the bytes already received are on disk in
    bounded blocks."""
    import asyncio
    import json

    from starlette.requests import Request

    from app.api import notebook_transfer_routes as routes

    data = archive_bytes(world)
    block = 4096
    world.monkeypatch.setattr(routes, "UPLOAD_WRITE_BYTES", block)
    writes: list[int] = []
    real_write = routes._write_all

    def recording(fd: int, payload: bytes) -> None:
        writes.append(len(payload))
        real_write(fd, payload)

    def no_body(self):
        raise AssertionError("the upload body was buffered")

    world.monkeypatch.setattr(routes, "_write_all", recording)
    world.monkeypatch.setattr(Request, "body", no_body)
    piece = 1000
    pieces = [data[start:start + piece] for start in range(0, len(data), piece)]
    lagging: list[tuple[int, int]] = []
    sent = 0

    async def receive() -> dict:
        nonlocal sent
        # Before each next message: everything but the last block is written.
        if sum(writes) < sent - block - piece:
            lagging.append((sent, sum(writes)))
        if not pieces:
            return {"type": "http.disconnect"}
        chunk = pieces.pop(0)
        sent += len(chunk)
        return {"type": "http.request", "body": chunk, "more_body": bool(pieces)}

    messages: list[dict] = []

    async def send(message: dict) -> None:
        messages.append(message)

    token = world.owner["Authorization"].encode()
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": "/api/notebook-imports",
        "raw_path": b"/api/notebook-imports", "root_path": "",
        "query_string": "name=%E6%B5%81%E5%BC%8F".encode(),
        "headers": [
            (b"host", b"testserver"), (b"authorization", token),
            (b"content-type", b"application/zip"),
        ],
        "client": ("testclient", 50000), "server": ("testserver", 80),
    }
    asyncio.run(world.client.app(scope, receive, send))
    start = next(m for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    assert start["status"] == 202, body
    assert json.loads(body)["state"] == "succeeded"
    assert sum(writes) == len(data)
    assert len(writes) > 2 and max(writes) < block + piece, writes
    assert lagging == [], lagging


def check_bad_uploads_are_refused(world: World) -> None:
    wrong_type = world.client.post(
        "/api/notebook-imports", content=b"PK", headers={**world.owner, "Content-Type": "text/plain"}
    )
    assert wrong_type.status_code == 415, wrong_type.text
    assert upload(world, b"").status_code == 400
    garbage = upload(world, b"not a zip at all")
    assert garbage.status_code == 400, garbage.text
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("hello.txt", "hi")
    assert upload(world, buffer.getvalue()).status_code == 400
    assert world.client.post(
        "/api/notebook-imports", content=b"PK", headers={"Content-Type": "application/zip"}
    ).status_code == 401
    assert [f for f in world.transfer_files() if f.startswith("imports/")] == []


def check_tickets_are_single_use_and_expire(world: World) -> None:
    job = exported(world)
    archive = world.storage / "transfers" / "exports" / f"{job['id']}.snbk"
    first = ticket(world, job["id"])
    assert first.status_code == 200, first.text
    url = first.json()["url"]
    assert url.startswith("/api/notebook-transfers/download?ticket=")
    token = url.split("ticket=", 1)[1]
    assert len(token) >= 43
    assert token not in str(world.jobs._tickets), "only the ticket's digest is kept"
    fetched = world.client.get(url)
    assert fetched.status_code == 200, fetched.text
    assert fetched.content == archive.read_bytes()
    assert fetched.headers["cache-control"] == "no-store"
    assert fetched.headers["referrer-policy"] == "no-referrer"
    assert fetched.headers["content-type"] == "application/zip"
    assert "filename*=UTF-8''" in fetched.headers["content-disposition"]
    assert world.client.get(f"/api/notebook-transfers/{job['id']}",
                            headers=world.owner).json()["downloaded"] is True
    # Replay.
    assert_refused_download(world.client.get(url))
    # Expiry: valid until 60 seconds have passed.
    almost = ticket(world, job["id"]).json()["url"]
    world.clock.advance(jobs_module.TICKET_TTL_SECONDS - 1)
    assert world.client.get(almost).status_code == 200
    late = ticket(world, job["id"]).json()["url"]
    world.clock.advance(jobs_module.TICKET_TTL_SECONDS)
    assert_refused_download(world.client.get(late))
    # Unknown, empty, absent, oversized.
    for target in (
        "/api/notebook-transfers/download?ticket=nope",
        "/api/notebook-transfers/download?ticket=",
        "/api/notebook-transfers/download",
        "/api/notebook-transfers/download?ticket=" + "a" * 500,
    ):
        assert_refused_download(world.client.get(target))
    # A not finished job has no ticket.
    world.monkeypatch.setattr(world.jobs, "_submit", lambda *a, **k: None)
    queued = export(world).json()
    assert ticket(world, queued["id"]).status_code == 409


def check_a_ticket_is_bound_to_its_job_and_user(world: World) -> None:
    """A ticket another user could not have asked for, and one whose job is gone,
    the user disabled, or the job expired, all fail the same way."""
    data = archive_bytes(world)
    job = exported(world)
    other_job = world.client.post(
        "/api/notebook-imports", content=data,
        headers={**world.other, "Content-Type": "application/zip"},
    ).json()
    assert other_job["state"] == "succeeded", other_job

    def forge(token: str, user_id: str) -> str:
        world.jobs._tickets[jobs_module._digest(token)] = jobs_module._Ticket(
            job_id=job["id"], user_id=user_id,
            expires_at=world.clock() + jobs_module.TICKET_TTL_SECONDS,
        )
        return f"/api/notebook-transfers/download?ticket={token}"

    # Bound to (job, user): a ticket of the other user naming the owner's job is
    # refused, the same ticket bound to the owner downloads.
    assert_refused_download(world.client.get(forge("forged-for-other", world.other_id)))
    assert world.client.get(forge("forged-for-owner", world.owner_id)).status_code == 200
    # The job deleted after the ticket was issued.
    url = ticket(world, job["id"]).json()["url"]
    assert world.client.delete(
        f"/api/notebook-transfers/{job['id']}", headers=world.owner).status_code == 200
    assert_refused_download(world.client.get(url))
    # The user disabled after the ticket was issued.
    again = exported(world)
    url = ticket(world, again["id"]).json()["url"]
    world.sql("UPDATE users SET status = 'disabled' WHERE id = ?", (world.owner_id,))
    assert_refused_download(world.client.get(url))
    world.sql("UPDATE users SET status = 'active' WHERE id = ?", (world.owner_id,))
    # The job expired after the ticket was issued (the ticket itself still fresh).
    url = ticket(world, again["id"]).json()["url"]
    world.jobs._jobs[again["id"]].finished_at -= world.repo.settings.notebook_transfer_ttl_seconds
    assert_refused_download(world.client.get(url))


def check_two_concurrent_redemptions_have_one_winner(world: World) -> None:
    job = exported(world)
    token = ticket(world, job["id"]).json()["url"].split("ticket=", 1)[1]
    contenders = 8
    barrier = threading.Barrier(contenders)
    results: list = []
    lock = threading.Lock()

    def redeem() -> None:
        barrier.wait()
        download = world.jobs.redeem_ticket(token, user_active=lambda user_id: True)
        with lock:
            results.append(download)
        if download is not None:
            os.close(download.fd)

    threads = [threading.Thread(target=redeem) for _ in range(contenders)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert len(results) == contenders
    assert sum(1 for result in results if result is not None) == 1, results


def check_the_ticket_never_reaches_a_log(world: World, caplog, log_dir: Path) -> None:
    """The access log filter drops the download's (and the import's) query; the
    request event log and every other log line carry no ticket."""
    from app.core.auth_logging import AuthenticationAccessFilter

    caplog.set_level(logging.DEBUG)
    job = exported(world)
    url = ticket(world, job["id"]).json()["url"]
    token = url.split("ticket=", 1)[1]
    assert world.client.get(url).status_code == 200
    assert world.client.get(url).status_code == 404
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 1, '%s - "%s %s HTTP/%s" %d',
        ("client", "GET", url, "1.1", 200), None,
    )
    assert AuthenticationAccessFilter().filter(record)
    assert token not in record.getMessage()
    assert "/api/notebook-transfers/download" in record.getMessage()
    named = logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 1, '%s - "%s %s HTTP/%s" %d',
        ("client", "POST", "/api/notebook-imports?name=%E6%9C%BA%E5%AF%86", "1.1", 202), None,
    )
    assert AuthenticationAccessFilter().filter(named)
    assert "name=" not in named.getMessage()
    # Every server-side record (the test's own HTTP client logs its request URL).
    server = [
        record for record in caplog.records
        if not record.name.startswith(("httpx", "httpcore"))
    ]
    assert server, "nothing was logged at all"
    assert not [r.name for r in server if token in r.getMessage()]
    logged = [
        path.read_text("utf-8", errors="replace")
        for path in log_dir.rglob("*") if path.is_file()
    ]
    assert any("/api/notebook-transfers/download" in text for text in logged), logged
    assert not any(token in text for text in logged)


def check_finished_jobs_and_stray_files_expire(world: World) -> None:
    job = exported(world)
    ttl = world.repo.settings.notebook_transfer_ttl_seconds
    # One timer is armed for the first expiry; a later job does not arm another.
    assert job["expires_at"] and [t.delay for t in world.timers] == [ttl + 1.0]
    world.clock.advance(10)
    later = exported(world, notebook_id=second_notebook(world))
    assert len(world.timers) == 1
    exports = world.storage / "transfers" / "exports"
    imports = world.storage / "transfers" / "imports"
    stale_part = exports / "xfer-0123456789abcdef0123456789abcdef.snbk.0a1b2c.part"
    stale_upload = imports / "xfer-fedcba9876543210fedcba9876543210.upload.part"
    fresh_upload = imports / "xfer-00000000000000000000000000000000.upload.part"
    for path in (stale_part, stale_upload, fresh_upload):
        path.write_bytes(b"partial")
    old = world.clock() - ttl - 5
    for path in (stale_part, stale_upload):
        os.utime(path, (old, old))
    os.utime(fresh_upload, (world.clock(), world.clock()))
    url = ticket(world, job["id"]).json()["url"]
    world.clock.advance(ttl - 12)  # the first job is 2 s short of expiry
    assert world.client.get(
        f"/api/notebook-transfers/{job['id']}", headers=world.owner).status_code == 200
    # Reads never scan the directories: the strays are still there.
    assert stale_part.exists() and stale_upload.exists()
    world.clock.advance(3)
    # The armed timer fires: the first job expires, the later one stays, the strays
    # go, and the timer is armed again for the later one.
    (armed,) = world.timers
    armed.action()
    assert not stale_part.exists() and not stale_upload.exists()
    assert fresh_upload.exists(), "an upload still being written is kept"
    assert world.client.get(
        f"/api/notebook-transfers/{job['id']}", headers=world.owner).status_code == 404
    assert world.client.get(
        f"/api/notebook-transfers/{later['id']}", headers=world.owner).status_code == 200
    # Due 9 s from now (+1 s margin).
    assert [t.delay for t in world.timers][1:] == [10.0]
    assert not (exports / f"{job['id']}.snbk").exists()
    assert (exports / f"{later['id']}.snbk").exists()
    assert not (world.storage / "transfers" / "jobs" / f"{job['id']}.json").exists()
    assert world.jobs._tickets == {}
    assert_refused_download(world.client.get(url))
    world.clock.advance(10)
    assert world.jobs.sweep() == 1
    # The upload part untouched for the TTL by now is a stray as well.
    assert world.transfer_files() == []


def check_a_restart_fails_the_jobs_that_were_running(world: World) -> None:
    """A second registry over the same storage (the next process): running and
    queued jobs are failed and their files removed; a finished export survives
    and can be downloaded; files no job owns are gone."""
    from app.services.notebook_transfer.jobs import NotebookTransferJobs
    from app.services.startup_warmup import _recover_notebook_transfers

    done = exported(world)
    data = download(world, done["id"])
    vanished = exported(world, notebook_id=second_notebook(world))
    (world.storage / "transfers" / "exports" / f"{vanished['id']}.snbk").unlink()
    captured: list = []
    world.monkeypatch.setattr(world.jobs, "_submit", lambda fn, *args, **kw: captured.append(args))
    queued_import = upload(world, data).json()
    running_export = export(world).json()
    (job,) = [args[0] for args in captured if args and getattr(args[0], "kind", "") == "export"]
    assert world.jobs._claim(job, "writing")
    stray = world.storage / "transfers" / "exports" / f"{job.id}.snbk.abcdef.part"
    stray.write_bytes(b"half an archive")
    orphan = world.storage / "transfers" / "imports" / "xfer-11111111111111111111111111111111.upload.part"
    orphan.write_bytes(b"cut off")

    runtime = world.repo._runtime
    successor = NotebookTransferJobs(
        settings=world.repo.settings, storage_dir=lambda: runtime.storage_dir,
        export_rows=runtime.sharing_store, embeddings=lambda: {}, object_schemas=lambda: [],
        importer=lambda: runtime.notebook_import, submit=inline_submit,
        notify=lambda user_id: None, timer=lambda delay, action: None,
    )
    successor._clock = world.clock
    assert successor.recover_on_start() == 2
    states = {view["id"]: view for view in successor.list_jobs(world.owner_id)}
    assert states[queued_import["id"]]["state"] == "failed"
    assert states[queued_import["id"]]["error"]["code"] == "import_interrupted"
    assert states[running_export["id"]]["error"]["code"] == "export_interrupted"
    assert states[done["id"]]["state"] == "succeeded"
    assert vanished["id"] not in states, "a finished export whose archive is gone is dropped"
    files = sorted(
        str(p.relative_to(world.storage / "transfers"))
        for p in (world.storage / "transfers").rglob("*") if p.is_file()
    )
    assert files == sorted([
        f"exports/{done['id']}.snbk",
        *(f"jobs/{job_id}.json" for job_id in states),
    ]), files
    # The survivor still downloads, through the successor.
    token = successor.issue_ticket(world.owner_id, done["id"])["url"].split("ticket=", 1)[1]
    fetched = successor.redeem_ticket(token, user_active=lambda user_id: True)
    assert fetched is not None and fetched.size == len(data)
    os.close(fetched.fd)

    # Server startup is what runs it.
    calls: list[str] = []
    world.monkeypatch.setattr(runtime.notebook_transfers, "recover_on_start",
                              lambda: calls.append("recovered") or 0)
    _recover_notebook_transfers(world.repo)
    assert calls == ["recovered"]


def check_export_download_import_round_trip(world: World) -> None:
    """Export -> download -> import: the new notebook is the importer's and
    readable; the import re-embedded what it dropped and asked for the scale index
    only after that run finished; the bell showed each step."""
    order: list[str] = []
    real_run = notebook_reembed.NotebookReembedService.run_claimed

    def run_claimed(self, notebook_id):
        status = real_run(self, notebook_id)
        order.append(f"reembed:{status.state}")
        return status

    world.monkeypatch.setattr(notebook_reembed.NotebookReembedService, "run_claimed", run_claimed)
    real_index = world.repo.maybe_auto_index
    world.monkeypatch.setattr(
        world.repo, "maybe_auto_index",
        lambda notebook_id: (order.append("index"), real_index(notebook_id))[1],
    )
    job = exported(world)
    bell = world.client.get("/api/me/pending-actions", headers=world.owner).json()
    (item,) = [i for i in bell["items"] if i["type"] == "notebook_transfer"]
    assert (item["kind"], item["state"], item["job_id"]) == ("export", "succeeded", job["id"])
    counted = bell["count"]
    data = download(world, job["id"])
    bell = world.client.get("/api/me/pending-actions", headers=world.owner).json()
    assert not [i for i in bell["items"] if i["type"] == "notebook_transfer"]
    assert bell["count"] == counted - 1

    bind_models(world)
    response = upload(world, data, world.other, name="导入的副本")
    assert response.status_code == 202, response.text
    imported = response.json()
    assert (imported["kind"], imported["state"], imported["phase"]) == (
        "import", "succeeded", "done"), imported
    new_id = imported["notebook_id"]
    assert new_id and new_id != NB
    assert imported["notebook_name"] == "导入的副本"
    assert imported["report"]["owner_id"] == world.other_id
    assert imported["reembed"]["state"] == "succeeded", imported["reembed"]
    assert imported["index"] == {"state": "requested"}
    assert order == ["reembed:succeeded", "index"], order
    assert [f for f in world.transfer_files() if f.startswith("imports/")] == []

    detail = world.client.get(f"/api/notebooks/{new_id}", headers=world.other)
    assert detail.status_code == 200, detail.text
    assert detail.json()["name"] == "导入的副本"
    sources = world.client.get(f"/api/notebooks/{new_id}/sources", headers=world.other)
    assert sources.status_code == 200, sources.text
    assert world.repo.visible_document_count(new_id) == world.repo.visible_document_count(NB)
    assert world.client.get(f"/api/notebooks/{new_id}", headers=world.owner).status_code == 404
    # Every job record is the user's own: the owner sees the export, the importer
    # the import.
    owners = {
        j["id"] for j in world.client.get("/api/notebook-transfers", headers=world.owner).json()["jobs"]
    }
    assert imported["id"] not in owners and job["id"] in owners


def check_an_import_without_legs_still_asks_for_the_index(world: World) -> None:
    from app.services.notebook_transfer.import_ import NotebookImportService

    order: list[str] = []
    real_import = NotebookImportService.import_notebook

    def without_legs(self, *args, **kwargs):
        return dataclasses.replace(real_import(self, *args, **kwargs), reembed_legs=())

    world.monkeypatch.setattr(NotebookImportService, "import_notebook", without_legs)
    world.monkeypatch.setattr(world.repo, "maybe_auto_index", lambda nb: order.append(nb))
    imported = upload(world, archive_bytes(world)).json()
    assert imported["state"] == "succeeded" and imported["reembed"] is None, imported
    assert order == [imported["notebook_id"]]


def check_a_failed_reembed_builds_no_index(world: World) -> None:
    """With no embedding model bound the re-embedding run fails; the import is
    still done, and no scale index is asked for."""
    called: list[str] = []
    world.monkeypatch.setattr(world.repo, "maybe_auto_index", lambda nb: called.append(nb))
    imported = upload(world, archive_bytes(world)).json()
    assert imported["state"] == "succeeded", imported
    assert imported["reembed"]["state"] == "failed", imported["reembed"]
    assert imported["index"] == {"state": "skipped", "reason": "reembed_not_done"}
    assert called == []


def _run_on_a_thread(world: World) -> tuple[list, Callable[[], threading.Thread]]:
    captured: list = []
    world.monkeypatch.setattr(
        world.jobs, "_submit", lambda fn, *args, **kwargs: captured.append((fn, args))
    )

    def start() -> threading.Thread:
        fn, args = captured.pop(0)
        thread = threading.Thread(target=fn, args=args)
        thread.start()
        return thread

    return captured, start


def check_delete_cancels_a_running_export(world: World) -> None:
    """DELETE stops a running export at its very next checkpoint: no page or
    block is written after it, the run ends ``cancelled`` and leaves no file."""
    from app.services.notebook_transfer import export as export_module

    _captured, start = _run_on_a_thread(world)
    entered, release = threading.Event(), threading.Event()
    real_check = export_module._ArchiveWriter._check
    after_release: list[int] = []

    def paused(self) -> None:
        if not entered.is_set():
            entered.set()
            assert release.wait(timeout=30)
        elif release.is_set():
            after_release.append(1)
        real_check(self)

    world.monkeypatch.setattr(export_module._ArchiveWriter, "_check", paused)
    job = export(world).json()
    assert job["state"] == "queued"
    live = world.jobs._jobs[job["id"]]
    thread = start()
    assert entered.wait(timeout=30)
    assert world.client.get(
        f"/api/notebook-transfers/{job['id']}", headers=world.owner).json()["state"] == "running"
    deleted = world.client.delete(f"/api/notebook-transfers/{job['id']}", headers=world.owner)
    assert deleted.status_code == 200 and deleted.json() == {
        "id": job["id"], "outcome": "cancelled", "kept_notebook_id": None}
    release.set()
    thread.join(timeout=60)
    assert not thread.is_alive()
    assert live.state == "cancelled", live.state
    assert after_release == [], "the export went on writing after it was cancelled"
    assert world.transfer_files() == [], world.transfer_files()
    assert world.client.get(
        f"/api/notebook-transfers/{job['id']}", headers=world.owner).status_code == 404


def check_delete_cancels_a_queued_or_running_import(world: World) -> None:
    from app.services.notebook_transfer import jobs as module

    data = archive_bytes(world)
    before = import_cases.residue(world.repo)
    _captured, start = _run_on_a_thread(world)
    # Queued: removed with its upload; the run that comes later does nothing.
    queued = upload(world, data).json()
    deleted = world.client.delete(f"/api/notebook-transfers/{queued['id']}", headers=world.owner)
    assert deleted.json()["outcome"] == "cancelled", deleted.json()
    assert [f for f in world.transfer_files() if f.startswith("imports/")] == []
    start().join(timeout=60)
    # Running, before publish: the import compensates.
    entered, release = threading.Event(), threading.Event()
    real_progress = module.NotebookTransferJobs._import_progress

    def paused(self, job, event, written):
        if event.stage == "rows" and not entered.is_set():
            entered.set()
            assert release.wait(timeout=30)
        return real_progress(self, job, event, written)

    world.monkeypatch.setattr(module.NotebookTransferJobs, "_import_progress", paused)
    running = upload(world, data).json()
    live = world.jobs._jobs[running["id"]]
    thread = start()
    assert entered.wait(timeout=30)
    # DELETE waits for the run to settle: send it from another thread and, once
    # it has asked the run to stop, let the run go on to its next progress event.
    answers: list = []
    deleting = threading.Thread(target=lambda: answers.append(world.client.delete(
        f"/api/notebook-transfers/{running['id']}", headers=world.owner)))
    deleting.start()
    assert live.cancel.wait(timeout=30)
    release.set()
    deleting.join(timeout=60)
    thread.join(timeout=60)
    assert not thread.is_alive()
    assert answers[0].json() == {
        "id": running["id"], "outcome": "cancelled", "kept_notebook_id": None}
    import_cases.assert_no_residue(world.repo, before)
    left = [f for f in world.transfer_files() if queued["id"] in f or running["id"] in f]
    assert left == [], left


# ------------------------------------------------------- review follow-ups


def _inflating_archive(inflated_bytes: int) -> bytes:
    """A small zip whose ``sources`` entry inflates to ``inflated_bytes`` (rows
    varied enough to stay under the protocol's per-entry ratio limit)."""
    import random

    rng = random.Random(7)
    lines, size = [], 0
    while size < inflated_bytes:
        line = (
            f'{{"id":"src-{len(lines):08d}","source_type":"memory",'
            f'"title":"{rng.getrandbits(32):08x}"}}\n'
        ).encode()
        lines.append(line)
        size += len(line)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", "{}")
        archive.writestr("rows/sources.jsonl", b"".join(lines))
    return buffer.getvalue()


def check_an_inflating_archive_is_refused_before_decompression(world: World) -> None:
    """The web import holds the zip directory to ``WEB_IMPORT_MAX_INFLATION`` times
    the web limit before a single entry is decompressed -- in the request's
    pre-check and in the job's own preflight -- whatever the protocol limits
    would allow."""
    from app.services.notebook_transfer.import_ import (
        NotebookImportService,
        NotebookImportTooLarge,
    )

    world.monkeypatch.setattr(world.repo.settings, "notebook_transfer_web_max_mb", 1)
    world.monkeypatch.setattr(jobs_module, "WEB_IMPORT_MAX_INFLATION", 2)
    assert world.jobs.import_inflation_bound() == 2 * MIB
    body = _inflating_archive(3 * MIB)
    assert len(body) < MIB, len(body)
    opened: list[str] = []
    real_open = zipfile.ZipFile.open

    def refuse_open(self, name, *args, **kwargs):
        opened.append(str(getattr(name, "filename", name)))
        raise AssertionError("an entry was decompressed")

    world.monkeypatch.setattr(zipfile.ZipFile, "open", refuse_open)
    refused = upload(world, body, world.other)
    assert refused.status_code == 413, refused.text
    assert "命令行" in refused.json()["detail"]
    assert opened == []
    assert [f for f in world.transfer_files() if f.startswith("imports/")] == []
    path = world.tmp / "inflating.snbk"
    path.write_bytes(body)
    with pytest.raises(NotebookImportTooLarge):
        world.repo._runtime.notebook_import.import_notebook(
            path, owner_id=world.other_id, max_inflated_bytes=2 * MIB
        )
    assert opened == []
    world.monkeypatch.setattr(zipfile.ZipFile, "open", real_open)

    # The job passes the same bound to the importer's preflight.
    seen: list = []
    real_import = NotebookImportService.import_notebook

    def recording(self, *args, **kwargs):
        seen.append(kwargs.get("max_inflated_bytes"))
        return real_import(self, *args, **kwargs)

    world.monkeypatch.setattr(jobs_module, "WEB_IMPORT_MAX_INFLATION", 100)
    world.monkeypatch.setattr(world.repo.settings, "notebook_transfer_web_max_mb", 512)
    world.monkeypatch.setattr(NotebookImportService, "import_notebook", recording)
    data = archive_bytes(world)
    assert upload(world, data).json()["state"] == "succeeded"
    assert seen == [100 * 512 * MIB]


def check_each_user_has_at_most_two_active_jobs(world: World) -> None:
    """Two active jobs per user; asking again to export a notebook whose export
    is active returns that job; a third job is 409 -- an import before its body
    is read; another user is not affected; a finished job frees its place."""
    from starlette.requests import Request

    world.monkeypatch.setattr(world.jobs, "_submit", lambda *args, **kwargs: None)
    first = export(world)
    assert first.status_code == 202 and first.json()["state"] == "queued"
    again = export(world)
    assert again.status_code == 202 and again.json() == first.json()
    other_notebook = second_notebook(world)
    assert export(world, notebook_id=other_notebook).status_code == 202
    third = export(world, notebook_id=second_notebook(world, "第三本"))
    assert third.status_code == 409, third.text
    assert str(jobs_module.MAX_ACTIVE_JOBS_PER_USER) in third.json()["detail"]

    def no_stream(self):
        raise AssertionError("the body of a refused import was read")

    with world.monkeypatch.context() as patched:
        patched.setattr(Request, "stream", no_stream)
        busy = upload(world, b"PK not read")
    assert busy.status_code == 409, busy.text
    assert [f for f in world.transfer_files() if f.startswith("imports/")] == []
    assert world.client.get("/api/notebook-transfers", headers=world.other).json() == {"jobs": []}
    assert export(world, world.other).status_code == 404  # not the other's notebook
    assert world.client.delete(
        f"/api/notebook-transfers/{first.json()['id']}", headers=world.owner).status_code == 200
    assert export(world).status_code == 202


def check_a_finished_export_supersedes_the_older_ones(world: World) -> None:
    older = exported(world)
    other = exported(world, notebook_id=second_notebook(world))
    newer = exported(world)
    assert newer["id"] != older["id"]
    exports = world.storage / "transfers" / "exports"
    assert world.client.get(
        f"/api/notebook-transfers/{older['id']}", headers=world.owner).status_code == 404
    assert not (exports / f"{older['id']}.snbk").exists()
    for kept in (newer, other):
        assert world.client.get(
            f"/api/notebook-transfers/{kept['id']}", headers=world.owner).status_code == 200
        assert (exports / f"{kept['id']}.snbk").exists()


def check_a_download_closes_its_file_and_counts_only_when_sent_whole(world: World) -> None:
    import gc

    from app.api import notebook_transfer_routes as routes

    job = exported(world)
    opened: list[int] = []
    real_redeem = world.jobs.redeem_ticket

    def recording(token, **kwargs):
        download = real_redeem(token, **kwargs)
        if download is not None:
            opened.append(download.fd)
        return download

    world.monkeypatch.setattr(world.jobs, "redeem_ticket", recording)

    def token() -> str:
        return ticket(world, job["id"]).json()["url"].split("ticket=", 1)[1]

    def is_open(fd: int) -> bool:
        try:
            os.fstat(fd)
        except OSError:
            return False
        return True

    # A response whose body never starts (the client went away first).
    response = routes.download_notebook_transfer(ticket=token())
    assert response.status_code == 200 and is_open(opened[-1])
    del response
    gc.collect()
    assert not is_open(opened[-1]), "the file descriptor leaked"
    # A body cut off after its first block.
    world.monkeypatch.setattr(routes, "UPLOAD_WRITE_BYTES", 1024)
    import asyncio

    async def first_block_only(response) -> bytes:
        body = response.body_iterator
        block = await body.__anext__()
        await body.aclose()
        return block

    response = routes.download_notebook_transfer(ticket=token())
    assert len(asyncio.run(first_block_only(response))) == 1024
    del response
    gc.collect()
    assert not is_open(opened[-1])
    assert world.jobs.job(world.owner_id, job["id"])["downloaded"] is False
    # Sent whole: now it counts.
    assert world.client.get(ticket(world, job["id"]).json()["url"]).status_code == 200
    assert world.jobs.job(world.owner_id, job["id"])["downloaded"] is True


def check_a_published_import_survives_a_restart_and_a_failure(world: World) -> None:
    """The new notebook id is on the sidecar from the moment of publish: a crash
    after it is reported as an import that happened (do not import again), with
    the notebook id; a failure after it ends the job ``failed`` but keeps the id;
    DELETE says the notebook was kept."""
    from app.services.notebook_transfer.jobs import NotebookTransferJobs

    data = archive_bytes(world)
    jobs_dir = world.storage / "transfers" / "jobs"
    snapshot: dict = {}
    real_progress = jobs_module.NotebookTransferJobs._import_progress

    def watching(self, job, event, written):
        real_progress(self, job, event, written)
        if event.stage == "project":
            snapshot["sidecar"] = (jobs_dir / f"{job.id}.json").read_text("utf-8")
            snapshot["id"] = job.id

    world.monkeypatch.setattr(jobs_module.NotebookTransferJobs, "_import_progress", watching)
    world.monkeypatch.setattr(jobs_module, "_import_report", lambda report: 1 / 0)
    failed = upload(world, data).json()
    assert failed["state"] == "failed", failed
    assert failed["error"]["code"] == "import_followup_failed"
    new_id = failed["notebook_id"]
    assert new_id and world.client.get(
        f"/api/notebooks/{new_id}", headers=world.owner).status_code == 200
    # The process died right after publish: the sidecar as it was then.
    import json as _json

    assert _json.loads(snapshot["sidecar"])["notebook_id"] == new_id
    (jobs_dir / f"{snapshot['id']}.json").write_text(snapshot["sidecar"], "utf-8")
    runtime = world.repo._runtime
    successor = NotebookTransferJobs(
        settings=world.repo.settings, storage_dir=lambda: runtime.storage_dir,
        export_rows=runtime.sharing_store, embeddings=lambda: {}, object_schemas=lambda: [],
        importer=lambda: runtime.notebook_import, submit=inline_submit,
        notify=lambda user_id: None, timer=lambda delay, action: FakeTimer(delay, action),
    )
    successor._clock = world.clock
    assert successor.recover_on_start() == 1
    recovered = successor.job(world.owner_id, snapshot["id"])
    assert recovered["error"]["code"] == "import_followup_interrupted", recovered["error"]
    assert "不要重新导入" in recovered["error"]["message"]
    assert recovered["notebook_id"] == new_id
    assert successor.delete(world.owner_id, snapshot["id"]) == {
        "outcome": "deleted", "kept_notebook_id": new_id}
    deleted = world.client.delete(f"/api/notebook-transfers/{failed['id']}", headers=world.owner)
    assert deleted.json() == {"id": failed["id"], "outcome": "deleted", "kept_notebook_id": new_id}


def check_every_run_ends_in_a_terminal_state(world: World) -> None:
    from app.services.notebook_reembed import NotebookReembedService, ReembedStatus

    with world.monkeypatch.context() as patched:
        patched.setattr(jobs_module, "_export_report", lambda report: 1 / 0)
        job = export(world).json()
    assert (job["state"], job["error"]["code"]) == ("failed", "export_failed"), job
    data = archive_bytes(world)
    waited: list = []

    def still_running(run_id, timeout=None):
        waited.append(timeout)
        return ReembedStatus(run_id=run_id, notebook_id="", state="running", legs=(),
                             progress={}, passes=1, error="")

    world.monkeypatch.setattr(NotebookReembedService, "wait", staticmethod(still_running))
    imported = upload(world, data).json()
    assert waited == [jobs_module.REEMBED_WAIT_SECONDS]
    assert imported["state"] == "succeeded", imported
    assert imported["index"] == {"state": "skipped", "reason": "reembed_still_running"}


def check_the_expiry_timer_has_one_live_arming(world: World) -> None:
    exported(world)
    sweeps: list[int] = []
    real_sweep = world.jobs.sweep
    world.monkeypatch.setattr(world.jobs, "sweep", lambda: sweeps.append(1) or real_sweep())
    (first,) = world.timers
    # An earlier due supersedes (and cancels) the armed one.
    world.jobs._schedule_sweep(world.clock() + 5)
    second = world.timers[-1]
    assert first.cancelled and not second.cancelled
    first.action()  # a stale arming firing anyway does nothing
    assert sweeps == []
    second.action()
    assert sweeps == [1]
    third = world.timers[-1]
    assert third is not second, "the firing arms the next expiry"
    second.action()  # firing twice does not form a second chain
    assert sweeps == [1] and world.timers[-1] is third
    world.repo._runtime.notebook_transfers.close()
    assert third.cancelled
    third.action()
    assert sweeps == [1]


def check_reads_never_scan_the_directories(world: World) -> None:
    job = exported(world)

    def no_scan(*args, **kwargs):
        raise AssertionError("a read scanned the transfer directories")

    world.monkeypatch.setattr(world.jobs, "_sweep_files", no_scan)
    base = f"/api/notebook-transfers/{job['id']}"
    assert world.client.get(base, headers=world.owner).status_code == 200
    assert world.client.get("/api/notebook-transfers", headers=world.owner).status_code == 200
    assert world.client.get("/api/me/pending-actions", headers=world.owner).status_code == 200
    url = ticket(world, job["id"]).json()["url"]
    assert world.client.get(url).status_code == 200


# ------------------------------------------------ second review follow-ups


async def _asgi_upload(world: World, headers: dict, receive) -> tuple[int, bytes]:
    """One ``POST /api/notebook-imports`` driven through the ASGI app with the
    caller's ``receive`` (so several can be in flight on one event loop)."""
    messages: list[dict] = []

    async def send(message: dict) -> None:
        messages.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": "/api/notebook-imports",
        "raw_path": b"/api/notebook-imports", "root_path": "", "query_string": b"",
        "headers": [
            (b"host", b"testserver"), (b"authorization", headers["Authorization"].encode()),
            (b"content-type", b"application/zip"),
        ],
        "client": ("testclient", 50000), "server": ("testserver", 80),
    }
    await world.client.app(scope, receive, send)
    start = next(m for m in messages if m["type"] == "http.response.start")
    return start["status"], b"".join(
        m.get("body", b"") for m in messages if m["type"] == "http.response.body"
    )


def check_concurrent_uploads_hold_their_slots(world: World) -> None:
    """Two uploads still being received hold both of the user's slots: a third,
    sent at the same time, is refused before a byte of its body is read; once
    the two fail, their slots are free again."""
    import asyncio

    async def scenario() -> tuple:
        receiving = [asyncio.Event(), asyncio.Event()]
        proceed = asyncio.Event()
        third_read: list[int] = []

        def slow(index: int):
            sent = False

            async def receive() -> dict:
                nonlocal sent
                if sent:
                    return {"type": "http.disconnect"}
                receiving[index].set()
                await proceed.wait()
                sent = True
                return {"type": "http.request", "body": b"not an archive", "more_body": False}

            return receive

        async def third_receive() -> dict:
            third_read.append(1)
            return {"type": "http.request", "body": b"PK", "more_body": False}

        first = asyncio.create_task(_asgi_upload(world, world.owner, slow(0)))
        second = asyncio.create_task(_asgi_upload(world, world.owner, slow(1)))
        for event in receiving:
            await asyncio.wait_for(event.wait(), 30)
        held = dict(world.jobs._reserved)
        third = await _asgi_upload(world, world.owner, third_receive)
        proceed.set()
        return held, third, third_read, await first, await second

    held, third, third_read, first, second = asyncio.run(scenario())
    assert sorted(held.values()) == [world.owner_id, world.owner_id]
    assert third[0] == 409 and third_read == [], (third, third_read)
    assert (first[0], second[0]) == (400, 400), (first, second)
    assert world.jobs._reserved == {}
    assert [f for f in world.transfer_files() if f.startswith("imports/")] == []
    assert upload(world, archive_bytes(world)).status_code == 202


def check_a_deleted_running_job_keeps_its_slot(world: World) -> None:
    """A deleted job whose run is still going counts against its user until the
    run ends: an export still writing, and an import whose notebook is published
    and whose re-embedding is still awaited."""
    from app.services.notebook_transfer import export as export_module

    data = archive_bytes(world)
    captured, start = _run_on_a_thread(world)
    entered, release = threading.Event(), threading.Event()
    real_check = export_module._ArchiveWriter._check

    def paused(self) -> None:
        if not entered.is_set():
            entered.set()
            assert release.wait(timeout=30)
        real_check(self)

    with world.monkeypatch.context() as patched:
        patched.setattr(export_module._ArchiveWriter, "_check", paused)
        running = export(world).json()
        thread = start()
        assert entered.wait(timeout=30)
        deleted = world.client.delete(
            f"/api/notebook-transfers/{running['id']}", headers=world.owner).json()
        assert deleted["outcome"] == "cancelled"
        assert export(world, notebook_id=second_notebook(world)).status_code == 202
        assert export(world, notebook_id=second_notebook(world, "第三本")).status_code == 409
        release.set()
        thread.join(timeout=60)
    assert world.jobs._draining == {}
    assert export(world, notebook_id=second_notebook(world, "第四本")).status_code == 202
    for job in list(world.jobs._jobs.values()):
        if job.state == "queued":
            world.jobs.delete(world.owner_id, job.id)
    captured.clear()

    # A published import waiting for its re-embedding run.
    imported = upload(world, data).json()
    start().join(timeout=60)
    live = world.jobs._jobs[imported["id"]]
    assert (live.state, live.phase) == ("running", "reembed"), (live.state, live.phase)
    deleted = world.client.delete(
        f"/api/notebook-transfers/{imported['id']}", headers=world.owner).json()
    assert deleted == {"id": imported["id"], "outcome": "kept",
                       "kept_notebook_id": live.notebook_id}
    assert export(world).status_code == 202
    assert export(world, notebook_id=second_notebook(world, "第五本")).status_code == 409
    start().join(timeout=120)  # the watcher: the run ends, the slot is free
    assert world.jobs._draining == {}
    assert export(world, notebook_id=second_notebook(world, "第六本")).status_code == 202


def check_delete_of_a_running_import_waits_for_its_outcome(world: World) -> None:
    """A DELETE that arrives while the import publishes cannot stop it: it waits
    and answers ``kept`` with the notebook. One that cannot wait long enough
    answers ``pending``; the job stays readable and ends ``cancelled``."""
    from app.services.notebook_transfer import jobs as module

    data = archive_bytes(world)
    _captured, start = _run_on_a_thread(world)
    store = world.repo._runtime.sharing_store
    entered, release = threading.Event(), threading.Event()
    real_publish = store.publish_import

    def publishing(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=30)
        return real_publish(*args, **kwargs)

    with world.monkeypatch.context() as patched:
        patched.setattr(store, "publish_import", publishing)
        # The answer comes when the run publishes, not when the wait runs out.
        patched.setattr(module, "DELETE_SETTLE_SECONDS", 600)
        running = upload(world, data).json()
        live = world.jobs._jobs[running["id"]]
        thread = start()
        assert entered.wait(timeout=30)
        answers: list = []
        deleting = threading.Thread(target=lambda: answers.append(world.client.delete(
            f"/api/notebook-transfers/{running['id']}", headers=world.owner)), daemon=True)
        deleting.start()
        assert live.cancel.wait(timeout=30)
        release.set()
        thread.join(timeout=60)
        deleting.join(timeout=30)
        assert not deleting.is_alive(), "DELETE did not settle when the import published"
    assert answers[0].json() == {
        "id": running["id"], "outcome": "kept", "kept_notebook_id": live.notebook_id,
    }, answers[0].json()
    assert world.client.get(
        f"/api/notebooks/{live.notebook_id}", headers=world.owner).status_code == 200
    _captured.clear()

    world.monkeypatch.setattr(module, "DELETE_SETTLE_SECONDS", 0.05)
    entered, release = threading.Event(), threading.Event()
    real_progress = module.NotebookTransferJobs._import_progress

    def paused(self, job, event, written):
        if event.stage == "rows" and not entered.is_set():
            entered.set()
            assert release.wait(timeout=30)
        return real_progress(self, job, event, written)

    world.monkeypatch.setattr(module.NotebookTransferJobs, "_import_progress", paused)
    slow = upload(world, data).json()
    thread = start()
    assert entered.wait(timeout=30)
    pending = world.client.delete(f"/api/notebook-transfers/{slow['id']}", headers=world.owner)
    assert pending.json() == {"id": slow["id"], "outcome": "pending", "kept_notebook_id": None}
    assert world.client.get(
        f"/api/notebook-transfers/{slow['id']}", headers=world.owner).json()["state"] == "running"
    release.set()
    thread.join(timeout=60)
    final = world.client.get(f"/api/notebook-transfers/{slow['id']}", headers=world.owner).json()
    assert (final["state"], final["notebook_id"] and final["phase"]) == ("cancelled", "rows")


def check_a_crash_right_after_publish_reads_as_imported(world: World) -> None:
    """The new notebook id is on the sidecar from the sentinel on; a restart
    decides by the database: a crash between the publish commit and the next
    event reads as imported (do not import again), one before publish -- the
    import compensated, the notebook gone -- as interrupted."""
    import json as _json

    from app.services.notebook_transfer.jobs import NotebookTransferJobs

    data = archive_bytes(world)
    jobs_dir = world.storage / "transfers" / "jobs"
    snapshots: dict = {}
    real_progress = jobs_module.NotebookTransferJobs._import_progress

    def watching(self, job, event, written):
        real_progress(self, job, event, written)
        if event.stage in ("files", "publish"):
            snapshots[(job.id, event.stage)] = (jobs_dir / f"{job.id}.json").read_text("utf-8")

    world.monkeypatch.setattr(jobs_module.NotebookTransferJobs, "_import_progress", watching)
    published = upload(world, data).json()
    assert published["state"] == "succeeded", published
    store = world.repo._runtime.sharing_store
    with world.monkeypatch.context() as patched:
        patched.setattr(store, "validate_import", lambda *args, **kwargs: 1 / 0)
        compensated = upload(world, data).json()
    assert compensated["state"] == "failed", compensated
    after_publish = snapshots[(published["id"], "publish")]
    before_publish = snapshots[(compensated["id"], "files")]
    assert _json.loads(after_publish)["notebook_id"] == published["notebook_id"]
    assert _json.loads(before_publish)["notebook_id"]
    (jobs_dir / f"{published['id']}.json").write_text(after_publish, "utf-8")
    (jobs_dir / f"{compensated['id']}.json").write_text(before_publish, "utf-8")
    runtime = world.repo._runtime
    successor = NotebookTransferJobs(
        settings=world.repo.settings, storage_dir=lambda: runtime.storage_dir,
        export_rows=runtime.sharing_store, embeddings=lambda: {}, object_schemas=lambda: [],
        importer=lambda: runtime.notebook_import, submit=inline_submit,
        notify=lambda user_id: None, timer=lambda delay, action: FakeTimer(delay, action),
    )
    successor._clock = world.clock
    assert successor.recover_on_start() == 2
    assert successor.job(world.owner_id, published["id"])["error"]["code"] == (
        "import_followup_interrupted")
    assert successor.job(world.owner_id, compensated["id"])["error"]["code"] == (
        "import_interrupted")


def check_admins_are_not_counted_and_tables_cannot_hide_more_than_exist(world: World) -> None:
    """An administrator's upload is admitted on its directory and manifest alone
    (no row is read for a count nobody uses; the inflation bound still holds); a
    forged archive with more Knowhow tables than sources is refused."""
    from app.services.notebook_transfer import archive_reader

    path = world.tmp / "plain.snbk"
    path.write_bytes(archive_bytes(world))
    world.sql("UPDATE users SET role = 'admin' WHERE id = ?", (world.other_id,))
    world.monkeypatch.setattr(world.jobs, "_submit", lambda *args, **kwargs: None)
    read: list[str] = []
    real_batches = archive_reader.ArchiveRowSource.batches

    def recording(self, table):
        read.append(table)
        return real_batches(self, table)

    with world.monkeypatch.context() as patched:
        patched.setattr(archive_reader.ArchiveRowSource, "batches", recording)
        admitted = upload(world, path.read_bytes(), world.other)
        assert admitted.status_code == 202, admitted.text
        assert read == []
        patched.setattr(jobs_module, "WEB_IMPORT_MAX_INFLATION", 0)
        refused = upload(world, path.read_bytes(), world.other)
        assert refused.status_code == 413, refused.text

    tables = archive_cases.archive_rows(path)
    sources = len(tables["sources"])
    (table,) = tables["knowhow_tables"][:1]
    forged = world.tmp / "many-tables.snbk"
    import_cases.rebuild(path, forged, rows=lambda name, rows: [
        {**table, "id": f"{table['id']}-{index}"} for index in range(sources + 1)
    ] if name == "knowhow_tables" else rows)
    with pytest.raises(NotebookImportError, match="Knowhow"):
        count(forged)
    assert upload(world, forged.read_bytes(), world.owner).status_code == 400
