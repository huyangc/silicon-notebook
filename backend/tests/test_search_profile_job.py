"""Agentic Memory P3 (B-Profile, T7): the deterministic, zero-LLM
``answer_language`` inference job.

Three groups of coverage:

1. **The language classifier** (``classify_ask_language``) — table-driven,
   pure function, no I/O.
2. **The gating/single-flight/aggregation logic** (``SearchProfileInference
   Service``) against FAKE ``ask_state``/``identity`` doubles — fast, no
   database, exercises threshold/majority/fail-open/wiring-off in isolation.
3. **Integration against real SQLite stores** — the "job never overwrites a
   user-authored field" rule, and the store-level projection guarantee that
   the raw question text never crosses ``recent_user_ask_languages``'s
   boundary (with an accompanying mutation-verification note in the module
   docstring below).

⚠ Mutation verified by hand while authoring this file (per CLAUDE.md's "加了
守卫 ≠ 有效" rule): temporarily changed the SQLite adapter's
``recent_user_ask_languages`` to also return the raw ``question`` text
alongside ``language``, re-ran
``test_recent_user_ask_languages_projects_only_the_language_key`` below, and
confirmed it failed (the ``keys() == {"language"}`` assertion caught the
extra key). Reverted before committing — see the task report for the actual
red/green transcript.
"""
from __future__ import annotations

import itertools
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.models.ask import AskRequest
from app.repositories.ports import (
    SEARCH_PROFILE_LANGUAGE_MAJORITY_RATIO,
    SEARCH_PROFILE_LANGUAGE_MIN_SAMPLES,
    SEARCH_PROFILE_LANGUAGE_SAMPLE_LIMIT,
)
from app.repositories.sqlite.ask_state_store import AskStateStore
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.identity_store import IdentityStore
from app.repositories.sqlite.migrations import SqliteMigrator
from app.services.search_profile import classify_ask_language, parse_search_profile
from app.services.search_profile_job import SearchProfileInferenceService

NOW = "2026-08-20T00:00:00+00:00"
NOTEBOOK_ID = "nb-search-profile"
USER_A = "user-a"


# --------------------------------------------------------------------------- #
# 1. classify_ask_language — table-driven
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text, expected",
    [
        # 中英混排,CJK 占绝对多数(仅 "API" 三个拉丁字母)——应判为 zh。
        ("这是一段中文提问，带一点 API 术语", "zh"),
        # 纯英文散文,无 CJK——应判为 en。
        ("How do I configure the pipeline correctly", "en"),
        # 纯标识符/单号:多为数字与连字符,字母占比过低——不足以判定语言。
        ("REQ-2024-08-0091", "other"),
        # 空问题 / 仅空白 / None——同一个「other」桶,零证据。
        ("", "other"),
        ("   \n\t  ", "other"),
        (None, "other"),
    ],
)
def test_classify_ask_language_table(text, expected):
    assert classify_ask_language(text) == expected


# --------------------------------------------------------------------------- #
# 2. gating / single-flight / aggregation — fake doubles
# --------------------------------------------------------------------------- #
class _FakeAskState:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls: list[tuple[str, int]] = []

    def recent_user_ask_languages(self, user_id: str, *, limit: int) -> list[dict]:
        self.calls.append((user_id, limit))
        return list(self.rows)


class _RaisingAskState:
    def recent_user_ask_languages(self, user_id: str, *, limit: int) -> list[dict]:
        raise RuntimeError("boom")


class _FakeIdentity:
    def __init__(self):
        self.calls: list[tuple[str, dict, str]] = []

    def set_user_search_profile(self, user_id: str, fields: dict, origin: str):
        self.calls.append((user_id, dict(fields), origin))


class _Events:
    def __init__(self):
        self.emitted: list[dict] = []

    def emit(self, payload):
        self.emitted.append(payload)


class _Settings:
    user_search_profile_enabled = True
    user_search_profile_trigger = 3


def _service(*, ask_state=None, identity=None, events=None, settings=None):
    return SearchProfileInferenceService(
        settings=settings or _Settings(),
        ask_state=ask_state,
        identity=identity,
        event_log=events or _Events(),
    )


def _zh_rows(n):
    return [{"language": "zh"} for _ in range(n)]


def test_below_threshold_never_reads_or_writes():
    ask_state = _FakeAskState(_zh_rows(SEARCH_PROFILE_LANGUAGE_MIN_SAMPLES))
    identity = _FakeIdentity()
    service = _service(ask_state=ask_state, identity=identity)
    service.note_ask_completed(USER_A)
    service.note_ask_completed(USER_A)  # trigger=3, still one short
    assert ask_state.calls == []
    assert identity.calls == []


def test_threshold_triggers_exactly_one_read_and_write():
    rows = _zh_rows(SEARCH_PROFILE_LANGUAGE_MIN_SAMPLES)
    ask_state = _FakeAskState(rows)
    identity = _FakeIdentity()
    events = _Events()
    service = _service(ask_state=ask_state, identity=identity, events=events)
    for _ in range(3):  # _Settings.user_search_profile_trigger == 3
        service.note_ask_completed(USER_A)
    assert ask_state.calls == [(USER_A, SEARCH_PROFILE_LANGUAGE_SAMPLE_LIMIT)]
    assert identity.calls == [
        (USER_A, {"answer_language": "zh"}, "job")
    ]
    assert events.emitted[-1]["status"] == "done"
    assert events.emitted[-1]["language"] == "zh"


def test_counter_resets_after_the_run():
    rows = _zh_rows(SEARCH_PROFILE_LANGUAGE_MIN_SAMPLES)
    ask_state = _FakeAskState(rows)
    identity = _FakeIdentity()
    service = _service(ask_state=ask_state, identity=identity)
    for _ in range(3):
        service.note_ask_completed(USER_A)
    assert len(ask_state.calls) == 1
    service.note_ask_completed(USER_A)
    service.note_ask_completed(USER_A)
    assert len(ask_state.calls) == 1  # still one short of the NEXT window
    service.note_ask_completed(USER_A)
    assert len(ask_state.calls) == 2


def test_insufficient_samples_reads_but_does_not_write():
    rows = _zh_rows(SEARCH_PROFILE_LANGUAGE_MIN_SAMPLES - 1)
    ask_state = _FakeAskState(rows)
    identity = _FakeIdentity()
    events = _Events()
    service = _service(ask_state=ask_state, identity=identity, events=events)
    for _ in range(3):
        service.note_ask_completed(USER_A)
    assert len(ask_state.calls) == 1
    assert identity.calls == []
    assert events.emitted[-1]["status"] == "skipped"
    assert events.emitted[-1]["reason"] == "insufficient_samples"


def test_no_clear_majority_does_not_write():
    # 6/10 = 0.6, below SEARCH_PROFILE_LANGUAGE_MAJORITY_RATIO (0.7).
    assert SEARCH_PROFILE_LANGUAGE_MAJORITY_RATIO == 0.7
    rows = [{"language": "zh"}] * 6 + [{"language": "en"}] * 4
    ask_state = _FakeAskState(rows)
    identity = _FakeIdentity()
    events = _Events()
    service = _service(ask_state=ask_state, identity=identity, events=events)
    for _ in range(3):
        service.note_ask_completed(USER_A)
    assert identity.calls == []
    assert events.emitted[-1]["status"] == "skipped"
    assert events.emitted[-1]["reason"] == "no_clear_majority"


def test_clear_majority_writes_the_winning_language():
    # 8/10 = 0.8 >= 0.7, and "other" rows must not count toward either
    # candidate's numerator NOR be excluded from the denominator — the
    # ratio is computed against the full sample.
    rows = [{"language": "en"}] * 8 + [{"language": "zh"}] + [{"language": "other"}]
    ask_state = _FakeAskState(rows)
    identity = _FakeIdentity()
    service = _service(ask_state=ask_state, identity=identity)
    for _ in range(3):
        service.note_ask_completed(USER_A)
    assert identity.calls == [(USER_A, {"answer_language": "en"}, "job")]


def test_full_sample_denominator_rejects_a_majority_that_an_excluded_denominator_would_accept():
    """T7-T9 修复轮(P2-3):挑一组两种分母口径给出**不同**答案的样本,
    把「分母保留全样本」这条裁决(见 SEARCH_PROFILE_LANGUAGE_MAJORITY_RATIO
    在 ``app.repositories.ports`` 的自身文档)钉成可辨的行为,而不只是文档
    措辞。6 条 zh + 4 条 other:
      * 全样本分母(本仓库的实际选择):6/10 = 0.6,低于 0.7 → 不写。
      * 排除 other 的分母(未采纳的替代口径):6/6 = 1.0,会写。
    这条测试断言前者——若未来有人把分母改成排除 "other",这条测试必须报红。
    """
    rows = [{"language": "zh"}] * 6 + [{"language": "other"}] * 4
    ask_state = _FakeAskState(rows)
    identity = _FakeIdentity()
    events = _Events()
    service = _service(ask_state=ask_state, identity=identity, events=events)
    for _ in range(3):
        service.note_ask_completed(USER_A)
    assert identity.calls == []
    assert events.emitted[-1]["status"] == "skipped"
    assert events.emitted[-1]["reason"] == "no_clear_majority"


def test_majority_ratio_boundary_at_exactly_the_threshold_writes():
    """T7-T9 修复轮(P2-4):当前判据是 ``winner_count / total <
    MAJORITY_RATIO`` 才跳过——恰好等于阈值(7/10 = 0.7)不满足 ``<``,因此
    照常写入。钉住这条边界,不让判据悄悄从 ``<`` 漂移成 ``<=``。"""
    assert SEARCH_PROFILE_LANGUAGE_MAJORITY_RATIO == 0.7
    rows = [{"language": "zh"}] * 7 + [{"language": "en"}] * 3
    ask_state = _FakeAskState(rows)
    identity = _FakeIdentity()
    service = _service(ask_state=ask_state, identity=identity)
    for _ in range(3):
        service.note_ask_completed(USER_A)
    assert identity.calls == [(USER_A, {"answer_language": "zh"}, "job")]


def test_wiring_disabled_skips_everything():
    class _Disabled(_Settings):
        user_search_profile_enabled = False

    ask_state = _FakeAskState(_zh_rows(SEARCH_PROFILE_LANGUAGE_MIN_SAMPLES))
    identity = _FakeIdentity()
    service = _service(ask_state=ask_state, identity=identity, settings=_Disabled())
    for _ in range(5):
        service.note_ask_completed(USER_A)
    assert ask_state.calls == []
    assert identity.calls == []


def test_unwired_ask_state_is_a_silent_no_op():
    identity = _FakeIdentity()
    service = _service(ask_state=None, identity=identity)
    for _ in range(5):
        service.note_ask_completed(USER_A)  # must not raise
    assert identity.calls == []


def test_empty_user_id_short_circuits():
    ask_state = _FakeAskState(_zh_rows(SEARCH_PROFILE_LANGUAGE_MIN_SAMPLES))
    identity = _FakeIdentity()
    service = _service(ask_state=ask_state, identity=identity)
    for _ in range(5):
        service.note_ask_completed("")
    assert ask_state.calls == []
    assert identity.calls == []


def test_fail_open_when_the_read_raises():
    identity = _FakeIdentity()
    events = _Events()
    service = _service(ask_state=_RaisingAskState(), identity=identity, events=events)
    for _ in range(3):
        service.note_ask_completed(USER_A)  # must not raise
    assert identity.calls == []
    assert events.emitted[-1]["status"] == "failed"
    assert events.emitted[-1]["reason"] == "RuntimeError"


def test_single_flight_under_a_concurrent_burst():
    """``trigger`` calls arriving from ``trigger`` threads at (nearly) the same
    instant must still produce exactly ONE read and ONE write — see the
    service's own docstring for why the whole read+write runs inside the same
    critical section that claims the trigger, rather than a separate
    running-set.
    """
    trigger = 5

    class _Trigger5(_Settings):
        user_search_profile_trigger = trigger

    rows = _zh_rows(SEARCH_PROFILE_LANGUAGE_MIN_SAMPLES)
    ask_state = _FakeAskState(rows)
    identity = _FakeIdentity()
    service = _service(ask_state=ask_state, identity=identity, settings=_Trigger5())

    barrier = threading.Barrier(trigger)

    def call():
        barrier.wait(timeout=5)
        service.note_ask_completed(USER_A)

    threads = [threading.Thread(target=call) for _ in range(trigger)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert len(ask_state.calls) == 1
    assert len(identity.calls) == 1


# --------------------------------------------------------------------------- #
# 3. integration — real SQLite stores
# --------------------------------------------------------------------------- #
@pytest.fixture
def sqlite_harness(tmp_path: Path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'test.db'}")
    database = SqliteDatabase(settings, tmp_path)
    assert SqliteMigrator(database, settings).migrate()
    with database.write() as db:
        db.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,"
            "updated_at) VALUES (?,?,?,?,?,?,?)",
            (USER_A, f"{USER_A}@example.test", USER_A, "user", "active", NOW, NOW),
        )
        db.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,"
            "created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (NOTEBOOK_ID, "NB", "", "engineering", "ready", USER_A, NOW, NOW),
        )
    ids = itertools.count(1)
    seams = SimpleNamespace(now=lambda: NOW, new_id=lambda prefix: f"{prefix}-{next(ids)}")
    ask_state = AskStateStore(database, seams)
    identity = IdentityStore(database, settings)
    return SimpleNamespace(
        settings=settings, database=database, ask_state=ask_state, identity=identity,
    )


def _ask_and_finish(harness, question: str, *, status: str = "done") -> str:
    job_id, _conv = harness.ask_state.begin_durable_job(
        NOTEBOOK_ID, AskRequest(question=question), "reasoning", USER_A,
    )
    harness.ask_state.finish_job(job_id, status)
    return job_id


def test_recent_user_ask_languages_projects_only_the_language_key(sqlite_harness):
    """Store-level projection guarantee (see module docstring for the
    hand-verified mutation this pins)."""
    _ask_and_finish(sqlite_harness, "这是一条完整问题")
    rows = sqlite_harness.ask_state.recent_user_ask_languages(USER_A, limit=30)
    assert rows == [{"language": "zh"}]
    for row in rows:
        assert set(row.keys()) == {"language"}


def test_recent_user_ask_languages_excludes_unfinished_jobs(sqlite_harness):
    _ask_and_finish(sqlite_harness, "done question", status="done")
    _ask_and_finish(sqlite_harness, "cancelled question", status="cancelled")
    _ask_and_finish(sqlite_harness, "failed question", status="failed")
    rows = sqlite_harness.ask_state.recent_user_ask_languages(USER_A, limit=30)
    assert len(rows) == 1


def test_job_never_overwrites_a_user_authored_field(sqlite_harness):
    """Integration form of ``search_profile.merge_field``'s "job never
    overwrites user" rule (unit-tested in ``test_search_profile_document.py``)
    — pins that THIS job calls ``set_user_search_profile`` with
    ``origin="job"`` (never ``"user"``), so the existing store-level guard
    actually protects it end to end.
    """
    sqlite_harness.identity.set_user_search_profile(
        USER_A, {"answer_language": "en"}, origin="user"
    )
    for _ in range(SEARCH_PROFILE_LANGUAGE_MIN_SAMPLES):
        _ask_and_finish(sqlite_harness, "这是一条中文提问用来制造多数")

    service = SearchProfileInferenceService(
        settings=SimpleNamespace(
            user_search_profile_enabled=True,
            user_search_profile_trigger=SEARCH_PROFILE_LANGUAGE_MIN_SAMPLES,
        ),
        ask_state=sqlite_harness.ask_state,
        identity=sqlite_harness.identity,
        event_log=_Events(),
    )
    for _ in range(SEARCH_PROFILE_LANGUAGE_MIN_SAMPLES):
        service.note_ask_completed(USER_A)

    with sqlite_harness.database.write() as db:
        row = db.execute(
            "SELECT search_profile_json FROM user_profiles WHERE user_id=?",
            (USER_A,),
        ).fetchone()
    stored = parse_search_profile(row["search_profile_json"])
    entry = stored["fields"]["answer_language"]
    assert entry["value"] == "en"
    assert entry["origin"] == "user"


def test_repeated_job_write_of_the_same_value_skips_the_update(
    monkeypatch, sqlite_harness
):
    """T7-T9 修复轮(P2-6):合并结果与既存 ``search_profile_json`` 逐字相同
    时,``set_user_search_profile`` 必须跳过 ``UPDATE`` 本身——这是 T7 job
    的常见形状(连续两次触发,归纳出同一个赢家语言)。

    直接比对最终值不能证明「真的跳过了」:哪怕 UPDATE 真的执行了,只要值
    没变,最终值断言照样通过。这里改用一把会推进的假时钟(``_now``)钉住
    ``user_profiles.updated_at`` 这一列本身没有被第二次写入——第二次调用
    如果真的执行了 UPDATE,``updated_at`` 会跳到假时钟的第二个刻度;如果
    被正确跳过,它必须原地停在第一次调用的刻度。

    ``merge_field`` 自己写入的是文档内**逐字段**的 ``updated_at``(走真实
    ``datetime.now(timezone.utc)``,与本方法用来推进**行级** ``updated_at``
    列的 ``_now()`` seam 是两个不同的时钟输入——见 ``merge_field`` 自己的
    docstring)。为了让两次调用产出逐字相同的 ``search_profile_json``(否则
    这条测试本身在秒边界上会偶发抖动,与本仓库的确定性红线冲突),这里连带
    把 ``search_profile`` 模块的 ``datetime`` 也钉死到一个固定值。
    """
    from datetime import datetime as _real_datetime
    from datetime import timezone as _timezone

    from app.repositories.sqlite import identity_store as identity_store_module
    from app.domain import search_profile as search_profile_module  # B3: merge_field now lives (and reads datetime) here

    class _FixedDatetime(_real_datetime):
        @classmethod
        def now(cls, tz=None):
            return _real_datetime(2026, 8, 20, 0, 0, 0, tzinfo=tz)

    monkeypatch.setattr(search_profile_module, "datetime", _FixedDatetime)
    assert search_profile_module.datetime.now(_timezone.utc).isoformat() == (
        "2026-08-20T00:00:00+00:00"
    )

    row_clock = iter(["2026-08-20T00:00:00", "2026-08-20T00:00:05"])
    monkeypatch.setattr(identity_store_module, "_now", lambda: next(row_clock))

    sqlite_harness.identity.set_user_search_profile(
        USER_A, {"answer_language": "zh"}, origin="job"
    )
    sqlite_harness.identity.set_user_search_profile(
        USER_A, {"answer_language": "zh"}, origin="job"
    )

    with sqlite_harness.database.write() as db:
        row = db.execute(
            "SELECT updated_at, search_profile_json FROM user_profiles "
            "WHERE user_id=?",
            (USER_A,),
        ).fetchone()
    # The row-level updated_at must still be the FIRST clock tick, not the
    # second one -- the second call's UPDATE (and its clock read) must never
    # have run.
    assert row["updated_at"] == "2026-08-20T00:00:00"
    stored = parse_search_profile(row["search_profile_json"])
    assert stored["fields"]["answer_language"]["value"] == "zh"


def test_fullwidth_latin_and_halfwidth_katakana_are_not_chinese():
    """codex #535 R1 P2:0xFF00-0xFFEF 整块含全角拉丁与半角片假名——OCR 出的
    「ＨＥＬＬＯ」/半角片假名问题不得判成 zh;中文常用全角标点仍计入。"""
    assert classify_ask_language("ＨＥＬＬＯ ＷＯＲＬＤ ＴＥＳＴ") != "zh"
    assert classify_ask_language("ｱｲｳｴｵ ｶｷｸｹｺ ｻｼｽｾｿ") != "zh"
    # 全角标点混在中文正文里照常计入 CJK
    assert classify_ask_language("这个库支持哪些格式？请列出全部！") == "zh"


def test_sync_ask_paths_deliberately_do_not_notify_inference():
    """codex #535 R4 P2 的驳回护栏:同步 ask(ask_current——POST /ask 与 MCP
    ask_notebook 的路径)刻意不触发三条 note_ask_completed 链,P1 起的登记
    口径(CLAUDE.md「同步 POST /ask 不计入覆盖层触发计数」)。若有人接入,
    本用例红,提醒他那是在改三条链共同的计数语义、需单独过评审。"""
    import ast
    from pathlib import Path

    source = Path("backend/app/services/ask_service.py").read_text("utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "ask_current":
            body_src = ast.get_source_segment(source, node) or ""
            assert "note_ask_completed" not in body_src
            break
    else:  # pragma: no cover
        raise AssertionError("ask_current not found")


def test_kana_bearing_questions_are_never_chinese():
    """codex #535 R12 P2:汉字重的日文问题(假名在场)不判 zh——假名是日文
    专属信号,重复的日文提问绝不能归纳出中文回答偏好。"""
    assert classify_ask_language("これは日本語の質問です") == "other"
    assert classify_ask_language("データベースの設定を確認してください") == "other"
    assert classify_ask_language("ﾃﾞｰﾀ設定の質問") == "other"   # 半角片假名
    # 无假名的中文照常
    assert classify_ask_language("这个库的检索配置在哪里？") == "zh"
