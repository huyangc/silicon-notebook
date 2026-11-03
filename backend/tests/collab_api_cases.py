"""Observable collaboration contracts, shared unchanged by SQLite and PG."""
import io
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

from app.api import deps
from app.domain.collaboration import PulseActor
from app.services.notebook_activity_writer import _drain_activity_writers_for_tests
from tests.notebook_capability_matrix_cases import build_world


def world(client, postgres, monkeypatch):
    from app.services.knowhow import api

    monkeypatch.setattr(api, "get_scheduler", lambda repo: type("Scheduler", (), {"schedule": lambda *args: None})())
    result = build_world(client, postgres=postgres)
    _drain_activity_writers_for_tests()
    return result


def patch_cell(w, shape, content):
    return w.client.patch(
        f"/api/notebooks/{w.notebook}/knowhow/{w.table}/rows/{w.row}/cells/{w.column}",
        json={"content_md": content}, headers=w.headers[shape],
    )


def sql_spy(database, monkeypatch, observe=None):
    statements = []

    class Connection:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, sql, *args, **kwargs):
            statements.append(" ".join(str(sql).split()))
            if observe is not None:
                observe(statements[-1])
            return self.connection.execute(sql, *args, **kwargs)

        def executemany(self, sql, *args, **kwargs):
            statements.append(" ".join(str(sql).split()))
            return self.connection.executemany(sql, *args, **kwargs)

    for name in ("connect", "write"):
        original = getattr(database, name)

        def wrapped(*args, _original=original, **kwargs):
            @contextmanager
            def context():
                with _original(*args, **kwargs) as connection:
                    yield Connection(connection)
            return context()

        monkeypatch.setattr(database, name, wrapped)
    return statements


def detail_cursor_and_presence(client, postgres, monkeypatch):
    w = world(client, postgres, monkeypatch)
    rt = deps.repository()._runtime
    service = rt.collaboration
    summaries = client.get("/api/notebooks", headers=w.headers["owner"]).json()
    assert all(item.get("collab_pulse") is None and item.get("pulse_cursor") is None for item in summaries)
    assert client.get(f"/api/notebooks/{w.target_notebook}", headers=w.headers["owner"]).json()["collab_pulse"] is False
    get_notebook = rt.catalog.get_notebook

    def interleaved_read(*args, **kwargs):
        service.pulse.touch(w.notebook, "sources", PulseActor(w.users["editor"], "读取期间", "editor"))
        return get_notebook(*args, **kwargs)

    monkeypatch.setattr(rt.catalog, "get_notebook", interleaved_read)
    detail = client.get(f"/api/notebooks/{w.notebook}", headers=w.headers["owner"]).json()
    assert detail["collab_pulse"] is True
    poll = client.post(f"/api/notebooks/{w.notebook}/pulse", json={"since": detail["pulse_cursor"]}, headers=w.headers["owner"])
    assert poll.status_code == 200, poll.text
    assert poll.json()["reset"] is False
    assert [change["kind"] for change in poll.json()["changes"]] == ["sources"]
    for shape in ("editor", "group_viewer", "link_member"):
        assert client.post(f"/api/notebooks/{w.notebook}/pulse", json={}, headers=w.headers[shape]).status_code == 200
    state = client.post(f"/api/notebooks/{w.notebook}/pulse", json={}, headers=w.headers["owner"]).json()
    assert {viewer["user_id"] for viewer in state["viewers"]} == {w.users["owner"], w.users["editor"]}
    assert state["other_reader_count"] == 2
    client.post(f"/api/notebooks/{w.notebook}/pulse", json={"visible": False}, headers=w.headers["editor"])
    state = client.post(f"/api/notebooks/{w.notebook}/pulse", json={}, headers=w.headers["owner"]).json()
    assert [viewer["user_id"] for viewer in state["viewers"]] == [w.users["owner"]]
    assert client.post(f"/api/notebooks/{w.notebook}/pulse", json={}, headers=w.headers["stranger"]).status_code == 404


def pulse_exact_queries(client, postgres, monkeypatch):
    w = world(client, postgres, monkeypatch)
    rt = deps.repository()._runtime
    service = rt.collaboration
    calls = {"auth": 0, "access": 0, "wanted": 0}
    active = threading.local()
    attributed = {}
    for key, obj, name in (("auth", rt.identity, "resolve_session"), ("access", rt.sharing, "notebook_access_level"), ("wanted", rt.sharing, "collab_pulse_wanted")):
        original = getattr(obj, name)

        def record(*args, _key=key, _original=original, **kwargs):
            calls[_key] += 1
            previous = getattr(active, "category", "other")
            active.category = _key
            try:
                return _original(*args, **kwargs)
            finally:
                active.category = previous

        monkeypatch.setattr(obj, name, record)
    statements = sql_spy(rt.database, monkeypatch, observe=lambda sql: attributed.setdefault(getattr(active, "category", "other"), []).append(sql))
    for shape in ("owner", "editor", "group_viewer"):
        statements.clear()
        attributed.clear()
        calls.update(auth=0, access=0, wanted=0)
        response = client.post(f"/api/notebooks/{w.notebook}/pulse", json={}, headers=w.headers[shape])
        assert response.status_code == 200, response.text
        assert calls == {"auth": 1, "access": 1, "wanted": 0}
        assert len(attributed.get("access", [])) == 1, attributed
        assert attributed.get("auth"), attributed
        assert not attributed.get("other") and not attributed.get("wanted"), attributed
        assert len(statements) == len(attributed["auth"]) + 1
    calls.update(auth=0, access=0, wanted=0)
    assert client.get(f"/api/notebooks/{w.notebook}", headers=w.headers["owner"]).status_code == 200
    assert calls["wanted"] == 1
    calls["wanted"] = 0
    client.get("/api/notebooks", headers=w.headers["owner"])
    assert calls["wanted"] == 0
    assert service.pool_wait.wait_ms() >= 0
    clock = [0.0]
    stats = {"requests_num": 0, "requests_wait_ms": 0}
    monkeypatch.setattr(service.pool_wait, "_read_stats", lambda: stats)
    monkeypatch.setattr(service.pool_wait, "_clock", lambda: clock[0])
    service.pool_wait._samples.clear()
    service.pool_wait.wait_ms()
    clock[0] = 1.0
    stats.update(requests_num=1, requests_wait_ms=250)
    pressure = client.post(f"/api/notebooks/{w.notebook}/pulse", json={}, headers=w.headers["owner"])
    assert pressure.json()["next_interval_s"] == int(service.settings.collab_pulse_slow_intervals.split(",")[1])


def write_overhead_and_activity(client, postgres, monkeypatch):
    w = world(client, postgres, monkeypatch)
    rt = deps.repository()._runtime
    service = rt.collaboration
    statements = sql_spy(rt.database, monkeypatch)
    record = service.record
    for shape in ("owner", "editor"):
        monkeypatch.setattr(service, "record", lambda *args: None)
        statements.clear()
        assert patch_cell(w, shape, "基线").status_code == 200
        baseline = list(statements)
        monkeypatch.setattr(service, "record", record)
        statements.clear()
        assert patch_cell(w, shape, "保存").status_code == 200
        _drain_activity_writers_for_tests()
        observed = list(statements)
        insertions = [sql for sql in observed if "INSERT INTO notebook_activity" in sql]
        assert len(insertions) == (shape != "owner"), observed
        assert [sql for sql in observed if sql not in insertions] == baseline
    response = client.get(f"/api/notebooks/{w.notebook}/activity", headers=w.headers["editor"])
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert {item["actor_id"] for item in items} >= {w.users["owner"], w.users["editor"]}
    assert all(set(item) == {"id", "notebook_id", "kind", "capability", "actor_id", "actor_name", "target", "created_at"} for item in items)
    assert any(item["target"] == "PATCH /api/notebooks/{notebook_id}/knowhow/{table_id}/rows/{row_id}/cells/{column_id}" for item in items)
    assert client.get(f"/api/notebooks/{w.notebook}/activity", headers=w.headers["group_viewer"]).status_code == 404


def blocked_insert_is_fire_and_forget(client, postgres, monkeypatch):
    w = world(client, postgres, monkeypatch)
    service = deps.repository()._runtime.collaboration
    entered, release = threading.Event(), threading.Event()
    insert = service.store.insert_activity

    def blocked(row):
        entered.set()
        assert release.wait(10)
        insert(row)

    monkeypatch.setattr(service.store, "insert_activity", blocked)
    with ThreadPoolExecutor(max_workers=1) as requests:
        future = requests.submit(patch_cell, w, "editor", "响应无需等待审计")
        try:
            assert entered.wait(10)
            assert future.result(timeout=5).status_code == 200
            assert not release.is_set()
        finally:
            release.set()
            _drain_activity_writers_for_tests()
    assert any(row["actor_id"] == w.users["editor"] for row in service.store.list_recent_activity(w.notebook, 50))


def append_preview_does_not_touch(client, postgres, monkeypatch):
    from openpyxl import Workbook

    w = world(client, postgres, monkeypatch)
    service = deps.repository()._runtime.collaboration
    workbook = Workbook()
    workbook.active.append(["现象", "方法"])
    workbook.active.append(["掉电", "检查供电"])
    data = io.BytesIO()
    workbook.save(data)
    cursor = service.pulse.cursor()
    url = f"/api/notebooks/{w.notebook}/knowhow/{w.table}/append"
    for mode in ("preview", "commit"):
        result = client.post(url, data={"mode": mode}, files={"file": ("append.xlsx", data.getvalue())}, headers=w.headers["editor"])
        assert result.status_code == 200, result.text
        if mode == "preview":
            assert service.pulse.cursor() == cursor
        else:
            assert service.pulse.cursor() != cursor
    _drain_activity_writers_for_tests()
    assert len(service.store.list_recent_activity(w.notebook, 50)) == 1


def body_scoped_writes_and_revocation(client, postgres, monkeypatch):
    w = world(client, postgres, monkeypatch)
    service = deps.repository()._runtime.collaboration
    cursor = service.pulse.cursor()
    private = client.put(f"/api/notebooks/{w.notebook}/understanding/retrieval_notes", json={"scope": "mine", "value": "私人偏好", "expected_revision": 0}, headers=w.headers["editor"])
    assert private.status_code == 200, private.text
    assert service.pulse.cursor() == cursor
    shared = client.put(f"/api/notebooks/{w.notebook}/understanding/corpus_shape", json={"scope": "shared", "value": "共享底座", "expected_revision": 0}, headers=w.headers["editor"])
    assert shared.status_code == 200, shared.text
    assert service.pulse.cursor() != cursor
    cursor = service.pulse.cursor()
    patched = client.patch(f"/api/notebooks/{w.notebook}/knowledge/{w.knowledge}", json={"status": "deprecated"}, headers=w.headers["editor"])
    assert patched.status_code == 200, patched.text
    assert service.pulse.cursor() != cursor
    cursor = service.pulse.cursor()
    code = client.put(f"/api/agent/knowhow/rows/{w.row}/cells/{w.column}/code", json={"code_text": "x = 1", "language": "python"}, headers=w.headers["editor"])
    assert code.status_code == 200, code.text
    assert service.pulse.cursor() != cursor
    _drain_activity_writers_for_tests()
    items = service.store.list_recent_activity(w.notebook, 50)
    assert {item["kind"] for item in items} == {"understanding", "knowledge", "knowhow"}
    revoked = client.delete(f"/api/notebooks/{w.notebook}/collaborators/{w.users['editor']}", headers=w.headers["owner"])
    assert revoked.status_code == 204, revoked.text
    assert client.post(f"/api/notebooks/{w.notebook}/pulse", json={}, headers=w.headers["editor"]).status_code == 404


def repeated_capability_uses_one_probe_and_null_owner_is_authorized(client, postgres, monkeypatch):
    from fastapi import Depends, Request

    w = world(client, postgres, monkeypatch)
    guard = deps.require_notebook_capability("knowhow:write")
    rt = deps.repository()._runtime
    calls = []
    probe = rt.sharing.owner_if_can_edit

    def counted(*args):
        calls.append(args)
        return probe(*args)

    monkeypatch.setattr(rt.sharing, "owner_if_can_edit", counted)

    @client.app.get("/api/notebooks/{notebook_id}/collab-dedupe-check", dependencies=[Depends(guard), Depends(guard)])
    def dedupe(notebook_id: str, request: Request, one=Depends(guard)):
        return {"ok": one, "owner_id": request.state.collab_marks[0].owner_id}

    statement = "UPDATE notebooks SET created_by=NULL WHERE id=" + ("%s" if postgres else "?")
    with rt.database.write() as db:
        db.execute(statement, (w.notebook,))
    response = client.get(f"/api/notebooks/{w.notebook}/collab-dedupe-check", headers=w.headers["editor"])
    assert response.status_code == 200, response.text
    assert len(calls) == 1
    assert response.json() == {"ok": w.notebook, "owner_id": ""}


def knowledge_reader_branches_touch_only_after_committed_changes(client, postgres, monkeypatch):
    from types import SimpleNamespace

    from tests.memory_promotion_isolation_cases import _reader_with_memory

    w = world(client, postgres, monkeypatch)
    repo = deps.repository()
    service = repo._runtime.collaboration
    reader = _reader_with_memory(SimpleNamespace(client=client, repo=repo, nb=w.notebook, q=lambda sql: sql.replace("?", "%s") if postgres else sql))
    cursor = service.pulse.cursor()
    refused = client.patch(f"/api/notebooks/{w.notebook}/knowledge/{w.knowledge}", json={"status": "deprecated"}, headers=reader)
    assert refused.status_code == 404, refused.text
    assert service.pulse.cursor() == cursor
    merged = client.post(f"/api/notebooks/{w.notebook}/knowledge/ko-c-dup-2/merge", json={"into_id": "ko-c-dup-1"}, headers=reader)
    assert merged.status_code == 200, merged.text
    assert service.pulse.cursor() != cursor
    cursor = service.pulse.cursor()
    restored = client.patch(f"/api/notebooks/{w.notebook}/knowledge/ko-c-dup-2", json={"status": "approved"}, headers=reader)
    assert restored.status_code == 200, restored.text
    assert service.pulse.cursor() != cursor
    _drain_activity_writers_for_tests()
    items = service.store.list_recent_activity(w.notebook, 50)
    assert len(items) == 2 and {item["kind"] for item in items} == {"knowledge"}


def source_deletion_publishes_derived_hints_with_one_activity_insert(client, postgres, monkeypatch):
    from app.repositories.ports import UploadedSourceFile

    w = world(client, postgres, monkeypatch)
    repo = deps.repository()
    service = repo._runtime.collaboration
    statements = sql_spy(repo._runtime.database, monkeypatch)
    original_record = service.record
    for shape in ("owner", "group_admin"):
        baseline = None
        for enabled in (False, True):
            source = repo.upload_sources(w.notebook, [UploadedSourceFile(file_name=f"{shape}-{enabled}.md", content_type="text/markdown", content=f"{shape}-{enabled}".encode())], scheduler=lambda _: None)[0]
            repo.store_kg(w.notebook, source.id, [{"local_id": "node", "object_type": "concept", "payload": {"name": "Delete me"}, "evidence": []}], [])
            monkeypatch.setattr(service, "record", original_record if enabled else lambda *args: None)
            statements.clear()
            cursor = service.pulse.cursor()
            owner_before = {row["id"] for row in service.pulse.recent_owner_changes(w.notebook, w.users["owner"])}
            response = client.delete(f"/api/sources/{source.id}", headers=w.headers[shape])
            assert response.status_code == 204, response.text
            _drain_activity_writers_for_tests()
            observed = list(statements)
            if not enabled:
                baseline = observed
                continue
            inserts = [sql for sql in observed if "INSERT INTO notebook_activity" in sql]
            assert len(inserts) == (shape != "owner"), observed
            assert [sql for sql in observed if sql not in inserts] == baseline
            changes = service.pulse.poll(w.notebook, PulseActor("observer", "observer", "reader"), since=cursor, visible=True)["changes"]
            assert {change["kind"] for change in changes} == {"sources", "knowledge", "kg", "notebook"}
            owner_records = [row for row in service.pulse.recent_owner_changes(w.notebook, w.users["owner"]) if row["id"] not in owner_before]
            assert [row["kind"] for row in owner_records] == (["sources"] if shape == "owner" else [])
            cursor = service.pulse.cursor()
            assert client.delete(f"/api/sources/{source.id}", headers=w.headers[shape]).status_code == 404
            assert service.pulse.cursor() == cursor


def knowhow_delete_and_transfer_publish_the_changed_notebooks(client, postgres, monkeypatch):
    from app.services.knowhow.api import build_projector

    w = world(client, postgres, monkeypatch)
    repo = deps.repository()
    service = repo._runtime.collaboration
    store = repo._runtime.knowhow_store
    for operation in ("delete", "copy", "move"):
        table = store.create_knowhow_table(w.notebook, operation, "", [{"name": "现象", "role": "anchor"}, {"name": "方法", "role": "procedure"}])
        detail = store.get_knowhow_table(table)
        store.add_knowhow_row(table, {detail["columns"][0]["id"]: "振铃", detail["columns"][1]["id"]: "加阻尼"})
        build_projector(repo).project_table(table, embed=False)
        cursor = service.pulse.cursor()
        owner_before = {nb: {row["id"] for row in service.pulse.recent_owner_changes(nb, w.users["owner"])} for nb in (w.notebook, w.target_notebook)}
        if operation == "delete":
            response = client.delete(f"/api/notebooks/{w.notebook}/knowhow/{table}", headers=w.headers["group_admin"])
        else:
            response = client.post(f"/api/notebooks/{w.notebook}/knowhow/{table}/transfer", json={"target_notebook_id": w.target_notebook, "mode": operation}, headers=w.headers["owner"])
        assert response.status_code == (204 if operation == "delete" else 200), response.text
        _drain_activity_writers_for_tests()
        for notebook in (w.notebook, w.target_notebook):
            changes = service.pulse.poll(notebook, PulseActor("observer", "observer", "reader"), since=cursor, visible=True)["changes"]
            affected = (notebook == w.notebook and operation != "copy") or (notebook == w.target_notebook and operation != "delete")
            assert {change["kind"] for change in changes} == ({"knowhow", "knowledge", "kg", "notebook"} if affected else set())
            new_owner = [row for row in service.pulse.recent_owner_changes(notebook, w.users["owner"]) if row["id"] not in owner_before[notebook]]
            assert [row["kind"] for row in new_owner] == (["knowhow"] if affected and operation != "delete" else [])
        items = service.store.list_recent_activity(w.notebook, 50)
        assert len(items) == 1 and items[0]["kind"] == "knowhow"


def partially_committed_moves_publish_only_the_committed_notebooks(client, postgres, monkeypatch):
    from types import SimpleNamespace

    from app.services.knowhow import transfer
    from app.services.knowhow.api import build_projector
    from tests.notebook_capability_matrix_cases import _grant

    w = world(client, postgres, monkeypatch)
    monkeypatch.setattr(transfer, "get_scheduler", lambda repo: SimpleNamespace(schedule=lambda table: None))
    repo = deps.repository()
    rt, service = repo._runtime, repo._runtime.collaboration
    _grant(postgres, w.target_notebook, "user", w.users["group_admin"], "admin", "partial-target-admin")
    store = rt.knowhow_store
    for shape in ("owner", "group_admin"):
        for phase in ("unprojected", "before_projection", "projected", "partial_projection"):
            table = store.create_knowhow_table(w.notebook, f"{shape}-{phase}", "", [{"name": "现象", "role": "anchor"}, {"name": "方法", "role": "procedure"}])
            detail = store.get_knowhow_table(table)
            store.add_knowhow_row(table, {detail["columns"][0]["id"]: "振铃", detail["columns"][1]["id"]: "加阻尼"})
            hidden = None
            if phase != "unprojected":
                build_projector(repo).project_table(table, embed=False)
                hidden = store.get_knowhow_table(table)["hidden_source_id"]
                with rt.database.connect() as db:
                    assert db.execute("SELECT COUNT(*) c FROM chunks WHERE source_id=" + ("%s" if postgres else "?"), (hidden,)).fetchone()["c"] > 0
            before = {nb: len(service.store.list_recent_activity(nb, 50)) for nb in (w.notebook, w.target_notebook)}
            owner_before = {nb: {row["id"] for row in service.pulse.recent_owner_changes(nb, w.users["owner"])} for nb in before}
            fingerprint = rt.knowhow_transfer_store.table_fingerprint(table)
            cursor = service.pulse.cursor()

            def refuse_delete(*args, **kwargs):
                raise RuntimeError("source delete failed after target commit")

            with monkeypatch.context() as scoped:
                checks = {"owner": 0, "level": 0, "read": 0, "auth": 0, "name": 0}
                for label, owner, method in (("owner", rt.sharing, "owner_if_can_edit"), ("level", rt.sharing, "notebook_access_level"), ("read", rt.sharing, "user_can_read_notebook"), ("auth", rt.identity, "resolve_session"), ("name", rt.groups, "find_user_by_id")):
                    original = getattr(owner, method)

                    def counted(*args, _label=label, _original=original, **kwargs):
                        checks[_label] += 1
                        return _original(*args, **kwargs)

                    scoped.setattr(owner, method, counted)
                if phase == "before_projection":
                    real_copy = transfer.copy_table

                    def copy_then_source_changes(repo_, source_table, target, actor, **kwargs):
                        created = real_copy(repo_, source_table, target, actor, **kwargs)
                        store.add_knowhow_row(source_table, {detail["columns"][0]["id"]: "concurrent"})
                        return created

                    scoped.setattr(transfer, "copy_table", copy_then_source_changes)
                elif phase == "partial_projection":
                    real_delete = rt.knowledge.delete_relations_by_source

                    def fail_after_chunk_commit(db, source_id):
                        if source_id == hidden:
                            raise RuntimeError("graph transaction failed after chunk commit")
                        return real_delete(db, source_id)

                    scoped.setattr(rt.knowledge, "delete_relations_by_source", fail_after_chunk_commit)
                else:
                    scoped.setattr(rt.knowhow_transfer_store, "delete_table_if_unchanged", refuse_delete)
                response = client.post(f"/api/notebooks/{w.notebook}/knowhow/{table}/transfer", json={"target_notebook_id": w.target_notebook, "mode": "move"}, headers=w.headers[shape])
            assert checks == {"owner": 1, "level": 1, "read": 0, "auth": 1, "name": 0}, checks
            assert response.status_code == 409, response.text
            assert set(response.json()["detail"]) == {"code", "new_table_id", "message"}
            assert response.json()["detail"]["code"] == ("source_changed_kept" if phase == "before_projection" else "source_cleanup_failed")
            new_table = response.json()["detail"]["new_table_id"]
            assert store.get_knowhow_table(new_table)["notebook_id"] == w.target_notebook
            assert store.get_knowhow_table(table)["notebook_id"] == w.notebook
            assert (rt.knowhow_transfer_store.table_fingerprint(table) != fingerprint) == (phase == "before_projection")
            _drain_activity_writers_for_tests()
            expected_hints = 8 if phase in {"projected", "partial_projection"} else 4
            assert int(service.pulse.cursor().rsplit(":", 1)[1]) - int(cursor.rsplit(":", 1)[1]) == expected_hints
            for notebook in (w.notebook, w.target_notebook):
                changed = notebook == w.target_notebook or phase in {"projected", "partial_projection"}
                changes = service.pulse.poll(notebook, PulseActor("observer", "observer", "reader"), since=cursor, visible=True)["changes"]
                assert {change["kind"] for change in changes} == ({"knowhow", "knowledge", "kg", "notebook"} if changed else set())
                records = service.store.list_recent_activity(notebook, 50)
                assert len(records) - before[notebook] == int(shape != "owner" and notebook == w.target_notebook)
                new_owner = [row for row in service.pulse.recent_owner_changes(notebook, w.users["owner"]) if row["id"] not in owner_before[notebook]]
                assert [row["kind"] for row in new_owner] == (["knowhow"] if shape == "owner" and notebook == w.target_notebook else [])
            if hidden is not None:
                with rt.database.connect() as db:
                    remaining = db.execute("SELECT COUNT(*) c FROM chunks WHERE source_id=" + ("%s" if postgres else "?"), (hidden,)).fetchone()["c"]
                    assert (remaining > 0) if phase == "before_projection" else remaining == 0
        # The insert transaction itself refuses before COMMIT: no hints and
        # no target row may be manufactured from this different failure.
        build_projector(repo).project_table(w.table, embed=False)
        cursor = service.pulse.cursor()
        real_insert = rt.knowhow_transfer_store.insert_transfer
        with rt.database.connect() as db:
            before_tables = db.execute("SELECT COUNT(*) c FROM knowhow_tables WHERE notebook_id=" + ("%s" if postgres else "?"), (w.target_notebook,)).fetchone()["c"]
        with monkeypatch.context() as scoped:
            def refuse_insert(payload, expected_counts, **kwargs):
                payload["source"]["source_type"] = "memory"
                return real_insert(payload, expected_counts, **kwargs)

            scoped.setattr(rt.knowhow_transfer_store, "insert_transfer", refuse_insert)
            response = client.post(f"/api/notebooks/{w.notebook}/knowhow/{w.table}/transfer", json={"target_notebook_id": w.target_notebook, "mode": "copy"}, headers=w.headers[shape])
        assert response.status_code == 409, response.text
        assert service.pulse.cursor() == cursor
        with rt.database.connect() as db:
            assert db.execute("SELECT COUNT(*) c FROM knowhow_tables WHERE notebook_id=" + ("%s" if postgres else "?"), (w.target_notebook,)).fetchone()["c"] == before_tables


def last_editor_self_exit_notifies_owner_only_after_removing_the_grant(client, postgres, monkeypatch):
    from tests.memory_purge_cases import wire_world

    w = world(client, postgres, monkeypatch)
    repo = deps.repository()
    wire_world(repo)
    service = repo._runtime.collaboration
    owner_reads = []
    original_owner = repo._runtime.identity.notebook_owner

    def read_owner(notebook):
        owner_reads.append(notebook)
        return original_owner(notebook)

    monkeypatch.setattr(repo._runtime.identity, "notebook_owner", read_owner)
    borrowed = client.post("/api/notebooks", json={"name": "借用库"}, headers=w.headers["stranger"]).json()["id"]
    repo._runtime.sharing.add_member(borrowed, w.users["owner"])
    for with_memory in (False, True):
        private = client.post("/api/notebooks", json={"name": "退出后恢复引用"}, headers=w.headers["owner"]).json()["id"]
        assert client.put(f"/api/notebooks/{private}/bases", json={"base_notebook_ids": [borrowed]}, headers=w.headers["owner"]).status_code == 200

        def active_reference():
            response = client.get(f"/api/notebooks/{private}/bases", headers=w.headers["owner"])
            assert response.status_code == 200
            return next(item["active"] for item in response.json() if item["id"] == borrowed)

        assert active_reference()
        route = f"/api/notebooks/{private}/collaborators/{w.users['editor']}"
        assert client.put(route, headers=w.headers["owner"]).status_code == 200
        assert not active_reference()
        if with_memory:
            repo._runtime.memory_service.create_candidate(private, w.users["editor"], None, "req-exit", "记忆", "Body.", [], "reason", {}, [])
        _drain_activity_writers_for_tests()
        before = len(service.store.list_recent_activity(private, 50))
        owner_before = service.pulse.recent_owner_changes(private, w.users["owner"])
        cursor = service.pulse.cursor()
        if with_memory:
            owner_reads.clear()
            refused = client.delete(route, headers=w.headers["editor"])
            assert owner_reads == []
            assert refused.status_code == 409 and refused.json()["detail"]["code"] == "exit_disclosure_required"
            assert service.pulse.cursor() == cursor
            assert repo._runtime.groups.user_grant_role(private, w.users["editor"]) == "editor"
            assert not active_reference()
        owner_reads.clear()
        response = client.delete(route, params={"acknowledged_memory_count": 1} if with_memory else {}, headers=w.headers["editor"])
        assert owner_reads == []
        assert response.status_code == (200 if with_memory else 204), response.text
        if with_memory:
            assert response.json() == {"deleted_memory_count": 1, "deleted_suggestion_count": 0}
        assert repo._runtime.groups.user_grant_role(private, w.users["editor"]) is None
        assert active_reference()
        result = service.pulse.poll(private, PulseActor(w.users["owner"], "owner", "owner"), since=cursor, visible=True)
        assert [change["kind"] for change in result["changes"]] == ["notebook"]
        _drain_activity_writers_for_tests()
        records = service.store.list_recent_activity(private, 50)
        assert len(records) == before + 1
        assert records[0]["actor_id"] == w.users["editor"]
        assert service.pulse.recent_owner_changes(private, w.users["owner"]) == owner_before
        cursor = service.pulse.cursor()
        for notebook, acknowledged, status in ((private, None, 204), (private, 1, 409), ("missing-notebook", None, 204)):
            owner_reads.clear()
            retry = client.delete(f"/api/notebooks/{notebook}/collaborators/{w.users['editor']}", params={} if acknowledged is None else {"acknowledged_memory_count": acknowledged}, headers=w.headers["editor"])
            assert owner_reads == []
            assert retry.status_code == status, retry.text
            assert service.pulse.cursor() == cursor
        _drain_activity_writers_for_tests()
        assert len(service.store.list_recent_activity(private, 50)) == before + 1

    # A different request removes or replaces the claimed edge before this
    # finish. A successful stale claim must not attribute that change here.
    memory_store = repo._runtime.memory_store
    for replace in (False, True):
        assert client.put(route, headers=w.headers["owner"]).status_code == 200
        _drain_activity_writers_for_tests()
        cursor = service.pulse.cursor()
        before = len(service.store.list_recent_activity(private, 50))
        with monkeypatch.context() as scoped:
            original = memory_store.finish_grant_exit

            def supersede_claim(nb, actor, token, **kwargs):
                assert repo._runtime.groups.delete_user_grant(nb, actor)
                if replace:
                    repo._runtime.groups.set_user_editor_grant(nb, owner_id=w.users["owner"], target_user_id=actor)
                completed = original(nb, actor, token, **kwargs)
                assert not completed.ended
                return completed

            scoped.setattr(memory_store, "finish_grant_exit", supersede_claim)
            owner_reads.clear()
            response = client.delete(route, headers=w.headers["editor"])
            assert owner_reads == []
        assert response.status_code == 204, response.text
        assert (repo._runtime.groups.user_grant_role(private, w.users["editor"]) == "editor") == replace
        assert service.pulse.cursor() == cursor
        _drain_activity_writers_for_tests()
        assert len(service.store.list_recent_activity(private, 50)) == before

    # The inverse race: the initial permission read sees no edge, then an
    # owner grants access before the real claim. This request does end that
    # new edge and must publish even though its earlier read returned None.
    assert repo._runtime.groups.delete_user_grant(private, w.users["editor"])
    owner_reads.clear()
    cursor = service.pulse.cursor()
    before = len(service.store.list_recent_activity(private, 50))
    marks = []
    with monkeypatch.context() as scoped:
        original_access = repo._runtime.sharing.notebook_access_level
        original_record = service.record

        def grant_after_denied_read(nb, actor):
            access = original_access(nb, actor)
            assert access is None
            repo._runtime.groups.set_user_editor_grant(nb, owner_id=w.users["owner"], target_user_id=actor)
            return access

        scoped.setattr(repo._runtime.sharing, "notebook_access_level", grant_after_denied_read)

        def record_exit(mark, *args):
            marks.append(mark)
            return original_record(mark, *args)

        scoped.setattr(service, "record", record_exit)
        response = client.delete(route, headers=w.headers["editor"])
    assert response.status_code == 204, response.text
    assert owner_reads == [private]
    assert repo._runtime.groups.user_grant_role(private, w.users["editor"]) is None
    assert active_reference()
    result = service.pulse.poll(private, PulseActor(w.users["owner"], "owner", "owner"), since=cursor, visible=True)
    assert [change["kind"] for change in result["changes"]] == ["notebook"]
    _drain_activity_writers_for_tests()
    records = service.store.list_recent_activity(private, 50)
    assert len(records) == before + 1 and records[0]["actor_id"] == w.users["editor"]
    assert len(marks) == 1 and marks[0].owner_id == w.users["owner"] and marks[0].actor.user_id == w.users["editor"]

    # Recovery is an observer read after commit: its failure cannot turn a
    # genuinely completed exit into a failed HTTP operation.
    cursor = service.pulse.cursor()
    with monkeypatch.context() as scoped:
        scoped.setattr(repo._runtime.sharing, "notebook_access_level", grant_after_denied_read)
        scoped.setattr(repo._runtime.identity, "notebook_owner", lambda _: (_ for _ in ()).throw(RuntimeError("observer unavailable")))
        response = client.delete(route, headers=w.headers["editor"])
    assert response.status_code == 204, response.text
    assert repo._runtime.groups.user_grant_role(private, w.users["editor"]) is None
    assert service.pulse.cursor() == cursor


CASES = {case.__name__: case for case in (detail_cursor_and_presence, pulse_exact_queries, write_overhead_and_activity, blocked_insert_is_fire_and_forget, append_preview_does_not_touch, body_scoped_writes_and_revocation, repeated_capability_uses_one_probe_and_null_owner_is_authorized, knowledge_reader_branches_touch_only_after_committed_changes, source_deletion_publishes_derived_hints_with_one_activity_insert, knowhow_delete_and_transfer_publish_the_changed_notebooks, partially_committed_moves_publish_only_the_committed_notebooks, last_editor_self_exit_notifies_owner_only_after_removing_the_grant)}
