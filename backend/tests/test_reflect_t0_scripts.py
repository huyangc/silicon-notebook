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
import subprocess
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


def test_stop_backend_refuses_a_pid_that_is_not_our_backend(monkeypatch):
    """后端已退出、PID 被别的进程复用时,照 PID 发 SIGTERM/SIGKILL 会打到用户的
    无关进程(codex #700 R11 P2)。发信号前逐字比 seed/restart 记下的进程身份;
    不符就一个信号都不发。老 state 没记身份时至少要求命令行是 uvicorn 的
    `app.main:app`。"""
    import os as _os

    kills: list[tuple[int, int]] = []

    def fake_kill(pid, sig):
        kills.append((pid, sig))
        if sig == 0:
            raise ProcessLookupError(pid)

    monkeypatch.setattr(_os, "kill", fake_kill)
    runner = rig.Runner(dry_run=False, out_dir=Path("/nonexistent"))
    ours = "Tue Sep  9 10:00:00 2026 uvicorn app.main:app --port 8011"

    monkeypatch.setattr(rig, "_process_identity", lambda pid: "Tue Sep  9 11:00:00 2026 vim notes.txt")
    assert rig._stop_backend_gracefully(runner, 4242, expected_identity=ours) is False
    assert kills == []
    # 老 state 没有身份:命令行不是 rig 的 uvicorn 后端也不发。
    assert rig._stop_backend_gracefully(runner, 4242, expected_identity=None) is False
    assert kills == []

    monkeypatch.setattr(rig, "_process_identity", lambda pid: None)
    assert rig._stop_backend_gracefully(runner, 4242, expected_identity=ours) is False
    assert kills == []

    monkeypatch.setattr(rig, "_process_identity", lambda pid: ours)
    assert rig._stop_backend_gracefully(runner, 4242, expected_identity=ours) is True
    assert kills[0] == (4242, 15)
    assert all(sig in (15, 0) for _, sig in kills), "身份对上就不该走到 SIGKILL"


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


def test_default_database_url_follows_db_name(monkeypatch):
    """`--db-name another_t0 seed` 建的是 another_t0,默认 `--database-url` 却
    钉在常量 DEFAULT_TEST_DB 上——扩展安装与迁移/播种会打到旧库(存在就被写,
    不存在就报一句无关的连接错)(codex #700 R5 P2)。默认 URL 必须跟着
    `--db-name` 走;显式给了 `--database-url` 时不动它。"""
    seen: list[Any] = []
    monkeypatch.setattr(rig, "cmd_seed", lambda args, runner: seen.append(args) or 0)

    assert rig.main(["--dry-run", "--db-name", "another_t0", "seed"]) == 0
    assert seen[-1].database_url == "postgresql://127.0.0.1:5432/another_t0"
    assert seen[-1].database_url_explicit is False

    assert rig.main(["--dry-run", "seed"]) == 0
    assert seen[-1].database_url == f"postgresql://127.0.0.1:5432/{rig.DEFAULT_TEST_DB}"

    explicit = "postgresql://db.internal:6543/whatever_test"
    assert rig.main([
        "--dry-run", "--db-name", "another_t0", "--database-url", explicit, "seed",
    ]) == 0
    assert seen[-1].database_url == explicit
    assert seen[-1].database_url_explicit is True


def test_seed_refuses_when_database_url_and_db_name_disagree(tmp_path, capsys):
    """显式 `--database-url` 指向的库名与 `--db-name` 不一致 ⇒ seed 在建库之前
    就拒绝(退出码 2),连 dry-run 也一样——这是字面核对,不必连库
    (codex #700 R5 P2)。"""
    rc = rig.main([
        "--dry-run", "--out-dir", str(tmp_path / "t0"),
        "--db-name", "another_t0",
        "--database-url", "postgresql://127.0.0.1:5432/silicon_notebook_t0_test",
        "seed",
    ])
    printed = capsys.readouterr().out
    assert rc == 2
    assert "refuse" in printed
    assert "another_t0" in printed and "silicon_notebook_t0_test" in printed
    assert "create database" not in printed, "拒绝要发生在建库计划打印之前"
    assert "install extensions" not in printed

    args = rig.build_parser().parse_args([
        "--db-name", "x_test", "--database-url", "postgresql://h/x_test?sslmode=require",
        "--admin-url", "postgresql://h:5432/postgres",
        "seed",
    ])
    assert rig._seed_target_mismatch(args) is None, "查询串不该干扰库名比对"

    # 库名一致但 --admin-url 与 --database-url 不是同一台服务器 ⇒ 建库落 A 机、
    # 扩展/迁移/播种落 B 机,同样在建库之前拒绝(codex #700 R6 P2)。
    rc = rig.main([
        "--dry-run", "--out-dir", str(tmp_path / "t0"),
        "--admin-url", "postgresql://db-a.internal:5432/postgres",
        "--database-url", "postgresql://db-b.internal:5432/silicon_notebook_t0_test",
        "seed",
    ])
    printed = capsys.readouterr().out
    assert rc == 2
    assert "不是同一台服务器" in printed
    assert "create database" not in printed

    # SQLite 冒烟(README 的 `--skip-create-db` 路径)不是 PG 目标:库名与 admin
    # 端点核对对它没有意义,不得把它挡在门外(codex #700 R7 P2)。
    sqlite_url = f"sqlite:///{tmp_path / 't0.db'}"
    args = rig.build_parser().parse_args(["--database-url", sqlite_url, "seed"])
    assert rig._seed_target_mismatch(args) is None
    rc = rig.main([
        "--dry-run", "--out-dir", str(tmp_path / "t0-sqlite"),
        "--database-url", sqlite_url, "--skip-create-db", "seed",
    ])
    capsys.readouterr()
    assert rc == 0


def test_rig_tag_decoder_rejects_segments_that_are_not_short_codes():
    """API 放行 128 字的幂等键,一段 65 字的题号却会让 `project_run` 的值形状守卫
    抛 ValueError、打死整次导出(codex #700 R20 P2)。畸形段按「不是 rig 发的」
    处理:返回空字典,只进基线表。"""
    from export_reasoning_traces import decode_client_request_id

    good = "t0:A-q01:A_kg:legacy:deep:reasoning"
    assert decode_client_request_id(good)["question_key"] == "A-q01"
    too_long = "t0:" + "q" * 65 + ":A_kg:legacy:deep:reasoning"
    assert len(too_long) <= 128
    assert decode_client_request_id(too_long) == {}
    assert decode_client_request_id("t0:A q01:A_kg:legacy:deep:reasoning") == {}
    assert decode_client_request_id("t0::A_kg:legacy:deep:reasoning") == {}


def test_await_parse_walks_every_page_of_sources(monkeypatch):
    """`sources` 单页上限 200:超过 200 个来源时只看第一页会在后页还在解析时就
    宣布完成,后页的失败也数不到(codex #700 R20 P2)。以短页为终止翻完全部。"""
    calls: list[str] = []
    page1 = [{"id": f"s{i}", "parse_status": "extracted"} for i in range(200)]
    page2 = [{"id": "s200", "parse_status": "queued"}]
    state = {"round": 0}

    def fake_http(self, method, url, **kwargs):
        calls.append(url)
        if "offset=0" in url:
            return {"items": page1, "total": 201}
        if "offset=200" in url:
            # 第一轮还在排队,第二轮解析完——只看第一页的实现会在第一轮就返回。
            status = "queued" if state["round"] == 0 else "extracted"
            state["round"] += 1
            return {"items": [{**page2[0], "parse_status": status}], "total": 201}
        raise AssertionError(f"unexpected page {url}")

    monkeypatch.setattr(rig.Runner, "http", fake_http)
    monkeypatch.setattr(rig.time, "sleep", lambda *_: None)
    runner = rig.Runner(dry_run=False, out_dir=Path("/nonexistent"))
    items = rig.await_parse(runner, "http://b", "nb", token="t", timeout=60)
    assert len(items) == 201
    assert state["round"] == 2, "第二页的排队项必须被等到终态"
    assert any("offset=200" in url for url in calls)


def test_http_error_bodies_are_reduced_to_safe_codes(monkeypatch):
    """4xx 正文会回显被拒的输入(问题原文、意图契约);整段抄进 RuntimeError 打到
    stderr 就是原文泄露(codex #700 R19 P2)。只保留短错误码,其余脱敏;
    `Runner.http` 与 `Runner.stream` 同一条规则。"""
    import io
    import urllib.error
    import urllib.request

    secret = "机密问题原文:RTL到GDSII流程"
    assert rig._safe_http_error_detail(
        json.dumps({"detail": {"code": "invalid_username", "message": secret}}).encode()
    ) == "code=invalid_username"
    assert rig._safe_http_error_detail(
        json.dumps({"detail": secret}).encode()
    ) == "<body redacted>"
    assert rig._safe_http_error_detail(secret.encode()) == "<body redacted>"
    assert rig._safe_http_error_detail(
        json.dumps({"detail": {"code": "has space in it"}}).encode()
    ) == "<body redacted>"

    def _raise_422(request, timeout=0):
        raise urllib.error.HTTPError(
            request.full_url, 422, "Unprocessable", None,
            io.BytesIO(json.dumps({"detail": {"code": "intent_mismatch",
                                             "question": secret}}).encode()),
        )

    monkeypatch.setattr(urllib.request, "urlopen", _raise_422)
    runner = rig.Runner(dry_run=False, out_dir=Path("/nonexistent"))
    for call in (
        lambda: runner.http("POST", "http://127.0.0.1:1/ask/intent", json_body={"q": secret}),
        lambda: runner.stream("POST", "http://127.0.0.1:1/ask/stream", json_body={"q": secret}),
    ):
        with pytest.raises(RuntimeError) as excinfo:
            call()
        message = str(excinfo.value)
        assert "422" in message and "code=intent_mismatch" in message
        assert secret not in message


def test_start_backend_refuses_an_endpoint_that_already_answers(monkeypatch):
    """就绪探测分不清应答者是刚起的子进程还是早就占着端口的别人;后者会让
    `_start_backend` 把别人的 PID 当成功返回,seed 的注册/上传/建图全打到那台
    后端上(codex #700 R5 P2)。任何 HTTP 应答(含 503/404)都算占用;只有连接
    被拒/超时才算空闲。"""
    import io
    import urllib.error
    import urllib.request

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda url, timeout=0: _Resp(b'{"ready": true}'),
    )
    with pytest.raises(RuntimeError, match="已经有后端在应答"):
        rig._assert_endpoint_free("http://127.0.0.1:8011")

    def _http_503(url, timeout=0):
        raise urllib.error.HTTPError(url, 503, "warming", None, None)

    monkeypatch.setattr(urllib.request, "urlopen", _http_503)
    with pytest.raises(RuntimeError, match="已经有后端在应答"):
        rig._assert_endpoint_free("http://127.0.0.1:8011")

    def _refused(url, timeout=0):
        raise urllib.error.URLError(ConnectionRefusedError(61, "refused"))

    monkeypatch.setattr(urllib.request, "urlopen", _refused)
    assert rig._assert_endpoint_free("http://127.0.0.1:8011") is None


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


def test_admin_targets_same_server_compares_live_identity_not_forwarded_port():
    """带真连接的那一半比的是两条连接各自落到的**服务器身份**,不是「服务端监听
    端口 == URL 端口」(codex #700 R8 P2):Docker 端口映射 / SSH 隧道
    `localhost:15432 → server:5432` 下 `inet_server_port()` 回 5432,老判据会把
    合法的 seed/teardown 全拒掉。用替身注入身份:同身份放行,异身份拒绝。"""
    admin = "postgresql://127.0.0.1:15432/postgres"
    db = "postgresql://127.0.0.1:15432/silicon_notebook_t0_test"

    def ident(sysid, endpoint=("10.0.0.5", 5432)):
        return {"system_identifier": sysid, "server_endpoint": endpoint}

    same = {admin: ident(7), db: ident(7)}
    ok, reason = rig._admin_targets_same_server(admin, db, identity=same.get)
    assert ok is True and reason == ""

    other = {admin: ident(7), db: ident(8)}
    ok, reason = rig._admin_targets_same_server(admin, db, identity=other.get)
    assert ok is False
    assert "不是同一台服务器" in reason

    # 混合权限(codex #700 R9 P2):admin 是超级用户读得到 system_identifier,
    # database 角色读不到——同一台服务器要按两边都有的尺(服务端 addr:port)比,
    # 不能因为表示形不同就拒。
    mixed_same = {admin: ident(7), db: ident(None)}
    ok, reason = rig._admin_targets_same_server(admin, db, identity=mixed_same.get)
    assert ok is True, reason
    mixed_other = {admin: ident(7), db: ident(None, ("10.0.0.6", 5432))}
    ok, reason = rig._admin_targets_same_server(admin, db, identity=mixed_other.get)
    assert ok is False


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
        return {"event": "final"}

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


def test_ask_counts_runs_without_a_final_frame_as_failures(tmp_path, monkeypatch, capsys):
    """执行阶段失败/取消走 HTTP 200 里的 `event="error"`/`"cancelled"` 帧;只看
    标签落库只证明 job 建了,整批全失败也会退出码 0(codex #700 R9 P2)。终帧不是
    `final` 的 run 计失败、整批退出码 1;日志只记事件名,不记 error 正文。"""
    out_dir = tmp_path / "t0"
    out_dir.mkdir()
    (out_dir / "rig-state.json").write_text(json.dumps({
        "policy": "legacy", "token": "tok", "notebooks": {"A_nokg": "nb-a"},
    }), encoding="utf-8")
    monkeypatch.setattr(rig.Runner, "http", lambda self, *a, **k: None)
    monkeypatch.setattr(
        rig.Runner, "stream",
        lambda self, *a, **k: {"event": "error", "error": "秘密错误正文 RuntimeError"},
    )
    monkeypatch.setattr(rig, "_assert_tag_landed", lambda *a, **k: None)

    rc = rig.main([
        "--out-dir", str(out_dir), "--limit", "1", "--cell", "A_nokg", "ask",
    ])
    out = capsys.readouterr()
    assert rc == 1
    assert "terminal event=error" in out.out
    assert "秘密错误正文" not in out.out and "秘密错误正文" not in out.err


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


def test_analysis_consumes_every_scalar_projection_key():
    """投影里每一个**标量**键都要被某一组指标消费掉(T-BF7 评审 F4)。

    `RUN_PROJECTION_KEYS` 是写侧的闭集,聚合器的四组指标是读侧;写侧加了键、读侧
    忘了加,那一列就在报告里静默缺席——`assessment_rejections` 与 `scope_narrowed`
    正是这么漏掉的。下面按「投影行里出现过的键」反查,身份/分组键与结构化键
    (dict/list)显式豁免。

    变异:把 `assessment_rejections` 或 `scope_narrowed` 从指标元组里删掉 ⇒ 这条红。
    """
    from app.domain.reasoning_trace_stats import RUN_PROJECTION_KEYS

    covered = set(
        analyze.NUMERIC_METRICS + analyze.BOOLEAN_METRICS
        + analyze.CATEGORICAL_METRICS + analyze.COUNTER_METRICS)
    # 身份/分组维度(它们是分格依据,不是被聚合的指标)与自由文本身份。
    dimensions = {
        "consumer", "mode", "effort", "kg_in_scope", "policy_version",
        "corpus_cell", "question_key", "notebook_bucket", "section_index",
        "merge_key", "requested_mode",
    }
    # 结构化键:值是 dict/list,四组标量指标按定义都吃不下它们。
    structured = {"citation_contribution", "action_seq"}
    missing = RUN_PROJECTION_KEYS - covered - dimensions - structured
    assert not missing, sorted(missing)
    assert {"assessment_rejections", "scope_narrowed"} <= covered


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


def test_min_samples_must_be_positive_and_empty_metrics_never_reach_quantiles(tmp_path):
    """`--min-samples 0`/负数会让没有观测的指标进分位数分支,`_nearest([])` 抛
    IndexError 打死整份报告(codex #700 R22 P3)。CLI 拒绝非正数;函数层即便被
    直接传 0 也不对空观测算分位数。"""
    source = _write_rows(tmp_path / "rows.jsonl", [_row(total_ms=None)])
    with pytest.raises(SystemExit):
        analyze.main([str(source), "--min-samples", "0"])
    with pytest.raises(SystemExit):
        analyze.main([str(source), "--min-samples", "-3"])
    summary = analyze.summarize_group([_row(total_ms=None)], min_samples=0)
    entry = summary["numeric"]["total_ms"]
    assert entry["n_observed"] == 0
    assert "p50" not in entry and "p95" not in entry


def test_pair_table_does_not_pair_across_workloads(tmp_path, capsys):
    """同题同格同档,一侧是只跑检索的进程内 run(`trace_source=in_process`)、
    另一侧是导出的完整 Ask run(`trace_steps`):不是同一种工作负载,不能配成
    一对去比耗时(codex #700 R11 P2)。同工作负载才配。"""
    js = tmp_path / "t0.json"
    mixed = _write_rows(tmp_path / "m.jsonl", [
        _row(trace_source="in_process"),
        _row(policy_version="v2", trace_source="trace_steps"),
    ])
    analyze.main([str(mixed), "--out-json", str(js)])
    capsys.readouterr()
    assert json.loads(js.read_text("utf-8"))["pairs"] == []

    same = _write_rows(tmp_path / "s.jsonl", [
        _row(trace_source="in_process"),
        _row(policy_version="v2", trace_source="in_process"),
    ])
    analyze.main([str(same), "--out-json", str(js)])
    capsys.readouterr()
    pairs = json.loads(js.read_text("utf-8"))["pairs"]
    assert len(pairs) == 1 and pairs[0]["trace_source"] == "in_process"

    # `--no-intent` 的 run(无冻结契约)与带契约的 run 也不是同一种工作负载
    # (codex #700 R15 P2)。
    intent_mixed = _write_rows(tmp_path / "i.jsonl", [
        _row(has_intent_contract=True),
        _row(policy_version="v2", has_intent_contract=False),
    ])
    analyze.main([str(intent_mixed), "--out-json", str(js)])
    capsys.readouterr()
    assert json.loads(js.read_text("utf-8"))["pairs"] == []


def test_sqlite_reader_encodes_odd_filenames_and_stays_read_only(tmp_path):
    """路径里带 `#`/`?`/`%`/空格时,直接内插进 `file:` URI 会把 `mode=ro` 当片段丢
    掉、以可写方式打开(甚至新建)别的文件(codex #700 R17 P2)。先按 RFC 编码
    再拼查询串:读得到真实文件,且仍是只读。"""
    import sqlite3

    from export_reasoning_traces import _Reader

    odd_dir = tmp_path / "odd dir #1"
    odd_dir.mkdir()
    db_path = odd_dir / "t0 100%?.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE t (v TEXT)")
        conn.execute("INSERT INTO t VALUES ('ok')")
        conn.commit()

    with _Reader(f"sqlite:///{db_path}") as reader:
        assert reader.query("SELECT v FROM t", ())[0]["v"] == "ok"
        with pytest.raises(sqlite3.OperationalError):
            reader._conn.execute("INSERT INTO t VALUES ('nope')")
    # 没有因为 URI 被截断而新建出别的文件。
    assert sorted(p.name for p in odd_dir.iterdir()) == [db_path.name]


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


def test_dry_run_prints_both_the_call_count_and_the_request_ceiling(capsys):
    """计划 §3 T-PS5:dry-run 打**两个**数,不是一个。

    上面那个数的是**逻辑调用**(llm.jsonl 的行数、`model_calls` 的口径),下面
    那个数的是**真正发出去的请求**(行上 `attempts` 的口径)。只打一个数会让读
    的人拿逻辑调用去估端点负载——而静默 fallback 与重试都发生在那之下,两个数
    在最坏情况下差一倍以上。
    """
    assert rig.main(["--dry-run", "--limit", "1", "--no-intent", "search"]) == 0
    printed = capsys.readouterr().out
    assert "次逻辑调用" in printed
    assert "请求上界 ≤" in printed
    assert "静默 fallback 另计" in printed
    # 上界不是预测:它恒 ≥ 逻辑调用数,乘数是配置里的重试预算。
    assert rig.REASONING_ATTEMPT_BUDGET >= 2


def test_the_attempt_budget_tracks_the_config_default_it_hardcodes():
    """rig 那个 `1 + 1` 抄的是 `REASONING_MAX_RETRIES` 的默认值,得钉住。

    不钉的失败场景:有人把配置默认改成 3,rig 打的「请求上界 ≤ 212」比真实上界
    低一倍——而那个数正是用来估端点负载、决定这批跑不跑得起的。

    **只有用例 import config**:dry-run 本身不 import 它是另一条约束(起一份
    Settings 要读 .env、连不上的库还会拖慢预演),由别的用例管,这里不碰。
    """
    from app.core.config import Settings

    assert rig.REASONING_ATTEMPT_BUDGET == 1 + Settings().reasoning_max_retries


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


#: `_rig_process_env` 的**全部**键(`--env-file` 那一项另算)。写成一份清单而
#: 不是逐条 assert:漏加一项在数据上看不出任何区别(codex 质量评审 P2-9),而
#: 多加一项(比如哪天有人往里塞一个进程级的 optimization)会当场打红。
RIG_PROCESS_ENV_KEYS = {
    "DATABASE_URL",
    "RETRIEVAL_EXPERIENCE_INJECT_ENABLED",
    "REASONING_CONSULT_MEMORY_ENABLED",
    "AGENT_PROFILE_ENABLED",
    "LLM_LOG_PATH",
    "EVENT_LOG_DIR",
}


def test_search_process_env_forces_every_injection_switch_off():
    """主库只读不许依赖「那三个端口恰好没接线」这个会随重构漂移的前提。"""
    args = rig.build_parser().parse_args(
        ["--database-url", "postgresql://127.0.0.1:5432/main", "search"])
    env = rig._search_process_env(args)
    assert set(env) == RIG_PROCESS_ENV_KEYS
    assert env["DATABASE_URL"] == "postgresql://127.0.0.1:5432/main"
    assert env["RETRIEVAL_EXPERIENCE_INJECT_ENABLED"] == "false"
    assert env["REASONING_CONSULT_MEMORY_ENABLED"] == "false"
    assert env["AGENT_PROFILE_ENABLED"] == "false"


def test_rig_never_writes_to_the_machine_wide_log_directory(tmp_path):
    """计划 §3 T-PS5 G5:两串日志都圈进本次 out-dir,`.local/logs` 一个字节不写。

    默认落点是**整台机器共用**的:同一天里别的后端、别的冒烟、别的 rig 会话都
    往同一份 `llm-*.jsonl` / `events-*.jsonl` 追加。共用日志上做时间窗切片会把
    别的进程的调用记到本 run 头上(成本三键),per-call 表的右表里也会混进别人
    的调度事件。
    """
    out = tmp_path / "rig-out"
    args = rig.build_parser().parse_args(
        ["--database-url", "postgresql://127.0.0.1:5432/main",
         "--out-dir", str(out), "search"])
    env = rig._search_process_env(args)
    assert env["LLM_LOG_PATH"] == str((out / "llm" / "llm.jsonl").resolve())
    assert env["EVENT_LOG_DIR"] == str((out / "events").resolve())
    for value in env.values():
        assert ".local/logs" not in value
    # 临时后端那一份同理:`seed` / `restart` 起的 uvicorn 也是 rig 的进程。
    backend = rig.backend_env(args, "legacy")
    assert backend["EVENT_LOG_DIR"] == str((out / "events").resolve())


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
    # **没开跑的 run 没有墙钟**:`run_wall_ms` 是 `None`,不是 0(计划 §3 T-PS5)。
    # 0 会被读成「这个 run 零耗时跑完了」,而且它会真的进 P50/P95 的分位数。
    assert row["run_wall_ms"] is None


def test_backend_env_forces_kg_auto_extract_off():
    """有图/无图格靠 rig 只对 *_kg 格调 `/kg/build` 区分;继承环境或 --env-file
    开着 KG_AUTO_EXTRACT 会让上传即建图,A_nokg/B_nokg 不再无图(codex #700 R12
    P2)。fixture 后端必须显式关掉,且不受 --env-file 影响(显式环境变量优先)。"""
    args = rig.build_parser().parse_args(["--env-file", "/tmp/x.env", "seed"])
    args.database_url = "postgresql://127.0.0.1:5432/x_test"
    env = rig.backend_env(args, "legacy")
    assert env["KG_AUTO_EXTRACT"] == "false"
    assert env["SILICON_NOTEBOOK_ENV_FILE"].endswith("x.env")


def test_search_loop_serial_writes_a_failed_row_and_counts_it(
    rrepo, tmp_path, monkeypatch,
):
    """串行路径(默认并发 1、SQLite 强制)里 `run_search_once` 抛异常此前直接
    打断整批、不落行(codex #700 R12 P2):要与并发路径同口径——失败 run 落
    `status=failed` 行、日志只记异常类名、返回失败数;取消仍然向上抛。"""
    from app.services.cancellation import AskCancelled

    notebook = _seed_two_nodes(rrepo)
    calls: list[str] = []

    def fake_run_search_once(repo, settings, *, notebook, item, prepared,
                             on_step, cancel_event, actor_id,
                             scope_source_ids=None):
        calls.append(item["question_key"])
        if item["question_key"] == "A-q01":
            on_step(types.SimpleNamespace(step_type="reflect", detail={}, duration_ms=5))
            raise ValueError("provider exploded 秘密原文")
        return types.SimpleNamespace(termination=None)

    monkeypatch.setattr(rig, "run_search_once", fake_run_search_once)
    first = dict(_search_item("legacy"))
    second = dict(_search_item("legacy")); second["question_key"] = "A-q02"
    facts = {"A_nokg": {"notebook": notebook.id, "sources": 0, "kg_in_scope": False}}
    out_dir = tmp_path / "serial"
    runner = rig.Runner(dry_run=False, out_dir=out_dir)
    failed = rig._search_loop(
        _search_loop_args(f"sqlite:///{tmp_path / 't.db'}"), runner,
        [first, second], facts, rrepo,
        {"legacy": rrepo.settings, "v2": rrepo.settings},
        actor_id="t0-owner", cancel_event=threading.Event(),
    )
    assert failed == 1
    assert calls == ["A-q01", "A-q02"], "失败不该打断后面的 run"
    rows = [json.loads(l) for l in (out_dir / "search-legacy.jsonl").read_text().splitlines()]
    by_key = {r["question_key"]: r for r in rows}
    assert by_key["A-q01"]["status"] == "failed"
    assert by_key["A-q01"]["reflect_turns"] == 1, "失败前捕获的轨迹步不该被丢成 0"
    # 失败 run 也有 run 墙钟(计划 §3 T-PS5 验收):它崩在半路,但确实占了这么
    # 久,而「哪一侧更容易崩、崩之前烧了多少时间」正是要量的东西之一。
    assert isinstance(by_key["A-q01"]["run_wall_ms"], int)
    assert isinstance(by_key["A-q02"]["run_wall_ms"], int)
    assert by_key["A-q02"]["status"] != "failed"
    log_text = (out_dir / "search-runs.log").read_text("utf-8")
    assert "A-q01 A_nokg legacy standard FAILED ValueError" in log_text
    assert "秘密原文" not in log_text

    def cancelled(*a, **k):
        raise AskCancelled()

    monkeypatch.setattr(rig, "run_search_once", cancelled)
    with pytest.raises(AskCancelled):
        rig._search_loop(
            _search_loop_args(f"sqlite:///{tmp_path / 't2.db'}"),
            rig.Runner(dry_run=False, out_dir=tmp_path / "serial2"),
            [dict(_search_item("legacy"))], facts, rrepo,
            {"legacy": rrepo.settings, "v2": rrepo.settings},
            actor_id="t0-owner", cancel_event=threading.Event(),
        )


def test_failed_run_row_keeps_the_requested_policy_and_loops_count_it(
    rrepo, tmp_path, monkeypatch,
):
    """空轨迹会被 `project_search_run` 推成 legacy,v2 的失败 run 就进了 legacy
    那一列(codex #700 R9 P2):失败行 `policy_version` 按 rig 声明的策略;串行
    循环把 `status=failed` 的行计进返回的失败数(并发循环见
    `test_search_loop_concurrent_writes_a_failed_row_and_reports_failures`)。"""
    fact = {"notebook": "nb", "sources": 1, "kg_in_scope": False}
    item = {"question_key": "B-q09", "corpus_cell": "B_kg", "policy": "v2",
            "effort": "deep", "question": "q"}
    assert rig._failed_run_row(item, fact)["policy_version"] == "v2"

    notebook = _seed_two_nodes(rrepo)

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("范围解析失败时不该调 run_search_once")

    monkeypatch.setattr(rig, "run_search_once", _boom)
    scoped = dict(_search_item("legacy"))
    scoped["scope_source_titles"] = ("no-such-title.md",)
    facts = {"A_nokg": {"notebook": notebook.id, "sources": 0, "kg_in_scope": False}}
    runner = rig.Runner(dry_run=False, out_dir=tmp_path / "serial")
    failed = rig._search_loop(
        _search_loop_args(f"sqlite:///{tmp_path / 't.db'}"), runner, [scoped], facts,
        rrepo, {"legacy": rrepo.settings, "v2": rrepo.settings},
        actor_id="t0-owner", cancel_event=threading.Event(),
    )
    assert failed == 1


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


def test_search_loop_concurrent_writes_a_failed_row_and_reports_failures(
    tmp_path, monkeypatch,
):
    """worker 抛异常的 run 不能只留一行日志就从 JSONL 里消失(codex #700 R8
    P2):分析脚本只读 JSONL,失败会从状态分布里蒸发、样本数偏向跑成的一侧。
    要求:失败 run 落成 `status=failed` 的闭集行(题号/格/策略/档位齐全、
    `scope_narrowed=None`),其余 run 照常;`_search_loop_concurrent` 回失败数,
    日志里有 FAILED 与异常类名、没有问题原文。"""
    plan = [
        {"question_key": f"Q{i:02d}", "corpus_cell": "A_nokg", "policy": "legacy",
         "effort": "standard", "question": "秘密原文不得进日志"}
        for i in range(4)
    ]
    facts = {"A_nokg": {"notebook": "nb-a", "sources": 1, "kg_in_scope": True}}
    settings_by_policy = {"legacy": object(), "v2": object()}

    def fake_run_search_once(repo, settings, *, notebook, item, prepared,
                             on_step, cancel_event, actor_id,
                             scope_source_ids=None):
        if item["question_key"] == "Q02":
            # 失败前已经走了一轮反思:这一步不能从失败行里消失(codex #700 R13 P2)。
            on_step(types.SimpleNamespace(step_type="reflect", detail={}, duration_ms=5))
            raise ValueError("provider exploded")
        return types.SimpleNamespace(termination=None)

    monkeypatch.setattr(rig, "run_search_once", fake_run_search_once)
    runner = rig.Runner(dry_run=False, out_dir=tmp_path)
    failed = rig._search_loop_concurrent(
        _concurrent_search_args(), runner, plan, facts, None,
        settings_by_policy, actor_id="t0-owner",
        profile=types.SimpleNamespace(id="t0-owner"), concurrency=2,
    )

    assert failed == 1
    rows = [
        json.loads(line)
        for line in (tmp_path / "search-legacy.jsonl").read_text().splitlines()
    ]
    assert len(rows) == len(plan)
    by_key = {row["question_key"]: row for row in rows}
    assert by_key["Q02"]["status"] == "failed"
    assert by_key["Q02"]["scope_narrowed"] is None
    assert by_key["Q02"]["policy_version"] == "legacy"
    assert by_key["Q02"]["reflect_turns"] == 1, "失败前捕获的轨迹步不该被丢成 0"
    assert by_key["Q02"]["trace_steps"] == 1
    assert by_key["Q02"]["effort"] == "standard"
    assert by_key["Q02"]["corpus_cell"] == "A_nokg"
    assert all(by_key[k]["status"] != "failed" for k in ("Q00", "Q01", "Q03"))
    # 并发路径的 run 墙钟:成功与失败的 run **都**有(计划 §3 T-PS5 验收)。
    # 轨迹里没有这件事实(`total_ms` 是各步耗时之和,漏掉排队与步间空隙),
    # 只有 rig 手上有。
    assert all(isinstance(row["run_wall_ms"], int) for row in rows)
    log_text = (tmp_path / "search-runs.log").read_text("utf-8")
    assert "Q02" in log_text and "status=failed reason=ValueError" in log_text
    assert "秘密原文" not in log_text


def test_search_loop_concurrent_does_not_start_a_run_registered_after_abort(
    tmp_path, monkeypatch,
):
    """竞态(codex #700 R21 P2):worker 过了入口 `aborted` 检查、还在解析范围时
    别的 run 触发 `abort()`;它随后才登记 cancel_event,快照里没有它,于是带着一个
    永远不会被 set 的事件开始调模型。修复后登记与 abort 同锁复核:收摊已开始的
    worker 直接退出,`run_search_once` 不再被调。用「Q01 的范围解析等到 abort 发生
    之后才返回」把时序钉死,不靠 sleep。"""
    plan = [
        {"question_key": "Q00", "corpus_cell": "A_nokg", "policy": "v2",
         "effort": "standard", "question": "q"},
        {"question_key": "Q01", "corpus_cell": "A_nokg", "policy": "legacy",
         "effort": "standard", "question": "q"},
    ]
    facts = {"A_nokg": {"notebook": "nb-a", "sources": 1, "kg_in_scope": True}}
    settings_by_policy = {"legacy": object(), "v2": object()}
    aborted_seen = threading.Event()
    executed: list[str] = []
    lock = threading.Lock()

    real_resolve = rig._resolve_item_scope

    def slow_resolve(args, item, fact):
        if item["question_key"] == "Q01":
            assert aborted_seen.wait(timeout=5), "abort 一直没发生,时序没钉住"
        return real_resolve(args, item, fact)

    def fake_run_search_once(repo, settings, *, notebook, item, prepared,
                             on_step, cancel_event, actor_id,
                             scope_source_ids=None):
        with lock:
            executed.append(item["question_key"])
        # Q00 声明 v2 却回 legacy 形状的证据 ⇒ 主循环 abort()。
        return types.SimpleNamespace(termination=None)

    real_say = rig.Runner.say

    def spy_say(self, action, detail=""):
        if action == "search aborted":
            aborted_seen.set()
        return real_say(self, action, detail)

    monkeypatch.setattr(rig, "_resolve_item_scope", slow_resolve)
    monkeypatch.setattr(rig, "run_search_once", fake_run_search_once)
    monkeypatch.setattr(rig.Runner, "say", spy_say)
    runner = rig.Runner(dry_run=False, out_dir=tmp_path)
    with pytest.raises(RuntimeError, match="声明 --policy"):
        rig._search_loop_concurrent(
            _concurrent_search_args(), runner, plan, facts, None,
            settings_by_policy, actor_id="t0-owner",
            profile=types.SimpleNamespace(id="t0-owner"), concurrency=2,
        )
    assert executed == ["Q00"], "abort 之后才登记的 Q01 不该开始调模型"


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
    # 报告的投影要带真实上下文,不是 None payload 投出来的 unknown/false
    # (codex #700 R6 P2):depth=1 ⇒ 生产映射 overview;契约已确认 ⇒ True。
    assert all(row["effort"] == "overview" for row in rows)
    assert all(row["has_intent_contract"] is True for row in rows)


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
    # 失败行要带与成功行相同的工作负载维度,否则配对表按维度分格时对照消失
    # (codex #700 R14 P2):depth=1 ⇒ overview;声明策略 legacy。
    assert rows[0]["effort"] == "overview"
    assert rows[0]["mode"] == "reasoning"
    assert rows[0]["trace_source"] == "in_process"
    assert rows[0]["policy_version"] == "legacy"
    assert rows[0]["failed"] is True, "整份报告失败,不能被记成一节『没失败』的观测"


def test_report_generate_marks_planning_failure_failed_on_the_straight_path(
    rrepo, monkeypatch,
):
    """两条执行路径(自动确认直通 / rig 代答后重跑)在 `_generate_report` 末尾
    汇合;只在代答分支里看终态,直通路径上规划或生成失败(引擎把报告置成
    `status="failed"`、`sections` 恒空)仍会被 `_run_reports` 打成『report
    done』(codex #700 R4 P2)。修复后:任何非 `done` 终态都记
    `gate_reason="report_<status>"`,投影出一行 `status="failed"`。"""
    from types import SimpleNamespace

    from app.services.model_work import ModelPriority, model_work_scope
    from app.services.report_engine import ReportEngine
    from app.services.source_scope import source_scope_context
    from tests.test_report_engine import _AutoRunLLM, _bind_report_llm, _mk_nb

    nb = _mk_nb(rrepo)
    _bind_report_llm(rrepo, _AutoRunLLM())
    _stub_report_generation_at_class_level(monkeypatch)
    monkeypatch.setattr(rrepo.retrieval, "federated_retrieve", lambda a, q: [])

    def _boom(self, n, q):
        raise RuntimeError("corpus map exploded")

    # 直通路径:意图自动确认成功,规划阶段炸掉——引擎自己吞异常记 failed。
    monkeypatch.setattr(ReportEngine, "_build_corpus_map", _boom)
    question = "分析 PLL 稳定性"
    report_id = rrepo.create_report(nb.id, question, depth=1)
    item = _report_item(question)
    profile = SimpleNamespace(id="rig-owner")

    captured, gate_reason = rig._generate_report(
        rrepo, ReportEngine, profile, nb.id, report_id, item,
        model_work_scope=model_work_scope, model_priority=ModelPriority.REPORT,
        source_scope_context=source_scope_context,
    )

    detail = rrepo.get_report(nb.id, report_id)
    assert detail["status"] == "failed", detail
    assert gate_reason == "report_failed"

    rows = list(rig._report_rows(
        rrepo, nb.id, report_id, item, captured, gate_reason=gate_reason,
    ))
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert "merge_key" not in rows[0]


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


# --- T-PS4 聚合器的第二维臂与测量列 ------------------------------------------


def _measured_row(**overrides) -> dict:
    """带 T-PS4 测量列的一行。默认是 `v2` + `off` 那一臂。"""
    base = _row(
        policy_version="v2", termination_inferred=False,
        termination_reason="model_sufficient",
        optimization="off", run_wall_ms=9000, model_calls_real=4,
        attempts_observed=True, response_chars_total=500,
        prefix_bytes_median=720, prefix_bytes_min=700, prefix_turns=3,
        context_chars={"s": 800, "c": 1200, "bytes_total": 9600},
    )
    base.update(overrides)
    return base


def test_every_measurement_column_reaches_the_report(tmp_path, capsys):
    """九个新列各自被某一组指标消费掉,并且真的出现在 JSON 报告里。

    变异:把 `run_wall_ms` 从 `NUMERIC_METRICS`、或 `attempts_observed` 从
    `BOOLEAN_METRICS`、或 `optimization` 从 `CATEGORICAL_METRICS`、或
    `context_chars` 从 `COUNTER_METRICS` 里删掉 ⇒ 这条红(也会让
    `test_analysis_consumes_every_scalar_projection_key` 红)。
    """
    source = _write_rows(tmp_path / "rows.jsonl",
                         [_measured_row() for _ in range(5)])
    js = tmp_path / "t0.json"
    analyze.main([str(source), "--out-json", str(js)])
    capsys.readouterr()
    summary = json.loads(js.read_text("utf-8"))["groups"][0]["summary"]
    for metric in ("run_wall_ms", "model_calls_real", "prefix_bytes_median",
                   "prefix_bytes_min", "prefix_turns", "response_chars_total"):
        assert summary["numeric"][metric]["n_observed"] == 5, metric
    assert summary["boolean"]["attempts_observed"] == {
        "n_observed": 5, "n_missing": 0, "n_true": 5}
    assert summary["categorical"]["optimization"] == {"off": 5}
    # 跨 run 求和:五条 run 各 9600 字节。
    assert summary["counters"]["context_chars"]["bytes_total"] == 48000


def test_prefix_delta_columns_land_in_their_own_tables(tmp_path, capsys):
    """`prefix_delta` 专属两列各落进各自那张表:`context_rebuilds` 是数值
    (有 mean/p50/p95),`context_fallback` 是布尔(n_true/n_observed)——不能
    只靠「两者都在某个元组里」的并集守卫钉住(评审 P2-1)。

    变异:把 `context_fallback` 从 `BOOLEAN_METRICS` 挪进 `NUMERIC_METRICS`
    (或反过来把 `context_rebuilds` 挪进 `CATEGORICAL_METRICS`)⇒ 这条红——
    并集守卫(`test_every_scalar_projection_key_lands_somewhere` 那一类)看不见
    这个移动,但这条用例会:该列从原来那张表里消失,`summary["boolean"]` 或
    `summary["numeric"]` 直接 `KeyError`,或者(挪进 numeric 后)`_numeric`
    把 bool 值全滤掉,报出「5 条一次都没量到回退」这种假话,而实际有 2 条。
    """
    source = _write_rows(tmp_path / "rows.jsonl", [
        _measured_row(optimization="prefix_delta", context_rebuilds=0,
                      context_fallback=False),
        _measured_row(optimization="prefix_delta", context_rebuilds=1,
                      context_fallback=False),
        _measured_row(optimization="prefix_delta", context_rebuilds=2,
                      context_fallback=True),
        _measured_row(optimization="prefix_delta", context_rebuilds=0,
                      context_fallback=False),
        _measured_row(optimization="prefix_delta", context_rebuilds=1,
                      context_fallback=True),
    ])
    js = tmp_path / "t0.json"
    analyze.main([str(source), "--group-by", "optimization", "--out-json",
                  str(js)])
    capsys.readouterr()
    summary = json.loads(js.read_text("utf-8"))["groups"][0]["summary"]
    numeric = summary["numeric"]["context_rebuilds"]
    assert numeric["n_observed"] == 5
    assert {"mean", "p50", "p95"} <= numeric.keys()
    assert summary["boolean"]["context_fallback"] == {
        "n_observed": 5, "n_missing": 0, "n_true": 2}


def test_optimization_is_a_default_grouping_dimension(tmp_path, capsys):
    """不按 `optimization` 分格,两臂会被摊进同一格的均值里。

    变异:把 `optimization` 从 `DEFAULT_GROUP_BY` 里删掉 ⇒ 只剩一格,这条红。
    """
    source = _write_rows(tmp_path / "rows.jsonl", [
        _measured_row(run_wall_ms=10000),
        _measured_row(optimization="prefix_snapshot", run_wall_ms=6000),
    ])
    js = tmp_path / "t0.json"
    analyze.main([str(source), "--out-json", str(js)])
    capsys.readouterr()
    groups = json.loads(js.read_text("utf-8"))["groups"]
    assert len(groups) == 2
    walls = sorted(
        group["summary"]["numeric"]["run_wall_ms"]["mean"] for group in groups
    )
    assert walls == [6000.0, 10000.0]
    assert "optimization" in analyze.DEFAULT_GROUP_BY


def test_unknown_measurements_are_missing_not_zero(tmp_path, capsys):
    """没开测量的 run 在新列上是 `n_missing`,不是 0。

    变异:把投影侧任何一个新列的缺失折成 0 ⇒ 这条红。
    """
    source = _write_rows(tmp_path / "rows.jsonl", [
        _measured_row(),
        _row(run_wall_ms=None, model_calls_real=None, attempts_observed=None,
             prefix_turns=None),
    ])
    js = tmp_path / "t0.json"
    analyze.main([str(source), "--group-by", "policy_version", "--out-json",
                  str(js)])
    capsys.readouterr()
    report = json.loads(js.read_text("utf-8"))
    merged = {}
    for group in report["groups"]:
        for metric, entry in group["summary"]["numeric"].items():
            bucket = merged.setdefault(metric, {"n_observed": 0, "n_missing": 0})
            bucket["n_observed"] += entry["n_observed"]
            bucket["n_missing"] += entry["n_missing"]
    assert merged["run_wall_ms"] == {"n_observed": 1, "n_missing": 1}
    assert merged["prefix_turns"] == {"n_observed": 1, "n_missing": 1}


def test_optimization_pairs_hold_the_policy_version_fixed(tmp_path, capsys):
    """沿 `optimization` 配对时 `policy_version` 固定:拿 legacy 的 `off` 去比
    v2 的 `prefix_snapshot`,差值里混着两处改动,谁也归因不了。

    变异:把 `optimization_pair_table` 的格依据里的 `policy_version` 去掉 ⇒
    第二段的 `pairs == []` 断言红。
    """
    js = tmp_path / "t0.json"
    same_policy = _write_rows(tmp_path / "a.jsonl", [
        _measured_row(run_wall_ms=10000, model_calls_real=4),
        _measured_row(optimization="prefix_snapshot", run_wall_ms=6000,
                      model_calls_real=4, prefix_bytes_median=3134),
    ])
    analyze.main([str(same_policy), "--out-json", str(js)])
    capsys.readouterr()
    pairs = json.loads(js.read_text("utf-8"))["optimization_pairs"]
    assert len(pairs) == 1
    assert pairs[0]["arm_dimension"] == "optimization"
    assert pairs[0]["policy_version"] == "v2"
    assert pairs[0]["variant_arm"] == "prefix_snapshot"
    # 两侧键名对称:`baseline`/`variant` 说的是「哪一侧」,臂身份另有
    # `variant_arm` 与侧内的 `optimization` 分布。基线臂名不当键名——那会让读的人
    # 先知道基线是谁才取得到那一格。
    assert pairs[0]["baseline"]["optimization"] == {"off": 1}
    assert "off" not in pairs[0]
    assert pairs[0]["baseline"]["run_wall_ms"] == 10000.0
    assert pairs[0]["variant"]["run_wall_ms"] == 6000.0
    assert pairs[0]["variant"]["prefix_bytes_median"] == 3134.0

    cross_policy = _write_rows(tmp_path / "b.jsonl", [
        _row(optimization="off"),
        _measured_row(optimization="prefix_snapshot"),
    ])
    analyze.main([str(cross_policy), "--out-json", str(js)])
    capsys.readouterr()
    assert json.loads(js.read_text("utf-8"))["optimization_pairs"] == []


def test_optimization_pairs_need_the_off_baseline_and_a_declared_arm(
    tmp_path, capsys,
):
    """`unknown` 不当臂,基线必须是 `off`。

    一条没声明 optimization 的 run 在这条轴上没有身份;拿它当一臂等于给差值找了
    个不知道是什么的对照面。

    变异:把 `optimization_pair_table` 里跳过 `unknown` 的那两行删掉 ⇒ 第一段红
    (`unknown` 会被当成一个变体,和 `off` 配成一对)。
    """
    js = tmp_path / "t0.json"
    undeclared = _write_rows(tmp_path / "a.jsonl", [
        _measured_row(optimization="off"),
        _measured_row(optimization="unknown"),
    ])
    analyze.main([str(undeclared), "--out-json", str(js)])
    capsys.readouterr()
    assert json.loads(js.read_text("utf-8"))["optimization_pairs"] == []

    # 两个变体、没有 `off`:一对都配不出来(不许拿其中一个变体当基线)。
    no_baseline = _write_rows(tmp_path / "b.jsonl", [
        _measured_row(optimization="prefix_snapshot"),
        _measured_row(optimization="prefix_delta"),
    ])
    analyze.main([str(no_baseline), "--out-json", str(js)])
    capsys.readouterr()
    assert json.loads(js.read_text("utf-8"))["optimization_pairs"] == []

    # 同一格里两个变体 + `off`:两行,各自与 `off` 比。
    both = _write_rows(tmp_path / "c.jsonl", [
        _measured_row(),
        _measured_row(optimization="prefix_snapshot"),
        _measured_row(optimization="prefix_delta"),
    ])
    analyze.main([str(both), "--out-json", str(js)])
    capsys.readouterr()
    pairs = json.loads(js.read_text("utf-8"))["optimization_pairs"]
    assert [pair["variant_arm"] for pair in pairs] == [
        "prefix_delta", "prefix_snapshot"]


def test_policy_pairs_split_the_v2_side_by_optimization(tmp_path, capsys):
    """legacy vs v2 **不**要求两侧 `optimization` 相同——`optimization` 对
    legacy 结构上不成立(v2 总闸关时恒 `off`),要求相同就永远配不出
    「legacy vs v2+prefix_snapshot」这一对,而那正是最终要看的对照。

    但 v2 那一侧必须按 `optimization` **拆行**(legacy 侧整批重复):同题同格里
    一条 v2/`off`(10000ms)与一条 v2/`prefix_snapshot`(6000ms)摊进同一个均值
    是 8000ms,那个数不描述任何一臂。

    变异:把 `pair_table` 改回单行(v2 侧不拆、`_pair_side(sides["v2"])` 一次
    汇总)⇒ `len(pairs) == 2` 与两条 `run_wall_ms` 断言一起红;给格依据加上
    `optimization` ⇒ legacy 侧那格里没有变体、一对都配不出来,第一条也红。
    """
    js = tmp_path / "t0.json"
    source = _write_rows(tmp_path / "a.jsonl", [
        _row(optimization="off"),
        _measured_row(optimization="prefix_snapshot", run_wall_ms=6000),
        _measured_row(optimization="off", run_wall_ms=10000),
    ])
    analyze.main([str(source), "--out-json", str(js)])
    capsys.readouterr()
    pairs = json.loads(js.read_text("utf-8"))["pairs"]
    assert len(pairs) == 2
    assert [pair["v2"]["optimization"] for pair in pairs] == [
        {"off": 1}, {"prefix_snapshot": 1}]
    assert [pair["v2"]["run_wall_ms"] for pair in pairs] == [10000.0, 6000.0]
    assert [pair["v2"]["n_runs"] for pair in pairs] == [1, 1]
    # legacy 侧不拆:同一批 run 的同一份汇总,在两行里重复。
    assert pairs[0]["arm_dimension"] == "policy_version"
    assert pairs[0]["legacy"] == pairs[1]["legacy"]
    assert pairs[0]["legacy"]["optimization"] == {"off": 1}


#: 一批**没有任何 optimization 声明**的 run(= 线上导出)配出来的 `pairs` 行。
#: 手写而不是从代码里算:v2 侧按 optimization 拆行这件事,对这批数据必须逐格
#: 无差别——只有一个臂(`unknown`),行数与拆行前相同,均值也相同。
FROZEN_ANONYMOUS_PAIR = {
    "arm_dimension": "policy_version",
    "consumer": "ask_single",
    "corpus_cell": "B_kg",
    "effort": "standard",
    "has_intent_contract": "unknown",
    "mode": "reasoning",
    "question_key": "B-q01",
    "trace_source": "unknown",
    "legacy": {
        "anchors": None,
        "assessment_rows_total": None,
        "context_rebuilds": None,
        "model_calls_real": None,
        "n_measured": {"assessment_rows_total": 0, "context_rebuilds": 0,
                       "model_calls_real": 0, "prefix_bytes_median": 0,
                       "prefix_turns": 0, "run_wall_ms": 0},
        "n_runs": 1,
        "optimization": {"unknown": 1},
        "prefix_bytes_median": None,
        "prefix_turns": None,
        "reflect_turns": 3.0,
        "run_wall_ms": None,
        "termination_reason": {"model_end": 1},
        "total_ms": 1000.0,
    },
    "v2": {
        "anchors": None,
        "assessment_rows_total": None,
        "context_rebuilds": None,
        "model_calls_real": None,
        "n_measured": {"assessment_rows_total": 0, "context_rebuilds": 0,
                       "model_calls_real": 0, "prefix_bytes_median": 0,
                       "prefix_turns": 0, "run_wall_ms": 0},
        "n_runs": 1,
        "optimization": {"unknown": 1},
        "prefix_bytes_median": None,
        "prefix_turns": None,
        "reflect_turns": 3.0,
        "run_wall_ms": None,
        "termination_reason": {"model_end": 1},
        "total_ms": 1000.0,
    },
}


def test_pairs_on_an_undeclared_batch_match_the_frozen_shape(tmp_path, capsys):
    """线上导出那批(`optimization` 全缺)的 `pairs` 逐键与冻结基线相同。

    这是「按 optimization 拆 v2 侧」这次改动的回归闸:那批数据在这条轴上只有
    一个臂,所以拆与不拆必须给出同一份输出——多出一行、多出一个键、或者哪个均值
    动了,都在这里当场红。

    变异:给 `pair_table` 的每行加一个 `entry["optimization"] = _arm` 字段(把臂
    写进 JSON 而不是从侧内分布读)⇒ 这条红。
    """
    js = tmp_path / "t0.json"
    source = _write_rows(tmp_path / "a.jsonl", [
        _row(), _row(policy_version="v2"),
    ])
    analyze.main([str(source), "--out-json", str(js)])
    capsys.readouterr()
    assert json.loads(js.read_text("utf-8"))["pairs"] == [FROZEN_ANONYMOUS_PAIR]


def test_a_sparse_pair_side_reports_its_observation_count(tmp_path, capsys):
    """稀疏指标的均值必须带着自己的 n:一侧三条全带、另一侧三条只一条带,两侧
    的均值在表里长得一样重,只有 n 能把「1 条比 3 条」揭出来(评审 P2)。

    `n_runs` 顶不了这件事——它数的是 run,不是观测数。

    变异:把 `_pair_side` 里的 `n_measured` 删掉 ⇒ 前两段红;把
    `_fmt_pair_metric` 换回直接印均值 ⇒ 最后那段 markdown 断言红。
    """
    js, md = tmp_path / "t0.json", tmp_path / "t0.md"
    source = _write_rows(tmp_path / "a.jsonl", [
        # `off` 三条都量到了墙钟,`prefix_snapshot` 三条里只有一条量到。
        *[_measured_row(run_wall_ms=9000) for _ in range(3)],
        _measured_row(optimization="prefix_snapshot", run_wall_ms=6000),
        _measured_row(optimization="prefix_snapshot", run_wall_ms=None),
        _measured_row(optimization="prefix_snapshot", run_wall_ms=None),
    ])
    analyze.main([str(source), "--out-json", str(js), "--out-md", str(md)])
    capsys.readouterr()
    pair = json.loads(js.read_text("utf-8"))["optimization_pairs"][0]
    assert pair["baseline"]["n_runs"] == pair["variant"]["n_runs"] == 3
    assert pair["baseline"]["run_wall_ms"] == 9000.0
    assert pair["variant"]["run_wall_ms"] == 6000.0
    # 均值一样重,n 不一样:三条观测 vs 一条观测。
    assert pair["baseline"]["n_measured"]["run_wall_ms"] == 3
    assert pair["variant"]["n_measured"]["run_wall_ms"] == 1
    # 稀疏五项各有自己的 n,不共用一个。
    assert set(pair["variant"]["n_measured"]) == set(
        analyze.SPARSE_PAIR_SIDE_METRICS)
    assert pair["variant"]["n_measured"]["prefix_bytes_median"] == 3
    # markdown 也带上 n,而不是只把它留在 JSON 里。
    rendered = md.read_text("utf-8")
    assert "9000.0(n=3)" in rendered
    assert "6000.0(n=1)" in rendered


def test_context_rebuilds_appears_in_the_optimization_pair_table(tmp_path, capsys):
    """`prefix_delta` 变体的重建次数进配对差值表:`variant` 臂印出真实的观测
    (均值+n),`off` 基线臂走既有稀疏列的缺值显示(评审 P3-2)——两侧都印同一列,
    而不是像 `prefix_bytes_median` 那样只印 variant 侧,好让「这件事对 `off`
    臂压根不成立」在表面上看得见。

    变异:把 `context_rebuilds` 从 `PAIR_SIDE_METRICS`/`SPARSE_PAIR_SIDE_METRICS`
    里删掉 ⇒ 这条红(JSON 侧该键消失或 n_measured 缺键;markdown 侧那两列跟着
    消失)。
    """
    source = _write_rows(tmp_path / "rows.jsonl", [
        _measured_row(optimization="off"),
        _measured_row(optimization="off"),
        _measured_row(optimization="prefix_delta", context_rebuilds=1,
                      context_fallback=False),
        _measured_row(optimization="prefix_delta", context_rebuilds=3,
                      context_fallback=True),
    ])
    js, md = tmp_path / "t0.json", tmp_path / "t0.md"
    analyze.main([str(source), "--out-json", str(js), "--out-md", str(md)])
    capsys.readouterr()
    pair = json.loads(js.read_text("utf-8"))["optimization_pairs"][0]
    assert pair["variant_arm"] == "prefix_delta"
    assert pair["variant"]["context_rebuilds"] == 2.0
    assert pair["variant"]["n_measured"]["context_rebuilds"] == 2
    # `off` 臂结构上不带这个观测:均值缺失、n=0,而不是 0 或被静默丢弃这一列。
    assert pair["baseline"]["context_rebuilds"] is None
    assert pair["baseline"]["n_measured"]["context_rebuilds"] == 0

    rendered = md.read_text("utf-8")
    assert "off context_rebuilds(n)" in rendered
    assert "variant context_rebuilds(n)" in rendered
    assert "2.0(n=2)" in rendered
    assert f"{analyze.UNKNOWN}(n=0)" in rendered


def test_assessment_rows_total_and_aspects_unassessed_reach_the_numeric_table(
    tmp_path, capsys,
):
    """T-PL2 的两个新列各自落进 `NUMERIC_METRICS`,真的出现在 JSON 报告里
    (呼应 `test_analysis_consumes_every_scalar_projection_key` 的反向并集
    守卫)。

    变异:把 `assessment_rows_total` 或 `aspects_unassessed` 从
    `NUMERIC_METRICS` 里删掉 ⇒ 这条红,且
    `test_analysis_consumes_every_scalar_projection_key` 也跟着红。
    """
    source = _write_rows(tmp_path / "rows.jsonl", [
        _measured_row(assessment_rows_total=3, aspects_unassessed=0),
        _measured_row(assessment_rows_total=1, aspects_unassessed=2),
    ])
    js = tmp_path / "t0.json"
    analyze.main([str(source), "--out-json", str(js)])
    capsys.readouterr()
    summary = json.loads(js.read_text("utf-8"))["groups"][0]["summary"]
    assert summary["numeric"]["assessment_rows_total"]["n_observed"] == 2
    assert summary["numeric"]["assessment_rows_total"]["mean"] == 2.0
    assert summary["numeric"]["aspects_unassessed"]["n_observed"] == 2
    assert summary["numeric"]["aspects_unassessed"]["mean"] == 1.0


def test_assessment_rows_total_appears_in_the_optimization_pair_table(
    tmp_path, capsys,
):
    """`assessment_rows_total`(T-PL2)四臂通写,配对差值表**两侧都是真实观测**
    ——与 `context_rebuilds` 那道"基线臂结构上没有"的分工不同(评审要点见计划
    §3 T-PL2):`off` 基线侧不是恒 `n_measured=0`,而是自己的均值+n。

    变异:把 `assessment_rows_total` 从 `PAIR_SIDE_METRICS`/
    `SPARSE_PAIR_SIDE_METRICS` 里删掉 ⇒ 这条红(JSON 侧该键消失或
    n_measured 缺键;markdown 侧那两列跟着消失)。
    """
    source = _write_rows(tmp_path / "rows.jsonl", [
        _measured_row(optimization="off", assessment_rows_total=4),
        _measured_row(optimization="off", assessment_rows_total=6),
        _measured_row(optimization="prefix_delta_lean",
                      assessment_rows_total=1),
        _measured_row(optimization="prefix_delta_lean",
                      assessment_rows_total=3),
    ])
    js, md = tmp_path / "t0.json", tmp_path / "t0.md"
    analyze.main([str(source), "--out-json", str(js), "--out-md", str(md)])
    capsys.readouterr()
    pair = json.loads(js.read_text("utf-8"))["optimization_pairs"][0]
    assert pair["variant_arm"] == "prefix_delta_lean"
    assert pair["variant"]["assessment_rows_total"] == 2.0
    assert pair["variant"]["n_measured"]["assessment_rows_total"] == 2
    # 四臂通写:`off` 基线侧同样是真实观测,不是恒缺席。
    assert pair["baseline"]["assessment_rows_total"] == 5.0
    assert pair["baseline"]["n_measured"]["assessment_rows_total"] == 2

    rendered = md.read_text("utf-8")
    assert "off assessment_rows_total(n)" in rendered
    assert "variant assessment_rows_total(n)" in rendered
    assert "5.0(n=2)" in rendered
    assert "2.0(n=2)" in rendered


def test_optimization_pair_table_cells_align_with_their_header(tmp_path, capsys):
    """8 个数值格必须落在**自己**的表头下面,不能靠子串猜(质量评审 P2-3)。

    上面那条用例只断言 `"5.0(n=2)" in rendered` 这类子串在整份渲染文本里出现
    过;子串挂在错列上同样能通过。这条用例给四个稀疏指标(`run_wall_ms` /
    `model_calls_real` / `context_rebuilds` / `assessment_rows_total`)各配一对
    互不相同的基线/变体均值,再按**表头文本**取出对应单元格逐一核对——8 个格子
    的值两两不同,任何一次错位都会让某个断言拿到别的格子的值。

    变异:把 `render_markdown` 里拼这 8 格的两层 `for`(`metric` 外层、`label`
    内层)对调成 `label` 外层、`metric` 内层(不删任何东西,单纯调序)⇒ 表头
    不变但单元格顺序变成"基线四项挨着、变体四项挨着",除了两侧首尾各一格,其
    余全部错位,这条红。
    """
    source = _write_rows(tmp_path / "rows.jsonl", [
        _measured_row(optimization="off", run_wall_ms=1000,
                      model_calls_real=10, context_rebuilds=100,
                      assessment_rows_total=1),
        _measured_row(optimization="off", run_wall_ms=2000,
                      model_calls_real=20, context_rebuilds=200,
                      assessment_rows_total=2),
        _measured_row(optimization="prefix_delta_lean", run_wall_ms=3000,
                      model_calls_real=30, context_rebuilds=300,
                      assessment_rows_total=3),
        _measured_row(optimization="prefix_delta_lean", run_wall_ms=4000,
                      model_calls_real=40, context_rebuilds=400,
                      assessment_rows_total=4),
    ])
    md = tmp_path / "t0.md"
    analyze.main([str(source), "--out-md", str(md)])
    capsys.readouterr()
    rendered = md.read_text("utf-8")
    section = rendered.split("## off / 优化变体对照(成对,同 policy_version)")[1]
    table_lines = [line for line in section.splitlines() if line.startswith("|")]
    header = [cell.strip() for cell in table_lines[0].strip("|").split("|")]
    body = [cell.strip() for cell in table_lines[2].strip("|").split("|")]
    cells = dict(zip(header, body))
    assert len(cells) == len(header), "表头有重复列名,取索引前先看一眼常量"
    assert cells["off run_wall_ms(n)"] == "1500.0(n=2)"
    assert cells["variant run_wall_ms(n)"] == "3500.0(n=2)"
    assert cells["off model_calls_real(n)"] == "15.0(n=2)"
    assert cells["variant model_calls_real(n)"] == "35.0(n=2)"
    assert cells["off context_rebuilds(n)"] == "150.0(n=2)"
    assert cells["variant context_rebuilds(n)"] == "350.0(n=2)"
    assert cells["off assessment_rows_total(n)"] == "1.5(n=2)"
    assert cells["variant assessment_rows_total(n)"] == "3.5(n=2)"


def test_the_markdown_report_renders_both_arm_axes(tmp_path, capsys):
    """两张成对表都在,且 legacy/v2 那张把 v2 侧的臂写在自己的一列里。

    变异:把 `v2 optimization` 列从 `render_markdown` 里删掉 ⇒ 表头断言红;把
    `_pair_arm` 改成恒返回第一个键(而不是把多臂拼出来)⇒
    `test_pair_arm_labels_a_mixed_side_as_mixed` 红。
    """
    source = _write_rows(tmp_path / "rows.jsonl", [
        _measured_row(),
        _measured_row(optimization="prefix_snapshot"),
        _row(optimization="off"),
    ])
    md = tmp_path / "t0.md"
    analyze.main([str(source), "--out-md", str(md)])
    capsys.readouterr()
    rendered = md.read_text("utf-8")
    assert "## legacy / v2 对照(成对)" in rendered
    assert "## off / 优化变体对照(成对,同 policy_version)" in rendered
    assert "prefix_snapshot" in rendered
    assert "v2 optimization" in rendered
    # 拆行后 legacy/v2 那张表是两行,各自的 v2 臂写在那一列里。
    section = rendered.split("## legacy / v2 对照(成对)")[1].split("## off /")[0]
    pair_rows = [line for line in section.splitlines()
                 if line.startswith("| B-q01 |")]
    assert len(pair_rows) == 2
    assert sum("prefix_snapshot" in line for line in pair_rows) == 1


def test_pair_arm_labels_a_mixed_side_as_mixed():
    """一侧混了两臂时,标签必须把两个都写出来,不许挑一个盖住混合。"""
    assert analyze._pair_arm({"optimization": {"off": 2}}) == "off"
    assert analyze._pair_arm(
        {"optimization": {"off": 1, "prefix_snapshot": 1}}
    ) == "off+prefix_snapshot"
    assert analyze._pair_arm({}) == "unknown"


# --- T-EX9 `analyze` 的三处读出口子 ------------------------------------------
#
# `--baseline-arm` / `--pair-rows` / `--key-set` 三个开关的默认值逐字保持接入前
# 的行为(计划 §2 Q8),所以这一族的第一条是**默认输出等价**,其余每一条都要显式
# 打开对应开关。

#: T-EX9 的基线修订。默认输出的并列比对拿它当「接入前」那一版。
#:
#: 它是一个**可能被 rebase 改写**的 SHA:PR 合入走 `--rebase`,合入后这个 SHA 在
#: master 上不再可达,那条并列比对用例会自动 skip(见
#: `test_default_analysis_output_matches_the_base_revision`)。所以形状那一半由
#: 不依赖 git 的 `test_the_default_report_shape_is_frozen` 长期守着。
BASE_REVISION = "a61e81bb5"

#: 被比对的那个脚本在仓库里的路径,`git show` 用。
ANALYZE_REL_PATH = "scripts/analyze_reasoning_trace.py"

#: 默认参数下报告 JSON 的**全部**顶层键。手写而不是从代码里算——这一族新增的两
#: 个键(`quality_evidence` / `optimization_pair_rows`)都是**条件出现**的,而
#: 「默认参数下一个键都不多」正是那条形状合同本身。
FROZEN_DEFAULT_REPORT_KEYS = {
    "n_rows", "group_by", "min_samples", "groups", "pairs",
    "optimization_pairs",
}

#: 默认参数下 markdown 的**全部**一/二级标题,按出现顺序。
FROZEN_DEFAULT_SECTIONS = [
    "# reflect T0 轨迹聚合",
    "## 分组概览",
    "## legacy / v2 对照(成对)",
    "## off / 优化变体对照(成对,同 policy_version)",
]

#: 默认输出的 golden:一份冻结的输入行 + 它逐字节的 md 与 json 产物。
#:
#: `BASE_REVISION` 那条并列比对在 CI 上**必然 skip**(`actions/checkout` 默认
#: `fetch-depth=1`,那个 SHA 不可达),合入后本机也永久 skip;而
#: `FROZEN_DEFAULT_REPORT_KEYS` / `FROZEN_DEFAULT_SECTIONS` 只盯顶层键与标题,
#: 盯不住**数值与舍入**。这三份 fixture 是那颗守卫在 CI 上的替身(质量评审 P3-3)。
GOLDEN_DIR = Path(__file__).resolve().parent / "fixtures"
GOLDEN_INPUT = GOLDEN_DIR / "reflect_t0_analysis_golden.jsonl"
GOLDEN_MD = GOLDEN_DIR / "reflect_t0_analysis_golden.md"
GOLDEN_JSON = GOLDEN_DIR / "reflect_t0_analysis_golden.json"


def _ab_row(**overrides) -> dict:
    """一行 `ab-runs.jsonl` 形状:T0 测量行 + A/B 专属键(§7.1)。

    判分/人工那几列默认 `None` —— T-AB1/T-AB3 一格未动(计划 M6),所以今天真跑
    出来的 A/B 行就是这个样子,质量门那一节的默认输入也是它。
    """
    base = _measured_row(
        paired=True, arm="v2:off", repeat=1, answer_chars=1200, citations=3,
        gold_facts_total=None, gold_facts_hit=None,
        human_factual_error=None, human_completeness_false=None,
        human_citation_bad=None,
    )
    base.update(overrides)
    return base


def _pair_row_fixture(question: str, arm: str, **overrides) -> dict:
    """逐题配对用例的一行:v2 臂 + 一个显式 `run_wall_ms`。"""
    return _measured_row(question_key=question, optimization=arm, **overrides)


def _pair_row_cells(md: Path) -> dict[str, str]:
    """逐题配对表**第一行**的「表头 → 单元格」映射。

    按表头文本取格,不用子串:那张表的分格列上到处都是 `unknown`,而
    `"unknown" in rendered` 这种断言挂在错列上同样能通过。
    """
    section = md.read_text("utf-8").split("## 逐题配对差值")[1]
    table_lines = [line for line in section.splitlines()
                   if line.startswith("|")]
    header = [cell.strip() for cell in table_lines[0].strip("|").split("|")]
    body = [cell.strip() for cell in table_lines[2].strip("|").split("|")]
    assert len(set(header)) == len(header), "表头有重复列名,取索引前先看常量"
    return dict(zip(header, body))


def test_default_analysis_output_matches_the_base_revision(tmp_path, capsys):
    """同一份行喂进新旧两版 `main`,JSON 与 markdown **逐字节**相同(计划 §3
    T-EX9 验收第一条)。

    这是三个新开关「默认值逐字保持今天行为」的直接判据:既有那一百多条用例盯的
    是各自那一格,而这一条盯的是整份产物——多一个 JSON 键、多一行 markdown、
    或者哪个数被顺手 round 了一位,都在这里当场红。

    `BASE_REVISION` 被 rebase 改写之后这条 skip(那时它守的那次改动早已合入,
    形状那一半由 `test_the_default_report_shape_is_frozen` 接着守)。

    变异:把 `build_report` 里 `quality_evidence` 的条件去掉(改成无条件写)⇒
    这条红。
    """
    probe = subprocess.run(
        ["git", "cat-file", "-e", f"{BASE_REVISION}:{ANALYZE_REL_PATH}"],
        cwd=ROOT, capture_output=True,
    )
    if probe.returncode != 0:
        pytest.skip(f"基线修订 {BASE_REVISION} 不可达(已被 rebase 改写)")
    dumped = subprocess.run(
        ["git", "show", f"{BASE_REVISION}:{ANALYZE_REL_PATH}"],
        cwd=ROOT, capture_output=True, check=True,
    )
    previous_path = tmp_path / "analyze_previous.py"
    previous_path.write_bytes(dumped.stdout)
    spec = importlib.util.spec_from_file_location(
        "analyze_reasoning_trace_previous", previous_path)
    previous = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(previous)

    source = _write_rows(tmp_path / "rows.jsonl", [
        _row(), _row(policy_version="v2"),
        _measured_row(),
        _measured_row(optimization="prefix_snapshot", run_wall_ms=6000),
        _measured_row(optimization="prefix_delta", context_rebuilds=1,
                      context_fallback=True),
        _measured_row(optimization="prefix_delta_lean",
                      assessment_rows_total=1),
    ])
    rendered = {}
    for tag, module in (("previous", previous), ("current", analyze)):
        md, js = tmp_path / f"{tag}.md", tmp_path / f"{tag}.json"
        assert module.main([str(source), "--out-md", str(md),
                            "--out-json", str(js)]) == 0
        rendered[tag] = (md.read_bytes(), js.read_bytes())
    capsys.readouterr()
    assert rendered["current"][0] == rendered["previous"][0]
    assert rendered["current"][1] == rendered["previous"][1]


def test_the_default_report_shape_is_frozen(tmp_path, capsys):
    """默认参数下报告的顶层键与 markdown 的节标题都与冻结基线逐项相同。

    两个新读出口子都是**条件出现**的(`build_report`):不给 `--pair-rows` 就
    没有 `optimization_pair_rows`,`--key-set t0` 就没有 `quality_evidence`
    ——T0 键集结构上没有 `gold_facts_total`,对一份压根没有质量列的数据集印一句
    质量结论本身就是假话。

    变异:把这两个键改成无条件写入 ⇒ 这条红(且上面那条并列比对也红)。
    """
    source = _write_rows(tmp_path / "rows.jsonl", [
        _row(), _measured_row(),
        _measured_row(optimization="prefix_snapshot"),
    ])
    md, js = tmp_path / "t0.md", tmp_path / "t0.json"
    analyze.main([str(source), "--out-md", str(md), "--out-json", str(js)])
    capsys.readouterr()
    assert set(json.loads(js.read_text("utf-8"))) == FROZEN_DEFAULT_REPORT_KEYS
    rendered = md.read_text("utf-8")
    assert [line for line in rendered.splitlines()
            if line.startswith("# ") or line.startswith("## ")] == (
        FROZEN_DEFAULT_SECTIONS)


def test_the_default_report_matches_the_golden_fixture(tmp_path, capsys):
    """默认参数下的 md 与 json 与冻结的 golden **逐字节**相同——包括每一个数与它
    的舍入位数(质量评审 P3-3)。

    这一条与 `test_default_analysis_output_matches_the_base_revision` 的分工:
    那条比的是「新旧两版脚本」,只在 `BASE_REVISION` 可达时才跑(CI 的
    `fetch-depth=1` 下必然 skip,合入后永久 skip);这一条比的是「脚本与一份存档
    产物」,不依赖 git,所以它在 CI 上真的跑。

    golden 的输入刻意让几个均值落在**非整**上(`total_ms` 六条 1000/1001/1003/
    1007/1015/1017 ⇒ 均值 1007.167、P50 1003、P95 1017;legacy 侧三条 ⇒
    1001.333),于是「把某一处 `round(…, 3)` 改成 `round(…, 2)`」这种改动在这里
    当场红,而顶层键与节标题那两颗守卫对它一无所知。

    产物变了而且是**有意**变的时候,重新签一次:

        python3 scripts/analyze_reasoning_trace.py \\
            backend/tests/fixtures/reflect_t0_analysis_golden.jsonl \\
            --out-md backend/tests/fixtures/reflect_t0_analysis_golden.md \\
            --out-json backend/tests/fixtures/reflect_t0_analysis_golden.json

    ——重签是一个要在 PR 里说明理由的动作,不是让这条用例变绿的手段。
    """
    md, js = tmp_path / "golden.md", tmp_path / "golden.json"
    assert analyze.main([str(GOLDEN_INPUT), "--out-md", str(md),
                         "--out-json", str(js)]) == 0
    capsys.readouterr()
    assert md.read_bytes() == GOLDEN_MD.read_bytes()
    assert js.read_bytes() == GOLDEN_JSON.read_bytes()


def test_baseline_arm_pairs_the_delta_and_lean_arms(tmp_path, capsys):
    """只有 D 与 L 两臂的一批:默认基线 `off` 一对都配不出来,
    `--baseline-arm prefix_delta` 配得出来,且基线那一侧**如实报自己的
    `optimization`**(计划 M5 缺件 1、§10.2-4 那条采用门的读处)。

    变异:把 `optimization_pair_table` 的 `baseline` 参数改回读模块级
    `OPTIMIZATION_BASELINE` ⇒ 第二段红(仍然配不出来)。把 markdown 的节标题/
    列名改回硬编码 `off` ⇒ 最后两条红(一份 D 基线的报告顶着 `off` 的表头)。
    """
    source = _write_rows(tmp_path / "rows.jsonl", [
        _measured_row(optimization="prefix_delta", run_wall_ms=9000),
        _measured_row(optimization="prefix_delta_lean", run_wall_ms=7000),
    ])
    js, md = tmp_path / "t0.json", tmp_path / "t0.md"

    analyze.main([str(source), "--out-json", str(js)])
    capsys.readouterr()
    assert json.loads(js.read_text("utf-8"))["optimization_pairs"] == []

    analyze.main([str(source), "--baseline-arm", "prefix_delta",
                  "--out-json", str(js), "--out-md", str(md)])
    capsys.readouterr()
    pairs = json.loads(js.read_text("utf-8"))["optimization_pairs"]
    assert len(pairs) == 1
    assert pairs[0]["variant_arm"] == "prefix_delta_lean"
    # 基线侧自证身份:JSON 里没有「我用的基线是谁」这个字段,读的人从这一格取。
    assert pairs[0]["baseline"]["optimization"] == {"prefix_delta": 1}
    assert pairs[0]["variant"]["optimization"] == {"prefix_delta_lean": 1}
    assert pairs[0]["baseline"]["run_wall_ms"] == 9000.0
    assert pairs[0]["variant"]["run_wall_ms"] == 7000.0

    rendered = md.read_text("utf-8")
    assert "## prefix_delta / 优化变体对照(成对,同 policy_version)" in rendered
    assert "prefix_delta run_wall_ms(n)" in rendered
    assert "off run_wall_ms(n)" not in rendered


def test_an_undeclared_baseline_arm_is_refused_at_the_command_line(tmp_path):
    """`--baseline-arm unknown` 在命令行这一层就被挡住:`unknown` 不当臂。

    变异:把 `--baseline-arm` 的 `choices` 去掉 ⇒ 这条红(`unknown` 会被当成
    一个合法基线,而那一格里的 run 在这条轴上压根没有身份)。
    """
    source = _write_rows(tmp_path / "rows.jsonl", [_measured_row()])
    with pytest.raises(SystemExit):
        analyze.main([str(source), "--baseline-arm", "unknown"])
    with pytest.raises(SystemExit):
        analyze.main([str(source), "--baseline-arm", "prefix_dleta"])


def test_pair_rows_take_the_median_over_repeats_before_comparing(
    tmp_path, capsys,
):
    """逐题配对:**格内先对重复取中位数**,再出 `Δms` 与 `ratio`(§10.1)。

    基线那一格的三次重复是 1000/1000/7000 —— 中位数 1000,均值 3000。变体三次
    都是 2000。所以配对差是 +1000、比值 2.0;若格内改用均值,两个数会变成
    -1000 与 0.667,**方向相反**。

    变异:把 `_pair_row_side` 里的 `_nearest(values, 0.50)` 换成 `_mean`
    (即直接复用 `_pair_side` 的汇总)⇒ 这条红。
    """
    source = _write_rows(tmp_path / "rows.jsonl", [
        _pair_row_fixture("B-q01", "off", run_wall_ms=1000),
        _pair_row_fixture("B-q01", "off", run_wall_ms=1000),
        _pair_row_fixture("B-q01", "off", run_wall_ms=7000),
        *[_pair_row_fixture("B-q01", "prefix_delta_lean", run_wall_ms=2000)
          for _ in range(3)],
    ])
    js = tmp_path / "t0.json"
    analyze.main([str(source), "--pair-rows", "--out-json", str(js)])
    capsys.readouterr()
    payload = json.loads(js.read_text("utf-8"))["optimization_pair_rows"]
    assert len(payload["cells"]) == 1
    cell = payload["cells"][0]
    assert cell["variant_arm"] == "prefix_delta_lean"
    assert cell["baseline"]["run_wall_ms"] == 1000.0
    assert cell["baseline"]["n_measured"]["run_wall_ms"] == 3
    assert cell["variant"]["run_wall_ms"] == 2000.0
    assert cell["metrics"]["run_wall_ms"] == {"delta_ms": 1000.0, "ratio": 2.0}
    # 同一格的三次重复只贡献**一个**配对观测,不是三个独立观测。
    assert payload["rollup"][0]["metrics"]["run_wall_ms"]["n_pairs"] == 1
    assert payload["rollup"][0]["n_cells"] == 1


def test_pair_rows_ratio_is_paired_not_two_independent_p50s(tmp_path, capsys):
    """rollup 的 `ratio p50` / `Δms p50` 是**逐格配对**观测的分位数,不是两组
    独立 P50 之比 / 之差(§10.1 逐字点名)。

    三格配对:(1000→4000)、(2000→6000)、(8000→4000)。
    配对比值 [4.0, 3.0, 0.5] ⇒ p50 = 3.0;配对差 [3000, 4000, -4000] ⇒
    p50 = 3000。
    两组独立 P50 分别是 variant 4000、baseline 2000 ⇒ 比值 2.0、差值 2000
    —— 两个数都不同,所以这条用例把两种算法分得开。

    变异:把 `_rollup_metric` 换成「先各自取两侧中位数、再相除/相减」
    (`median(cand)/median(base)`)⇒ 这条红。
    """
    source = _write_rows(tmp_path / "rows.jsonl", [
        _pair_row_fixture("B-q01", "off", run_wall_ms=1000),
        _pair_row_fixture("B-q01", "prefix_delta_lean", run_wall_ms=4000),
        _pair_row_fixture("B-q02", "off", run_wall_ms=2000),
        _pair_row_fixture("B-q02", "prefix_delta_lean", run_wall_ms=6000),
        _pair_row_fixture("B-q03", "off", run_wall_ms=8000),
        _pair_row_fixture("B-q03", "prefix_delta_lean", run_wall_ms=4000),
    ])
    js = tmp_path / "t0.json"
    analyze.main([str(source), "--pair-rows", "--min-samples", "3",
                  "--out-json", str(js)])
    capsys.readouterr()
    payload = json.loads(js.read_text("utf-8"))["optimization_pair_rows"]
    ratios = sorted(cell["metrics"]["run_wall_ms"]["ratio"]
                    for cell in payload["cells"])
    assert ratios == [0.5, 3.0, 4.0]
    stat = payload["rollup"][0]["metrics"]["run_wall_ms"]
    assert stat["n_pairs"] == 3
    assert stat["ratio_p50"] == 3.0
    assert stat["delta_ms_p50"] == 3000.0
    # 两组独立 P50 会给出这两个数,它们不该出现在这一格里。
    assert stat["ratio_p50"] != 2.0
    assert stat["delta_ms_p50"] != 2000.0


def test_cancelled_runs_are_censored_and_never_enter_the_median(
    tmp_path, capsys,
):
    """`cancelled` 落**删失**一列、`failed` 落失败一列,两者都不进中位数
    (§10.1:不把超时值说成真实完成耗时,也不把失败丢掉后只比较成功者)。

    变体那一格里跑成的只有 5000 一条,另两条分别带着 99999(取消)与
    88888(失败)——它们要是进了中位数,那一格会变成 88888。

    变异:把 `_pair_row_side` 里筛 `success` 的那两行删掉 ⇒ 这条红。
    """
    source = _write_rows(tmp_path / "rows.jsonl", [
        _pair_row_fixture("B-q01", "off", run_wall_ms=4000),
        _pair_row_fixture("B-q01", "prefix_delta_lean", run_wall_ms=5000),
        _pair_row_fixture("B-q01", "prefix_delta_lean", run_wall_ms=99999,
                          status="cancelled"),
        _pair_row_fixture("B-q01", "prefix_delta_lean", run_wall_ms=88888,
                          status="failed"),
    ])
    js = tmp_path / "t0.json"
    analyze.main([str(source), "--pair-rows", "--out-json", str(js)])
    capsys.readouterr()
    payload = json.loads(js.read_text("utf-8"))["optimization_pair_rows"]
    variant = payload["cells"][0]["variant"]
    assert variant["n_runs"] == 3
    assert variant["n_success"] == 1
    assert variant["n_censored"] == 1
    assert variant["n_failed"] == 1
    assert variant["run_wall_ms"] == 5000.0
    assert variant["n_measured"]["run_wall_ms"] == 1
    assert payload["cells"][0]["metrics"]["run_wall_ms"]["delta_ms"] == 1000.0
    # 删失与失败在 rollup 上也分开,不合成一个「异常数」;而且**分两侧**印,
    # 好让「哪一侧更容易崩」读得出来(质量评审 P3-1)。
    rollup = payload["rollup"][0]
    assert rollup["n_censored_baseline"] == 0
    assert rollup["n_censored_variant"] == 1
    assert rollup["n_failed_baseline"] == 0
    assert rollup["n_failed_variant"] == 1
    assert "n_censored" not in rollup


def test_only_done_runs_feed_the_paired_median(tmp_path, capsys):
    """成功是**白名单**(`status == "done"`):`running` 与任何未知状态进
    `n_unfinished`,不进中位数(规格评审 P2-1)。

    变体那一格里跑成的只有 5000 一条,另两条是一条 `running`(`total_ms=50`,
    已跑步骤的部分和)与一条状态压根没写的行。它们要是被当成成功读走,那一格的
    `run_wall_ms` 会变成 50 —— 一条没跑完的 run 被读成 100× 提速,而删失/失败两列
    还都是 0。

    四格计数**可加**:`n_runs == n_success + n_censored + n_failed +
    n_unfinished`。

    变异:把 `_pair_row_side` 的白名单改回黑名单(`not in (CENSORED_STATUS,
    FAILED_STATUS)`)⇒ 这条红(中位数变 50、`n_unfinished` 变 0)。
    """
    source = _write_rows(tmp_path / "rows.jsonl", [
        _pair_row_fixture("B-q01", "off", run_wall_ms=4000, total_ms=400),
        _pair_row_fixture("B-q01", "prefix_delta_lean", run_wall_ms=5000),
        _pair_row_fixture("B-q01", "prefix_delta_lean", run_wall_ms=50,
                          total_ms=50, status="running"),
        _pair_row_fixture("B-q01", "prefix_delta_lean", run_wall_ms=60,
                          total_ms=60, status=None),
    ])
    js, md = tmp_path / "t0.json", tmp_path / "t0.md"
    analyze.main([str(source), "--pair-rows", "--out-json", str(js),
                  "--out-md", str(md)])
    capsys.readouterr()
    payload = json.loads(js.read_text("utf-8"))["optimization_pair_rows"]
    variant = payload["cells"][0]["variant"]
    assert variant["n_runs"] == 3
    assert variant["n_success"] == 1
    assert variant["n_censored"] == 0
    assert variant["n_failed"] == 0
    assert variant["n_unfinished"] == 2
    assert (variant["n_success"] + variant["n_censored"]
            + variant["n_failed"] + variant["n_unfinished"]
            == variant["n_runs"])
    # 那条 `running` 的部分和不进中位数,也不进观测数。
    assert variant["run_wall_ms"] == 5000.0
    assert variant["n_measured"]["run_wall_ms"] == 1
    assert payload["rollup"][0]["n_unfinished_variant"] == 2
    # 未完成那一格在 markdown 上也看得见:逐格表本来不印 `n_runs`,少了这一列,
    # 三条 run 里两条没跑完这件事在最容易被引用的那份输出上就是隐形的。
    assert _pair_row_cells(md)["n_unfinished(基线/变体)"] == "0/2"


def test_pair_rows_withhold_quantiles_below_min_samples(tmp_path, capsys):
    """`n_pairs < --min-samples` 时不出分位数,但**逐题差值照出**(§10.1:小样本
    展示逐题差值、中位数和最大值)。

    `*_max` 不受那道门约束——最大值不是分位数,而「尾部有没有可重复的恶化」这个
    问题在小样本上照样要答。

    变异:把 `_rollup_metric` 里那道 `len(deltas) >= min_samples` 去掉 ⇒ 这条
    红(两条 `not in` 断言)。
    """
    source = _write_rows(tmp_path / "rows.jsonl", [
        _pair_row_fixture("B-q01", "off", run_wall_ms=1000),
        _pair_row_fixture("B-q01", "prefix_delta_lean", run_wall_ms=3000),
        _pair_row_fixture("B-q02", "off", run_wall_ms=1000),
        _pair_row_fixture("B-q02", "prefix_delta_lean", run_wall_ms=2000),
    ])
    js = tmp_path / "t0.json"
    analyze.main([str(source), "--pair-rows", "--min-samples", "5",
                  "--out-json", str(js)])
    capsys.readouterr()
    payload = json.loads(js.read_text("utf-8"))["optimization_pair_rows"]
    stat = payload["rollup"][0]["metrics"]["run_wall_ms"]
    assert stat["n_pairs"] == 2
    assert "delta_ms_p50" not in stat
    assert "ratio_p50" not in stat
    assert stat["delta_ms_max"] == 2000.0
    assert stat["ratio_max"] == 3.0
    # 逐题差值不受门槛影响:两格各自的 Δ 与 ratio 都在。
    assert sorted(cell["metrics"]["run_wall_ms"]["delta_ms"]
                  for cell in payload["cells"]) == [1000.0, 2000.0]


def test_a_pair_row_without_both_medians_is_unknown_not_zero(tmp_path, capsys):
    """两侧中位数任一缺席 ⇒ `delta_ms` / `ratio` 都是 unknown,不折 0。

    「没量到」和「差值恰好是 0」在这张表上是两条不同的结论。

    变异:把 `_paired_delta` 的缺席分支改成返回 `0` ⇒ 这条红。
    """
    source = _write_rows(tmp_path / "rows.jsonl", [
        _pair_row_fixture("B-q01", "off", run_wall_ms=None, total_ms=None),
        _pair_row_fixture("B-q01", "prefix_delta_lean", run_wall_ms=3000),
    ])
    js, md = tmp_path / "t0.json", tmp_path / "t0.md"
    analyze.main([str(source), "--pair-rows", "--out-json", str(js),
                  "--out-md", str(md)])
    capsys.readouterr()
    report = json.loads(js.read_text("utf-8"))
    cell = report["optimization_pair_rows"]["cells"][0]
    assert cell["baseline"]["run_wall_ms"] is None
    assert cell["metrics"]["run_wall_ms"] == {"delta_ms": None, "ratio": None}
    # markdown 印 `unknown` 而不是空格:空格会被读成「这一列不适用」。按**表头**
    # 取那一格,不用子串——`unknown` 在这张表的分格列上到处都是。
    assert _pair_row_cells(md)["run_wall_ms Δms"] == analyze.UNKNOWN
    assert _pair_row_cells(md)["run_wall_ms ratio"] == analyze.UNKNOWN


def test_a_zero_baseline_median_loses_the_ratio_but_keeps_the_delta(
    tmp_path, capsys,
):
    """基线中位数为 0 时 `ratio` 记 unknown,`delta_ms` 仍然成立——除以 0 得不到
    一个可报告的比值,但那不该连坐差值。

    变异:把 `_paired_delta` 的 `if base_value else None` 去掉 ⇒
    `ZeroDivisionError`,这条红。
    """
    source = _write_rows(tmp_path / "rows.jsonl", [
        _pair_row_fixture("B-q01", "off", run_wall_ms=0),
        _pair_row_fixture("B-q01", "prefix_delta_lean", run_wall_ms=3000),
    ])
    js = tmp_path / "t0.json"
    analyze.main([str(source), "--pair-rows", "--out-json", str(js)])
    capsys.readouterr()
    payload = json.loads(js.read_text("utf-8"))["optimization_pair_rows"]
    assert payload["cells"][0]["metrics"]["run_wall_ms"] == {
        "delta_ms": 3000.0, "ratio": None}
    stat = payload["rollup"][0]["metrics"]["run_wall_ms"]
    # 两个分母不同:差值有一格,比值没有——合成一个 n 会让某一侧看着样本更多。
    assert stat["n_pairs"] == 1
    assert stat["n_ratio_pairs"] == 0
    assert "ratio_max" not in stat


def test_pair_row_cells_align_with_their_header(tmp_path, capsys):
    """逐题配对表的 8 个数值格必须落在**自己**的表头下面(沿用
    `test_optimization_pair_table_cells_align_with_their_header` 的守卫范式)。

    两个指标各配四格互不相同的值,再按**表头文本**取单元格逐一核对。

    变异:把 `_render_pair_rows` 里拼这四格的元组从
    `(基线, 变体, Δ, ratio)` 调成 `(基线, 变体, ratio, Δ)`(表头不动,单纯调
    序)⇒ 这条红。
    """
    source = _write_rows(tmp_path / "rows.jsonl", [
        _pair_row_fixture("B-q01", "off", run_wall_ms=1000, total_ms=100),
        _pair_row_fixture("B-q01", "prefix_delta_lean", run_wall_ms=3000,
                          total_ms=700),
    ])
    md = tmp_path / "t0.md"
    analyze.main([str(source), "--pair-rows", "--out-md", str(md)])
    capsys.readouterr()
    cells = _pair_row_cells(md)
    assert cells["off run_wall_ms P50(n)"] == "1000.0(n=1)"
    assert cells["variant run_wall_ms P50(n)"] == "3000.0(n=1)"
    assert cells["run_wall_ms Δms"] == "2000.0"
    assert cells["run_wall_ms ratio"] == "3.0"
    assert cells["off total_ms P50(n)"] == "100.0(n=1)"
    assert cells["variant total_ms P50(n)"] == "700.0(n=1)"
    assert cells["total_ms Δms"] == "600.0"
    assert cells["total_ms ratio"] == "7.0"


def test_the_two_pair_tables_never_share_a_column_header(tmp_path, capsys):
    """同一份报告里的两张配对表**不许**用逐字相同的表头印两个不同的统计量
    (规格评审 P2-2)。

    `optimization_pair_table` 那张给的是 `_mean` 的**均值**,逐题配对那张给的是
    格内先对重复取的**中位数**。同一格输入下这两个数可以方向相反(1000/1000/7000
    ⇒ 均值 3000、中位 1000),而操作者按 §10.2-3 引用「off 侧 run_wall_ms」时,
    只有列名能告诉他手上是哪一个。

    变异:把逐题配对表的列名改回 `{baseline_arm} {metric}(n)` ⇒ 这条红。
    """
    source = _write_rows(tmp_path / "rows.jsonl", [
        _pair_row_fixture("B-q01", "off", run_wall_ms=1000),
        _pair_row_fixture("B-q01", "off", run_wall_ms=1000),
        _pair_row_fixture("B-q01", "off", run_wall_ms=7000),
        _pair_row_fixture("B-q01", "prefix_delta_lean", run_wall_ms=2000),
    ])
    md = tmp_path / "t0.md"
    analyze.main([str(source), "--pair-rows", "--out-md", str(md)])
    capsys.readouterr()
    rendered = md.read_text("utf-8")

    def headers(section: str) -> set[str]:
        table = [line for line in rendered.split(section)[1].splitlines()
                 if line.startswith("|")]
        return {cell.strip() for cell in table[0].strip("|").split("|")}

    means = headers("## off / 优化变体对照(成对,同 policy_version)")
    medians = headers("## 逐题配对差值")
    assert "off run_wall_ms(n)" in means
    assert "off run_wall_ms(n)" not in medians
    assert "off run_wall_ms P50(n)" in medians
    assert "off run_wall_ms P50(n)" not in means
    # 两张表在同一份 md 里对同一侧给出的确实是两个不同的数——列名分开的正是它们。
    assert "3000.0(n=3)" in rendered
    assert "1000.0(n=3)" in rendered


def test_the_rollup_buckets_each_effort_and_corpus_cell_on_its_own(
    tmp_path, capsys,
):
    """rollup 除了全局那一层,还按 `effort` 与 `corpus_cell` 各出一层桶
    (规格评审 P2-4、§10.2-3「各主要题型/档位的系统性慢化」)。

    三格配对:`deep` 那一格慢一倍(1000→2000,ratio 2.0)、`standard`/`B_kg`
    那一格快一半(4000→2000,ratio 0.5)、`standard`/`B_nokg` 那一格不动
    (3000→3000)。全局配对比值 [0.5, 1.0, 2.0] 的中位数**正好是 1.0**、Δms 中位
    数**正好是 0** —— 全局那一行逐字读作「没有收益也没有慢化」,而 `deep` 档那一
    格的慢化是 100%,远超 §10.2-3 那道 10% 的线。

    变异:把 `optimization_pair_rows` 里的 `rollup_by` 去掉(或把
    `ROLLUP_FACETS` 清空)⇒ 这条红。
    """
    source = _write_rows(tmp_path / "rows.jsonl", [
        _pair_row_fixture("B-q01", "off", effort="deep", run_wall_ms=1000),
        _pair_row_fixture("B-q01", "prefix_delta_lean", effort="deep",
                          run_wall_ms=2000),
        _pair_row_fixture("B-q01", "off", effort="standard",
                          run_wall_ms=4000),
        _pair_row_fixture("B-q01", "prefix_delta_lean", effort="standard",
                          run_wall_ms=2000),
        _pair_row_fixture("B-q02", "off", effort="standard",
                          corpus_cell="B_nokg", run_wall_ms=3000),
        _pair_row_fixture("B-q02", "prefix_delta_lean", effort="standard",
                          corpus_cell="B_nokg", run_wall_ms=3000),
    ])
    js, md = tmp_path / "t0.json", tmp_path / "t0.md"
    analyze.main([str(source), "--pair-rows", "--min-samples", "1",
                  "--out-json", str(js), "--out-md", str(md)])
    capsys.readouterr()
    payload = json.loads(js.read_text("utf-8"))["optimization_pair_rows"]

    # 全局那一行把两档相反的偏离抵消掉了:两个数都读作「什么都没发生」。
    overall = payload["rollup"][0]["metrics"]["run_wall_ms"]
    assert overall["n_pairs"] == 3
    assert overall["ratio_p50"] == 1.0
    assert overall["delta_ms_p50"] == 0.0

    by_effort = {entry["effort"]: entry["metrics"]["run_wall_ms"]
                 for entry in payload["rollup_by"]["effort"]}
    assert set(by_effort) == {"deep", "standard"}
    assert by_effort["deep"]["n_pairs"] == 1
    assert by_effort["deep"]["ratio_p50"] == 2.0
    assert by_effort["deep"]["delta_ms_p50"] == 1000.0
    assert by_effort["standard"]["n_pairs"] == 2
    assert by_effort["standard"]["ratio_p50"] == 0.5
    assert by_effort["standard"]["delta_ms_p50"] == -2000.0
    # 逐格数据本来就在,这一层只是聚合口径:格数与全局那一行对得上。
    assert sum(entry["n_cells"]
               for entry in payload["rollup_by"]["effort"]) == 3

    by_cell = {entry["corpus_cell"]: entry["metrics"]["run_wall_ms"]
               for entry in payload["rollup_by"]["corpus_cell"]}
    assert set(by_cell) == {"B_kg", "B_nokg"}
    assert by_cell["B_kg"]["ratio_p50"] == 0.5
    assert by_cell["B_nokg"]["ratio_p50"] == 1.0

    rendered = md.read_text("utf-8")
    assert "按 `effort` 分桶" in rendered
    assert "按 `corpus_cell` 分桶" in rendered
    assert "| v2 | prefix_delta_lean | deep | run_wall_ms | 1 | 1 | 1000.0" in (
        rendered)


def test_pair_rows_say_so_when_nothing_pairs(tmp_path, capsys):
    """`--pair-rows` 零配对时印一句「(没有成对样本:…)」,而不是几张只有表头的
    空表——与隔壁 `optimization_pairs` 同口径(质量评审 P3-4)。

    变异:把 `_render_pair_rows` 的空分支去掉 ⇒ 这条红(渲染出光表头的空表)。
    """
    source = _write_rows(tmp_path / "rows.jsonl", [
        _pair_row_fixture("B-q01", "prefix_delta", run_wall_ms=1000),
        _pair_row_fixture("B-q01", "prefix_delta_lean", run_wall_ms=2000),
    ])
    js, md = tmp_path / "t0.json", tmp_path / "t0.md"
    assert analyze.main([str(source), "--pair-rows", "--out-json", str(js),
                         "--out-md", str(md)]) == 0
    capsys.readouterr()
    payload = json.loads(js.read_text("utf-8"))["optimization_pair_rows"]
    assert payload["cells"] == []
    assert payload["rollup"] == []
    section = md.read_text("utf-8").split("## 逐题配对差值")[1]
    assert "(没有成对样本:" in section
    # 一行表格都没有:空表会被读成「配出来了,只是数都没量到」。
    assert [line for line in section.splitlines() if line.startswith("|")] == []


def test_key_set_ab_admits_ab_rows_and_t0_still_refuses_them(tmp_path, capsys):
    """`--key-set ab` 让 `ab-runs.jsonl` 直接喂得进来;`t0`(默认)仍**整批
    拒绝**(计划 M5 缺件 3、Q8:显式选键集是知情声明,不是自动嗅探)。

    变异:把 `load_rows` 改成两份键集的并集(或按行自动嗅探)⇒ 第二段红——
    「多出来的键必须让人看见」这条纪律就被取消了。
    """
    source = _write_rows(tmp_path / "rows.jsonl", [_ab_row()])
    js = tmp_path / "t0.json"
    assert analyze.main([str(source), "--key-set", "ab",
                         "--out-json", str(js)]) == 0
    capsys.readouterr()
    assert json.loads(js.read_text("utf-8"))["n_rows"] == 1

    with pytest.raises(SystemExit, match="闭集外的键"):
        analyze.main([str(source), "--out-json", str(js)])

    # 分格维度跟着键集走:`arm` 在 ab 下合法、在 t0 下仍是未知维度。
    assert analyze.main([str(source), "--key-set", "ab", "--group-by", "arm",
                         "--out-json", str(js)]) == 0
    capsys.readouterr()
    with pytest.raises(SystemExit):
        analyze.main([str(source), "--group-by", "arm"])


def test_the_ab_key_set_is_the_frozen_one_not_a_local_copy():
    """`ab` 那份键集就是 `AB_PROJECTION_KEYS` 本身,不是抄的一份。

    抄一份的代价是它会与写侧分叉:rig 哪天往 `ab-runs.jsonl` 加一列,分析侧这份
    副本不会跟着动,于是「多出来的键必须让人看见」在两边给出不同的答案。

    变异:把 `KEY_SETS["ab"]` 换成一个字面量集合 ⇒ 这条红。
    """
    from app.domain.reasoning_trace_stats import RUN_PROJECTION_KEYS
    from app.eval.reflect_ab import AB_PROJECTION_KEYS

    assert analyze.KEY_SETS["t0"] is RUN_PROJECTION_KEYS
    assert analyze.KEY_SETS["ab"] is AB_PROJECTION_KEYS
    assert analyze.DEFAULT_KEY_SET == "t0"


def test_the_quality_gate_prints_unverified_without_gold_or_review(
    tmp_path, capsys,
):
    """`gold_facts_total` 与人工三列全 `None` 时,报告要印一行显式的「判分与人工
    那半边未验证」,而不是把那一节留空(计划 §5 风险 6:§10.2 明说未验证 ≠ 通过,
    而一份只有时间列漂亮的报告最容易被读成通过)。

    那句话的作用域只到「判分与人工」为止:确定性那几列 T-AB2 已实现,它们读得出
    (见 `test_the_quality_gate_reads_the_deterministic_columns`),所以这里不能
    再写成「这一批读不出质量那一条」——那是关于**工具**的事实,不是关于这一批的
    (规格评审 P2-3)。

    变异:把 `_render_quality` 的 `unverified` 分支改成「什么都不印」⇒ 这条红。
    """
    source = _write_rows(tmp_path / "rows.jsonl", [_ab_row(), _ab_row()])
    js, md = tmp_path / "t0.json", tmp_path / "t0.md"
    analyze.main([str(source), "--key-set", "ab", "--out-json", str(js),
                  "--out-md", str(md)])
    capsys.readouterr()
    evidence = json.loads(js.read_text("utf-8"))["quality_evidence"]
    assert evidence["verdict"] == "unverified"
    assert set(evidence["observed"]) == set(analyze.QUALITY_EVIDENCE_KEYS)
    assert all(count == 0 for count in evidence["observed"].values())
    rendered = md.read_text("utf-8")
    assert "## 质量门(§10.2-1)" in rendered
    assert "判分与人工那半边未验证" in rendered
    # 确定性那几列一列都没量到时印 unknown 而不是 0:「没量到」与「一条都没有」
    # 在门 1 上是两句不同的话,后者才是通过条件。
    assert "citations_out_of_scope=unknown(n=0)" in rendered
    # 结论只有两格短码,不产出任何比率(命名与词面红线)。
    assert "%" not in rendered.split("## 分组概览")[0]


def test_the_quality_gate_reads_the_deterministic_columns(tmp_path, capsys):
    """§10.2-1 里 **T-AB2 已实现**的那几列(越范围引用 / 解析不到的锚点 / 落在
    gold 来源上的锚点 / 完整性声明 / 空答案)要真的被读出来,md 与 json 都有
    (规格评审 P2-3、计划 M5 的 §10.2 对账表门 1)。

    失败场景就是这条用例的输入:一批里变体侧新增 3 条越范围引用、还虚报了完整性
    ——数据集里**已经有**这个观测,报告要是既不列它、又断言「这一批读不出质量那
    一条」,§10.2-2「若有新增重大错误,先修复或退回较简单候选」在报告面上就完全
    不可见。

    **不新增投影列**:这几个键早已在 `AB_PROJECTION_KEYS` 里,这一条只钉读侧。

    变异:把 `QUALITY_COUNT_KEYS` / `QUALITY_LABEL_KEYS` 从
    `QUALITY_EVIDENCE_KEYS` 里摘掉(退回只读 gold 与 `human_*` 的四列)⇒ 这条红。
    """
    source = _write_rows(tmp_path / "rows.jsonl", [
        _ab_row(citations_out_of_scope=3, anchors_unresolved=2,
                completeness_claim="complete", answer_empty=False),
        _ab_row(citations_out_of_scope=0, anchors_unresolved=0,
                anchors_on_gold=1, completeness_claim="partial",
                answer_empty=False),
    ])
    js, md = tmp_path / "t0.json", tmp_path / "t0.md"
    analyze.main([str(source), "--key-set", "ab", "--out-json", str(js),
                  "--out-md", str(md)])
    capsys.readouterr()
    evidence = json.loads(js.read_text("utf-8"))["quality_evidence"]
    # 确定性列有观测,但判分/人工那半边仍然没有 ⇒ 结论不变。
    assert evidence["verdict"] == "unverified"
    assert evidence["observed"]["citations_out_of_scope"] == 2
    # 和走 `_numeric` 出来,所以是 float —— 与这份报告里每一个别的数同一种形状。
    assert evidence["deterministic_totals"]["citations_out_of_scope"] == {
        "n_observed": 2, "total": 3.0}
    assert evidence["deterministic_totals"]["anchors_unresolved"] == {
        "n_observed": 2, "total": 2.0}
    assert evidence["deterministic_totals"]["anchors_on_gold"] == {
        "n_observed": 1, "total": 1.0}
    assert evidence["deterministic_distributions"]["completeness_claim"] == {
        "complete": 1, "partial": 1}
    assert evidence["deterministic_distributions"]["answer_empty"] == {
        "False": 2}

    rendered = md.read_text("utf-8")
    assert "citations_out_of_scope=3.0(n=2)" in rendered
    assert "anchors_unresolved=2.0(n=2)" in rendered
    assert "completeness_claim: complete=1, partial=1" in rendered
    # 依然不产出比率:那几列只报计数与分布(命名与词面红线)。
    assert "%" not in rendered.split("## 分组概览")[0]


def test_the_quality_gate_reports_records_when_gold_or_review_exists(
    tmp_path, capsys,
):
    """有 gold 或盲审记录时换另一格短码,并如实报出逐列观测数——但结论仍归人工
    盲审,这几列只说明证据在不在。

    变异:把 `quality_evidence` 的 `verdict` 改成恒 `unverified` ⇒ 这条红。
    """
    source = _write_rows(tmp_path / "rows.jsonl", [
        _ab_row(gold_facts_total=3, gold_facts_hit=2),
        _ab_row(human_factual_error=False),
    ])
    js, md = tmp_path / "t0.json", tmp_path / "t0.md"
    analyze.main([str(source), "--key-set", "ab", "--out-json", str(js),
                  "--out-md", str(md)])
    capsys.readouterr()
    evidence = json.loads(js.read_text("utf-8"))["quality_evidence"]
    assert evidence["verdict"] == "records_present"
    assert evidence["observed"]["gold_facts_total"] == 1
    assert evidence["observed"]["human_factual_error"] == 1
    rendered = md.read_text("utf-8")
    assert "质量:有记录(gold_facts_total=1" in rendered
    assert "由人工盲审下" in rendered


def test_no_analysis_name_mentions_a_hit_rate():
    """命名红线:分析侧的常量、报告键与表头一律不出现 `cache_hit` / 命中率 /
    hit rate 形状的名字(前缀复用最终设计 review 调整第 4 条)。

    这一格量的是「稳定前缀的时间收益」,不是命中率——一个叫 `cache_hit_rate` 的
    列会让读的人以为这批数据回答了一个它压根回答不了的问题。

    变异:给 `PAIR_ROW_METRICS` 或 `QUALITY_EVIDENCE_KEYS` 加一格
    `cache_hit_rate` ⇒ 这条红。
    """
    forbidden = ("cache_hit", "hit_rate", "hitrate", "命中")
    names = (
        *analyze.PAIR_ROW_METRICS, *analyze.QUALITY_EVIDENCE_KEYS,
        *analyze.QUALITY_COUNT_KEYS, *analyze.QUALITY_LABEL_KEYS,
        *analyze.QUALITY_JUDGED_KEYS,
        *analyze.ROLLUP_FACETS, *analyze.ROLLUP_SIDE_COUNTS,
        *analyze.PAIR_SIDE_METRICS, *analyze.SPARSE_PAIR_SIDE_METRICS,
        *analyze.NUMERIC_METRICS, *analyze.BOOLEAN_METRICS,
        *analyze.CATEGORICAL_METRICS, *analyze.COUNTER_METRICS,
        *analyze.KEY_SETS,
    )
    for name in names:
        assert not any(token in name.lower() for token in forbidden), name
