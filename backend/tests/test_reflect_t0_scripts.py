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
from pathlib import Path

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
    # `--db-name` 有默认值,而 DROP 走的是 `--admin-url`:一次跑在 SQLite 上的
    # rig 不该让 teardown 去 PG 上删一个同名的、别人的库。
    def parsed(database_url: str):
        return rig.build_parser().parse_args(
            ["--database-url", database_url, "teardown"]
        )

    assert rig._teardown_targets_this_run(
        parsed("postgresql://127.0.0.1:5432/silicon_notebook_t0_test")
    ) is True
    assert rig._teardown_targets_this_run(
        parsed(f"sqlite:///{tmp_path / 'silicon_notebook_t0_test'}")
    ) is False
    assert rig._teardown_targets_this_run(
        parsed("postgresql://127.0.0.1:5432/somebody_elses_db")
    ) is False


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
