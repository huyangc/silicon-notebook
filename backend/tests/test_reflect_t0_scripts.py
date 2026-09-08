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
from pathlib import Path

import pytest

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
    for command in ("seed", "ask", "report", "export", "teardown"):
        assert rig.main(["--dry-run", "--out-dir", str(out_dir), command]) == 0
    capsys.readouterr()
    assert not out_dir.exists()


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
