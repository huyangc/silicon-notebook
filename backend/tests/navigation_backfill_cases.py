"""Shared cases for the navigation-page marker (SQLite v101 / PostgreSQL 0082):
the build write, the compare-and-set store methods and the background pass.

Used by ``tests/test_navigation_backfill.py`` (SQLite) and
``tests/postgres/test_navigation_backfill_pg.py`` (PostgreSQL). Every case takes
a repository facade and the dialect's placeholder; seeding goes through the
stores so both backends run the same code.
"""
from __future__ import annotations

from contextlib import nullcontext
from uuid import uuid4

from app.models.notebooks import NotebookCreate
from app.repositories.ports import CHUNKED_AT_UNCHECKED, ChunkWrite, SourceElementWrite
from app.services.navigation_backfill import NavigationBackfill

TITLES = (
    "Introduction to Chiplet Interconnect",
    "Package Integration Options",
    "Protocol Layer Architecture",
    "Flit Format Definition",
    "Retry Buffer Management",
    "Link Initialization Sequence",
    "Loss and Crosstalk Mask",
    "Standard Package Channel",
)
BODY = (
    "The transmitter shall drive the lane within the stated swing and the "
    "receiver shall tolerate the specified common mode noise at the bump. "
) * 3
TOC_SECTION = "Contents"


def spec_elements() -> list[tuple[str, str, str]]:
    """``(element_type, text, section_path)``: a table of contents as ONE
    paragraph (so a one-chunk-per-element plugin sees it whole), then the
    numbered sections with bodies."""
    toc = "\n".join(
        f"{number} {title} {'.' * 16} {10 + number * 3}"
        for number, title in enumerate(TITLES, start=1)
    )
    elements = [("heading", TOC_SECTION, ""), ("paragraph", toc, "")]
    for number, title in enumerate(TITLES, start=1):
        elements.append(("heading", f"{number} {title}", ""))
        elements.append(("paragraph", BODY, ""))
    return elements


def create_notebook(repo, name: str = "nav") -> str:
    return repo.create_notebook(NotebookCreate(name=name)).id


def seed_source(repo, notebook_id: str, elements=None) -> str:
    runtime = repo._runtime
    source_id = f"src-nav-{uuid4().hex[:10]}"
    runtime.source_store.insert_source(
        source_id=source_id, notebook_id=notebook_id, title="spec",
        source_type="document", status="parsed", parse_status="extracted",
        file_name="spec.md", file_path="", file_size=0, file_hash=source_id,
        summary="", doc_type="",
    )
    rows = [
        SourceElementWrite(
            f"el-{source_id}-{index:04d}", element_type, f"p{index}", text,
            {"section_path": section_path} if section_path else {},
        )
        for index, (element_type, text, section_path) in enumerate(
            elements if elements is not None else spec_elements(), start=1
        )
    ]
    with runtime.database.write() as db:
        runtime.source_store.replace_elements(
            db, source_id, rows, created_at=runtime.seams.now()
        )
    return source_id


def chunk_flags(repo, source_id: str, q: str) -> dict[str, int]:
    """section_path -> max navigation flag over its chunks."""
    with repo._runtime.database.connect() as db:
        rows = db.execute(
            f"SELECT section_path, navigation FROM chunks WHERE source_id={q}",
            (source_id,),
        ).fetchall()
    flags: dict[str, int] = {}
    for row in rows:
        flags[row["section_path"]] = max(flags.get(row["section_path"], 0), int(row["navigation"]))
    return flags


def stamps(repo, source_id: str, q: str):
    with repo._runtime.database.connect() as db:
        row = db.execute(
            f"SELECT chunked_at, navigation_scanned_at FROM sources WHERE id={q}",
            (source_id,),
        ).fetchone()
    return row["chunked_at"], row["navigation_scanned_at"]


def chunk_count(repo, source_id: str, q: str) -> int:
    with repo._runtime.database.connect() as db:
        row = db.execute(
            f"SELECT COUNT(*) AS n FROM chunks WHERE source_id={q}", (source_id,)
        ).fetchone()
    return int(row["n"])


def bump_chunked_at(repo, source_id: str, q: str) -> None:
    """Another writer committed a new generation (only the token moves)."""
    with repo._runtime.database.write() as db:
        db.execute(
            f"UPDATE sources SET chunked_at={q} WHERE id={q}",
            (repo._runtime.seams.now(), source_id),
        )


def expected_flags() -> dict[str, int]:
    flags = {TOC_SECTION: 1}
    for number, title in enumerate(TITLES, start=1):
        flags[f"{number} {title}"] = 0
    return flags


def backfill(repo, *, marked=None, hold_lock=None, offline=False, submit=None,
             page_size=None) -> NavigationBackfill:
    offline_active = offline if callable(offline) else (lambda: offline)
    kwargs = {"is_ready": lambda: True, "offline_active": offline_active}
    if submit is not None:
        kwargs["submit"] = submit
    if page_size is not None:
        kwargs["page_size"] = page_size
    service = NavigationBackfill.for_repository(repo, **kwargs)
    if marked is not None:
        service._mark_dirty = marked.append
    if hold_lock is not None:
        service._hold_lock = hold_lock
    return service


def drain(service: NavigationBackfill):
    """Run a pass to its end synchronously: every job it submits for the next
    page is run in turn. Returns ``(result, jobs run)``."""
    queue: list = []

    def submit(fn, *args, **kwargs):
        assert kwargs == {"name": "navigation-backfill"}
        queue.append((fn, args))
        return "job"

    service._submit = submit
    result = service.run_pass()
    jobs = 1
    while queue:
        fn, args = queue.pop(0)
        result = fn(*args)
        jobs += 1
    return result, jobs


# ------------------------------------------------------------------ cases
def case_build_writes_flags_and_stamp(repo, q: str) -> None:
    notebook_id = create_notebook(repo)
    source_id = seed_source(repo, notebook_id)
    repo._runtime.source_chunking.build_chunks_for_source(source_id)
    assert chunk_flags(repo, source_id, q) == expected_flags()
    chunked_at, scanned_at = stamps(repo, source_id, q)
    assert chunked_at is not None and scanned_at == chunked_at
    assert repo._runtime.chunk_store.navigation_pending_count() == 0


def case_build_with_detection_off_writes_zero_and_no_stamp(repo, q: str, monkeypatch) -> None:
    monkeypatch.setattr(repo._runtime.settings, "navigation_detection_enabled", False)
    notebook_id = create_notebook(repo)
    source_id = seed_source(repo, notebook_id)
    repo._runtime.source_chunking.build_chunks_for_source(source_id)
    assert set(chunk_flags(repo, source_id, q).values()) == {0}
    chunked_at, scanned_at = stamps(repo, source_id, q)
    assert chunked_at is not None and scanned_at is None
    assert repo._runtime.chunk_store.navigation_pending_count() == 1


def case_replace_compare_and_set(repo, q: str) -> None:
    notebook_id = create_notebook(repo)
    source_id = seed_source(repo, notebook_id)
    store = repo._runtime.chunk_store
    repo._runtime.source_chunking.build_chunks_for_source(source_id)
    before = chunk_count(repo, source_id, q)
    token = store.chunk_generation(source_id)
    element = f"el-{source_id}-0002"
    write = [ChunkWrite("ck-cas-1", "text", "s", (element,), navigation=True)]
    now = repo._runtime.seams.now()
    # A stale token: nothing is written, the previous generation survives.
    assert store.replace_source_chunks(
        source_id, notebook_id, write, created_at=now, mark_chunked_at=now,
        navigation_scanned_at=now, expected_chunked_at="2000-01-01T00:00:00+00:00",
    ) is False
    assert chunk_count(repo, source_id, q) == before
    assert store.chunk_generation(source_id) == token
    # The current token: written, flag and stamp included.
    assert store.replace_source_chunks(
        source_id, notebook_id, write, created_at=now, mark_chunked_at=now,
        navigation_scanned_at=now, expected_chunked_at=token,
    ) is True
    assert chunk_flags(repo, source_id, q) == {"s": 1}
    chunked_at, scanned_at = stamps(repo, source_id, q)
    assert scanned_at == chunked_at
    # Unchecked (the knowhow projector / the build's fallback) still writes.
    assert store.replace_source_chunks(
        source_id, notebook_id, [], created_at=now,
        expected_chunked_at=CHUNKED_AT_UNCHECKED,
    ) is True
    assert chunk_count(repo, source_id, q) == 0


def case_build_redoes_under_the_lock_when_the_generation_moved(repo, q: str, monkeypatch) -> None:
    """Another writer commits between the build's lock-free judgement and its
    write: the compare-and-set write is refused and the build redoes read,
    judge and write under the notebook lock -- one generation, correct flags."""
    notebook_id = create_notebook(repo)
    source_id = seed_source(repo, notebook_id)
    service = repo._runtime.source_chunking
    real_flags = service._navigation_flags
    calls = []

    def racing_flags(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            bump_chunked_at(repo, source_id, q)
        return real_flags(*args, **kwargs)

    writes = []
    store = repo._runtime.chunk_store
    real_replace = store.replace_source_chunks

    def counting_replace(*args, **kwargs):
        outcome = real_replace(*args, **kwargs)
        writes.append(outcome)
        return outcome

    monkeypatch.setattr(service, "_navigation_flags", racing_flags)
    monkeypatch.setattr(store, "replace_source_chunks", counting_replace)
    service.build_chunks_for_source(source_id)
    assert writes == [False, True]
    assert chunk_flags(repo, source_id, q) == expected_flags()
    assert chunk_count(repo, source_id, q) == len(expected_flags())
    chunked_at, scanned_at = stamps(repo, source_id, q)
    assert scanned_at == chunked_at


def _pending_source(repo, q: str, monkeypatch, notebook_id: str | None = None) -> tuple[str, str]:
    """A source chunked while detection was off: unflagged, unstamped."""
    with monkeypatch.context() as patch:
        patch.setattr(repo._runtime.settings, "navigation_detection_enabled", False)
        notebook_id = notebook_id or create_notebook(repo)
        source_id = seed_source(repo, notebook_id)
        repo._runtime.source_chunking.build_chunks_for_source(source_id)
    assert stamps(repo, source_id, q)[1] is None
    return notebook_id, source_id


def set_stamp(repo, source_id: str, q: str, value) -> None:
    with repo._runtime.database.write() as db:
        db.execute(
            f"UPDATE sources SET navigation_scanned_at={q} WHERE id={q}", (value, source_id)
        )


def case_backfill_judges_pending_sources_and_resumes(repo, q: str, monkeypatch) -> None:
    notebook_id, source_id = _pending_source(repo, q, monkeypatch)
    store = repo._runtime.chunk_store
    applied = []
    real_apply = store.apply_navigation_flags

    def spy_apply(source, **kwargs):
        applied.append(list(kwargs["changes"]))
        return real_apply(source, **kwargs)

    monkeypatch.setattr(store, "apply_navigation_flags", spy_apply)
    marked: list[str] = []
    result, _jobs = drain(backfill(repo, marked=marked))
    assert result is not None
    assert (result.pending_before, result.judged, result.pending_after) == (1, 1, 0)
    assert result.sources_changed == 1 and result.chunks_changed == 1
    # only the rows whose flag changed are written
    assert len(applied) == 1 and len(applied[0]) == 1 and applied[0][0][1] is True
    assert marked == [notebook_id]
    assert chunk_flags(repo, source_id, q) == expected_flags()
    # the stamp is the generation token: a copy of chunked_at
    chunked_at, scanned_at = stamps(repo, source_id, q)
    assert scanned_at == chunked_at
    # resumable: nothing left, nothing written, nothing marked
    again, _jobs = drain(backfill(repo, marked=marked))
    assert (again.pending_before, again.judged) == (0, 0)
    assert len(applied) == 1 and marked == [notebook_id]
    # ANY stamp that differs from chunked_at is pending, earlier or later: no
    # clock or text ordering decides it
    for stale in ("2000-01-01T00:00:00.000000+00:00", "2999-01-01T00:00:00.000000+00:00"):
        set_stamp(repo, source_id, q, stale)
        assert store.navigation_pending_count() == 1
        third, _jobs = drain(backfill(repo, marked=marked))
        assert third.judged == 1 and third.chunks_changed == 0
        assert applied[-1] == []
        assert marked == [notebook_id]  # no changed row, no dirty mark
        assert stamps(repo, source_id, q)[1] == chunked_at
        assert store.navigation_pending_count() == 0


def case_backfill_runs_one_page_per_job(repo, q: str, monkeypatch) -> None:
    """Each background job judges one page and submits the next page as a new
    job; the pass ends with a job that finds nothing left."""
    notebook_id, _first = _pending_source(repo, q, monkeypatch)
    for _ in range(2):
        _pending_source(repo, q, monkeypatch, notebook_id=notebook_id)
    marked: list[str] = []
    result, jobs = drain(backfill(repo, marked=marked, page_size=1))
    assert jobs == 4  # one job per page, then one that finds the end
    assert (result.pages, result.judged, result.pending_after) == (3, 3, 0)
    assert marked == [notebook_id] * 3  # once per page that changed rows


def case_backfill_abandons_a_source_whose_generation_moved(repo, q: str, monkeypatch) -> None:
    _notebook_id, source_id = _pending_source(repo, q, monkeypatch)
    store = repo._runtime.chunk_store
    real_headings = store.source_heading_elements

    def racing_headings(source):
        # read after the token: a rebuild commits in between
        bump_chunked_at(repo, source, q)
        return real_headings(source)

    monkeypatch.setattr(store, "source_heading_elements", racing_headings)
    marked: list[str] = []
    result, _jobs = drain(backfill(repo, marked=marked))
    assert (result.judged, result.abandoned, result.pending_after) == (0, 1, 1)
    assert marked == []
    assert set(chunk_flags(repo, source_id, q).values()) == {0}
    assert stamps(repo, source_id, q)[1] is None
    monkeypatch.setattr(store, "source_heading_elements", real_headings)
    resumed, _jobs = drain(backfill(repo, marked=marked))
    assert (resumed.judged, resumed.pending_after) == (1, 0)
    assert chunk_flags(repo, source_id, q) == expected_flags()


def case_backfill_reads_headings_only(repo, q: str, monkeypatch) -> None:
    _notebook_id, source_id = _pending_source(repo, q, monkeypatch)
    store = repo._runtime.chunk_store
    headings = store.source_heading_elements(source_id)
    assert [h["element_type"] for h in headings] == ["heading"] * (len(TITLES) + 1)
    assert [h["text"] for h in headings][:2] == [TOC_SECTION, f"1 {TITLES[0]}"]
    assert set(headings[0]) == {"id", "element_type", "text", "section_path"}

    def no_full_read(_source):
        raise AssertionError("the pass must not read every element")

    monkeypatch.setattr(store, "source_elements_for_chunking", no_full_read)
    result, _jobs = drain(backfill(repo))
    assert result.judged == 1 and result.failed == 0
    assert chunk_flags(repo, source_id, q) == expected_flags()


def case_backfill_retries_busy_sources_once(repo, q: str, monkeypatch) -> None:
    _notebook_id, source_id = _pending_source(repo, q, monkeypatch)
    # held for good: still busy after the one retry round, left pending
    result, _jobs = drain(backfill(repo, hold_lock=lambda _sid: nullcontext(False)))
    assert (result.busy, result.judged, result.pending_after) == (1, 0, 1)
    assert stamps(repo, source_id, q)[1] is None
    # held during the scan, free by the end of it: judged in the retry round
    attempts: list[str] = []

    def held_once(sid):
        attempts.append(sid)
        return nullcontext(len(attempts) > 1)

    result, _jobs = drain(backfill(repo, hold_lock=held_once))
    assert attempts == [source_id, source_id]
    assert (result.busy, result.judged, result.pending_after) == (0, 1, 0)


def case_backfill_switch_off_and_offline_batch(repo, q: str, monkeypatch) -> None:
    notebook_id, _source = _pending_source(repo, q, monkeypatch)
    _pending_source(repo, q, monkeypatch, notebook_id=notebook_id)
    submitted = []

    def collect(*args, **kwargs):
        submitted.append(kwargs)
        return "job"

    monkeypatch.setattr(repo._runtime.settings, "navigation_detection_enabled", False)
    assert backfill(repo, submit=collect).schedule() is None
    assert backfill(repo).run_pass() is None
    monkeypatch.setattr(repo._runtime.settings, "navigation_detection_enabled", True)
    assert backfill(repo, offline=True, submit=collect).schedule() is None
    stopped, _jobs = drain(backfill(repo, offline=True))
    assert stopped.stopped_offline_batch and stopped.judged == 0
    assert submitted == []
    # an offline batch scope that opens mid-pass stops it at the next source
    checks: list[int] = []

    def opens_after_first():
        checks.append(1)
        return len(checks) > 1

    partial, _jobs = drain(backfill(repo, offline=opens_after_first))
    assert partial.stopped_offline_batch and partial.judged == 1
    assert partial.pending_after == 1
    assert backfill(repo, submit=collect).schedule() == "job"
    assert submitted == [{"name": "navigation-backfill"}]


def case_clear_resets_flags_and_stamps(repo, q: str) -> None:
    notebook_id = create_notebook(repo)
    source_id = seed_source(repo, notebook_id)
    repo._runtime.source_chunking.build_chunks_for_source(source_id)
    other = create_notebook(repo, "other")
    other_source = seed_source(repo, other)
    repo._runtime.source_chunking.build_chunks_for_source(other_source)
    store = repo._runtime.chunk_store
    assert store.clear_navigation_flags(notebook_id=notebook_id) == (1, 1, [notebook_id])
    assert set(chunk_flags(repo, source_id, q).values()) == {0}
    assert stamps(repo, source_id, q)[1] is None
    assert chunk_flags(repo, other_source, q) == expected_flags()  # out of scope
    assert store.navigation_pending_count() == 1
    assert store.navigation_scan_page(after_source_id="", limit=10) == [(source_id, notebook_id)]
    assert {row[0] for row in store.navigation_scan_page(
        after_source_id="", limit=10, pending_only=False
    )} == {source_id, other_source}


def case_mirrored_notebooks_are_left_alone(repo, q: str, monkeypatch) -> None:
    """A mirrored notebook (``sync_origin`` set, docs/incremental-sync-design.md
    §5) carries the source environment's flags and stamps: neither the pass
    nor ``--clear`` writes it."""
    notebook_id, source_id = _pending_source(repo, q, monkeypatch)
    with repo._runtime.database.write() as db:
        db.execute(f"UPDATE notebooks SET sync_origin={q} WHERE id={q}", ("env-a", notebook_id))
    store = repo._runtime.chunk_store
    assert store.navigation_pending_count() == 0
    assert drain(backfill(repo))[0].judged == 0
    assert stamps(repo, source_id, q)[1] is None
    with repo._runtime.database.write() as db:
        db.execute(f"UPDATE chunks SET navigation=1 WHERE source_id={q}", (source_id,))
    assert store.clear_navigation_flags(notebook_id=None) == (0, 0, [])
    assert set(chunk_flags(repo, source_id, q).values()) == {1}
