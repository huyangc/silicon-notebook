"""Agentic Memory P1 (T4): the shared-base consolidation chain.

What this file pins is the set of behaviours that, if quietly broken, cost
either money or the feature itself while every other test stays green:

1. **The threshold gate is deterministic and cheap.** Below it, nothing is
   claimed and nothing is submitted — no model call, no thread. A gate that
   fires per source change turns a background refresh into a per-upload LLM
   bill.
2. **Single flight, and the counter survives the run.** ``claim`` returns the
   ``pending_signal`` snapshot; ``settle`` subtracts exactly that. Signals
   arriving mid-run must therefore still be there afterwards — the earlier
   ``reset_signal=True`` contract silently discarded every change made while a
   consolidation was in flight.
3. **Every exit path settles the row**, including ``BaseException`` and a
   submit that never started a thread. The chain's single-flight slot is a
   durable row: a run that exits without settling holds that notebook's chain
   until the next process restart, and every later trigger silently no-ops.
4. **Fail-open keeps the previous blocks.** An unconfigured model, an empty
   reply, a malformed reply or an unknown label must leave the existing
   understanding exactly as it was — never half-written, never cleared.
5. **User-edited blocks are authority** (design §5.4): they reach the prompt
   marked as such, so the job adds to them rather than replacing them.
6. **The event is counts-only.** No block text, no document ids, no owner.

The isolation property itself (this chain structurally cannot read usage data)
is pinned statically by ``test_agent_profile_isolation_guard.py`` — a runtime
test can only show that today's code path did not, not that tomorrow's cannot.

Built directly on the stores plus a bare migrated ``SqliteDatabase`` rather
than through the full ``SQLiteRepository`` composition, for the same reason
``test_agent_profile_store.py`` is: what is under test is this service's
protocol, and the full repository would drag parsing, embedding and the KG
pipeline into a file about a threshold counter and a settle discipline.
"""
from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest

from app.core.config import Settings
from app.repositories.ports import (
    AGENT_PROFILE_MALFORMED_MESSAGE,
    AGENT_PROFILE_MODEL_UNAVAILABLE_MESSAGE,
    AGENT_PROFILE_RESTART_FAILURE_MESSAGE,
    AGENT_PROFILE_SUBMISSION_FAILED_MESSAGE,
)
from app.repositories.sqlite.agent_profile_store import AgentProfileStore
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.migrations import SqliteMigrator
from app.repositories.sqlite.query_store import QueryStore
from app.repositories.sqlite.source_store import SourceStore
from app.services import background_jobs
from app.services.agent_profile_job import (
    AGENT_PROFILE_EVIDENCE_MAX_IDS,
    AGENT_PROFILE_WORKLOAD,
    BASE_CHAIN_OWNER,
    AgentProfileConsolidationService,
    CorpusStats,
    render_current_blocks,
)

NOW = "2026-08-18T00:00:00+00:00"
NOTEBOOK_ID = "nb-1"
OTHER_NOTEBOOK_ID = "nb-2"


# --------------------------------------------------------------------- doubles
class _Client:
    """A stub consolidation model. ``reply`` maps the prompt to raw JSON text."""

    settings = None  # no ``settings`` -> cap_kwargs stays off

    def __init__(self, reply):
        self.reply = reply
        self.prompts: list[str] = []

    def chat_json(self, messages, schema_hint, **kwargs):
        prompt = messages[0]["content"]
        self.prompts.append(prompt)
        return self.reply(prompt) if callable(self.reply) else self.reply


class _Models:
    def __init__(self, client: _Client | None):
        self.client = client

    def configured(self, workload_id: str) -> bool:
        assert workload_id == AGENT_PROFILE_WORKLOAD
        return self.client is not None

    def chat(self, workload_id: str):
        assert workload_id == AGENT_PROFILE_WORKLOAD
        assert self.client is not None
        return self.client


class _EventLog:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def emit(self, event, **_kwargs) -> None:
        self.events.append(event)


class _Submitter:
    """Records submissions instead of starting threads.

    Synchronous by default: the run is what most of these tests are about, and
    a real background pool would make every assertion a poll loop. The
    scheduling contract itself (which pool this job lands in) is pinned
    separately against ``background_jobs`` own registry, which is where that
    decision actually lives.
    """

    def __init__(self, *, run: bool = True, fail: BaseException | None = None):
        self.run = run
        self.fail = fail
        self.calls: list[dict] = []

    def __call__(self, fn, *args, name=None, notify_pending=False, **kwargs):
        self.calls.append({"name": name, "args": args, "notify_pending": notify_pending})
        if self.fail is not None:
            raise self.fail
        if self.run:
            fn(*args, **kwargs)
        return None


# -------------------------------------------------------------------- fixtures
def _settings(tmp_path: Path, **env) -> Settings:
    return Settings(database_url=f"sqlite:///{tmp_path / 'test.db'}", **env)


@pytest.fixture
def harness(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENT_PROFILE_BASE_TRIGGER", "3")
    settings = _settings(tmp_path)
    database = SqliteDatabase(settings, tmp_path)
    assert SqliteMigrator(database, settings).migrate()

    with database.write() as db:
        db.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,"
            "updated_at) VALUES (?,?,?,?,?,?,?)",
            ("user-owner", "owner@example.test", "Owner", "admin", "active", NOW, NOW),
        )
        for notebook_id in (NOTEBOOK_ID, OTHER_NOTEBOOK_ID):
            db.execute(
                "INSERT INTO notebooks(id,name,purpose,primary_domain,status,"
                "created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (notebook_id, "NB", "", "engineering", "ready", "user-owner", NOW, NOW),
            )

    # The KG type-count memo is process-global and keyed by (notebook id,
    # kg_mutation_seq) — two tests reusing "nb-1" over two different temporary
    # databases would otherwise read each other's cached counts.
    from app.repositories.sqlite import knowledge_counts_cache

    for notebook_id in (NOTEBOOK_ID, OTHER_NOTEBOOK_ID):
        knowledge_counts_cache.invalidate(notebook_id)

    # A COUNTING id seam, not a constant one: this store mints the chain's
    # ``claim_token`` generation, and two claims sharing a token would be
    # indistinguishable — the very thing the token exists to prevent.
    claim_ids = itertools.count(1)
    profiles = AgentProfileStore(
        database, new_id=lambda p: f"{p}-{next(claim_ids)}", now=lambda: NOW
    )
    return {
        "settings": settings,
        "database": database,
        "profiles": profiles,
        "sources": SourceStore(database, now=lambda: NOW),
        "queries": QueryStore(database, settings),
    }


def _run_base(service, claimed):
    """Run exactly the way ``start_base`` does: the claim's snapshot AND its
    generation token (Agentic Memory P2). ``claim_token`` is keyword-only on
    ``run_base`` (P2-T2 tightening) — no default, so a caller cannot forget it
    and silently reintroduce the ABA a missing token used to open."""
    return service.run_base(
        NOTEBOOK_ID, claimed.pending_signal, claim_token=claimed.token
    )


def _service(harness, *, client: _Client | None = None):
    return AgentProfileConsolidationService(
        settings=harness["settings"],
        profiles=harness["profiles"],
        database=harness["database"],
        sources=harness["sources"],
        queries=harness["queries"],
        models=_Models(client),
        event_log=harness.setdefault("event_log", _EventLog()),
    )


def _with_submitter(monkeypatch, submitter):
    monkeypatch.setattr(background_jobs, "submit", submitter)
    return submitter


def _add_source(
    harness,
    source_id: str,
    *,
    notebook_id: str = NOTEBOOK_ID,
    source_type: str = "upload",
    parse_status: str = "extracted",
    elements: tuple[tuple[str, int], ...] = (),
) -> None:
    with harness["database"].write() as db:
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,status,"
            "parse_status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (source_id, notebook_id, source_id, source_type, "active",
             parse_status, NOW, NOW),
        )
        index = 0
        for element_type, count in elements:
            for _ in range(count):
                index += 1
                db.execute(
                    "INSERT INTO source_elements(id,source_id,element_type,"
                    "location_label,text,created_at) VALUES (?,?,?,?,?,?)",
                    (f"el-{source_id}-{index}", source_id, element_type,
                     "p.1", "x", NOW),
                )


def _add_kg_object(
    harness, object_id: str, object_type: str, *, source_id: str = ""
) -> None:
    with harness["database"].write() as db:
        db.execute(
            "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,"
            "source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
            (object_id, NOTEBOOK_ID, object_type, "approved", source_id, NOW, NOW),
        )


def _add_cluster(
    harness, canonical_id: str, member_object_id: str, canonical_name: str
) -> None:
    """One ``concept_clusters`` member row — the only place a concept's NAME is
    materialized (names live inside ``knowledge_objects.payload`` JSON)."""
    with harness["database"].write() as db:
        db.execute(
            "INSERT INTO concept_clusters(id,notebook_id,canonical_id,"
            "member_object_id,canonical_name,object_type,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (f"cc-{member_object_id}", NOTEBOOK_ID, canonical_id,
             member_object_id, canonical_name, "concept", NOW),
        )


def _reply(blocks) -> str:
    return json.dumps({"blocks": blocks})


def _job(harness, notebook_id: str = NOTEBOOK_ID) -> dict | None:
    return harness["profiles"].job_row(notebook_id, "")


def _blocks(harness, notebook_id: str = NOTEBOOK_ID) -> dict[str, dict]:
    return {
        block["label"]: block
        for block in harness["profiles"].read_blocks(notebook_id, "")
    }


# ------------------------------------------------------------ threshold gate
def test_changes_below_the_threshold_neither_claim_nor_submit(harness, monkeypatch):
    submitter = _Submitter()
    _with_submitter(monkeypatch, submitter)
    service = _service(harness, client=_Client(_reply([])))

    service.note_corpus_change(NOTEBOOK_ID)
    service.note_corpus_change(NOTEBOOK_ID)

    assert submitter.calls == []
    row = _job(harness)
    assert row["pending_signal"] == 2
    assert row["status"] == "idle"
    assert row["runs"] == 0


def test_reaching_the_threshold_submits_exactly_one_light_job(harness, monkeypatch):
    client = _Client(_reply([{"label": "corpus_shape", "value": "手册为主", "evidence": []}]))
    submitter = _Submitter()
    _with_submitter(monkeypatch, submitter)
    service = _service(harness, client=client)

    for _ in range(3):
        service.note_corpus_change(NOTEBOOK_ID)

    assert [call["name"] for call in submitter.calls] == [f"agentprofile-{NOTEBOOK_ID}"]
    assert submitter.calls[0]["notify_pending"] is False
    row = _job(harness)
    assert row["status"] == "done"
    # Exactly the three signals the run was handed, and no more.
    assert row["pending_signal"] == 0
    assert row["runs"] == 1
    assert _blocks(harness)["corpus_shape"]["value"] == "手册为主"


def test_the_threshold_is_per_notebook(harness, monkeypatch):
    submitter = _Submitter(run=False)
    _with_submitter(monkeypatch, submitter)
    service = _service(harness, client=_Client(_reply([])))

    service.note_corpus_change(NOTEBOOK_ID)
    service.note_corpus_change(NOTEBOOK_ID)
    service.note_corpus_change(OTHER_NOTEBOOK_ID)

    assert submitter.calls == []
    assert _job(harness)["pending_signal"] == 2
    assert _job(harness, OTHER_NOTEBOOK_ID)["pending_signal"] == 1


def test_the_kill_switch_stops_the_chain_before_any_write(harness, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_PROFILE_ENABLED", "false")
    harness["settings"] = _settings(tmp_path)
    assert harness["settings"].agent_profile_enabled is False
    submitter = _Submitter()
    _with_submitter(monkeypatch, submitter)
    service = _service(harness, client=_Client(_reply([])))

    for _ in range(5):
        service.note_corpus_change(NOTEBOOK_ID)

    assert submitter.calls == []
    # Not even the counter moves: "off" means the feature left no trace at all.
    assert _job(harness) is None


def test_a_failing_store_never_breaks_the_ingestion_pipeline(harness, monkeypatch):
    service = _service(harness, client=_Client(_reply([])))

    def _boom(*_args, **_kwargs):
        raise RuntimeError("database is on fire")

    monkeypatch.setattr(harness["profiles"], "bump_signal", _boom)

    service.note_corpus_change(NOTEBOOK_ID)  # must not raise


# --------------------------------------------------------------- single flight
def test_a_change_during_a_run_does_not_start_a_second_chain(harness, monkeypatch):
    """The second trigger finds the slot held and leaves its signal pending."""
    submitter = _Submitter(run=False)  # claim taken, worker never runs
    _with_submitter(monkeypatch, submitter)
    service = _service(harness, client=_Client(_reply([])))

    for _ in range(3):
        service.note_corpus_change(NOTEBOOK_ID)
    assert len(submitter.calls) == 1
    assert _job(harness)["status"] == "running"

    for _ in range(3):
        service.note_corpus_change(NOTEBOOK_ID)

    assert len(submitter.calls) == 1, "a running chain must not be claimed again"
    assert _job(harness)["pending_signal"] == 6


def test_signals_arriving_during_a_run_survive_the_settle(harness, monkeypatch):
    """``consumed`` is the claim snapshot, not "everything pending now".

    Three changes trigger the run; three more land while it is in flight. If
    the settle zeroed the counter those three would be lost and the corpus
    would stay un-reconsolidated until three MORE arrived.
    """
    bumped: list[int] = []

    def reply(_prompt: str) -> str:
        # 只在第一轮的模型调用里攒满一个阈值;第二轮(重排出来的)不再攒。
        if not bumped:
            for _ in range(3):
                bumped.append(harness["profiles"].bump_signal(NOTEBOOK_ID, ""))
        return _reply([{"label": "corpus_shape", "value": "v", "evidence": []}])

    _with_submitter(monkeypatch, _Submitter())
    client = _Client(reply)
    service = _service(harness, client=client)

    for _ in range(3):
        service.note_corpus_change(NOTEBOOK_ID)

    # 运行期间攒进来的三个信号确实活过了 settle(bump 观察到 4/5/6)……
    assert bumped == [4, 5, 6]
    # ……而且不再滞留:codex R1 P2 的收尾自查发现剩余 pending 已达阈值,
    # 自动重排并消费掉了它们——总共两轮,不需要等下一次来源变更。
    assert client.prompts and len(client.prompts) == 2
    assert _job(harness)["pending_signal"] == 0
    assert _job(harness)["status"] == "done"


def test_a_superseded_settle_skips_the_leftover_recheck(harness, monkeypatch):
    """spec P2-1 / 质量 P3-3(变异 6):``run_base`` 的四处结算收尾都必须把真实
    settle 结果传给 ``_maybe_requeue_base``——修复前这四处调用一律不传参数,
    等价于永远假装 settle 成功(``AGENT_PROFILE_SETTLED``)。

    ``superseded`` 时一个更新的世代已经持有这条链路的 slot,而且会在它自己的
    终态跑同一次剩余复查(见 ``_maybe_requeue_base`` 自己的文档);旧世代如果
    假装自己正常结算,不但多做一次没用的读,还可能在新世代已经跑完、留下一份
    够阈值的 ``pending_signal`` 时抢claim 出一份不该存在的第三世代。"""
    submitter = _Submitter(run=False)
    monkeypatch.setattr(background_jobs, "submit", submitter)
    profiles = harness["profiles"]

    def reply(_prompt: str) -> str:
        # 模拟一次手动重建的竞态(run_base 自己文档里点名的场景):旧世代还
        # 卡在模型调用里的时候,链路已经被重新认领、跑完并落终态——留下一份
        # 够触发下一轮的 pending_signal。
        profiles.clear_job_row(NOTEBOOK_ID, BASE_CHAIN_OWNER)
        newer = profiles.claim(NOTEBOOK_ID, BASE_CHAIN_OWNER)
        for _ in range(3):
            profiles.bump_signal(NOTEBOOK_ID, BASE_CHAIN_OWNER)
        profiles.settle(
            NOTEBOOK_ID,
            BASE_CHAIN_OWNER,
            "done",
            claim_token=newer.token,
            consumed=0,
        )
        return _reply([{"label": "corpus_shape", "value": "v", "evidence": []}])

    service = _service(harness, client=_Client(reply))
    stale = profiles.claim(NOTEBOOK_ID, BASE_CHAIN_OWNER)

    _run_base(service, stale)

    assert _job(harness)["status"] == "done"
    assert _job(harness)["pending_signal"] == 3, "新世代自己的终态还没跑复查"
    assert submitter.calls == [], (
        "settle 报 superseded 时不该再抢一次——行已终态,那次抢会成功,"
        "白起一份不该存在的第三世代"
    )


def test_a_failed_run_still_consumes_its_batch(harness, monkeypatch):
    """Failure consumes the claim snapshot, exactly like success.

    Not a bookkeeping preference — a cost gate. If a failed run kept its
    signal the counter would still sit at the threshold, so the very NEXT
    source change would fire another call: a provider returning malformed JSON
    would be billed once per upload for as long as it stays broken.
    """
    _with_submitter(monkeypatch, _Submitter())
    service = _service(harness, client=None)  # model unconfigured

    for _ in range(3):
        service.note_corpus_change(NOTEBOOK_ID)

    row = _job(harness)
    assert row["status"] == "failed"
    assert row["pending_signal"] == 0


def test_a_broken_provider_costs_one_call_per_threshold_batch(harness, monkeypatch):
    """The cost gate, end to end: a model that always returns garbage must not
    be called once per upload.

    Six changes at a threshold of three = exactly two calls. Without the
    consume-on-failure rule the counter would stay at three after the first
    failure and every subsequent change would trigger a fresh one — five calls
    here, and unbounded in a real import.
    """
    calls: list[str] = []

    def reply(prompt: str) -> str:
        calls.append(prompt)
        return "not json at all"

    _with_submitter(monkeypatch, _Submitter())
    service = _service(harness, client=_Client(reply))

    for _ in range(6):
        service.note_corpus_change(NOTEBOOK_ID)

    assert len(calls) == 2
    assert _job(harness)["pending_signal"] == 0
    assert _job(harness)["runs"] == 2


def test_an_interrupted_run_also_consumes_its_batch(harness):
    def reply(_prompt: str) -> str:
        raise KeyboardInterrupt()

    service = _service(harness, client=_Client(reply))
    harness["profiles"].bump_signal(NOTEBOOK_ID, "")
    harness["profiles"].bump_signal(NOTEBOOK_ID, "")
    claimed = harness["profiles"].claim(NOTEBOOK_ID, "")

    with pytest.raises(KeyboardInterrupt):
        _run_base(service, claimed)

    assert _job(harness)["pending_signal"] == 0


def test_a_claim_that_never_ran_consumes_nothing(harness, monkeypatch):
    """The one path that legitimately keeps the signal: the submit raised, so
    no worker ever looked at those changes. Charging them would drop three real
    corpus changes on the floor for a pool error that has nothing to do with
    them."""
    _with_submitter(monkeypatch, _Submitter(fail=RuntimeError("pool is closed")))
    service = _service(harness, client=_Client(_reply([])))

    for _ in range(3):
        service.note_corpus_change(NOTEBOOK_ID)

    assert _job(harness)["pending_signal"] == 3


# ------------------------------------------------------------- terminal states
def test_a_submit_that_never_starts_a_thread_settles_the_row(harness, monkeypatch):
    submitter = _Submitter(fail=RuntimeError("pool is closed"))
    _with_submitter(monkeypatch, submitter)
    service = _service(harness, client=_Client(_reply([])))

    for _ in range(3):
        service.note_corpus_change(NOTEBOOK_ID)

    row = _job(harness)
    assert row["status"] == "failed"
    assert row["failure_reason"] == AGENT_PROFILE_SUBMISSION_FAILED_MESSAGE
    assert row["diagnostic"] == "job_submission_failed"
    # And the slot is free again: the very next threshold hit must be able to
    # claim it, which is the whole point of settling here.
    assert harness["profiles"].claim(NOTEBOOK_ID, "") is not None


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_a_base_exception_inside_the_run_still_settles_the_row(harness, interrupt):
    def reply(_prompt: str) -> str:
        raise interrupt()

    service = _service(harness, client=_Client(reply))
    claimed = harness["profiles"].claim(NOTEBOOK_ID, "")

    with pytest.raises(interrupt):
        _run_base(service, claimed)

    row = _job(harness)
    assert row["status"] == "failed"
    assert row["diagnostic"] == "worker_interrupted"


def test_an_ordinary_exception_settles_and_propagates(harness):
    def reply(_prompt: str) -> str:
        raise ValueError("provider exploded")

    service = _service(harness, client=_Client(reply))
    claimed = harness["profiles"].claim(NOTEBOOK_ID, "")

    with pytest.raises(ValueError):
        _run_base(service, claimed)

    assert _job(harness)["diagnostic"] == "internal_error"
    assert _job(harness)["status"] == "failed"


def test_the_startup_sweep_settles_a_stranded_running_chain(harness):
    service = _service(harness, client=_Client(_reply([])))
    assert harness["profiles"].claim(NOTEBOOK_ID, "") is not None

    assert service.sweep_on_start() == 1

    row = _job(harness)
    assert row["status"] == "failed"
    assert row["failure_reason"] == AGENT_PROFILE_RESTART_FAILURE_MESSAGE


def test_the_startup_sweep_runs_even_with_the_feature_switched_off(
    harness, monkeypatch, tmp_path
):
    """Otherwise a crash while the feature was on would strand rows forever,
    and switching it back on would find every notebook permanently busy."""
    monkeypatch.setenv("AGENT_PROFILE_ENABLED", "false")
    harness["settings"] = _settings(tmp_path)
    service = _service(harness, client=None)
    assert harness["profiles"].claim(NOTEBOOK_ID, "") is not None

    assert service.sweep_on_start() == 1


# ------------------------------------------------------------------ fail-open
def test_an_unconfigured_model_fails_the_chain_and_keeps_the_blocks(harness):
    harness["profiles"].write_block(
        NOTEBOOK_ID, "", "corpus_shape", value="旧的理解", evidence=[],
        expected_revision=0, origin="job", actor="",
    )
    service = _service(harness, client=None)
    claimed = harness["profiles"].claim(NOTEBOOK_ID, "")

    _run_base(service, claimed)

    row = _job(harness)
    assert row["status"] == "failed"
    assert row["failure_reason"] == AGENT_PROFILE_MODEL_UNAVAILABLE_MESSAGE
    assert _blocks(harness)["corpus_shape"]["value"] == "旧的理解"


@pytest.mark.parametrize(
    "raw, diagnostic",
    [
        ("", "empty_reply"),
        ("not json at all", "unparsable_reply"),
        ("[1, 2, 3]", "unparsable_reply"),
        ('{"blocks": "corpus_shape"}', "blocks_not_a_list"),
        ('{"blocks": ["corpus_shape"]}', "block_not_an_object"),
        ('{"blocks": [{"label": "retrieval_notes", "value": "x"}]}', "unknown_label"),
        # Type errors are STRUCTURAL, not salvageable: without the check
        # ``str({"text": ...})`` would store the literal characters
        # ``{'text': ...}`` as the library's understanding, and that string
        # would then ride in every planning prompt until a person noticed.
        (
            '{"blocks": [{"label": "corpus_shape", "value": {"text": "x"}}]}',
            "value_not_a_string",
        ),
        ('{"blocks": [{"label": "corpus_shape", "value": 42}]}', "value_not_a_string"),
        ('{"blocks": [{"label": "corpus_shape"}]}', "value_not_a_string"),
        (
            '{"blocks": [{"label": "corpus_shape", "value": "x", "evidence": "src-a"}]}',
            "evidence_not_a_list",
        ),
        # 退役标记(codex #520 R2 P2)也是结构:`retire` 只认字面 true,truthy 的
        # `1` / `"true"` 是模型在自创协议,而它自创的下一版可能是 `"false"`。
        (
            '{"blocks": [{"label": "corpus_shape", "retire": 1}]}',
            "retire_not_true",
        ),
        (
            '{"blocks": [{"label": "corpus_shape", "retire": "true"}]}',
            "retire_not_true",
        ),
        (
            '{"blocks": [{"label": "corpus_shape", "retire": false}]}',
            "retire_not_true",
        ),
        # 「既要退役又给正文」两个意思互相矛盾,猜哪一半都是错的。
        (
            '{"blocks": [{"label": "corpus_shape", "retire": true, "value": "还有话说"}]}',
            "retire_with_value",
        ),
    ],
)
def test_an_unusable_reply_keeps_every_previous_block(harness, raw, diagnostic):
    """Including the overlay labels: this chain has read nothing that could
    support ``retrieval_notes``/``usage_gaps``, so a reply naming one did not
    answer the question that was asked."""
    harness["profiles"].write_block(
        NOTEBOOK_ID, "", "corpus_shape", value="旧的理解", evidence=[],
        expected_revision=0, origin="job", actor="",
    )
    service = _service(harness, client=_Client(raw))
    claimed = harness["profiles"].claim(NOTEBOOK_ID, "")

    _run_base(service, claimed)

    row = _job(harness)
    assert row["status"] == "failed"
    assert row["failure_reason"] == AGENT_PROFILE_MALFORMED_MESSAGE
    assert row["diagnostic"] == diagnostic
    assert _blocks(harness)["corpus_shape"]["value"] == "旧的理解"
    assert _blocks(harness)["corpus_shape"]["revision"] == 1


def test_one_bad_label_discards_the_whole_reply_not_just_that_block(harness):
    service = _service(
        harness,
        client=_Client(
            _reply(
                [
                    {"label": "corpus_shape", "value": "会被一起丢弃", "evidence": []},
                    {"label": "nonsense", "value": "x", "evidence": []},
                ]
            )
        ),
    )
    claimed = harness["profiles"].claim(NOTEBOOK_ID, "")

    _run_base(service, claimed)

    assert _blocks(harness) == {}
    assert _job(harness)["status"] == "failed"


def test_an_empty_value_omits_the_block_instead_of_clearing_it(harness):
    harness["profiles"].write_block(
        NOTEBOOK_ID, "", "corpus_shape", value="保留我", evidence=[],
        expected_revision=0, origin="job", actor="",
    )
    service = _service(
        harness,
        client=_Client(_reply([{"label": "corpus_shape", "value": "  ", "evidence": []}])),
    )
    claimed = harness["profiles"].claim(NOTEBOOK_ID, "")

    _run_base(service, claimed)

    assert _blocks(harness)["corpus_shape"]["value"] == "保留我"
    assert _job(harness)["status"] == "done"
    assert _job(harness)["blocks_written"] == 0


def test_a_block_edited_mid_run_is_skipped_and_not_retried(harness):
    """The person's edit wins. Re-applying a value computed BEFORE their edit
    would overwrite it with a slower race, which is worse than skipping."""
    harness["profiles"].write_block(
        NOTEBOOK_ID, "", "corpus_shape", value="v1", evidence=[],
        expected_revision=0, origin="job", actor="",
    )

    def reply(_prompt: str) -> str:
        harness["profiles"].write_block(
            NOTEBOOK_ID, "", "corpus_shape", value="用户刚改的", evidence=[],
            expected_revision=1, origin="user", actor="user-owner",
        )
        return _reply(
            [
                {"label": "corpus_shape", "value": "模型写的", "evidence": []},
                {"label": "corpus_gaps", "value": "三份文档没有内容", "evidence": []},
            ]
        )

    service = _service(harness, client=_Client(reply))
    claimed = harness["profiles"].claim(NOTEBOOK_ID, "")

    _run_base(service, claimed)

    blocks = _blocks(harness)
    assert blocks["corpus_shape"]["value"] == "用户刚改的"
    assert blocks["corpus_gaps"]["value"] == "三份文档没有内容"
    row = _job(harness)
    assert row["status"] == "done"
    assert row["blocks_written"] == 1
    assert "cas_conflict:corpus_shape" in row["diagnostic"]


# ------------------------------------------------------------------- retiring
def test_a_job_written_block_can_be_retired_and_keeps_its_history(harness):
    """codex #520 R2 P2: without a withdrawal channel the prompt's "omission
    keeps the previous value" rule is a ratchet — a block written from
    documents that were since deleted rides in every planning prompt forever.

    The row and its history stay (this is a withdrawal, not a wipe), the origin
    recorded is ``job`` rather than ``user`` (``clear_block`` hardcodes the
    latter, and a job's decision filed as a person's is a lie in the one record
    that explains why the text disappeared), and the diagnostic names it.
    """
    harness["profiles"].write_block(
        NOTEBOOK_ID, "", "corpus_shape", value="这个库全是热设计手册", evidence=[],
        expected_revision=0, origin="job", actor="",
    )
    service = _service(harness, client=_Client(
        _reply([{"label": "corpus_shape", "retire": True}])
    ))

    _run_base(service, harness["profiles"].claim(NOTEBOOK_ID, ""))

    block = _blocks(harness)["corpus_shape"]
    assert block["value"] == ""
    assert block["updated_origin"] == "job"
    assert block["revision"] == 2
    assert block["history"], "退役必须留在历史里——这是一次撤回,不是抹掉这一行"
    row = _job(harness)
    assert row["status"] == "done"
    assert row["blocks_written"] == 1
    assert "retired:corpus_shape" in row["diagnostic"]


def test_a_user_written_block_is_never_retired(harness):
    """设计 §5.4:人写的块是权威输入,也是冷启动通道(用户直接告诉 agent 这个库是
    什么)。模型判定它「统计支持不了」而撤掉,删掉的正是这里唯一不是猜测的输入。

    拒绝是**过滤降级**而不是整份作废:同一次回复里别的块可能完全站得住。"""
    harness["profiles"].write_block(
        NOTEBOOK_ID, "", "corpus_shape", value="这是工具手册库", evidence=[],
        expected_revision=0, origin="user", actor="user-owner",
    )
    service = _service(harness, client=_Client(
        _reply([
            {"label": "corpus_shape", "retire": True},
            {"label": "corpus_gaps", "value": "没有图片", "evidence": []},
        ])
    ))

    _run_base(service, harness["profiles"].claim(NOTEBOOK_ID, ""))

    blocks = _blocks(harness)
    assert blocks["corpus_shape"]["value"] == "这是工具手册库"
    assert blocks["corpus_shape"]["revision"] == 1, "用户那一行根本不该被写"
    assert blocks["corpus_gaps"]["value"] == "没有图片", "同一次回复里的其他块被连坐"
    row = _job(harness)
    assert row["status"] == "done"
    assert "retire_refused:corpus_shape" in row["diagnostic"]
    assert "retired:" not in row["diagnostic"]


def test_retiring_a_block_that_has_nothing_to_withdraw_is_not_a_write(harness):
    """没有行、或行已经是空的,退役就是无事发生:把它记成一次写入会让
    ``blocks_written`` 报出没发生过的工作。"""
    service = _service(harness, client=_Client(
        _reply([{"label": "corpus_shape", "retire": True}])
    ))

    _run_base(service, harness["profiles"].claim(NOTEBOOK_ID, ""))

    row = _job(harness)
    assert row["status"] == "done"
    assert row["blocks_written"] == 0
    assert row["diagnostic"] == ""
    assert _blocks(harness) == {}


# ------------------------------------------------------- user authority input
def test_a_user_edited_block_reaches_the_prompt_marked_as_authoritative(harness):
    harness["profiles"].write_block(
        NOTEBOOK_ID, "", "corpus_shape", value="这是工具手册库", evidence=[],
        expected_revision=0, origin="user", actor="user-owner",
    )
    harness["profiles"].write_block(
        NOTEBOOK_ID, "", "corpus_gaps", value="模型上一轮的猜测", evidence=[],
        expected_revision=0, origin="job", actor="",
    )
    client = _Client(_reply([]))
    service = _service(harness, client=client)

    _run_base(service, harness["profiles"].claim(NOTEBOOK_ID, ""))

    prompt = client.prompts[0]
    assert "- corpus_shape (user-authored): 这是工具手册库" in prompt
    assert "- corpus_gaps: 模型上一轮的猜测" in prompt
    assert "authoritative" in prompt


# ------------------------------------------------------ evidence liveness (D)
#
# codex #520 P2-T1: without this, a block built on a document that has since
# been deleted or reparsed away rides in every planning prompt forever —
# ``retire`` (R2 P2) exists but nothing ever told the model a claim's evidence
# had gone missing. These are pure-function tests against
# ``render_current_blocks``/``CorpusStats`` directly: what matters here is the
# liveness partition (served / still-in-the-library / gone), which has
# nothing to do with the claim/settle machinery the rest of this file drives
# through a real database.


def _stats(*, served: tuple[str, ...] = (), visible: tuple[str, ...] = ()) -> CorpusStats:
    return CorpusStats(
        documents=0,
        per_document=(),
        documents_without_elements=0,
        element_totals={},
        element_document_counts={},
        kg_objects=(),
        served_ids=frozenset(served),
        visible_ids=frozenset(visible),
    )


def _job_block(
    label: str, source_ids: tuple[str, ...], *, value: str = "v", owner_id: str = ""
) -> dict:
    return {
        "label": label,
        "value": value,
        "owner_id": owner_id,
        "updated_origin": "job",
        "evidence": [{"claim_index": 0, "source_ids": list(source_ids)}],
    }


def test_evidence_still_in_the_current_statistics_is_named(harness):
    blocks = [_job_block("corpus_shape", ("s-a", "s-b"))]
    stats = _stats(served=("s-a", "s-b"), visible=("s-a", "s-b"))

    prompt = render_current_blocks(blocks, stats)

    assert "supported by: s-a, s-b" in prompt
    assert "no longer in the library" not in prompt
    assert "still in the library" not in prompt
    assert "all supporting documents are gone" not in prompt


def test_evidence_outside_the_sample_but_still_visible_is_not_reported_as_gone(harness):
    """The key anti-regression case: liveness must be judged against
    ``visible_ids`` (the
    FULL user-visible set), never ``served_ids`` (capped to
    ``AGENT_PROFILE_STATS_MAX_DOCUMENTS`` and only documents with a listed
    element kind). A document that merely fell outside the sample — or is
    healthy prose with nothing to list — is still in the library and must
    never be reported gone, or the model would be steered into retiring a
    claim that is still true."""
    blocks = [_job_block("corpus_shape", ("s-a", "s-b", "s-c"))]
    # s-a is served; s-b/s-c are visible (still in the library) but did not
    # make the sampled statistics.
    stats = _stats(served=("s-a",), visible=("s-a", "s-b", "s-c"))

    prompt = render_current_blocks(blocks, stats)

    assert "supported by: s-a" in prompt
    assert "+2 more still in the library" in prompt
    assert "no longer in the library" not in prompt
    assert "all supporting documents are gone" not in prompt


def test_evidence_entirely_gone_renders_the_explicit_marker(harness):
    blocks = [_job_block("corpus_shape", ("s-a", "s-b"))]
    stats = _stats(served=(), visible=())

    prompt = render_current_blocks(blocks, stats)

    assert "[all supporting documents are gone]" in prompt
    assert "supported by" not in prompt


def test_a_user_written_block_never_renders_an_evidence_line(harness):
    blocks = [
        {
            "label": "corpus_shape",
            "value": "这是工具手册库",
            "owner_id": "",
            "updated_origin": "user",
            "evidence": [{"claim_index": 0, "source_ids": ["s-a"]}],
        }
    ]
    stats = _stats(served=(), visible=())

    prompt = render_current_blocks(blocks, stats)

    line = next(
        line for line in prompt.splitlines() if line.startswith("- corpus_shape")
    )
    assert line == "- corpus_shape (user-authored): 这是工具手册库"


def test_rendered_ids_are_always_a_subset_of_served_ids(harness):
    """Echo safety (docstring of ``render_current_blocks``): an id outside
    ``served_ids`` that the model copies back into a claim's ``evidence`` is
    silently dropped by ``parse_base_reply``'s per-entry salvage (tallied only
    in ``evidence_dropped``) — the stored block would quietly lose a citation
    the model believes it made. So only ids the parser will accept back are
    ever spelled out."""
    blocks = [_job_block("corpus_shape", ("s-a", "s-b", "s-c"))]
    stats = _stats(served=("s-a",), visible=("s-a", "s-b"))

    prompt = render_current_blocks(blocks, stats)

    named_segment = prompt.split("supported by: ", 1)[1].split("]")[0].split(";")[0]
    named_ids = {piece.strip() for piece in named_segment.split(",")}
    assert named_ids <= stats.served_ids
    assert named_ids == {"s-a"}


def test_the_liveness_suffix_reuses_the_existing_evidence_cap(harness):
    """No new number: the same ``AGENT_PROFILE_EVIDENCE_MAX_IDS`` the write
    side already caps ``evidence`` at is the only cap applied here."""
    served = tuple(f"s-{i}" for i in range(AGENT_PROFILE_EVIDENCE_MAX_IDS + 5))
    blocks = [_job_block("corpus_shape", served)]
    stats = _stats(served=served, visible=served)

    prompt = render_current_blocks(blocks, stats)

    named_segment = prompt.split("supported by: ", 1)[1].split("]")[0]
    assert len(named_segment.split(", ")) == AGENT_PROFILE_EVIDENCE_MAX_IDS


def test_a_block_with_no_evidence_renders_no_liveness_suffix(harness):
    """``evidence: []`` is rule 4's own "no single document is the reason" —
    it must not be mistaken for "all supporting documents are gone"."""
    blocks = [_job_block("corpus_shape", ())]
    stats = _stats(served=(), visible=())

    prompt = render_current_blocks(blocks, stats)

    line = next(
        line for line in prompt.splitlines() if line.startswith("- corpus_shape")
    )
    assert line == "- corpus_shape: v"


def test_the_prompt_carries_the_reconciliation_only_rule(harness):
    client = _Client(_reply([]))
    harness["profiles"].write_block(
        NOTEBOOK_ID, "", "corpus_shape", value="v",
        evidence=[{"claim_index": 0, "source_ids": ["s-a"]}],
        expected_revision=0, origin="job", actor="",
    )
    service = _service(harness, client=client)

    _run_base(service, harness["profiles"].claim(NOTEBOOK_ID, ""))

    prompt = client.prompts[0]
    assert "not evidence to reuse" in prompt


# -------------------------------------------------------------------- reading
def test_the_statistics_describe_the_user_visible_corpus_only(harness):
    _add_source(harness, "src-a", elements=(("table", 3), ("formula", 1)))
    _add_source(harness, "src-b", elements=(("table", 1),))
    _add_source(harness, "src-empty")
    _add_source(harness, "src-memory", source_type="memory", elements=(("table", 9),))
    _add_source(harness, "src-knowhow", source_type="knowhow", elements=(("table", 9),))
    _add_source(harness, "src-elsewhere", notebook_id=OTHER_NOTEBOOK_ID,
                elements=(("table", 9),))
    _add_kg_object(harness, "ko-1", "concept")
    _add_kg_object(harness, "ko-2", "concept")
    _add_kg_object(harness, "ko-3", "claim")

    service = _service(harness, client=_Client(_reply([])))
    stats = service.corpus_stats(NOTEBOOK_ID)

    assert stats.documents == 3
    assert stats.element_totals["table"] == 4
    assert stats.element_totals["formula"] == 1
    assert stats.documents_without_elements == 1
    assert stats.kg_objects == (("concept", 2), ("claim", 1))
    assert [source_id for source_id, _ in stats.per_document] == ["src-a", "src-b"]
    assert stats.served_ids == frozenset({"src-a", "src-b"})


def test_private_memory_never_reaches_the_shared_statistics(harness):
    """A confirmed Memory lives in the notebook but belongs to ONE member, and
    the base block is read by every member. Its documents, its knowledge objects
    and its concept names must all be absent — three separate exclusions, and
    the KG one is the easy miss (``knowledge_objects`` carries no source type,
    so the notebook-wide count includes Memory unless it is subtracted)."""
    _add_source(harness, "src-a", elements=(("table", 1),))
    _add_source(harness, "src-memory", source_type="memory", elements=(("table", 9),))
    _add_kg_object(harness, "ko-shared", "concept", source_id="src-a")
    _add_kg_object(harness, "ko-private", "concept", source_id="src-memory")
    _add_cluster(harness, "can-shared", "ko-shared", "闸门设计")
    _add_cluster(harness, "can-private", "ko-private", "我的私人记忆主题")

    service = _service(harness, client=_Client(_reply([])))
    stats = service.corpus_stats(NOTEBOOK_ID)

    assert stats.documents == 1
    assert stats.kg_objects == (("concept", 1),), "Memory's objects were counted"
    assert [name for name, _members in stats.key_concepts] == ["闸门设计"]

    from app.services.agent_profile_job import render_corpus_block

    assert "我的私人记忆主题" not in render_corpus_block(stats)


def test_the_memory_exclusion_is_carried_by_the_statements_themselves(harness):
    """codex #520 R2 P1: the two aggregates exclude private Memory on their own.

    The service used to read the Memory source ids first and then subtract
    (counts) / pass them in as an exclusion list (names). Those are separate
    reads with no shared snapshot — PostgreSQL runs each at READ COMMITTED —
    so a Memory created or deleted in between made the subtrahend describe a
    different library than the minuend, and made the exclusion list miss a row
    whose CONCEPT NAMES then reached a block every member of a shared notebook
    reads.

    Calling the store methods DIRECTLY is the point: they are handed nothing
    but a notebook and the usable statuses, and the Memory rows still have to
    be gone. And the signatures are pinned so the exclusion cannot become
    optional again — a caller that could omit it is a caller that eventually
    will.
    """
    import inspect

    from app.services.knowledge_contracts import USABLE_STATUSES

    _add_source(harness, "src-a", elements=(("table", 1),))
    _add_source(harness, "src-memory", source_type="memory")
    _add_kg_object(harness, "ko-shared", "concept", source_id="src-a")
    _add_kg_object(harness, "ko-private", "concept", source_id="src-memory")
    _add_kg_object(harness, "ko-unowned", "claim")  # source_id '' — never a Memory
    _add_cluster(harness, "can-shared", "ko-shared", "闸门设计")
    _add_cluster(harness, "can-private", "ko-private", "我的私人记忆主题")

    queries = harness["queries"]
    with harness["database"].connect() as db:
        counts = {
            str(row["object_type"]): int(row["c"])
            for row in queries.knowledge_type_count_rows_excluding_memory(
                db, NOTEBOOK_ID, USABLE_STATUSES
            )
        }
        names = queries.top_concept_names(db, NOTEBOOK_ID, USABLE_STATUSES, 24)

    assert counts == {"concept": 1, "claim": 1}, (
        "语句内排除没生效:私有 Memory 的知识对象被算进了共享底座的计数,"
        "或者没有归属来源的对象被连坐排掉了(它的 source_id 是 ''，不是 Memory)"
    )
    assert [name for name, _members in names] == ["闸门设计"]

    for method in (
        queries.knowledge_type_count_rows_excluding_memory,
        queries.top_concept_names,
    ):
        params = set(inspect.signature(method).parameters)
        assert "exclude_source_ids" not in params, (
            f"{method.__name__} 又收回了调用方传入的排除清单参数。排除必须是语句"
            "自己的性质:参数化它就等于允许某个调用方省掉它,而省掉之后共享块里会"
            "出现某位成员私有 Memory 的内容,没有任何报错。"
        )


def test_the_concept_names_are_ordered_by_support_and_bounded(harness):
    _add_source(harness, "src-a", elements=(("table", 1),))
    for index in range(3):
        _add_kg_object(harness, f"ko-weak-{index}", "concept", source_id="src-a")
    _add_cluster(harness, "can-weak", "ko-weak-0", "边缘概念")
    for index in range(3):
        _add_kg_object(harness, f"ko-strong-{index}", "concept", source_id="src-a")
        _add_cluster(harness, "can-strong", f"ko-strong-{index}", "核心概念")

    service = _service(harness, client=_Client(_reply([])))
    stats = service.corpus_stats(NOTEBOOK_ID)

    assert stats.key_concepts == (("核心概念", 3), ("边缘概念", 1))


def test_parse_failures_are_reported_apart_from_prose_documents(harness):
    """The distinction the whole ``corpus_gaps`` wording turns on: a document
    with no tables/formulas/images/code blocks is prose, not a parse failure.
    Conflating them made the block describe a prose library as unparsed."""
    _add_source(harness, "src-prose")  # parsed, but no listed element kinds
    _add_source(harness, "src-broken", parse_status="failed")
    _add_source(harness, "src-running", parse_status="queued")

    service = _service(harness, client=_Client(_reply([])))
    stats = service.corpus_stats(NOTEBOOK_ID)

    assert stats.documents_without_elements == 3
    assert stats.documents_parse_failed == 1
    assert stats.documents_not_parsed == 1

    from app.services.agent_profile_job import render_corpus_block

    rendered = render_corpus_block(stats)
    assert "documents with no tables/formulas/images/code blocks: 3" in rendered
    assert "documents that failed to parse: 1" in rendered
    assert "documents not finished parsing yet: 1" in rendered


def test_the_consolidation_call_uses_its_own_output_budget(harness):
    """Not ``kg_extract_max_tokens`` (51 200): one malformed reply under that
    budget is fifty thousand billed tokens this parser then discards in full."""
    from app.services.agent_profile_job import AGENT_PROFILE_MAX_OUTPUT_TOKENS

    client = _Client(_reply([]))
    seen: list[dict] = []
    original = client.chat_json

    def spy(messages, schema_hint, **kwargs):
        seen.append(kwargs)
        return original(messages, schema_hint, **kwargs)

    client.chat_json = spy
    service = _service(harness, client=client)

    _run_base(service, harness["profiles"].claim(NOTEBOOK_ID, ""))

    assert seen == [{"max_tokens": AGENT_PROFILE_MAX_OUTPUT_TOKENS}]
    assert AGENT_PROFILE_MAX_OUTPUT_TOKENS <= 8192


def test_base_model_call_binds_the_notebook_artifact_scope(harness):
    from app.services.model_work import ModelPriority, make_model_work_context

    client = _Client(_reply([]))
    contexts = []
    original = client.chat_json

    def spy(messages, schema_hint, **kwargs):
        contexts.append(make_model_work_context(
            workload_id=AGENT_PROFILE_WORKLOAD,
            priority=ModelPriority.BACKGROUND,
        ))
        return original(messages, schema_hint, **kwargs)

    client.chat_json = spy
    service = _service(harness, client=client)
    claimed = harness["profiles"].claim(NOTEBOOK_ID, "")

    _run_base(service, claimed)

    assert len(contexts) == 1
    assert contexts[0].notebook_id == NOTEBOOK_ID
    assert contexts[0].parent_id == claimed.token


def test_evidence_the_statistics_never_served_is_dropped(harness):
    _add_source(harness, "src-a", elements=(("table", 2),))
    service = _service(
        harness,
        client=_Client(
            _reply(
                [
                    {
                        "label": "corpus_gaps",
                        "value": "缺少测试报告",
                        "evidence": ["src-a", "src-invented"],
                    }
                ]
            )
        ),
    )

    _run_base(service, harness["profiles"].claim(NOTEBOOK_ID, ""))

    block = _blocks(harness)["corpus_gaps"]
    assert block["evidence"] == [{"claim_index": 0, "source_ids": ["src-a"]}]
    assert "evidence_dropped:1" in _job(harness)["diagnostic"]


def test_an_oversized_value_is_clipped_before_it_is_stored(harness):
    service = _service(
        harness,
        client=_Client(
            _reply([{"label": "corpus_shape", "value": "长" * 900, "evidence": []}])
        ),
    )

    _run_base(service, harness["profiles"].claim(NOTEBOOK_ID, ""))

    value = _blocks(harness)["corpus_shape"]["value"]
    assert len(value) == 400
    assert value.endswith("…")


# ---------------------------------------------------------------- observation
def test_the_event_carries_counts_only(harness):
    _add_source(harness, "src-a", elements=(("table", 2),))
    service = _service(
        harness,
        client=_Client(
            _reply(
                [{"label": "corpus_shape", "value": "手册库", "evidence": ["src-a"]}]
            )
        ),
    )

    _run_base(service, harness["profiles"].claim(NOTEBOOK_ID, ""))

    event = harness["event_log"].events[-1]
    assert event["kind"] == "agent_profile_consolidated"
    assert event["chain"] == "base"
    assert event["status"] == "done"
    assert event["blocks"] == 1
    assert event["chars"] == 3
    assert event["evidence"] == 1
    assert isinstance(event["latency_ms"], int)
    assert set(event) == {
        "kind", "chain", "notebook_id", "status", "blocks", "chars",
        "evidence", "latency_ms",
    }
    serialized = json.dumps(event, ensure_ascii=False)
    assert "手册库" not in serialized
    assert "src-a" not in serialized
    assert "user-owner" not in serialized


# ------------------------------------------------------------ trigger wiring
def test_both_source_lifecycle_exits_notify_the_chain():
    """The three source lifecycle mouths — add, reparse, delete — must each
    reach ``note_corpus_change``, and there is no fourth one.

    Static, because the alternative is a full ingestion pipeline (parse,
    embed, extract) per assertion for a one-line call; and because what can
    silently disappear here is the CALL, not its effect: drop it and every
    ingestion test stays green while the feature simply never triggers again.
    Add and reparse share ``process_source`` (``parse_source`` delegates to
    it), so two call sites cover all three.
    """
    import ast
    from pathlib import Path as _Path

    import app.services.source_ingestion as ingestion

    tree = ast.parse(_Path(ingestion.__file__).read_text(encoding="utf-8"))
    functions = {
        node.name: node
        for parent in tree.body
        if isinstance(parent, ast.ClassDef)
        for node in parent.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for name in ("process_source", "delete_source"):
        calls = {
            node.func.attr
            for node in ast.walk(functions[name])
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "note_corpus_change" in calls, (
            f"{name} 不再通知库理解巡固链路。这是这个特性**唯一**的自动触发口:"
            "去掉它之后每一条既有用例仍然全绿,只是画像从此再也不更新。"
        )


def test_the_pipeline_hooks_carry_the_injected_callable():
    from app.services.source_ingestion import SourcePipelineHooks

    assert "note_corpus_change" in SourcePipelineHooks.__dataclass_fields__


def test_both_trigger_sites_gate_on_the_visible_source_predicate():
    """Hidden synthetic rows (memory/knowhow projections) must not advance the
    shared counter.

    Static for the same reason the call-site test above is: the failure mode is
    a MISSING guard, and without it every ingestion test stays green while one
    member's private Memory pays for — and triggers — the whole notebook's
    shared refresh. Deleting a Memory-derived source is how a member revokes a
    private Memory; that is not a corpus change anyone else can see.

    Pinned by shape rather than by behaviour: the guard must consume the single
    Python-level spelling of the predicate (``HIDDEN_SYNTHETIC_SOURCE_TYPES``),
    so a hand-rolled ``!= "memory"`` fails here even though it would "work".
    """
    import ast
    from pathlib import Path as _Path

    import app.services.source_ingestion as ingestion

    tree = ast.parse(_Path(ingestion.__file__).read_text(encoding="utf-8"))
    functions = {
        node.name: node
        for parent in tree.body
        if isinstance(parent, ast.ClassDef)
        for node in parent.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for name in ("process_source", "delete_source"):
        guarded = False
        for node in ast.walk(functions[name]):
            if not isinstance(node, ast.If):
                continue
            names = {
                child.id
                for child in ast.walk(node.test)
                if isinstance(child, ast.Name)
            }
            if "HIDDEN_SYNTHETIC_SOURCE_TYPES" not in names:
                continue
            calls = {
                inner.func.attr
                for inner in ast.walk(node)
                if isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
            }
            if "note_corpus_change" in calls:
                guarded = True
        assert guarded, (
            f"{name} 里的 note_corpus_change 不再受「用户可见来源」判据保护。"
            "隐藏合成源(memory/knowhow 投影)不是语料变更:私有 Memory 属于某一位成员,"
            "让它推进 notebook 共享计数器等于用一位成员的私人操作触发并支付全库的整理。"
        )


def test_startup_actually_sweeps_the_chain_on_a_runtime_that_has_one():
    """The startup wiring is a ``getattr`` chain, so BOTH halves can rot
    silently: rename the runtime attribute and the sweep is skipped forever
    (a stranded ``running`` row then holds that notebook's chain until someone
    notices it never refreshes). One assertion for the call, one for the
    attribute NAME actually existing on the runtime class."""
    import ast
    from pathlib import Path as _Path

    from app.services import repository_runtime, startup_warmup

    calls: list[int] = []

    class _Service:
        def sweep_on_start(self) -> int:
            calls.append(1)
            return 2

    class _Runtime:
        agent_profile_jobs = _Service()

    class _Repo:
        _runtime = _Runtime()

    startup_warmup._sweep_agent_profile_chains(_Repo())

    assert calls == [1], "startup 没有真的调到 sweep_on_start"

    # …and the name it reaches for is really the one the runtime assigns.
    # ``agent_profile_jobs`` is an INSTANCE attribute (set in ``__init__``), so
    # no attribute-level introspection of the class can see it — and
    # constructing a real runtime here would drag the whole composition root
    # into a file about a threshold counter. Compare the two spellings in the
    # source instead.
    startup_source = _Path(startup_warmup.__file__).read_text(encoding="utf-8")
    wanted = {
        node.args[1].value
        for node in ast.walk(ast.parse(startup_source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
        and node.args[1].value.startswith("agent_profile")
    }
    assert wanted == {"agent_profile_jobs"}, (
        f"startup 取的属性名是 {sorted(wanted)},与运行时座位对不上。"
    )
    assigned = {
        target.attr
        for node in ast.walk(
            ast.parse(_Path(repository_runtime.__file__).read_text(encoding="utf-8"))
        )
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Name)
        and target.value.id == "self"
    }
    assert "agent_profile_jobs" in assigned, (
        "RepositoryRuntime 不再赋值 `agent_profile_jobs`——startup 的 getattr 对"
        "错误的属性名不会报错,只会永远跳过清扫,而每一条既有用例仍然全绿。"
    )


# --------------------------------------------------------------- job plumbing
def test_the_job_name_routes_to_the_light_maintenance_pool():
    """Pinned against ``background_jobs``' own registry rather than through a
    submitted job: which pool a name resolves to IS the contract, and a job
    landing in the heavy pool would be starved by hour-long rebuilds exactly
    on the busy libraries this feature is for."""
    assert background_jobs._maintenance_pool(f"agentprofile-{NOTEBOOK_ID}") == (
        "light",
        "agentprofile",
    )
    assert background_jobs._diagnostic_job_name(
        lambda: None, f"agentprofile-{NOTEBOOK_ID}"
    ) == "agentprofile"


def test_a_cluster_with_any_memory_member_hides_its_name_entirely(harness):
    """codex #520 R8 P1:``canonical_name`` 是代表名整簇复制。

    一个簇同时含可见成员与 Memory 成员时,只过滤成员行(计数正确)洗不掉名字——
    代表可能恰好是那个 Memory 对象,名字会进全员可见的 key_entities。取名字的
    查询因此按**整簇**排除:任一成员归 Memory 源所有,整簇不出名字。计数不携带
    名字,仍按成员行过滤(可见成员照常计入)。
    """
    _add_source(harness, "src-a", elements=(("table", 1),))
    _add_source(harness, "src-memory", source_type="memory")
    _add_kg_object(harness, "ko-visible", "concept", source_id="src-a")
    _add_kg_object(harness, "ko-private", "concept", source_id="src-memory")
    # 同一个簇:代表名恰好来自 Memory 对象的措辞
    _add_cluster(harness, "can-mixed", "ko-visible", "私人记忆里的叫法")
    _add_cluster(harness, "can-mixed", "ko-private", "私人记忆里的叫法")
    # 对照:纯可见簇照常出名字
    _add_kg_object(harness, "ko-clean", "concept", source_id="src-a")
    _add_cluster(harness, "can-clean", "ko-clean", "干净概念")

    service = _service(harness, client=_Client(_reply([])))
    stats = service.corpus_stats(NOTEBOOK_ID)

    assert [name for name, _members in stats.key_concepts] == ["干净概念"]
    # 计数维度不受整簇排除影响:可见对象仍被数到(2 个可见 concept)
    assert stats.kg_objects == (("concept", 2),)


def test_mixed_dead_and_unsampled_evidence_never_renders_the_all_gone_marker(harness):
    """钉住 all-gone 分支的第二个合取项(质量评审 M3 变异存活):证据部分健在但
    掉出取样、部分已删除时,绝不能渲染「全部消失」——那会诱导模型撤回一条仍然
    成立的断言,而块被清空后不留证据可查。"""
    blocks = [_job_block("corpus_shape", ("s-alive-1", "s-alive-2", "s-dead"))]
    stats = _stats(served=(), visible=("s-alive-1", "s-alive-2"))
    prompt = render_current_blocks(blocks, stats)
    assert "all supporting documents are gone" not in prompt
    assert "+2 more still in the library" in prompt
    assert "1 no longer in the library" in prompt
