"""T0 三个脚本的门内用例(设计规格 2026-09-08 §2.4)。

rig 本体要网络与真实模型,**不进标准门**;进门的只有它的 `--dry-run` 枚举与
`client_request_id` 编码,以及分析脚本的 CLI(它零 DB、零模型)。导出脚本在这里
只被验到 SQLite 那一侧——PG 侧要一台真实数据库,由跑基线的人验。
"""
from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
import sys
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from tests.model_testkit import bind_chat_client
# `search` 的接线用例借的是 reasoning 检索自己那套测试替身:一个 SQLite 上的
# 真 repo(`rrepo`)加两个按序列作答的假模型。**不另起一份**——那两个替身钉的
# 正是 legacy / v2 两套 reflect 协议的真实载荷形状,抄一份只会让它们分叉。
from tests.test_reasoning_retrieval import (  # noqa: F401 — rrepo 是 fixture
    _GatedV2LLM,
    _SeqLLM,
    _seed_two_nodes,
    rrepo,
)

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def _load(name: str):
    """按路径加载 `scripts/` 里的脚本模块(它们不是包的一部分)。"""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


analyze = _load("analyze_reasoning_trace")
export = _load("export_reasoning_traces")
rig = _load("reflect_shadow_rig")


# --- client_request_id 编码是一份双向合同 ------------------------------------


def test_client_request_id_round_trips():
    encoded = rig.encode_client_request_id(
        question_key="B-q07", corpus_cell="B_nokg", policy="v2",
        effort="deep", requested_mode="reasoning",
    )
    assert encoded == "t0:B-q07:B_nokg:v2:deep:reasoning"
    assert export.decode_client_request_id(encoded) == {
        "question_key": "B-q07", "corpus_cell": "B_nokg", "policy": "v2",
        "effort": "deep", "requested_mode": "reasoning",
    }


def test_client_request_id_rejects_a_colon_in_any_field():
    """冒号是字段分隔符。让它进去,导出侧解出来的标签会**错位**而不是失败。"""
    with pytest.raises(ValueError):
        rig.encode_client_request_id(
            question_key="B:q07", corpus_cell="B_kg", policy="v2",
            effort="deep", requested_mode="reasoning",
        )


def test_foreign_client_request_ids_decode_to_no_tags():
    # 线上真实提问带的是浏览器铸的幂等键,不是 rig 的编码:它必须解成「没有
    # 标签」,而不是被硬凑成某个语料格。
    assert export.decode_client_request_id("ask-intent-mirror-9f3c") == {}
    assert export.decode_client_request_id("t0:too:few") == {}
    assert export.decode_client_request_id(None) == {}


def test_every_question_key_survives_the_encoding():
    questions = rig.load_questions()
    for row in questions["ask"] + questions["report"]:
        assert ":" not in row["key"], row["key"]


# --- dry-run 枚举 -----------------------------------------------------------


def test_dry_run_plan_covers_each_cell_effort_and_question(capsys):
    assert rig.main(["--dry-run", "--limit", "2", "ask"]) == 0
    printed = capsys.readouterr().out
    assert "[dry-run]" in printed
    # 每格 2 题 × 2 档 × 4 格 = 16 条;编码逐条上屏。
    assert printed.count("crid=t0:") == 16
    assert "crid=t0:A-q01:A_kg:legacy:standard:reasoning" in printed
    assert "crid=t0:B-q01:B_nokg:legacy:deep:reasoning" in printed


def test_dry_run_touches_nothing(tmp_path, capsys):
    out_dir = tmp_path / "t0"
    for command in ("seed", "ask", "report", "search", "restart", "export",
                    "teardown"):
        assert rig.main(["--dry-run", "--out-dir", str(out_dir), command]) == 0
    capsys.readouterr()
    assert not out_dir.exists()


# --- restart 子命令 -----------------------------------------------------------


def test_restart_dry_run_prints_the_stop_and_start_plan(tmp_path, capsys):
    out_dir = tmp_path / "t0"
    out_dir.mkdir()
    (out_dir / "rig-state.json").write_text(json.dumps({
        "policy": "legacy", "backend_pid": 4242, "port": 8011,
        "database_url": "postgresql://127.0.0.1:5432/silicon_notebook_t0_test",
        "storage_dir": ".local/storage-t0", "env_file": None,
        "notebooks": {},
    }), encoding="utf-8")
    assert rig.main([
        "--dry-run", "--out-dir", str(out_dir), "--policy", "v2", "restart",
    ]) == 0
    printed = capsys.readouterr().out
    assert "[dry-run]" in printed
    assert "pid=4242" in printed
    assert "policy legacy -> v2" in printed
    assert "REASONING_REFLECT_V2_ENABLED=true" in printed
    # dry-run 是纯打印:state 文件必须原封不动。
    assert json.loads((out_dir / "rig-state.json").read_text("utf-8"))["policy"] == "legacy"


def test_ask_refuses_to_run_when_state_policy_disagrees(tmp_path, capsys):
    """`ask` 开跑前核对 state 记的策略;不一致(哪怕只是 dry-run)也要当场报错,
    而不是悄悄跑出一批策略对不上号的轨迹(读 state 文件不是副作用,所以这条
    闸在 `--dry-run` 下同样生效)。
    """
    out_dir = tmp_path / "t0"
    out_dir.mkdir()
    (out_dir / "rig-state.json").write_text(json.dumps({
        "policy": "legacy", "backend_pid": 4242, "notebooks": {}, "token": "",
    }), encoding="utf-8")
    with pytest.raises(RuntimeError, match=r"restart --policy v2"):
        rig.main([
            "--dry-run", "--out-dir", str(out_dir), "--policy", "v2", "ask",
        ])


def test_report_refuses_to_run_when_state_policy_disagrees(tmp_path):
    out_dir = tmp_path / "t0"
    out_dir.mkdir()
    (out_dir / "rig-state.json").write_text(json.dumps({
        "policy": "v2", "backend_pid": 4242, "notebooks": {}, "token": "",
    }), encoding="utf-8")
    with pytest.raises(RuntimeError, match=r"restart --policy legacy"):
        rig.main([
            "--dry-run", "--out-dir", str(out_dir), "--policy", "legacy",
            "report",
        ])


def test_ask_proceeds_when_state_has_no_recorded_policy_yet(tmp_path, capsys):
    """线上从没跑过 `seed`/`restart` 的 out-dir:不知道就不拦,而不是报错。"""
    out_dir = tmp_path / "t0"
    assert rig.main([
        "--dry-run", "--out-dir", str(out_dir), "--limit", "1", "ask",
    ]) == 0


def test_ask_plan_pairs_legacy_and_v2_on_the_same_question_key():
    questions = rig.load_questions()
    plans = {
        policy: rig.ask_plan(
            questions, cells=["B_kg"], policy=policy, limit=3, lang="zh",
            mode="reasoning", efforts=("standard",),
        )
        for policy in ("legacy", "v2")
    }
    keys = {policy: [item["question_key"] for item in plan]
            for policy, plan in plans.items()}
    assert keys["legacy"] == keys["v2"]
    assert {item["client_request_id"] for item in plans["legacy"]}.isdisjoint(
        {item["client_request_id"] for item in plans["v2"]}
    )


def test_english_variants_get_their_own_question_key():
    questions = rig.load_questions()
    plan = rig.ask_plan(
        questions, cells=["A_kg"], policy="legacy", limit=1, lang="both",
        mode="reasoning", efforts=("standard",),
    )
    assert [item["question_key"] for item in plan] == ["A-q01", "A-q01-en"]


def test_ask_plan_carries_scope_source_titles_only_for_the_declaring_question():
    """`scope_sources`(计数)已经改成 `scope_source_titles`(标题列表)——
    没有一个消费者读过前者(codex #700 R3 P2),这里钉住 `ask_plan` 现在按
    标题原样转发,其余题目恒是空元组。"""
    questions = rig.load_questions()
    plan = rig.ask_plan(
        questions, cells=["B_nokg"], policy="legacy", limit=None, lang="zh",
        mode="reasoning", efforts=("standard",),
    )
    by_key = {item["question_key"]: item for item in plan}
    assert by_key["B-q09"]["scope_source_titles"] == (
        "qwen_papers__04_Qwen-VL_mineru.md",
        "deepseek_papers__01_DeepSeek-V2_mineru.md",
    )
    assert by_key["B-q01"]["scope_source_titles"] == ()
    assert by_key["B-q10"]["scope_source_titles"] == ()


def test_corpus_cells_only_receive_their_own_corpus_questions():
    questions = rig.load_questions()
    plan = rig.ask_plan(
        questions, cells=list(rig.CORPUS_CELLS), policy="legacy", limit=None,
        lang="zh", mode="reasoning", efforts=("standard",),
    )
    for item in plan:
        assert item["question_key"].startswith(item["corpus_cell"][0])


# --- A 语料重建 -------------------------------------------------------------


def test_heading_elements_become_markdown_headings():
    # `source_elements` 没有 section_path 列;层级只在 metadata 的
    # heading_level 里,缺了就退到 h2 而不是猜。
    assert rig._element_markdown(
        "heading", "2. Architecture", {"heading_level": 1}
    ) == "# 2. Architecture"
    assert rig._element_markdown("heading", "Abstract", {}) == "## Abstract"
    assert rig._element_markdown("list_item", "a", {}) == "- a"
    assert rig._element_markdown("paragraph", "a", {}) == "a"


# --- 上传体与 teardown 闸(都是纯函数,不碰网络/库) -------------------------


def test_upload_body_sends_one_doc_type_pair_per_file(tmp_path):
    # 后端按**位置**对齐 files / doc_types / doc_type_explicit
    # (`app/api/source_routes.py:_parse_source_upload`)。少发一个 doc_types
    # 不会报错,只会让后面每个文件都错位拿到上一个文件的类型——所以条数相等是
    # 断言,不是巧合。
    paths = []
    for name in ("a.md", "b.md"):
        path = tmp_path / name
        path.write_text("# " + name, encoding="utf-8")
        paths.append(path)
    body, content_type = rig.build_upload_body(paths)
    assert content_type.startswith("multipart/form-data; boundary=")
    text = body.decode("utf-8")
    assert text.count('name="files"') == 2
    assert text.count('name="doc_types"') == 2
    assert text.count('name="doc_type_explicit"') == 2
    # 表单键必须**恰好**是后端的闭集:多一个键是 422。
    keys = set(re.findall(r'(?<!file)name="([^"]+)"', text))
    assert keys == set(rig.UPLOAD_FORM_KEYS)
    for path in paths:
        assert f'filename="{path.name}"' in text
        assert path.read_text("utf-8") in text


def test_teardown_only_drops_the_postgres_database_this_run_wrote(tmp_path):
    """真源是 `rig-state.json` 里 seed 落的 `database_url`,**不是**这次调用
    自己的 `--database-url`——它有默认值,一次从没 seed 过的 out-dir、或者
    seed 在别的后端上跑过的 out-dir,不该让 teardown 拿命令行默认值去猜"大概
    率是同一个库"(codex #700 R1 P1)。
    """
    def parsed(database_url: str, *, db_name: str | None = None):
        argv = ["--database-url", database_url]
        if db_name is not None:
            argv = ["--db-name", db_name, *argv]
        return rig.build_parser().parse_args([*argv, "teardown"])

    pg_url = "postgresql://127.0.0.1:5432/silicon_notebook_t0_test"

    # state 与这次 --database-url 逐字一致、是 PG、库名对得上 --db-name ⇒ 删。
    assert rig._teardown_targets_this_run(
        parsed(pg_url), {"database_url": pg_url}
    ) is True
    # state 里没有这个键(比如 out-dir 从没 seed 过)⇒ 不删,不拿命令行默认值
    # 顶上去猜。
    assert rig._teardown_targets_this_run(parsed(pg_url), {}) is False
    assert rig._teardown_targets_this_run(parsed(pg_url), None) is False
    # state 记的库跟这次 --database-url 对不上号 ⇒ 不删,哪怕两边都是 PG。
    assert rig._teardown_targets_this_run(
        parsed(pg_url),
        {"database_url": "postgresql://127.0.0.1:5432/somebody_elses_db"},
    ) is False
    # 本条是这次修复要拦的真实场景:state 记的是 SQLite(比如一次 SQLite 冒烟
    # seed),这次调用却没显式给 --database-url,退回到"看起来合理"的默认 PG
    # 参数——两个默认值凑巧都"像"这次 rig 该用的东西,但根本不是同一个库。
    default_pg_url = f"postgresql://127.0.0.1:5432/{rig.DEFAULT_TEST_DB}"
    assert rig._teardown_targets_this_run(
        parsed(default_pg_url),
        {"database_url": f"sqlite:///{tmp_path / 'silicon_notebook_t0_test'}"},
    ) is False
    # state 与这次 --database-url 一致,但库名跟 --db-name 对不上 ⇒ 不删。
    other_db_url = "postgresql://127.0.0.1:5432/somebody_elses_db"
    assert rig._teardown_targets_this_run(
        parsed(other_db_url, db_name="silicon_notebook_t0_test"),
        {"database_url": other_db_url},
    ) is False


def test_admin_url_and_database_url_must_share_a_server_before_drop():
    """host:port 不一致就当场拒绝——不必真的连一次 PG(codex #700 R1 P1)。

    `inet_server_port()` 那一半需要一条真连接,交给跑基线的人在真实 PG 上验;
    这里钉的是不用连库就能判定的那一半:两个 URL 从字面上就不是同一台服务器。
    """
    ok, reason = rig._admin_targets_same_server(
        "postgresql://127.0.0.1:5432/postgres",
        "postgresql://10.0.0.9:5432/silicon_notebook_t0_test",
    )
    assert ok is False
    assert "不是" in reason and "同一台服务器" in reason

    ok, reason = rig._admin_targets_same_server(
        "postgresql://127.0.0.1:6543/postgres",
        "postgresql://127.0.0.1:5432/silicon_notebook_t0_test",
    )
    assert ok is False
    assert "6543" in reason or "5432" in reason


# --- 日志脱敏(codex #700 R1 P2) ---------------------------------------------


def test_redact_url_drops_the_password_keeps_user_host_port_db():
    assert rig._redact_url(
        "postgresql://admin:s3cr3t@10.0.0.5:5432/silicon_notebook"
    ) == "postgresql://admin@10.0.0.5:5432/silicon_notebook"
    # 没有凭据的 URL 原样(不额外加东西)。
    assert rig._redact_url(
        "postgresql://127.0.0.1:5432/silicon_notebook_t0_test"
    ) == "postgresql://127.0.0.1:5432/silicon_notebook_t0_test"
    # SQLite 没有凭据这回事,原样返回。
    assert rig._redact_url("sqlite:////tmp/t0.db") == "sqlite:////tmp/t0.db"


def test_redact_env_for_log_masks_every_sensitive_key():
    printed = rig._redact_env_for_log({
        "DATABASE_URL": "postgresql://admin:s3cr3t@10.0.0.5:5432/db",
        "SOME_API_KEY": "sk-secret",
        "ADMIN_PASSWORD": "hunter2",
        "PORT": "8001",
    })
    assert "s3cr3t" not in printed
    assert "sk-secret" not in printed
    assert "hunter2" not in printed
    assert "PORT=8001" in printed
    assert printed.count("<redacted>") == 3


# --- search 的仓储构造入口(codex #700 R1 P1) -------------------------------


def test_search_repository_disables_migrate_and_seed(monkeypatch):
    """`search` 的仓储必须带 `migrate=False, seed=False`——去掉这两个关键字
    (变异)必须让这条用例翻红,而不是安静地通过。默认的 `migrate=True,
    seed=True` 会在 PostgreSQL 上无条件重写 admin 密码哈希,这类写已经在生产
    主库上真实发生过一次。
    """
    calls: list[dict] = []

    def fake_create_repository(settings, **kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(
        "app.repositories.factory.create_repository", fake_create_repository,
    )
    sentinel = object()
    result = rig._search_repository(sentinel)
    assert result is not sentinel  # 拿到的是 create_repository 的返回值
    assert calls == [{"migrate": False, "seed": False}]


# --- users.updated_at 的只读断言(codex #700 R1 P1) --------------------------


def test_readonly_counts_include_users_updated_at_max_and_catch_its_drift(
    tmp_path, capsys,
):
    """`READONLY_TABLES` 按行数,对 `bundle._initialize` 的 admin 密码重写
    (一次 `UPDATE`,行数不动)是瞎的——这条真实事故就是从这个盲区钻过去的。
    这里钉住 `_readonly_counts` 额外带了 `users_updated_at_max`,并且
    `_assert_readonly` 真的会因为它翻红。
    """
    import sqlite3

    db_path = tmp_path / "t0.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE users (id TEXT PRIMARY KEY, updated_at TEXT)"
    )
    conn.execute(
        "INSERT INTO users VALUES ('user-local', '2026-09-08T00:00:00')"
    )
    conn.commit()
    conn.close()
    database_url = f"sqlite:///{db_path}"

    before = rig._readonly_counts(database_url)
    assert before[rig.USERS_UPDATED_AT_MAX_KEY] == "2026-09-08T00:00:00"

    runner = rig.Runner(dry_run=False, out_dir=tmp_path)
    rig._assert_readonly(runner, before, dict(before))
    capsys.readouterr()

    # 模拟 admin 密码重写:行数一根毫毛不动,只有 updated_at 变了。
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE users SET updated_at = '2026-09-09T00:00:00' "
        "WHERE id = 'user-local'"
    )
    conn.commit()
    conn.close()
    after = rig._readonly_counts(database_url)
    assert after["ask_jobs"] == before["ask_jobs"]  # 行数断言看不出任何差别
    with pytest.raises(RuntimeError, match="users_updated_at_max"):
        rig._assert_readonly(runner, before, after)
    assert "READ-ONLY VIOLATION" in capsys.readouterr().err


# --- ask 的必填澄清项代答(codex #700 R1 P2) ---------------------------------


def test_ask_answers_required_ambiguities_before_streaming(tmp_path, monkeypatch):
    """`/ask/intent` 返回带必填澄清项的契约时,提交给 `/ask/stream` 的
    `intent.answers` 不能是 `[]`——那会被 `finalize_query_intent` 的门拒掉,
    整批 `ask` 起不来(`_plan_intent` 已经踩过同一个坑,`auto_clarification_
    answers` 是同一条确定性代答规则)。
    """
    out_dir = tmp_path / "t0"
    out_dir.mkdir()
    (out_dir / "rig-state.json").write_text(json.dumps({
        "policy": "legacy", "token": "tok", "notebooks": {"A_nokg": "nb-a"},
    }), encoding="utf-8")

    contract = {
        "resolved_question": "RTL到GDSII流程是什么",
        "ambiguities": [
            {"id": "scope", "question": "限定哪种工艺?", "required": True,
             "options": ["7nm", "14nm"]},
            {"id": "optional-1", "question": "要不要举例?", "required": False},
        ],
    }
    submitted: list[dict] = []

    def fake_http(self, method, url, *, json_body=None, **kwargs):
        assert url.endswith("/ask/intent")
        return dict(contract)

    def fake_stream(self, method, url, *, json_body, **kwargs):
        submitted.append(json_body)
        return {}

    monkeypatch.setattr(rig.Runner, "http", fake_http)
    monkeypatch.setattr(rig.Runner, "stream", fake_stream)
    monkeypatch.setattr(rig, "_assert_tag_landed", lambda *a, **k: None)

    assert rig.main([
        "--out-dir", str(out_dir), "--limit", "1", "--cell", "A_nokg", "ask",
    ]) == 0
    assert submitted, "没有提交任何 /ask/stream 请求"
    for body in submitted:
        assert body["intent"]["answers"] == [{"id": "scope", "answer": "7nm"}]


# --- 导出(SQLite 侧) ------------------------------------------------------


def _sqlite_fixture(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE ask_jobs (id TEXT, notebook_id TEXT, mode TEXT,
          status TEXT, answer_id TEXT, client_request_id TEXT,
          created_at TEXT, trace_json TEXT);
        CREATE TABLE ask_trace_steps (job_id TEXT, seq INTEGER, step_json TEXT);
        CREATE TABLE answers (id TEXT, payload TEXT);
        CREATE TABLE sources (id TEXT, notebook_id TEXT);
        CREATE TABLE reports (id TEXT, notebook_id TEXT, depth INTEGER,
          created_at TEXT, sections_json TEXT);
        """
    )
    conn.execute(
        "INSERT INTO ask_jobs VALUES (?,?,?,?,?,?,?,?)",
        ("job-1", "nb-1", "reasoning", "done", "ans-1",
         "t0:A-q01:A_kg:legacy:standard:reasoning", "2026-09-08T01:00:00", ""),
    )
    steps = [
        {"step_type": "ppr", "summary": "s",
         "detail": {"phase": "seed", "result_ids": ["a"]}, "duration_ms": 5},
        {"step_type": "reflect", "summary": "s",
         "detail": {"next_action": "answer", "sufficient": True}},
        {"step_type": "synthesis", "summary": "s",
         "detail": {"anchors": 1, "anchor_evidence_ids": ["a"]}},
    ]
    for seq, item in enumerate(steps):
        conn.execute("INSERT INTO ask_trace_steps VALUES (?,?,?)",
                     ("job-1", seq, json.dumps(item)))
    conn.execute(
        "INSERT INTO answers VALUES (?,?)",
        ("ans-1", json.dumps({"retrieval_effort": "standard",
                              "mode": "reasoning", "kg_required": False,
                              "question": "不该出现在导出里",
                              "answer": "也不该出现"})),
    )
    conn.executemany("INSERT INTO sources VALUES (?,?)",
                     [(f"src-{i}", "nb-1") for i in range(3)])
    conn.execute(
        "INSERT INTO reports VALUES (?,?,?,?,?)",
        ("rep-1", "nb-1", 8, "2026-09-08T02:00:00", json.dumps([
            {"title": "第一节", "markdown": "正文", "evidence_level": "grounded",
             "grounded": True, "top_relevance": 0.5,
             "attempted": [{"query": "q", "new": 1}]},
        ])),
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def sqlite_db(tmp_path: Path) -> Path:
    path = tmp_path / "t0.db"
    _sqlite_fixture(path)
    return path


def test_sqlite_export_projects_rows_and_tags_them_from_the_idempotency_key(
    sqlite_db, tmp_path,
):
    out = tmp_path / "rows.jsonl"
    assert export.main([
        "--database-url", str(sqlite_db), "--reports", "--out", str(out),
    ]) == 0
    rows = [json.loads(line) for line in out.read_text("utf-8").splitlines()]
    ask = [row for row in rows if row["consumer"] != "report_section"]
    reports = [row for row in rows if row["consumer"] == "report_section"]
    assert len(ask) == 1 and len(reports) == 1
    assert ask[0]["question_key"] == "A-q01"
    assert ask[0]["corpus_cell"] == "A_kg"
    assert ask[0]["notebook_bucket"] == "2-5"
    assert ask[0]["trace_source"] == "trace_steps"
    assert ask[0]["citation_contribution"]["seed:ppr"]["cited_hits"] == 1


def test_exported_rows_carry_no_free_text(sqlite_db, tmp_path):
    out = tmp_path / "rows.jsonl"
    export.main([
        "--database-url", str(sqlite_db), "--reports", "--out", str(out),
    ])
    body = out.read_text("utf-8")
    for secret in ("不该出现在导出里", "也不该出现", "第一节", "正文",
                   "nb-1", "job-1", "rep-1", "ans-1"):
        assert secret not in body, secret


def test_export_window_excludes_rows_outside_it(sqlite_db, tmp_path):
    out = tmp_path / "rows.jsonl"
    export.main([
        "--database-url", str(sqlite_db), "--since", "2026-09-09",
        "--out", str(out),
    ])
    assert out.read_text("utf-8").strip() == ""


# --- 分析 CLI ---------------------------------------------------------------


def _write_rows(path: Path, rows: list[dict]) -> Path:
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    return path


def _row(**overrides) -> dict:
    base = {
        "consumer": "ask_single", "mode": "reasoning", "effort": "standard",
        "kg_in_scope": True, "policy_version": "legacy",
        "corpus_cell": "B_kg", "question_key": "B-q01",
        "notebook_bucket": "2-5", "status": "done",
        "reflect_turns": 3, "trace_steps": 9, "total_ms": 1000,
        "stale_breaker": False, "trace_truncated": False,
        "termination_reason": "model_end", "termination_inferred": True,
        "actions_by_type": {"ppr": 1}, "skip_reasons": {"kg_unavailable": 1},
        "citation_contribution": {
            "ppr": {"steps": 1, "steps_with_ids": 1, "unknown_steps": 0,
                    "cited_hits": 2},
        },
    }
    base.update(overrides)
    return base


def test_analysis_cli_writes_markdown_and_json(tmp_path, capsys):
    source = _write_rows(tmp_path / "rows.jsonl", [
        _row(), _row(policy_version="v2", termination_inferred=False,
                     termination_reason="model_sufficient"),
    ])
    md, js = tmp_path / "t0.md", tmp_path / "t0.json"
    assert analyze.main([
        str(source), "--group-by", "consumer,policy_version",
        "--out-md", str(md), "--out-json", str(js),
    ]) == 0
    capsys.readouterr()
    rendered = md.read_text("utf-8")
    assert "reflect T0 轨迹聚合" in rendered
    assert "legacy" in rendered and "v2" in rendered
    report = json.loads(js.read_text("utf-8"))
    assert report["n_rows"] == 2
    assert len(report["groups"]) == 2


def test_analysis_output_has_no_free_text_keys(tmp_path, capsys):
    from app.domain.reasoning_trace_stats import RUN_PROJECTION_KEYS

    source = _write_rows(tmp_path / "rows.jsonl", [_row() for _ in range(6)])
    js = tmp_path / "t0.json"
    analyze.main([str(source), "--out-json", str(js)])
    capsys.readouterr()
    report = json.loads(js.read_text("utf-8"))
    for group in report["groups"]:
        for section in ("numeric", "boolean", "categorical", "counters"):
            for metric in group["summary"].get(section, {}):
                assert metric in RUN_PROJECTION_KEYS, metric


def test_quantiles_are_withheld_below_min_samples(tmp_path, capsys):
    source = _write_rows(tmp_path / "rows.jsonl", [_row() for _ in range(3)])
    js = tmp_path / "t0.json"
    analyze.main([str(source), "--min-samples", "5", "--out-json", str(js)])
    capsys.readouterr()
    entry = json.loads(js.read_text("utf-8"))["groups"][0]["summary"]
    assert entry["numeric"]["reflect_turns"]["n_observed"] == 3
    assert "p50" not in entry["numeric"]["reflect_turns"]


def test_unknown_values_are_counted_as_missing_not_as_zero(tmp_path, capsys):
    source = _write_rows(tmp_path / "rows.jsonl", [
        _row(total_ms=None), _row(total_ms=None), _row(total_ms=400),
    ])
    js = tmp_path / "t0.json"
    analyze.main([str(source), "--out-json", str(js)])
    capsys.readouterr()
    entry = json.loads(js.read_text("utf-8"))["groups"][0]["summary"]["numeric"]
    assert entry["total_ms"] == {"n_observed": 1, "n_missing": 2, "mean": 400.0}


def test_pair_table_needs_both_sides_and_a_question_key(tmp_path, capsys):
    js = tmp_path / "t0.json"
    only_legacy = _write_rows(tmp_path / "a.jsonl", [_row(), _row()])
    analyze.main([str(only_legacy), "--out-json", str(js)])
    capsys.readouterr()
    assert json.loads(js.read_text("utf-8"))["pairs"] == []

    both = _write_rows(tmp_path / "b.jsonl", [_row(), _row(policy_version="v2")])
    analyze.main([str(both), "--out-json", str(js)])
    capsys.readouterr()
    pairs = json.loads(js.read_text("utf-8"))["pairs"]
    assert len(pairs) == 1
    assert pairs[0]["question_key"] == "B-q01"
    assert pairs[0]["legacy"]["n_runs"] == pairs[0]["v2"]["n_runs"] == 1

    anonymous = _write_rows(tmp_path / "c.jsonl", [
        _row(question_key="unknown"),
        _row(question_key="unknown", policy_version="v2"),
    ])
    analyze.main([str(anonymous), "--out-json", str(js)])
    capsys.readouterr()
    assert json.loads(js.read_text("utf-8"))["pairs"] == []


def test_analysis_refuses_rows_with_keys_outside_the_closed_set(tmp_path):
    source = _write_rows(tmp_path / "rows.jsonl", [_row(question="原文")])
    with pytest.raises(SystemExit, match="闭集外的键"):
        analyze.main([str(source)])


def test_analysis_refuses_an_unknown_group_by_dimension(tmp_path):
    source = _write_rows(tmp_path / "rows.jsonl", [_row()])
    with pytest.raises(SystemExit):
        analyze.main([str(source), "--group-by", "question"])


# --- search 子命令 -----------------------------------------------------------


def test_dry_run_search_enumerates_two_cells_two_policies_two_efforts(capsys):
    assert rig.main(["--dry-run", "--limit", "5", "search"]) == 0
    printed = capsys.readouterr().out
    # 5 题 × 2 格 × 2 策略 × 2 档 = 40。
    assert "planned runs  40" in printed
    assert "policy=legacy effort=standard" in printed
    assert "policy=v2 effort=deep" in printed
    # `search` 不建库也不建图,所以另外两格在主库上根本不存在,不许出现在计划里。
    assert "A_kg" not in printed and "B_nokg" not in printed
    # 主库连接必须显式给:`--database-url` 的默认值指向的是 ask/seed 的测试库。
    assert "<MISSING:必须显式给>" in printed
    # 调用量估计:每题一次意图 + 不调 plan(已确认意图直接作首轮种子)。
    assert "intent 10 + plan 0 + reflect ≤" in printed


def test_dry_run_search_charges_a_plan_call_per_run_without_intent(capsys):
    assert rig.main(["--dry-run", "--limit", "1", "--no-intent", "search"]) == 0
    printed = capsys.readouterr().out
    assert "intent 0 + plan 8 + reflect ≤" in printed


def test_search_plan_pairs_the_two_policies_and_carries_no_idempotency_key():
    questions = rig.load_questions()
    plan = rig.search_plan(
        questions, cells=list(rig.SEARCH_CELLS), policies=rig.POLICIES,
        limit=5, lang="zh", efforts=rig.EFFORTS,
    )
    assert len(plan) == 40
    # `search` 一条 ask_jobs 都不写,留着幂等键会让人以为库里查得到这批 run。
    assert all("client_request_id" not in item for item in plan)
    sides = {
        policy: [(item["question_key"], item["corpus_cell"], item["effort"])
                 for item in plan if item["policy"] == policy]
        for policy in rig.POLICIES
    }
    assert sides["legacy"] == sides["v2"]


def test_search_preflight_requires_the_main_database_url_and_both_notebooks():
    """两个硬前提在**跑之前**就说清楚,而不是跑到一半才发现连错了库。"""
    args = rig.build_parser().parse_args(["search"])
    args.database_url_explicit = False
    assert "必须显式给 --database-url" in rig._search_preflight(
        args, ["A_nokg"], {})
    args.database_url_explicit = True
    assert "--source-notebook-a" in rig._search_preflight(
        args, ["A_nokg"], {})
    assert "--source-notebook-b" in rig._search_preflight(
        args, ["B_kg"], {"A_nokg": "nb-a"})
    assert rig._search_preflight(
        args, ["A_nokg", "B_kg"], {"A_nokg": "nb-a", "B_kg": "nb-b"}
    ) == ""
    # `--cell A_kg search` 这种选空了的组合不许静默跑出零个 run。
    assert "不在 search 的范围里" in rig._search_preflight(args, [], {})


def test_search_process_env_forces_every_injection_switch_off():
    """主库只读不许依赖「那三个端口恰好没接线」这个会随重构漂移的前提。"""
    args = rig.build_parser().parse_args(
        ["--database-url", "postgresql://127.0.0.1:5432/main", "search"])
    env = rig._search_process_env(args)
    assert env["DATABASE_URL"] == "postgresql://127.0.0.1:5432/main"
    assert env["RETRIEVAL_EXPERIENCE_INJECT_ENABLED"] == "false"
    assert env["REASONING_CONSULT_MEMORY_ENABLED"] == "false"
    assert env["AGENT_PROFILE_ENABLED"] == "false"


def _search_item(policy: str) -> dict:
    return {
        "question_key": "A-q01", "corpus_cell": "A_nokg", "policy": policy,
        "effort": "standard", "question": "RTL到GDSII流程",
    }


def test_search_core_runs_both_policies_on_one_repo_and_writes_nothing(
    rrepo, tmp_path
):
    """**不调真实模型的端到端**:同一个 repo 上,legacy / v2 各跑一次真检索。

    钉住三件事:

    1. **策略切换靠换 Settings,不靠重启**。两次 run 用的是同一个 `rrepo`,
       只有传给 `run_search_once` 的那份 settings 不同;两行投影的
       `policy_version` 必须因此分成 legacy / v2。这正是
       `ReasoningRetriever.from_repository(repo, settings)` 读传入 settings
       (而不是 repo 内部那份)这条前提的验证。
    2. **投影是从真轨迹上出来的**,不是构造出来的:`reflect_turns >= 1`、
       没跑合成的那几列一律 unknown。
    3. **只读**:一次真检索前后,`READONLY_TABLES` 的行数逐张相等。

    这条走的是 CLI 底下的核心函数(`run_search_once` / `project_search_run`),
    不经 argparse、不连 PG、不发任何模型请求。
    """
    from app.domain.reasoning_trace_stats import TERMINATION_REASON_VALUES

    notebook = _seed_two_nodes(rrepo)
    # 熔断是另一条守卫的题目;这里要的是"两套协议都跑得到收尾"。
    rrepo.settings.reasoning_stale_limit = 9
    legacy_settings = rrepo.settings
    v2_settings = rrepo.settings.model_copy()
    v2_settings.reasoning_reflect_v2_enabled = True
    assert legacy_settings.reasoning_reflect_v2_enabled is False

    database_url = f"sqlite:///{tmp_path / 't.db'}"
    before = rig._readonly_counts(database_url)
    # 语料真的种进去了,否则下面那条只读断言是在一个空库上恒真。
    assert before["knowledge_objects"]

    rows: dict[str, dict] = {}
    for policy, settings, client in (
        ("legacy", legacy_settings, _SeqLLM(
            plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
            reflects=[{"next_action": "answer", "sufficient": True}])),
        ("v2", v2_settings, _GatedV2LLM(
            plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
            reflects=[{"next_action": "answer", "sufficient": True,
                       "arguments": {}}])),
    ):
        bind_chat_client(rrepo, "reasoning_agent", client)
        item = _search_item(policy)
        steps: list = []
        result = rig.run_search_once(
            rrepo, settings, notebook=notebook.id, item=item, prepared=None,
            on_step=lambda step: steps.append(rig.trace_step_row(step)),
            cancel_event=threading.Event(), actor_id="t0-owner",
        )
        rows[policy] = rig.project_search_run(
            steps, result=result, effort=item["effort"], policy=policy,
            question_key=item["question_key"],
            corpus_cell=item["corpus_cell"], kg_in_scope=True,
        )

    assert rows["legacy"]["policy_version"] == "legacy"
    assert rows["v2"]["policy_version"] == "v2"
    for policy, row in rows.items():
        assert row["reflect_turns"] >= 1, policy
        assert row["trace_source"] == "in_process", policy
        assert row["consumer"] == "ask_single", policy
        assert row["question_key"] == "A-q01", policy
        assert row["corpus_cell"] == "A_nokg", policy
        assert row["anchors"] is None, policy
        assert row["citation_contribution"] is None, policy
        assert row["trace_steps"] > 0, policy
    assert rows["legacy"]["termination_inferred"] is True
    assert rows["v2"]["termination_inferred"] is False
    assert rows["v2"]["termination_reason"] in TERMINATION_REASON_VALUES
    assert rig._readonly_counts(database_url) == before


def test_search_core_refuses_a_settings_that_disagrees_with_the_policy(rrepo):
    """声明 v2、settings 却是 legacy ⇒ 当场报错,而不是跑出一批贴错标的轨迹。"""
    notebook = _seed_two_nodes(rrepo)
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[{"next_action": "answer", "sufficient": True}]))
    with pytest.raises(RuntimeError, match="reflect_v2_active"):
        rig.run_search_once(
            rrepo, rrepo.settings, notebook=notebook.id,
            item=_search_item("v2"), prepared=None, on_step=None,
            cancel_event=threading.Event(), actor_id="t0-owner",
        )


# --- 显式来源范围(codex #700 R3 P2) -----------------------------------------


def _insert_source_row(repo: Any, source_id: str, notebook_id: str, title: str) -> None:
    """往 `sources` 表插一行,只为 `resolve_scope_source_ids` 的解析用例—— repo
    的公开建来源接口要走真实上传/解析流程,这里只需要 `id`/`notebook_id`/
    `title` 三个字段就位。"""
    with repo._connect() as db:
        db.execute(
            "INSERT INTO sources "
            "(id,notebook_id,title,source_type,status,parse_status,file_name,"
            "error_message,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (source_id, notebook_id, title, "text", "extracted", "extracted",
             title, "", "2026-09-08T00:00:00", "2026-09-08T00:00:00"),
        )


def test_resolve_scope_source_ids_matches_by_title_substring(rrepo, tmp_path):
    """`ask`/`seed` 重新上传出的测试库里,标题会带上主库存储层加的
    `{source_id}_` 前缀(见题集 B-q09 的 `note`);子串匹配让同一份标题在
    「主库既有笔记本」与「重新上传出的测试库」两条路上都能解析到位。"""
    notebook = _seed_two_nodes(rrepo)
    _insert_source_row(rrepo, "src-qwen", notebook.id,
                       "qwen_papers__04_Qwen-VL_mineru.md")
    _insert_source_row(
        rrepo, "src-deepseek-v2", notebook.id,
        # 模拟 `ask`/`seed` 重新上传的那份:磁盘文件名本身带着主库存储层的
        # `{source_id}_` 前缀,整段被当成新上传的文件名。
        "src-af28908c73_deepseek_papers__01_DeepSeek-V2_mineru.md",
    )
    # 同一篇论文的另一次解析(B-q07「同标题/同内容」那个),标题完全不同,
    # 不该被误撞进匹配。
    _insert_source_row(rrepo, "src-deepseek-dup", notebook.id,
                       "05_DeepSeekV2_mineru.md")
    database_url = f"sqlite:///{tmp_path / 't.db'}"
    resolved = rig.resolve_scope_source_ids(
        database_url, notebook.id,
        ("qwen_papers__04_Qwen-VL_mineru.md",
         "deepseek_papers__01_DeepSeek-V2_mineru.md"),
    )
    assert resolved == ["src-qwen", "src-deepseek-v2"]


def test_resolve_scope_source_ids_fails_loud_on_zero_or_ambiguous_matches(
    rrepo, tmp_path,
):
    """0 个命中(标题不存在)与 ≥2 个命中(同一子串撞了两个来源)都判失败——
    调用方据此把这道题标 `status=failed`,不静默退化成全库检索。"""
    notebook = _seed_two_nodes(rrepo)
    database_url = f"sqlite:///{tmp_path / 't.db'}"
    assert rig.resolve_scope_source_ids(
        database_url, notebook.id, ("no-such-title.md",),
    ) is None
    _insert_source_row(rrepo, "src-1", notebook.id, "paper_v1.md")
    _insert_source_row(rrepo, "src-2", notebook.id, "paper_v1_final.md")
    assert rig.resolve_scope_source_ids(
        database_url, notebook.id, ("paper_v1",),
    ) is None


def test_run_search_once_applies_the_resolved_source_scope(rrepo, monkeypatch):
    """`scope_source_ids` 非空时,`retriever.run` 执行期间的
    `current_source_scope()` 必须是 `mode="include"` + 那份 id 集合——
    `source_scope_context(notebook, None, None)` 那条 no-op 分支不该被走到。
    """
    from app.services.reasoning_retrieval import ReasoningRetriever
    from app.services.source_scope import current_source_scope

    notebook = _seed_two_nodes(rrepo)
    captured: dict[str, Any] = {}

    def fake_run(self, notebook_id, question, history="", on_step=None,
                 top_n=None, max_steps=None, intent_queries=None,
                 limits=None, intent_detail=None):
        scope = current_source_scope()
        captured["mode"] = scope.mode if scope else None
        captured["source_ids"] = set(scope.source_ids) if scope else None
        return types.SimpleNamespace(termination=None)

    monkeypatch.setattr(ReasoningRetriever, "run", fake_run)
    rig.run_search_once(
        rrepo, rrepo.settings, notebook=notebook.id, item=_search_item("legacy"),
        prepared=None, on_step=lambda step: None, cancel_event=threading.Event(),
        actor_id="t0-owner", scope_source_ids=["src-qwen", "src-deepseek-v2"],
    )
    assert captured["mode"] == "include"
    assert captured["source_ids"] == {"src-qwen", "src-deepseek-v2"}


def test_run_search_once_leaves_scope_a_no_op_when_no_ids_are_given(
    rrepo, monkeypatch,
):
    """改动前的默认行为逐字不变:`scope_source_ids=None`(未声明范围的题)
    仍然是 `source_scope_context(notebook, None, None)` 的 no-op。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    from app.services.source_scope import current_source_scope

    notebook = _seed_two_nodes(rrepo)
    captured: dict[str, Any] = {}

    def fake_run(self, notebook_id, question, history="", on_step=None,
                 top_n=None, max_steps=None, intent_queries=None,
                 limits=None, intent_detail=None):
        captured["scope"] = current_source_scope()
        return types.SimpleNamespace(termination=None)

    monkeypatch.setattr(ReasoningRetriever, "run", fake_run)
    rig.run_search_once(
        rrepo, rrepo.settings, notebook=notebook.id, item=_search_item("legacy"),
        prepared=None, on_step=lambda step: None, cancel_event=threading.Event(),
        actor_id="t0-owner",
    )
    assert captured["scope"] is None


def _search_loop_args(database_url: str) -> Any:
    args = rig.build_parser().parse_args(["search"])
    args.database_url = database_url
    args.no_intent = True
    args.keep_raw_trace = False
    return args


def test_search_loop_marks_unresolved_scope_failed_without_calling_run_search_once(
    rrepo, tmp_path, monkeypatch,
):
    """标题解析不到唯一匹配:整道题标 `status=failed`/`scope_narrowed=None`,
    `run_search_once` 绝不能被调到——那正是 codex #700 R3 P2 要堵的「解析不到
    位就悄悄退化成一次不设限的全库检索」。把这个守卫改回去(删掉
    `_search_loop` 里的 `scope_failed` 分支)时,这条用例会因为
    `run_search_once` 被调用而报红,这就是它的变异验证。
    """
    notebook = _seed_two_nodes(rrepo)
    database_url = f"sqlite:///{tmp_path / 't.db'}"

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "范围解析失败时不该调 run_search_once —— 不能悄悄退化成全库检索"
        )

    monkeypatch.setattr(rig, "run_search_once", _boom)

    item = dict(_search_item("legacy"))
    item["scope_source_titles"] = ("no-such-title.md",)
    out_dir = tmp_path / "out"
    runner = rig.Runner(dry_run=False, out_dir=out_dir)
    facts = {"A_nokg": {"notebook": notebook.id, "sources": 0,
                        "kg_in_scope": False}}
    rig._search_loop(
        _search_loop_args(database_url), runner, [item], facts, rrepo,
        {"legacy": rrepo.settings, "v2": rrepo.settings},
        actor_id="t0-owner", cancel_event=threading.Event(),
    )
    lines = (out_dir / "search-legacy.jsonl").read_text("utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["status"] == "failed"
    assert row["scope_narrowed"] is None
    assert row["question_key"] == "A-q01"


def test_search_loop_writes_scope_narrowed_true_for_a_resolved_scope(
    rrepo, tmp_path,
):
    """解析到位的题:`run_search_once` 真的带着 `include` 范围跑一次真检索,
    落盘的投影行 `scope_narrowed=True`。"""
    notebook = _seed_two_nodes(rrepo)
    _insert_source_row(rrepo, "src-a", notebook.id, "paper-a.md")
    _insert_source_row(rrepo, "src-b", notebook.id, "paper-b.md")
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[{"next_action": "answer", "sufficient": True}]))

    database_url = f"sqlite:///{tmp_path / 't.db'}"
    item = dict(_search_item("legacy"))
    item["scope_source_titles"] = ("paper-a.md", "paper-b.md")
    out_dir = tmp_path / "out"
    runner = rig.Runner(dry_run=False, out_dir=out_dir)
    facts = {"A_nokg": {"notebook": notebook.id, "sources": 2,
                        "kg_in_scope": True}}
    rig._search_loop(
        _search_loop_args(database_url), runner, [item], facts, rrepo,
        {"legacy": rrepo.settings, "v2": rrepo.settings},
        actor_id="t0-owner", cancel_event=threading.Event(),
    )
    lines = (out_dir / "search-legacy.jsonl").read_text("utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["status"] != "failed"
    assert row["scope_narrowed"] is True
    assert row["reflect_turns"] >= 1


def test_readonly_assertion_reports_every_table_that_moved(capsys):
    runner = rig.Runner(dry_run=False, out_dir=Path("."))
    before = {table: 3 for table in rig.READONLY_TABLES}
    rig._assert_readonly(runner, before, dict(before))
    capsys.readouterr()
    after = dict(before, answers=4, conversations=5)
    with pytest.raises(RuntimeError, match="answers: 3 -> 4"):
        rig._assert_readonly(runner, before, after)
    assert "READ-ONLY VIOLATION" in capsys.readouterr().err


def test_dry_run_search_refuses_a_cell_it_cannot_run(capsys):
    """`--cell A_kg search`:主库上没有那个格子,不许打印一份「零个 run」的计划。"""
    assert rig.main(["--dry-run", "--cell", "A_kg", "search"]) == 2
    assert "不在 search 的范围里" in capsys.readouterr().err


# --- search 并发(--concurrency) ---------------------------------------------


def test_dry_run_search_with_concurrency_flag_has_zero_side_effects(capsys):
    """`--dry-run --concurrency 4 search`:只打印,不连库、不起线程池。"""
    assert rig.main(
        ["--dry-run", "--concurrency", "4", "--limit", "1", "search"]
    ) == 0
    printed = capsys.readouterr().out
    assert "concurrency" in printed
    assert "4(两阶段" in printed


def test_resolve_concurrency_forces_sqlite_to_one(tmp_path, capsys):
    """SQLite 主库不为它验证真并发,真跑时一律 clamp 到 1。"""
    runner = rig.Runner(dry_run=False, out_dir=tmp_path)
    result = rig._resolve_concurrency(
        runner, f"sqlite:///{tmp_path / 't.db'}", object(), 4,
    )
    assert result == 1
    assert "SQLite" in capsys.readouterr().out
    # 请求的就是 1:不该打印一条没意义的 clamp 诊断
    capsys.readouterr()
    rig._resolve_concurrency(runner, f"sqlite:///{tmp_path / 't.db'}", object(), 1)
    assert "clamp" not in capsys.readouterr().out


def test_resolve_concurrency_clamps_to_postgres_pool_max_size(tmp_path, capsys):
    """请求的并发度超过 `POSTGRES_POOL_MAX_SIZE` 时 clamp,没超时原样通过。"""
    runner = rig.Runner(dry_run=False, out_dir=tmp_path)
    settings = types.SimpleNamespace(postgres_pool_max_size=3)
    clamped = rig._resolve_concurrency(
        runner, "postgresql://127.0.0.1:5432/main", settings, 10,
    )
    assert clamped == 3
    assert "POSTGRES_POOL_MAX_SIZE" in capsys.readouterr().out
    within = rig._resolve_concurrency(
        runner, "postgresql://127.0.0.1:5432/main", settings, 2,
    )
    assert within == 2


class _ThreadLocalLLM:
    """把 `chat_json` 转发给"当前线程绑定"的那个替身。

    并发用例要在同一个 workload_id(`"reasoning_agent"`)上同时跑 legacy / v2
    两套协议,而 `bind_chat_client` 的覆盖是**整个 repo 共享**的一个键——两个
    worker 线程各绑一次会互相踩。这里换一层:绑定只发生在各自的 worker 线程
    里(`threading.local`),`chat_json` 只转发,不持有任何跨线程的可变状态。
    """

    configured = True

    def __init__(self) -> None:
        self._local = threading.local()

    def bind(self, client: Any) -> None:  # noqa: ANN401 — 测试替身,类型从简
        self._local.client = client

    def chat_json(self, messages, schema_hint, **kwargs):
        return self._local.client.chat_json(messages, schema_hint, **kwargs)


def test_search_concurrent_two_questions_across_both_policies_on_one_repo(
    rrepo, tmp_path
):
    """concurrency=2 的真端到端:2 题 × 2 策略 = 4 个 run 真的并发跑在同一个
    SQLite repo 上。钉住:legacy / v2 各出两行、`policy_version` 正确、只读
    计数不变、JSONL 每行可原样序列化再解析回来。

    这条走的是 `run_search_once` 本身(`ThreadPoolExecutor.map` 直接驱动),
    不经 `_search_loop_concurrent` 的编排——编排层的并发峰值与失败隔离/取消
    由下面两条纯逻辑用例(假 `run_search_once`)分别钉住。
    """
    notebook = _seed_two_nodes(rrepo)
    rrepo.settings.reasoning_stale_limit = 9
    legacy_settings = rrepo.settings
    v2_settings = rrepo.settings.model_copy()
    v2_settings.reasoning_reflect_v2_enabled = True
    settings_by_policy = {"legacy": legacy_settings, "v2": v2_settings}

    dispatcher = _ThreadLocalLLM()
    bind_chat_client(rrepo, "reasoning_agent", dispatcher)

    plan = [
        {"question_key": key, "corpus_cell": "A_nokg", "policy": policy,
         "effort": "standard", "question": "RTL到GDSII流程"}
        for key in ("A-q01", "A-q02")
        for policy in ("legacy", "v2")
    ]

    database_url = f"sqlite:///{tmp_path / 't.db'}"
    before = rig._readonly_counts(database_url)
    assert before["knowledge_objects"]

    def run_one(item: dict) -> dict:
        policy = item["policy"]
        if policy == "legacy":
            client = _SeqLLM(
                plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
                reflects=[{"next_action": "answer", "sufficient": True}],
            )
        else:
            client = _GatedV2LLM(
                plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
                reflects=[{"next_action": "answer", "sufficient": True,
                           "arguments": {}}],
            )
        dispatcher.bind(client)
        steps: list = []
        result = rig.run_search_once(
            rrepo, settings_by_policy[policy], notebook=notebook.id, item=item,
            prepared=None,
            on_step=lambda step: steps.append(rig.trace_step_row(step)),
            cancel_event=threading.Event(), actor_id="t0-owner",
        )
        return rig.project_search_run(
            steps, result=result, effort=item["effort"], policy=policy,
            question_key=item["question_key"], corpus_cell=item["corpus_cell"],
            kg_in_scope=True,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(run_one, plan))

    assert len(rows) == 4
    for row in rows:
        assert row["reflect_turns"] >= 1
        assert row["trace_steps"] > 0
        assert row["question_key"] in ("A-q01", "A-q02")
        assert row["corpus_cell"] == "A_nokg"
        # 每行都能原样序列化再解析回来——JSONL"每行一份完整投影"的前提。
        line = json.dumps(row, ensure_ascii=False, sort_keys=True)
        assert json.loads(line) == row
    legacy_rows = [r for r in rows if r["policy_version"] == "legacy"]
    v2_rows = [r for r in rows if r["policy_version"] == "v2"]
    assert len(legacy_rows) == 2, rows
    assert len(v2_rows) == 2, rows
    assert rig._readonly_counts(database_url) == before


def _concurrent_search_args(no_intent: bool = True) -> Any:
    args = rig.build_parser().parse_args(["search"])
    args.no_intent = no_intent
    return args


def test_search_loop_concurrent_reaches_the_requested_peak_concurrency(
    tmp_path, monkeypatch,
):
    """`_search_loop_concurrent` 编排层的纯逻辑用例:假 `run_search_once` 用一个
    `threading.Barrier` 逼真并发——凑不齐 `concurrency` 个线程就会在这里超时,
    所以峰值确实达到 `concurrency` 是**确定性**结论,不是靠 sleep 猜时序。
    """
    concurrency = 3
    plan = [
        {"question_key": f"Q{i:02d}", "corpus_cell": "A_nokg", "policy": "legacy",
         "effort": "standard", "question": "q"}
        for i in range(concurrency * 2)
    ]
    facts = {"A_nokg": {"notebook": "nb-a", "sources": 1, "kg_in_scope": True}}
    settings_by_policy = {"legacy": object(), "v2": object()}
    barrier = threading.Barrier(concurrency, timeout=5)
    lock = threading.Lock()
    state = {"current": 0, "peak": 0}

    def fake_run_search_once(repo, settings, *, notebook, item, prepared,
                             on_step, cancel_event, actor_id,
                             scope_source_ids=None):
        with lock:
            state["current"] += 1
            state["peak"] = max(state["peak"], state["current"])
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        finally:
            with lock:
                state["current"] -= 1
        return types.SimpleNamespace(termination=None)

    monkeypatch.setattr(rig, "run_search_once", fake_run_search_once)

    runner = rig.Runner(dry_run=False, out_dir=tmp_path)
    profile = types.SimpleNamespace(id="t0-owner")
    rig._search_loop_concurrent(
        _concurrent_search_args(), runner, plan, facts, None,
        settings_by_policy, actor_id="t0-owner", profile=profile,
        concurrency=concurrency,
    )

    assert state["peak"] == concurrency
    rows = [
        json.loads(line)
        for line in (tmp_path / "search-legacy.jsonl").read_text().splitlines()
    ]
    assert len(rows) == len(plan)


def test_search_loop_concurrent_cancels_remaining_runs_on_policy_mismatch(
    tmp_path, monkeypatch,
):
    """守卫:声明的策略与第一个完成的 run 的证据不一致 ⇒ 取消剩余任务。

    第一个 run(`Q00`,声明 `--policy=v2`)立即回一个 legacy 形状的证据
    (`termination=None`),触发 `abort()`;其余 run 卡在**各自的**
    `cancel_event.wait()` 上,只有被 `abort()` 设过之后才会醒来抛
    `AskCancelled`。断言:异常真的抛出来,而且没有全部 8 个 run 都执行到——
    `executor.shutdown(cancel_futures=True)` 确实撤掉了还没开始跑的那些。
    """
    from app.services.cancellation import AskCancelled

    total = 8
    plan = [
        {"question_key": f"Q{i:02d}", "corpus_cell": "A_nokg",
         "policy": "v2" if i == 0 else "legacy", "effort": "standard",
         "question": "q"}
        for i in range(total)
    ]
    facts = {"A_nokg": {"notebook": "nb-a", "sources": 1, "kg_in_scope": True}}
    settings_by_policy = {"legacy": object(), "v2": object()}
    executed: list[str] = []
    lock = threading.Lock()

    def fake_run_search_once(repo, settings, *, notebook, item, prepared,
                             on_step, cancel_event, actor_id,
                             scope_source_ids=None):
        with lock:
            executed.append(item["question_key"])
        if item["question_key"] == "Q00":
            return types.SimpleNamespace(termination=None)
        if cancel_event.wait(timeout=5):
            raise AskCancelled()
        raise TimeoutError("cancel_event 一直没被设置——abort() 没生效")

    monkeypatch.setattr(rig, "run_search_once", fake_run_search_once)

    runner = rig.Runner(dry_run=False, out_dir=tmp_path)
    profile = types.SimpleNamespace(id="t0-owner")
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="声明 --policy"):
        rig._search_loop_concurrent(
            _concurrent_search_args(), runner, plan, facts, None,
            settings_by_policy, actor_id="t0-owner", profile=profile,
            concurrency=2,
        )
    elapsed = time.monotonic() - started

    assert len(executed) < total, executed
    # 队列里还没开始跑的那几个靠 `shutdown(cancel_futures=True)` 就够撤掉,
    # 但**已经在跑、卡在自己 cancel_event 上**的那一个(Q01)只能靠 `abort()`
    # 主动 `.set()` 唤醒——它上面绑的 5s 超时就是留给"abort() 没生效"这种
    # 退化的:那种情况下这条用例不会断言失败,但会顶着 5s 变慢,这里把它钉成
    # 响亮失败而不是"凑巧通过但慢一拍"。
    assert elapsed < 3.0, f"in-flight run 没有被 abort() 及时唤醒(耗时 {elapsed:.1f}s)"


# --- search 的 --only-question / --only-cell 筛选 ---------------------------


def test_dry_run_search_only_question_and_only_cell_filter_to_four_runs(capsys):
    """`--only-question B-q03 --only-cell B_kg`:1 题 × 1 格 × 2 策略 × 2 档 = 4。"""
    assert rig.main([
        "--dry-run", "--only-question", "B-q03", "--only-cell", "B_kg",
        "search",
    ]) == 0
    printed = capsys.readouterr().out
    assert "planned runs  4" in printed
    assert "A_nokg" not in printed
    for line in printed.splitlines():
        if line.startswith("[dry-run]") and " search " in line:
            assert "B-q03" in line and "cell=B_kg" in line


def test_dry_run_search_prints_the_scope_note_for_the_narrowed_question(capsys):
    """B-q09 声明了两个来源标题;dry-run 不连库,打印的只是静态计数
    `scope=2 sources`,不该触发 `resolve_scope_source_ids` 的任何 DB 读。"""
    assert rig.main([
        "--dry-run", "--only-question", "B-q09", "--only-cell", "B_kg",
        "search",
    ]) == 0
    printed = capsys.readouterr().out
    found = False
    for line in printed.splitlines():
        if line.startswith("[dry-run]") and " search " in line and "B-q09" in line:
            assert "scope=2 sources" in line
            found = True
    assert found, printed
    # 没声明范围的题不该被误标上一个 scope 注记。
    for line in printed.splitlines():
        if "B-q03" in line:
            assert "scope=" not in line


def test_dry_run_ask_prints_the_scope_note_for_the_narrowed_question(
    tmp_path, capsys,
):
    """`ask` 没有 `--only-question`,用 `--limit 9` 把 B 语料的前 9 题(含
    B-q09)排进计划(codex #700 R3 P2:HTTP `ask` 路径此前完全不知道范围
    这件事)。"""
    out_dir = tmp_path / "t0"
    assert rig.main([
        "--dry-run", "--out-dir", str(out_dir), "--cell", "B_nokg",
        "--limit", "9", "ask",
    ]) == 0
    printed = capsys.readouterr().out
    found = False
    for line in printed.splitlines():
        if line.startswith("[dry-run]") and " ask " in line and "B-q09" in line:
            assert "scope=2 sources" in line
            found = True
    assert found, printed


def test_only_question_filters_before_the_limit_is_applied():
    """先筛后限:`--only-question` 选中的题不该被 `--limit` 提前的切片挡在外面。"""
    questions = rig.load_questions()
    # B 语料的题号顺序里 B-q03 排第三;limit=1 若先切片会把它切掉。
    plan = rig.search_plan(
        questions, cells=["B_kg"], policies=rig.POLICIES, limit=1,
        lang="zh", efforts=rig.EFFORTS, only_questions=["B-q03"],
    )
    assert plan  # 先筛后限:B-q03 没有被 limit=1 的前置切片挡掉
    assert {item["question_key"] for item in plan} == {"B-q03"}
    assert len(plan) == 4  # 2 策略 × 2 档


def test_only_cell_narrows_the_cells_the_preflight_and_plan_both_see(capsys):
    """`--only-cell` 收窄的是 `cmd_search` 里同一份 `cells`,预检与计划打印同源。"""
    assert rig.main(["--dry-run", "--only-cell", "A_nokg", "search"]) == 0
    printed = capsys.readouterr().out
    assert "B_kg" not in printed
    assert "corpus cell  A_nokg" in printed


# --- search 的 --keep-raw-trace ---------------------------------------------


def test_search_loop_writes_raw_trace_files_only_when_the_flag_is_set(
    rrepo, tmp_path,
):
    """`--keep-raw-trace` 落盘的原始轨迹带 `summary`(投影里没有的那一份),
    并且带上了 termination DTO;不给这个开关时 `raw/` 目录不该被创建。
    """
    notebook = _seed_two_nodes(rrepo)
    rrepo.settings.reasoning_stale_limit = 9
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[{"next_action": "answer", "sufficient": True}]))

    plan = [_search_item("legacy")]
    facts = {"A_nokg": {"notebook": notebook.id, "sources": 1,
                        "kg_in_scope": True}}
    settings_by_policy = {"legacy": rrepo.settings, "v2": rrepo.settings}

    args = rig.build_parser().parse_args(["search"])
    args.no_intent = True
    args.keep_raw_trace = False
    runner = rig.Runner(dry_run=False, out_dir=tmp_path)
    rig._search_loop(
        args, runner, plan, facts, rrepo, settings_by_policy,
        actor_id="t0-owner", cancel_event=threading.Event(),
    )
    assert not (tmp_path / "raw").exists()

    # 同一个笔记本再跑一遍,这次打开开关。
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[{"next_action": "answer", "sufficient": True}]))
    args.keep_raw_trace = True
    rig._search_loop(
        args, runner, plan, facts, rrepo, settings_by_policy,
        actor_id="t0-owner", cancel_event=threading.Event(),
    )
    raw_path = tmp_path / "raw" / "legacy" / "A-q01_A_nokg_standard.json"
    assert raw_path.exists()
    payload = json.loads(raw_path.read_text(encoding="utf-8"))
    assert payload["trace_steps"], "原始轨迹不该是空的"
    first = payload["trace_steps"][0]
    assert set(first) == {"step_type", "summary", "detail", "duration_ms"}
    assert isinstance(first["summary"], str) and first["summary"]
    # legacy 没有 termination DTO。
    assert payload["termination"] is None


def test_search_loop_concurrent_writes_raw_trace_under_the_write_lock(
    tmp_path, monkeypatch,
):
    """并发路径:`--keep-raw-trace` 的文件写在既有 `write_lock` 保护的那一段里,
    每题一份、互不覆盖。"""
    plan = [
        {"question_key": f"Q{i:02d}", "corpus_cell": "A_nokg", "policy": "legacy",
         "effort": "standard", "question": "q"}
        for i in range(3)
    ]
    facts = {"A_nokg": {"notebook": "nb-a", "sources": 1, "kg_in_scope": True}}
    settings_by_policy = {"legacy": object(), "v2": object()}

    def fake_run_search_once(repo, settings, *, notebook, item, prepared,
                             on_step, cancel_event, actor_id,
                             scope_source_ids=None):
        on_step(types.SimpleNamespace(
            step_type="reflect", summary="人话",
            detail={"next_action": "answer", "sufficient": True},
            duration_ms=5,
        ))
        return types.SimpleNamespace(termination=None)

    monkeypatch.setattr(rig, "run_search_once", fake_run_search_once)

    args = _concurrent_search_args()
    args.keep_raw_trace = True
    runner = rig.Runner(dry_run=False, out_dir=tmp_path)
    profile = types.SimpleNamespace(id="t0-owner")
    rig._search_loop_concurrent(
        args, runner, plan, facts, None, settings_by_policy,
        actor_id="t0-owner", profile=profile, concurrency=3,
    )

    raw_files = sorted((tmp_path / "raw" / "legacy").glob("*.json"))
    assert [p.name for p in raw_files] == [
        "Q00_A_nokg_standard.json", "Q01_A_nokg_standard.json",
        "Q02_A_nokg_standard.json",
    ]
    for path in raw_files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["trace_steps"][0]["summary"] == "人话"


def test_auto_clarification_answers_only_required_rows_and_prefer_first_option():
    """无人在场时 rig 替用户答必填澄清项(首跑实测契约带必填项时 `answers=[]`
    被 `finalize_query_intent` 拒掉、整批起不来):有选项取第一个、无选项用固定
    句、非必填不答;两次调用同一份 seed 得到逐字相同的答案(确定性)。"""
    seed = {"ambiguities": [
        {"id": "a1", "question": "范围?", "required": True,
         "options": [" 全部来源 ", "仅本库"]},
        {"id": "a2", "question": "口径?", "required": True, "options": []},
        {"id": "a3", "question": "可选项", "required": False},
        {"question": "无 id 的坏行", "required": True},
    ]}
    answers = rig.auto_clarification_answers(seed)
    assert answers == [
        {"id": "a1", "answer": "全部来源"},
        {"id": "a2", "answer": rig.AUTO_CLARIFICATION_ANSWER},
    ]
    assert rig.auto_clarification_answers(seed) == answers
    assert rig.auto_clarification_answers({}) == []


# ---------------------------------------------------------------------------
# export:按 merge_key 合并,不重复(codex #700 R2 P2)
# ---------------------------------------------------------------------------


def test_export_merge_dedupes_by_merge_key_keeping_the_traced_row(tmp_path):
    """`--reports` 导出的结果级独行与 rig 自己写的 `report-trace-*.jsonl`
    (同一个 `merge_key`,但带了轨迹)撞号时,合并只留一行——且是带轨迹
    (`trace_steps` 键)的那行,不是逐字节拼接文件、把一份报告的一节报成两个
    run(README 479–481 的「每节一行」不变量)。没有 `merge_key` 的行(纯 Ask
    run)不受影响,原样透传。"""
    ask_runs = tmp_path / "ask-runs.jsonl"
    report_trace = tmp_path / "report-trace-legacy.jsonl"
    ask_runs.write_text(
        json.dumps({"consumer": "ask_single", "question_key": "A-q01"},
                   sort_keys=True) + "\n"
        + json.dumps({
            "consumer": "report_section", "merge_key": "deadbeef01234567",
            "section_index": 0, "section_total": 1, "attempted": 0,
        }, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    report_trace.write_text(
        json.dumps({
            "consumer": "report_section", "merge_key": "deadbeef01234567",
            "section_index": 0, "section_total": 1, "trace_steps": 3,
            "reflect_turns": 1, "policy_version": "legacy",
        }, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "t0-dataset.jsonl"

    rig._merge_export_parts([ask_runs, report_trace], out)

    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2, f"应该是 1 条 ask 行 + 1 条合并后的报告行,实得 {rows!r}"
    merged = [row for row in rows if row.get("merge_key") == "deadbeef01234567"]
    assert len(merged) == 1, "同一个 merge_key 撞出了两行——回到了逐字节拼接的老问题"
    assert merged[0]["trace_steps"] == 3, (
        "撞号时结果级独行(没有 trace_steps)赢了;应该是带轨迹的那行赢"
    )
    assert any(row.get("consumer") == "ask_single" for row in rows)


def test_export_merge_keeps_a_report_row_with_no_matching_trace_file(tmp_path):
    """结果级行如果没有轨迹半份可对(`report-trace-*.jsonl` 缺失或不含它),
    仍然要保留——不是「没有轨迹就整行丢弃」。"""
    ask_runs = tmp_path / "ask-runs.jsonl"
    ask_runs.write_text(
        json.dumps({
            "consumer": "report_section", "merge_key": "onlyresultlevel0",
            "section_index": 0, "section_total": 1,
        }, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "t0-dataset.jsonl"

    rig._merge_export_parts([ask_runs], out)

    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["merge_key"] == "onlyresultlevel0"
    assert "trace_steps" not in rows[0]


# ---------------------------------------------------------------------------
# report:澄清门代答 + 门控失败可观测(codex #700 R2 P2)
# ---------------------------------------------------------------------------


def _report_item(question: str) -> dict:
    return {
        "question_key": "t0-report-01", "corpus_cell": "B_kg",
        "policy": "legacy", "depth": 1, "profile": "shallow",
        "question": question,
    }


def _stub_report_generation_at_class_level(monkeypatch):
    """`_stub_auto_run_corpus`(test_report_engine.py)的类级等价物:rig 的
    `_generate_report` 在函数内部自建引擎实例,拿不到现成的 `eng` 打实例补丁,
    所以这里直接打在 `ReportEngine` 类上——子类(若有)未覆写这两个方法时同样
    生效。"""
    from app.services.reasoning_retrieval import ReasoningResult
    from app.services.report_engine import ReportEngine

    monkeypatch.setattr(ReportEngine, "_build_corpus_map", lambda self, n, q: "MAP")
    monkeypatch.setattr(
        ReportEngine, "_deep_dive",
        lambda self, nb_id, section, question, depth=None, on_step=None:
            ReasoningResult(),
    )


def test_report_generate_recovers_when_auto_confirm_intent_returns_none(
    rrepo, monkeypatch,
):
    """替身:强制 `ReportEngine._auto_confirm_intent` 一律 fail-open 返回
    `None`(其 docstring 承诺的行为——契约带必填澄清项、范围重验不过、CAS 输
    给别人时都会这样)。修复前 `_generate_report`/`_run_reports` 对此一无所
    知:报告原地停在 `intent_ready`,`sections` 恒空,调用方照样打『report
    done』——看起来完全像成功(codex #700 R2 P2)。修复后:rig 用与
    `POST .../reports/{id}/intent` 手工确认端点同一条入口
    (`confirmed_understanding` + `claim_report_intent`)代为确认,答案走
    `auto_clarification_answers` 那条确定性规则,报告应该正常跑到 `done`。"""
    from types import SimpleNamespace

    from app.services.model_work import ModelPriority, model_work_scope
    from app.services.report_engine import ReportEngine
    from app.services.source_scope import source_scope_context
    from tests.test_report_engine import _AutoRunLLM, _bind_report_llm, _mk_nb

    nb = _mk_nb(rrepo)
    llm = _AutoRunLLM()  # 清晰问题:本来完全不需要人工确认
    _bind_report_llm(rrepo, llm)
    _stub_report_generation_at_class_level(monkeypatch)
    monkeypatch.setattr(rrepo.retrieval, "federated_retrieve", lambda a, q: [])
    monkeypatch.setattr(
        ReportEngine, "_auto_confirm_intent", lambda self, *a, **kw: None,
    )
    question = "分析 PLL 稳定性"
    report_id = rrepo.create_report(nb.id, question, depth=1)
    item = _report_item(question)
    profile = SimpleNamespace(id="rig-owner")

    captured, gate_reason = rig._generate_report(
        rrepo, ReportEngine, profile, nb.id, report_id, item,
        model_work_scope=model_work_scope, model_priority=ModelPriority.REPORT,
        source_scope_context=source_scope_context,
    )

    assert gate_reason is None, "代答应该已经把报告解开,不该再判门控失败"
    detail = rrepo.get_report(nb.id, report_id)
    assert detail["status"] == "done", detail.get("error")

    rows = list(rig._report_rows(
        rrepo, nb.id, report_id, item, captured, gate_reason=gate_reason,
    ))
    assert rows, "代答成功后应该有真实的节可对"
    assert all(row.get("status") != "failed" for row in rows)


def test_report_generate_marks_clarification_gate_failed_when_the_claim_is_lost(
    rrepo, monkeypatch,
):
    """代答的确认入口本身也走不通(这里模拟 CAS 输给别的写者)时:不能再假装
    『report done』——报告记 `gate_reason="clarification_gate"`,投影行带
    `status="failed"`,而不是一行都不产出、看起来和"这份报告本来就没内容"一
    模一样(codex #700 R2 P2)。"""
    from types import SimpleNamespace

    from app.services.model_work import ModelPriority, model_work_scope
    from app.services.report_engine import ReportEngine
    from app.services.source_scope import source_scope_context
    from tests.test_report_engine import _AutoRunLLM, _bind_report_llm, _mk_nb

    nb = _mk_nb(rrepo)
    llm = _AutoRunLLM()
    _bind_report_llm(rrepo, llm)
    _stub_report_generation_at_class_level(monkeypatch)
    monkeypatch.setattr(rrepo.retrieval, "federated_retrieve", lambda a, q: [])
    monkeypatch.setattr(
        ReportEngine, "_auto_confirm_intent", lambda self, *a, **kw: None,
    )
    monkeypatch.setattr(rrepo, "claim_report_intent", lambda *a, **kw: False)
    question = "分析 PLL 稳定性"
    report_id = rrepo.create_report(nb.id, question, depth=1)
    item = _report_item(question)
    profile = SimpleNamespace(id="rig-owner")

    captured, gate_reason = rig._generate_report(
        rrepo, ReportEngine, profile, nb.id, report_id, item,
        model_work_scope=model_work_scope, model_priority=ModelPriority.REPORT,
        source_scope_context=source_scope_context,
    )

    assert gate_reason == "clarification_gate"
    detail = rrepo.get_report(nb.id, report_id)
    assert detail["status"] == "intent_ready", "没能代答,报告应该原地留在人工门"

    rows = list(rig._report_rows(
        rrepo, nb.id, report_id, item, captured, gate_reason=gate_reason,
    ))
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert "merge_key" not in rows[0], "失败行没有轨迹半份可对,不该带 merge_key"


# ---------------------------------------------------------------------------
# search:代答过的题以确认后的问题为权威,对齐生产判据(codex #700 R2 P2)
# ---------------------------------------------------------------------------


def test_prepare_search_intent_authoritative_only_without_submitted_answers():
    """`authoritative` 要与 `AskService._prepare_reasoning_ask` 的
    `auto_confirmed_clear_intent` 同一个判据:提交了契约、没有待澄清项、
    **也没有提交过澄清答案**才让用户原文权威。`finalize_query_intent` 恒把
    `needs_clarification` 清空,所以只看那一位在 rig 里恒真——真正把"代答过"
    和"本来就没有歧义"分开的是 `clarification_answers`。"""
    question = "原始问题文本（未澄清）"
    resolved = "确认后的研究问题（已澄清）"
    base_contract = {
        "objective": question,
        "resolved_question": resolved,
        "needs_clarification": False,
    }

    # 代答过必填项:合成后的 resolved_question 才是权威,不是用户原文。
    answered = {
        **base_contract,
        "clarification_answers": [
            {"id": "a1", "question": "范围？", "answer": "全部来源"},
        ],
    }
    prepared = rig._prepare_search_intent(answered, question, "standard")
    assert prepared["research_question"].startswith(resolved), (
        "提交过澄清答案时应该以确认后的问题为权威,而不是恒真地保留用户原文"
    )
    assert prepared["intent_queries"][0].startswith(resolved)

    # 没有代答过:与现状一致,用户原文仍是首要权威。
    clear = {**base_contract, "clarification_answers": []}
    prepared_clear = rig._prepare_search_intent(clear, question, "standard")
    assert prepared_clear["research_question"].startswith(question)
    assert prepared_clear["intent_queries"][0].startswith(question)
