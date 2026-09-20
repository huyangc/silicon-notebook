"""Bounded global admission and request-level authority/query behavior."""
from contextlib import contextmanager
from threading import Barrier, Event, Thread
import hashlib
import time

import pytest

from app.models.global_ask import GlobalAskRequest
from app.services.global_ask import GlobalAskError
from tests.test_global_ask import setup, finished, _stage_job_threads


def test_max_scope_notebooks_use_one_batch_per_authority_boundary(setup, monkeypatch):
    service, readable, _, _ = setup
    # The product ceiling is 8 participants, so a full-scope request is the
    # widest authority batch this boundary can ever be asked for.
    readable.update(f"library-{index}" for index in range(6))
    calls, frozen, workers = [], [], []
    service.notebooks = lambda user: {nb: nb for nb in sorted(readable)}
    service.can_read_many = lambda ids, user: calls.append(tuple(ids)) or readable.intersection(ids)
    service.can_read = lambda *args: pytest.fail("global authority must use batch lookup")
    service.sources.visible_source_ids_by_notebook = lambda ids: frozen.append(tuple(ids)) or {
        nb: [f"s-{nb}"] for nb in ids
    }
    monkeypatch.setattr("app.services.global_ask.threading.Thread.start",
                        _stage_job_threads(workers))
    job = service.start(GlobalAskRequest(question="compare"), user_id="u")
    assert len(job.resolved_notebook_ids) == 8
    assert len(calls) == 2 and all(len(ids) == 8 for ids in calls)
    assert len(frozen) == 1 and len(frozen[0]) == 8
    workers.pop().run()
    assert finished(service, job).status == "done"
    calls.clear()
    service.get_job(job.job_id, user_id="u")
    assert len(calls) == 1
    calls.clear()
    service.conversation(job.conversation_id, user_id="u")
    assert len(calls) == 1 and len(calls[0]) == 8


def test_history_and_conversation_recheck_union_once_and_exclude_failed_turns(setup):
    service, readable, _, syntheses = setup
    first = service.start(GlobalAskRequest(question="question one"), user_id="u")
    finished(service, first)
    successful = service.ask.synthesize
    service.ask.synthesize = lambda *args: (_ for _ in ()).throw(ValueError("failed"))
    failed = service.start(GlobalAskRequest(question="failed question", conversation_id=first.conversation_id), user_id="u")
    assert finished(service, failed).status == "failed"
    service.ask.synthesize = successful
    second = service.start(GlobalAskRequest(question="question two", conversation_id=first.conversation_id), user_id="u")
    finished(service, second)
    assert "question one" in syntheses[-1][3]
    assert "Assistant: answer" in syntheses[-1][3]
    assert "failed question" not in syntheses[-1][3]
    calls = []
    service.can_read_many = lambda ids, user: calls.append(tuple(ids)) or readable.intersection(ids)
    history, user_history = service._history(
        service.store.conversation(first.conversation_id, "u"), readable, "u", None, None,
    )
    assert "question two" in history and len(calls) == 1
    # 只含提问行的那一半:跟进改写的取数口永远看不到助手正文。
    assert "answer" not in user_history and "question two" in user_history
    calls.clear()
    assert len(service.conversation(first.conversation_id, user_id="u").turns) == 3
    assert len(calls) == 1


def test_participant_limit_rejects_whole_scope_before_source_freeze(setup):
    service, readable, _, _ = setup
    service.settings.global_ask_max_notebooks = 2
    readable.add("c")
    service.sources.visible_source_ids_by_notebook = lambda ids: pytest.fail("over-limit scope cannot read sources")
    with pytest.raises(GlobalAskError) as error:
        service.start(GlobalAskRequest(question="q"), user_id="u")
    assert error.value.status_code == 422
    assert not service.list_conversations(user_id="u")


def test_admission_bounds_new_conversations_and_preserves_idempotent_retry(setup):
    service, _, _, _ = setup
    service.settings.global_ask_max_concurrent = 1
    entered, release = Event(), Event()

    def synthesis(*args):
        entered.set()
        assert release.wait(5)
        return "answer", False, [], []

    service.ask.synthesize = synthesis
    request = GlobalAskRequest(question="q", client_request_id="retry")
    job = service.start(request, user_id="u")
    try:
        assert entered.wait(5)
        with pytest.raises(GlobalAskError) as error:
            service.start(GlobalAskRequest(question="another conversation"), user_id="u")
        assert error.value.status_code == 429
        assert service.start(request, user_id="u").job_id == job.job_id
        assert len(service.list_conversations(user_id="u")) == 1
    finally:
        release.set()
    finished(service, job)
    assert finished(service, service.start(GlobalAskRequest(question="after release"), user_id="u")).status == "done"


def test_concurrent_request_id_reuse_with_different_payload_returns_conflict(setup, monkeypatch):
    service, _, _, _ = setup
    checked = Barrier(2)
    request_job = service.store.request_job

    def simultaneous_initial_lookup(user_id, request_id):
        previous = request_job(user_id, request_id)
        if previous is None:
            checked.wait(timeout=5)
        return previous

    monkeypatch.setattr(service.store, "request_job", simultaneous_initial_lookup)
    jobs, errors = [], []

    def submit(question):
        try:
            jobs.append(service.start(
                GlobalAskRequest(question=question, client_request_id="shared-request"), user_id="u",
            ))
        except Exception as error:
            errors.append(error)

    submitters = [Thread(target=submit, args=(question,)) for question in ("first question", "different question")]
    for submitter in submitters:
        submitter.start()
    for submitter in submitters:
        submitter.join(10)
    assert all(not submitter.is_alive() for submitter in submitters)
    assert len(jobs) == len(errors) == 1
    assert isinstance(errors[0], GlobalAskError)
    assert errors[0].status_code == 409
    assert errors[0].message == "请求标识已用于其他问题，请重新提交。"
    assert finished(service, jobs[0]).status == "done"
    assert len(service.list_conversations(user_id="u")) == 1


def test_slow_scope_preparation_does_not_block_cancellation(setup):
    service, _, _, _ = setup
    synthesis_entered, synthesis_release = Event(), Event()
    listing_entered, listing_release = Event(), Event()

    def synthesis(*args):
        synthesis_entered.set()
        assert synthesis_release.wait(5)
        return "answer", False, [], []

    service.ask.synthesize = synthesis
    first = service.start(GlobalAskRequest(question="first"), user_id="u")
    assert synthesis_entered.wait(5)
    notebooks = service.notebooks

    def slow_notebooks(user):
        listing_entered.set()
        assert listing_release.wait(5)
        return notebooks(user)

    service.notebooks = slow_notebooks
    results = []
    starter = Thread(target=lambda: results.append(service.start(GlobalAskRequest(question="second"), user_id="u")))
    starter.start()
    try:
        assert listing_entered.wait(5)
        assert service.cancel(first.job_id, user_id="u").status == "cancelled"
    finally:
        listing_release.set()
        synthesis_release.set()
        starter.join(5)
    assert len(results) == 1
    finished(service, results[0])


def test_close_has_one_total_deadline_and_prevents_late_writes_or_new_admission(setup, monkeypatch):
    service, _, _, _ = setup
    service.settings.global_ask_shutdown_timeout_seconds = 0.01
    entered, release = Event(), Event()
    late_writes = []

    def synthesis(*args):
        entered.set()
        assert release.wait(5)
        return "late", False, [], []

    service.ask.synthesize = synthesis
    job = service.start(GlobalAskRequest(question="q"), user_id="u")
    try:
        assert entered.wait(5)
        before = time.monotonic()
        service.close()
        assert time.monotonic() - before < 1
        assert not release.is_set()
        with pytest.raises(GlobalAskError) as error:
            service.start(GlobalAskRequest(question="after shutdown"), user_id="u")
        assert error.value.status_code == 503
        monkeypatch.setattr(service.store, "save", lambda *args: late_writes.append(args) or False)
    finally:
        release.set()
    deadline = time.monotonic() + 5
    while job.job_id in service._events and time.monotonic() < deadline:
        Event().wait(0.01)
    assert job.job_id not in service._events
    assert not late_writes
    assert service.store.job(job.job_id, "u").status == "running"
    service.store.recover()
    assert service.store.job(job.job_id, "u").status == "interrupted"


def test_sqlite_narrow_access_and_source_freeze_use_three_queries_for_twenty_four_notebooks(setup, monkeypatch):
    service, _, _, _ = setup
    database = service.store.database
    # The fixture's repository shares these concrete stores with the service database.
    from app.repositories.sqlite.sharing_store import SharingStore
    from app.repositories.sqlite.source_store import SourceStore

    with database.write() as db:
        owner = db.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
        for index in range(24):
            db.execute(
                "INSERT INTO notebooks(id,name,purpose,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (f"nb-{index}", f"Library {index}", "", owner, "2026-09-19", "2026-09-19"),
            )
            for kind in ("markdown", "memory"):
                db.execute(
                    "INSERT INTO sources(id,notebook_id,title,source_type,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (f"source-{index}-{kind}", f"nb-{index}", "Original", kind, "2026-09-19", "2026-09-19"),
                )
        db.execute(
            "INSERT INTO source_elements(id,source_id,element_type,location_label,text,created_at) VALUES(?,?,?,?,?,?)",
            ("element", "source-0-markdown", "paragraph", "p1", "original evidence", "2026-09-19"),
        )
    # Bypass constructors' unrelated copy dependencies; these read methods only own a database.
    sharing = object.__new__(SharingStore)
    sharing.database = database
    sources = object.__new__(SourceStore)
    sources.database = database
    statements = []
    original = database.connect

    @contextmanager
    def traced():
        with original() as db:
            db.set_trace_callback(statements.append)
            try:
                yield db
            finally:
                db.set_trace_callback(None)

    monkeypatch.setattr(database, "connect", traced)
    names = sharing.readable_notebook_names(owner)
    ids = sharing.readable_notebook_ids(list(names), owner)
    ceilings = sources.visible_source_ids_by_notebook(list(ids))
    assert len(names) == len(ids) == len(ceilings) == 24
    assert all(values == [f"source-{nb.removeprefix('nb-')}-markdown"] for nb, values in ceilings.items())
    assert len([statement for statement in statements if statement.lstrip().upper().startswith("SELECT")]) == 3
    statements.clear()
    fingerprint = sources.evidence_fingerprints(["element"])
    assert fingerprint["element"][0] == "source-0-markdown"
    assert len(statements) == 1 and "metadata" not in statements[0]
    statements.clear()
    with database.write() as db:
        db.execute("UPDATE source_elements SET metadata=? WHERE id=?", ('{"image":"enhanced"}', "element"))
    assert sources.evidence_fingerprints(["element"]) == fingerprint
    with database.write() as db:
        db.execute("UPDATE source_elements SET text=? WHERE id=?", ("changed evidence", "element"))
    assert sources.evidence_fingerprints(["element"]) != fingerprint
    assert sharing.readable_notebook_ids(list(ids), "unrelated-user") == set()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def test_sqlite_passage_snapshot_reads_text_and_element_prints_in_one_statement(setup, monkeypatch):
    """一条语句、一个快照里同时给出段落原文摘要与它每个元素的指纹。

    这正是 ``evidence_fingerprints`` 单独读做不到的那件事:元素 id 是按
    ``(来源, 序号)`` 确定性复用的,所以「这个 id 现在的文字」并不等于「这次检索读到
    的那段原文」。两半必须同快照,消费者才能先核对原文、再采信指纹。
    """
    from app.repositories.sqlite.source_store import SourceStore

    service, _, _, _ = setup
    database = service.store.database
    with database.write() as db:
        owner = db.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
        db.execute(
            "INSERT INTO notebooks(id,name,purpose,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            ("nb-passage", "Passages", "", owner, "2026-09-20", "2026-09-20"),
        )
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            ("src-passage", "nb-passage", "Original", "markdown", "2026-09-20", "2026-09-20"),
        )
        for index, text in enumerate(("first half", "second half")):
            db.execute(
                "INSERT INTO source_elements(id,source_id,element_type,location_label,text,created_at) VALUES(?,?,?,?,?,?)",
                (f"el-src-passage-{index:04d}", "src-passage", "paragraph",
                 f"p{index}", text, "2026-09-20"),
            )
        db.execute(
            "INSERT INTO chunks(id,notebook_id,source_id,text,section_path,element_ids,created_at) VALUES(?,?,?,?,?,?,?)",
            ("chunk-wide", "nb-passage", "src-passage", "first half second half",
             "", '["el-src-passage-0000","el-src-passage-0001"]', "2026-09-20"),
        )
        # 一个不声明任何元素的段落:仍要出现在返回里(它存在),只是元素表为空——
        # 「段落没了」与「段落没有元素」是两件事,不能都退化成缺席。
        db.execute(
            "INSERT INTO chunks(id,notebook_id,source_id,text,section_path,element_ids,created_at) VALUES(?,?,?,?,?,?,?)",
            ("chunk-bare", "nb-passage", "src-passage", "standalone", "", "[]", "2026-09-20"),
        )
    sources = object.__new__(SourceStore)
    sources.database = database

    statements = []
    original = database.connect

    @contextmanager
    def traced():
        with original() as db:
            db.set_trace_callback(statements.append)
            try:
                yield db
            finally:
                db.set_trace_callback(None)

    monkeypatch.setattr(database, "connect", traced)
    snapshot = sources.passage_evidence_snapshot(
        ["chunk-wide", "chunk-bare", "chunk-vanished"]
    )
    # 同一个快照:原文摘要与两个元素指纹出自一条 SELECT。
    assert len([
        statement for statement in statements
        if statement.lstrip().upper().startswith("SELECT")
    ]) == 1
    assert snapshot["chunk-wide"] == {
        "text_sha": _sha("first half second half"),
        "elements": {
            "el-src-passage-0000": ("src-passage", _sha("first half")),
            "el-src-passage-0001": ("src-passage", _sha("second half")),
        },
    }
    assert snapshot["chunk-bare"] == {
        "text_sha": _sha("standalone"), "elements": {},
    }
    # 库里已经没有的段落缺席,不是一条空记录。
    assert "chunk-vanished" not in snapshot

    # 同一个 id 下换掉元素文字 → 指纹跟着变,段落摘要不变。
    with database.write() as db:
        db.execute("UPDATE source_elements SET text=? WHERE id=?",
                   ("first half, revised", "el-src-passage-0000"))
    after = sources.passage_evidence_snapshot(["chunk-wide"])
    assert after["chunk-wide"]["text_sha"] == snapshot["chunk-wide"]["text_sha"]
    assert after["chunk-wide"]["elements"]["el-src-passage-0000"] == (
        "src-passage", _sha("first half, revised"),
    )
    # 段落原文被重新解析覆盖 → 摘要变,这正是调用方用来拒绝旧答案的那一位。
    with database.write() as db:
        db.execute("UPDATE chunks SET text=? WHERE id=?",
                   ("entirely different", "chunk-wide"))
    assert sources.passage_evidence_snapshot(["chunk-wide"])["chunk-wide"][
        "text_sha"
    ] == _sha("entirely different")


def test_sqlite_passage_snapshot_batches_past_the_variable_limit(setup):
    """超过 SQLite 变量上限的段落清单按批走,而且每个段落仍在自己的那一个快照里。"""
    from app.repositories.sqlite.source_store import SourceStore

    service, _, _, _ = setup
    database = service.store.database
    wanted = [f"chunk-{index:05d}" for index in range(1500)]
    with database.write() as db:
        owner = db.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
        db.execute(
            "INSERT INTO notebooks(id,name,purpose,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            ("nb-bulk", "Bulk", "", owner, "2026-09-20", "2026-09-20"),
        )
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            ("src-bulk", "nb-bulk", "Original", "markdown", "2026-09-20", "2026-09-20"),
        )
        for chunk_id in wanted:
            db.execute(
                "INSERT INTO chunks(id,notebook_id,source_id,text,section_path,element_ids,created_at) VALUES(?,?,?,?,?,?,?)",
                (chunk_id, "nb-bulk", "src-bulk", f"text of {chunk_id}", "", "[]",
                 "2026-09-20"),
            )
    sources = object.__new__(SourceStore)
    sources.database = database

    snapshot = sources.passage_evidence_snapshot(wanted)
    assert len(snapshot) == len(wanted)
    assert snapshot[wanted[-1]]["text_sha"] == _sha(f"text of {wanted[-1]}")
