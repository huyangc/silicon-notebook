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
    AGENT_PROFILE_WORKLOAD,
    AgentProfileConsolidationService,
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

    profiles = AgentProfileStore(database, new_id=lambda p: f"{p}-1", now=lambda: NOW)
    return {
        "settings": settings,
        "database": database,
        "profiles": profiles,
        "sources": SourceStore(database, now=lambda: NOW),
        "queries": QueryStore(database, settings),
    }


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
    elements: tuple[tuple[str, int], ...] = (),
) -> None:
    with harness["database"].write() as db:
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,status,"
            "parse_status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (source_id, notebook_id, source_id, source_type, "active",
             "extracted", NOW, NOW),
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


def _add_kg_object(harness, object_id: str, object_type: str) -> None:
    with harness["database"].write() as db:
        db.execute(
            "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (object_id, NOTEBOOK_ID, object_type, "approved", NOW, NOW),
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
        for _ in range(3):
            bumped.append(harness["profiles"].bump_signal(NOTEBOOK_ID, ""))
        return _reply([{"label": "corpus_shape", "value": "v", "evidence": []}])

    _with_submitter(monkeypatch, _Submitter())
    service = _service(harness, client=_Client(reply))

    for _ in range(3):
        service.note_corpus_change(NOTEBOOK_ID)

    assert bumped == [4, 5, 6]
    assert _job(harness)["pending_signal"] == 3


def test_a_failed_run_consumes_nothing(harness, monkeypatch):
    _with_submitter(monkeypatch, _Submitter())
    service = _service(harness, client=None)  # model unconfigured

    for _ in range(3):
        service.note_corpus_change(NOTEBOOK_ID)

    row = _job(harness)
    assert row["status"] == "failed"
    assert row["pending_signal"] == 3, "a failed run's triggers still count"


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
        service.run_base(NOTEBOOK_ID, claimed)

    row = _job(harness)
    assert row["status"] == "failed"
    assert row["diagnostic"] == "worker_interrupted"


def test_an_ordinary_exception_settles_and_propagates(harness):
    def reply(_prompt: str) -> str:
        raise ValueError("provider exploded")

    service = _service(harness, client=_Client(reply))
    claimed = harness["profiles"].claim(NOTEBOOK_ID, "")

    with pytest.raises(ValueError):
        service.run_base(NOTEBOOK_ID, claimed)

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

    service.run_base(NOTEBOOK_ID, claimed)

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

    service.run_base(NOTEBOOK_ID, claimed)

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

    service.run_base(NOTEBOOK_ID, claimed)

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

    service.run_base(NOTEBOOK_ID, claimed)

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

    service.run_base(NOTEBOOK_ID, claimed)

    blocks = _blocks(harness)
    assert blocks["corpus_shape"]["value"] == "用户刚改的"
    assert blocks["corpus_gaps"]["value"] == "三份文档没有内容"
    row = _job(harness)
    assert row["status"] == "done"
    assert row["blocks_written"] == 1
    assert "cas_conflict:corpus_shape" in row["diagnostic"]


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

    service.run_base(NOTEBOOK_ID, harness["profiles"].claim(NOTEBOOK_ID, ""))

    prompt = client.prompts[0]
    assert "- corpus_shape (user-authored): 这是工具手册库" in prompt
    assert "- corpus_gaps: 模型上一轮的猜测" in prompt
    assert "authoritative" in prompt


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

    service.run_base(NOTEBOOK_ID, harness["profiles"].claim(NOTEBOOK_ID, ""))

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

    service.run_base(NOTEBOOK_ID, harness["profiles"].claim(NOTEBOOK_ID, ""))

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

    service.run_base(NOTEBOOK_ID, harness["profiles"].claim(NOTEBOOK_ID, ""))

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
