"""Write side of the extension admission snapshot: prime, converge, wire.

Three layers, and the boundary between the first two is the point of the
feature: ``refresh_extension_admission`` is loud (the composition root must not
hand out a repository whose toggle rows it could not read), the refresher is
quiet (a serving process must not fall over, or silently flip plugins, because
one poll failed). The third layer is the lifecycle wiring that guarantees the
thread exists exactly when a repository does.

Timing is never asserted here. Every "did it tick?" wait is on an
``threading.Event`` the fake store sets, and every teardown goes through
``stop`` (which joins), so a slow machine makes these tests slower, never red.
"""
from __future__ import annotations

import logging
import threading
from types import SimpleNamespace

import pytest

from app.core import extension_admission
from app.services import extension_toggles

REFRESHER_THREAD_NAME = "extension-admission-refresh"
LOGGER_NAME = "silicon_notebook.extension_admission"


@pytest.fixture(autouse=True)
def _clean_process_state():
    """Both pieces of process-global state this module owns, empty either side.

    The refresher stop comes first: a thread left running from a failed
    assertion would keep publishing into the holder we are about to reset.
    """

    extension_toggles.stop_extension_admission_refresher()
    extension_admission.reset_for_tests()
    yield
    extension_toggles.stop_extension_admission_refresher()
    extension_admission.reset_for_tests()
    assert not _refresher_threads(), "a refresher thread outlived its test"


def _refresher_threads() -> list[threading.Thread]:
    return [
        thread
        for thread in threading.enumerate()
        if thread.name == REFRESHER_THREAD_NAME and thread.is_alive()
    ]


class _Store:
    """The one method the refresh path uses, driven by a caller-supplied answer.

    Records reads and exposes an event per read count so a test can wait for
    "at least N ticks have happened" without sleeping on a guessed duration.
    """

    def __init__(self, answer) -> None:
        self._answer = answer
        self.reads = 0
        self._lock = threading.Lock()
        self._reached: dict[int, threading.Event] = {}

    def extension_runtime_disabled_ids(self):
        with self._lock:
            self.reads += 1
            reads = self.reads
            events = [
                event for count, event in self._reached.items() if count <= reads
            ]
        for event in events:
            event.set()
        return self._answer(reads)

    def wait_for_reads(self, count: int, timeout: float = 10.0) -> None:
        with self._lock:
            event = self._reached.setdefault(count, threading.Event())
            if self.reads >= count:
                event.set()
        assert event.wait(timeout), (
            f"refresher did not reach {count} read(s); saw {self.reads}"
        )


def _always(value):
    return lambda _reads: value


def _always_raising(exc_type=RuntimeError):
    def answer(reads):
        raise exc_type(f"toggle store unavailable (read {reads})")

    return answer


# --- refresh: the loud half -------------------------------------------------


def test_refresh_publishes_the_store_rows_and_returns_them():
    store = _Store(_always(frozenset({"corp.a", "corp.b"})))

    published = extension_toggles.refresh_extension_admission(store)

    assert published == frozenset({"corp.a", "corp.b"})
    assert extension_admission.disabled_plugin_ids() == published
    assert store.reads == 1


def test_refresh_propagates_store_failure_and_leaves_the_snapshot_intact():
    """A failed read must never be mistaken for "nothing is disabled"."""

    extension_toggles.refresh_extension_admission(
        _Store(_always(frozenset({"corp.kept"})))
    )

    with pytest.raises(RuntimeError, match="toggle store unavailable"):
        extension_toggles.refresh_extension_admission(_Store(_always_raising()))

    assert extension_admission.disabled_plugin_ids() == frozenset({"corp.kept"})


def test_refresh_lets_the_publishers_shape_check_through():
    """A store returning a ``str`` would silently disable single characters;
    the holder rejects it and refresh does not soften that into a no-op."""

    with pytest.raises(TypeError):
        extension_toggles.refresh_extension_admission(_Store(_always("corp.a")))

    assert extension_admission.disabled_plugin_ids() == frozenset()


# --- refresher: the quiet half ---------------------------------------------


def test_refresher_converges_the_snapshot_and_stops_on_request():
    store = _Store(_always(frozenset({"corp.late"})))

    stop = extension_toggles.start_extension_admission_refresher(store, 0.01)
    try:
        store.wait_for_reads(1)
    finally:
        stop()

    # stop() joins, so the tick that set the event has finished publishing.
    assert extension_admission.disabled_plugin_ids() == frozenset({"corp.late"})
    assert not _refresher_threads()

    # Idempotent, in both spellings, in either order.
    stop()
    extension_toggles.stop_extension_admission_refresher()


def test_refresher_keeps_the_last_snapshot_while_the_store_keeps_failing():
    extension_admission.publish_disabled_plugin_ids(frozenset({"corp.kept"}))
    store = _Store(_always_raising())

    stop = extension_toggles.start_extension_admission_refresher(store, 0.01)
    try:
        store.wait_for_reads(3)
        # Still ticking after repeated failures: the thread must survive a
        # database outage, not die on the first exception and leave the
        # process permanently frozen at whatever it last read.
        assert _refresher_threads()
        assert extension_admission.disabled_plugin_ids() == frozenset({"corp.kept"})
    finally:
        stop()

    assert extension_admission.disabled_plugin_ids() == frozenset({"corp.kept"})


def test_refresher_warns_once_per_outage_and_reports_the_recovery(caplog):
    """Log volume is bounded by *transitions*, not by ticks: an outage lasting
    an hour at a 3s interval must not write 1200 warnings."""

    recovered = frozenset({"corp.after"})

    def answer(reads):
        if reads <= 3:
            raise RuntimeError(f"toggle store unavailable (read {reads})")
        return recovered

    store = _Store(answer)
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)

    stop = extension_toggles.start_extension_admission_refresher(store, 0.01)
    try:
        store.wait_for_reads(6)
    finally:
        stop()

    records = [r for r in caplog.records if r.name == LOGGER_NAME]
    warnings = [r for r in records if r.levelno == logging.WARNING]
    infos = [r for r in records if r.levelno == logging.INFO]

    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert "keeping the last snapshot" in warnings[0].getMessage()
    # Class name only, never exc_info/str(exc): extension-surface logging is
    # content-free (a store fault's text can embed a DSN or a private path).
    assert "RuntimeError" in warnings[0].getMessage()
    assert warnings[0].exc_info is None
    # Every failing tick after the first is debug-level only.
    assert len([r for r in records if r.levelno == logging.DEBUG]) >= 2

    recovery = [r for r in infos if "recovered after" in r.getMessage()]
    assert len(recovery) == 1
    assert "3 consecutive failure(s)" in recovery[0].getMessage()
    # The change itself is announced exactly once, not once per later tick.
    changed = [r for r in infos if "snapshot updated" in r.getMessage()]
    assert len(changed) == 1
    assert extension_admission.disabled_plugin_ids() == recovered


def test_second_start_replaces_the_first_and_stale_stops_are_no_ops():
    """Defined behaviour for a repeat start: replace, do not reject.

    The only way to reach it is a new repository lifecycle in this process, and
    the old refresher then holds a store on a repository being torn down.
    Rejecting would leave that dead one as the process's only refresher.
    """

    first = _Store(_always(frozenset({"corp.first"})))
    second = _Store(_always(frozenset({"corp.second"})))

    stop_first = extension_toggles.start_extension_admission_refresher(first, 0.01)
    first.wait_for_reads(1)

    # start() joins the refresher it replaces, so `first` is frozen from here.
    stop_second = extension_toggles.start_extension_admission_refresher(second, 0.01)
    try:
        frozen_reads = first.reads
        second.wait_for_reads(3)
        assert first.reads == frozen_reads
        assert len(_refresher_threads()) == 1

        # The superseded handle must not shut down its successor.
        stop_first()
        assert len(_refresher_threads()) == 1
        reads_before = second.reads
        second.wait_for_reads(reads_before + 1)
    finally:
        stop_second()

    assert not _refresher_threads()
    assert extension_admission.disabled_plugin_ids() == frozenset({"corp.second"})


def test_a_retired_refresher_does_not_publish_a_read_that_outlived_its_stop(
    monkeypatch,
):
    """The bounded join is not a guarantee that the thread is gone.

    A read stuck on a slow query can return after ``stop`` gave up waiting.
    Deliberately nobody else publishes here, so the publish token would NOT
    save us — the retired read's token is still the highest one issued, and a
    token check alone would happily install its stale value with no later tick
    from that dead thread to correct it. Only the stop re-check refuses it,
    which is why both guards exist.
    """

    read_started = threading.Event()
    release_read = threading.Event()

    def answer(_reads):
        read_started.set()
        assert release_read.wait(10.0), "test never released the blocked read"
        return frozenset({"corp.stale"})

    store = _Store(answer)
    stop = extension_toggles.start_extension_admission_refresher(store, 0.01)
    assert read_started.wait(10.0)

    # Stop while the read is still in flight. The join times out (shortened
    # here so the test does not wait out the production budget), so this
    # returns with the thread still alive inside the store call.
    monkeypatch.setattr(extension_toggles, "_STOP_JOIN_TIMEOUT_SECONDS", 0.05)
    stop()

    retired = _refresher_threads()
    assert len(retired) == 1, "the blocked thread should have outlived the join"

    release_read.set()
    retired[0].join(10.0)
    assert not retired[0].is_alive()

    assert extension_admission.disabled_plugin_ids() == frozenset()


def test_publish_tokens_are_monotonic_and_reset_with_the_holder():
    first = extension_admission.begin_publish()
    second = extension_admission.begin_publish()
    assert second > first

    assert extension_admission.publish_disabled_plugin_ids(
        frozenset({"corp.b"}), second
    ) is True
    # Refused even though nothing else published in between: the token says
    # this caller's read is the older one, and that is all that matters.
    assert extension_admission.publish_disabled_plugin_ids(
        frozenset({"corp.a"}), first
    ) is False
    assert extension_admission.disabled_plugin_ids() == frozenset({"corp.b"})
    # A token is spent, not reusable.
    assert extension_admission.publish_disabled_plugin_ids(
        frozenset({"corp.c"}), second
    ) is False
    assert extension_admission.disabled_plugin_ids() == frozenset({"corp.b"})

    # The token-free form issues its own and therefore always wins.
    assert extension_admission.publish_disabled_plugin_ids(
        frozenset({"corp.d"})
    ) is True
    assert extension_admission.disabled_plugin_ids() == frozenset({"corp.d"})

    # Shape is checked before ordering: a caller with a bug hears about it
    # even on a publish that was going to be dropped anyway.
    doomed = extension_admission.begin_publish()
    extension_admission.publish_disabled_plugin_ids(frozenset({"corp.e"}))
    with pytest.raises(TypeError):
        extension_admission.publish_disabled_plugin_ids("corp.x", doomed)

    extension_admission.reset_for_tests()
    assert extension_admission.disabled_plugin_ids() == frozenset()
    assert extension_admission.begin_publish() == 1


def test_a_tick_that_read_before_an_admin_write_cannot_undo_it(caplog):
    """The race the publish token exists for.

    The tick's SELECT starts before the admin's row is committed, so it can
    only ever return the pre-write world; it finishes after the admin has
    published. Last-writer-wins would re-enable the plugin an admin just
    switched off, for up to a whole interval, with nothing to correct it until
    the next tick.
    """

    read_started = threading.Event()
    release_read = threading.Event()

    def answer(reads):
        if reads == 1:
            read_started.set()
            assert release_read.wait(10.0), "test never released the read"
            return frozenset()  # the pre-write world: nothing disabled
        return frozenset({"corp.disabled"})  # later reads see the commit

    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
    tick_store = _Store(answer)
    stop = extension_toggles.start_extension_admission_refresher(tick_store, 0.01)
    try:
        assert read_started.wait(10.0)

        # The whole admin write path runs inside the tick's read window.
        admin_store = _Store(_always(frozenset({"corp.disabled"})))
        assert extension_toggles.refresh_extension_admission(
            admin_store
        ) == frozenset({"corp.disabled"})

        release_read.set()
        # Read 2 starting proves the blocked tick's publish already ran.
        tick_store.wait_for_reads(2)
        assert extension_admission.disabled_plugin_ids() == frozenset(
            {"corp.disabled"}
        )
    finally:
        release_read.set()
        stop()

    assert any(
        "superseded" in record.getMessage()
        for record in caplog.records
        if record.name == LOGGER_NAME
    ), "the stale tick's publish should have been refused, not applied"


def test_an_older_read_cannot_overwrite_a_concurrent_newer_write():
    """Two admin writes interleaving: the one that read first publishes last.

    Same shape as the tick race above, without a refresher — this is what two
    concurrent PATCHes do to each other.
    """

    read_started = threading.Event()
    release_read = threading.Event()

    def answer(_reads):
        read_started.set()
        assert release_read.wait(10.0), "test never released the read"
        return frozenset({"corp.first"})

    first_store = _Store(answer)
    result: dict[str, frozenset[str]] = {}

    def run_first() -> None:
        result["live"] = extension_toggles.refresh_extension_admission(
            first_store
        )

    worker = threading.Thread(target=run_first, name="admin-write-a")
    worker.start()
    try:
        assert read_started.wait(10.0)
        # The second write takes its token and publishes entirely inside the
        # first one's read window.
        extension_toggles.refresh_extension_admission(
            _Store(_always(frozenset({"corp.second"})))
        )
    finally:
        release_read.set()
        worker.join(10.0)

    assert not worker.is_alive()
    assert extension_admission.disabled_plugin_ids() == frozenset({"corp.second"})
    # The loser is told what is actually in effect, not what it read: a set it
    # lost the race with was never in effect for anybody.
    assert result["live"] == frozenset({"corp.second"})


def test_an_invalid_snapshot_is_reported_every_tick_and_never_debounced(caplog):
    """A store handing back a non-set is a bug in our code, not an outage.

    De-bouncing it the way a database outage is de-bounced would hide it after
    the first tick — exactly backwards for a fault that will never fix itself.
    """

    extension_admission.publish_disabled_plugin_ids(frozenset({"corp.kept"}))
    store = _Store(_always(["corp.a", "corp.b"]))  # a list is not a Set
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)

    stop = extension_toggles.start_extension_admission_refresher(store, 0.01)
    try:
        store.wait_for_reads(3)
        assert _refresher_threads(), "a repairable fault must not kill the thread"
    finally:
        stop()

    records = [r for r in caplog.records if r.name == LOGGER_NAME]
    errors = [r for r in records if r.levelno == logging.ERROR]
    assert len(errors) >= 3, [r.getMessage() for r in records]
    assert all("invalid snapshot" in r.getMessage() for r in errors)
    # Same content-free discipline as the outage channel: class name only.
    assert all("TypeError" in r.getMessage() for r in errors)
    assert all(r.exc_info is None for r in errors)
    # Not routed through the outage channel at all.
    assert not [
        r for r in records if "keeping the last snapshot" in r.getMessage()
    ]
    assert extension_admission.disabled_plugin_ids() == frozenset({"corp.kept"})


def test_start_rejects_a_non_positive_interval():
    with pytest.raises(ValueError, match="must be positive"):
        extension_toggles.start_extension_admission_refresher(
            _Store(_always(frozenset())), 0
        )
    assert not _refresher_threads()


# --- composition root prime -------------------------------------------------


def _extension_runtime_double():
    """The seven hosts ``create_application_repository`` forwards, as opaque
    objects: this test is about the prime, not about the host wiring (which
    ``test_retrieval_contributor_wiring`` freezes)."""

    return SimpleNamespace(
        retrieval_contributors=object(),
        parser_chain=object(),
        ask_completed_observers=object(),
        report_completed_observers=object(),
        ask_engines=object(),
        indexing_pipelines=object(),
        gap_consult=object(),
    )


def _composed_repository_double(store, calls=None):
    recorder = [] if calls is None else calls
    return SimpleNamespace(
        _runtime=SimpleNamespace(extension_toggles=store),
        close=lambda: recorder.append("close"),
    )


def test_composition_root_primes_the_snapshot_before_returning(monkeypatch):
    from app import bootstrap
    from app.core.config import Settings

    store = _Store(_always(frozenset({"corp.disabled"})))
    seen: list[frozenset[str]] = []

    def fake_create_repository(_settings, **_hosts):
        # Nothing may have been published yet: the prime happens after the
        # repository (and therefore its migrations) exists, not before.
        seen.append(extension_admission.disabled_plugin_ids())
        return _composed_repository_double(store)

    monkeypatch.setattr(
        bootstrap, "application_extension_runtime", _extension_runtime_double
    )
    monkeypatch.setattr(bootstrap, "create_repository", fake_create_repository)

    repository = bootstrap.create_application_repository(Settings(_env_file=None))

    assert seen == [frozenset()]
    assert repository._runtime.extension_toggles is store
    assert store.reads == 1
    assert extension_admission.disabled_plugin_ids() == frozenset({"corp.disabled"})


def test_composition_fails_loudly_and_closes_the_half_built_repository(monkeypatch):
    """Loud, but not leaky: the repository already owns a pool by this point
    and no caller will ever receive it, so the prime's failure path has to be
    the thing that closes it."""

    from app import bootstrap
    from app.core.config import Settings

    calls: list[str] = []

    monkeypatch.setattr(
        bootstrap, "application_extension_runtime", _extension_runtime_double
    )
    monkeypatch.setattr(
        bootstrap,
        "create_repository",
        lambda _settings, **_hosts: _composed_repository_double(
            _Store(_always_raising()), calls
        ),
    )

    with pytest.raises(RuntimeError, match="toggle store unavailable"):
        bootstrap.create_application_repository(Settings(_env_file=None))

    assert calls == ["close"]
    assert extension_admission.disabled_plugin_ids() == frozenset()


def test_a_failing_close_does_not_replace_the_prime_diagnostic(monkeypatch):
    from app import bootstrap
    from app.core.config import Settings

    repo = _composed_repository_double(_Store(_always_raising()))

    def close_fails():
        raise OSError("pool already gone")

    repo.close = close_fails
    monkeypatch.setattr(
        bootstrap, "application_extension_runtime", _extension_runtime_double
    )
    monkeypatch.setattr(
        bootstrap, "create_repository", lambda _settings, **_hosts: repo
    )

    # The operator must see why the prime failed, not why the cleanup did.
    with pytest.raises(RuntimeError, match="toggle store unavailable"):
        bootstrap.create_application_repository(Settings(_env_file=None))


def test_maintenance_cli_composes_through_the_priming_root():
    """The CLI needs no prime of its own — it must keep using the composition
    root that has one, rather than reaching for the raw repository factory."""

    from app.bootstrap import create_application_repository
    from app.services import maintenance_cli

    assert maintenance_cli.create_repository is create_application_repository


# --- server lifecycle wiring ------------------------------------------------


@pytest.fixture
def recorded_refresher(monkeypatch):
    """Replace the real start with a recorder whose stop lands in ``calls``.

    ``startup_warmup`` imports it lazily inside the function that uses it, so
    patching the module attribute is what the production call actually
    resolves — this is the real edge, not a stand-in for it.

    ``calls`` is deliberately the SAME list the repository double appends its
    ``close`` to: the ordering between the two is the invariant under test
    (the refresher borrows a connection every tick, so stopping it after the
    pool closes would log a database error belonging to nothing), and two
    separate counters cannot express an ordering.
    """

    record = SimpleNamespace(started=[], calls=[])

    def start(store, interval_seconds):
        record.started.append((store, interval_seconds))
        return lambda: record.calls.append("stop")

    monkeypatch.setattr(
        extension_toggles, "start_extension_admission_refresher", start
    )
    return record


@pytest.fixture
def loaded_deployment_plugin(monkeypatch):
    """Pretend this process froze a deployment plugin.

    The stock topology has none, and the refresher is skipped entirely in that
    case (``test_no_deployment_plugins_means_no_polling_thread``), so every
    wiring test below has to opt in explicitly.
    """

    from app.services import startup_warmup

    monkeypatch.setattr(
        startup_warmup, "_deployment_plugins_are_loaded", lambda: True
    )


def _warmed_repository_double(store, calls):
    return SimpleNamespace(
        _runtime=SimpleNamespace(extension_toggles=store),
        _recover_interrupted_jobs=lambda: None,
        warm_open_path_caches=lambda **_kwargs: calls.append("warm") or 0,
        close=lambda: calls.append("close"),
    )


def _install_repository_double(monkeypatch, repo):
    from app.api import deps
    from app.services import startup_warmup

    def repository():
        return repo

    repository.cache_clear = lambda: None
    monkeypatch.setattr(deps, "repository", repository)
    monkeypatch.setattr(
        startup_warmup, "_reproject_legacy_knowhow_tables", lambda _repo: None
    )


def test_run_startup_starts_the_refresher_and_close_stops_it_before_closing(
    monkeypatch, recorded_refresher, loaded_deployment_plugin
):
    from app.core.config import get_settings
    from app.services import startup_warmup

    store = _Store(_always(frozenset()))
    repo = _warmed_repository_double(store, recorded_refresher.calls)
    _install_repository_double(monkeypatch, repo)

    lease = startup_warmup.begin_lifecycle()
    try:
        assert startup_warmup.run_startup(lease) is repo
        assert recorded_refresher.started == [
            (store, get_settings().extension_admission_refresh_seconds)
        ]
        assert recorded_refresher.calls == ["warm"]
    finally:
        startup_warmup.close_repository(lease, repo)

    # Order, not counts: "stop" must precede "close".
    assert recorded_refresher.calls == ["warm", "stop", "close"]


def test_failed_startup_stops_the_refresher_before_closing_the_pool(
    monkeypatch, recorded_refresher, loaded_deployment_plugin
):
    from app.core import readiness
    from app.services import startup_warmup

    store = _Store(_always(frozenset()))
    calls = recorded_refresher.calls

    def explode(**_kwargs):
        raise RuntimeError("warm-up exploded")

    repo = SimpleNamespace(
        _runtime=SimpleNamespace(extension_toggles=store),
        _recover_interrupted_jobs=lambda: None,
        warm_open_path_caches=explode,
        close=lambda: calls.append("close"),
    )
    _install_repository_double(monkeypatch, repo)

    lease = startup_warmup.begin_lifecycle()
    assert startup_warmup.run_startup(lease) is None
    assert readiness.snapshot()["ready"] is False
    assert len(recorded_refresher.started) == 1
    # Same ordering invariant on the failure path.
    assert calls == ["stop", "close"]

    startup_warmup.close_repository(lease, None)
    # The handle was taken out of the lifecycle state, so the tombstone
    # release cannot stop it a second time.
    assert calls == ["stop", "close"]


def test_run_startup_passes_the_configured_interval_not_a_literal(
    monkeypatch, recorded_refresher, loaded_deployment_plugin
):
    from app.core.config import get_settings
    from app.services import startup_warmup

    monkeypatch.setenv("EXTENSION_ADMISSION_REFRESH_SECONDS", "42")
    get_settings.cache_clear()
    store = _Store(_always(frozenset()))
    repo = _warmed_repository_double(store, recorded_refresher.calls)
    _install_repository_double(monkeypatch, repo)

    lease = startup_warmup.begin_lifecycle()
    try:
        assert startup_warmup.run_startup(lease) is repo
        assert recorded_refresher.started == [(store, 42.0)]
    finally:
        startup_warmup.close_repository(lease, repo)
        # monkeypatch restores the env var; the cache must not keep 42.
        get_settings.cache_clear()


def test_no_deployment_plugins_means_no_polling_thread(
    monkeypatch, recorded_refresher, caplog
):
    """The stock deployment — and therefore the whole test suite — must pay
    nothing for this feature: built-in bundles can never be admin-disabled, so
    there is nothing for a poll to discover."""

    from app.services import startup_warmup

    caplog.set_level(logging.INFO, logger="silicon_notebook.startup")
    assert startup_warmup._deployment_plugins_are_loaded() is False

    store = _Store(_always(frozenset()))
    repo = _warmed_repository_double(store, recorded_refresher.calls)
    _install_repository_double(monkeypatch, repo)

    lease = startup_warmup.begin_lifecycle()
    try:
        assert startup_warmup.run_startup(lease) is repo
        assert recorded_refresher.started == []
    finally:
        startup_warmup.close_repository(lease, repo)

    assert recorded_refresher.calls == ["warm", "close"]
    assert any(
        "no deployment plugins loaded" in record.getMessage()
        for record in caplog.records
    )
    assert not _refresher_threads()


def test_lifespan_passthrough_starts_no_refresher(
    recorded_refresher, loaded_deployment_plugin
):
    import asyncio

    from app.core import readiness
    from app.main import _lifespan

    readiness.mark_ready()  # a pre-marked-ready context owns no lifecycle

    async def exercise() -> None:
        async with _lifespan(SimpleNamespace()):
            pass

    asyncio.run(exercise())

    assert recorded_refresher.started == []
    assert recorded_refresher.calls == []
