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
from tests.test_reasoning_retrieval import (  # noqa: F401 — rrepo 是 fixture
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
        question_key="B-q07", corpus_cell="B_nokg",
        effort="deep", requested_mode="reasoning",
    )
    assert encoded == "t0:B-q07:B_nokg:legacy:deep:reasoning"
    assert export.decode_client_request_id(encoded) == {
        "question_key": "B-q07", "corpus_cell": "B_nokg", "policy": "legacy",
        "effort": "deep", "requested_mode": "reasoning",
    }


def test_client_request_id_rejects_a_colon_in_any_field():
    """冒号是字段分隔符。让它进去,导出侧解出来的标签会**错位**而不是失败。"""
    with pytest.raises(ValueError):
        rig.encode_client_request_id(
            question_key="B:q07", corpus_cell="B_kg",
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
         "backend_pid": 4242, "port": 8011,
        "database_url": "postgresql://127.0.0.1:5432/silicon_notebook_t0_test",
        "storage_dir": ".local/storage-t0", "env_file": None,
        "notebooks": {},
    }), encoding="utf-8")
    assert rig.main([
        "--dry-run", "--out-dir", str(out_dir), "restart",
    ]) == 0
    printed = capsys.readouterr().out
    assert "[dry-run]" in printed
    assert "pid=4242" in printed
    assert "start backend" in printed
    # dry-run 是纯打印:state 文件必须原封不动。
    assert json.loads((out_dir / "rig-state.json").read_text("utf-8")).get("backend_pid") == 4242


def test_ask_dry_run_proceeds_without_saved_state(tmp_path, capsys):
    """线上从没跑过 `seed`/`restart` 的 out-dir:不知道就不拦,而不是报错。"""
    out_dir = tmp_path / "t0"
    assert rig.main([
        "--dry-run", "--out-dir", str(out_dir), "--limit", "1", "ask",
    ]) == 0


def test_english_variants_get_their_own_question_key():
    questions = rig.load_questions()
    plan = rig.ask_plan(
        questions, cells=["A_kg"], limit=1, lang="both",
        mode="reasoning", efforts=("standard",),
    )
    assert [item["question_key"] for item in plan] == ["A-q01", "A-q01-en"]


def test_ask_plan_carries_scope_source_titles_only_for_the_declaring_question():
    """`scope_sources`(计数)已经改成 `scope_source_titles`(标题列表)——
    没有一个消费者读过前者(codex #700 R3 P2),这里钉住 `ask_plan` 现在按
    标题原样转发,其余题目恒是空元组。"""
    questions = rig.load_questions()
    plan = rig.ask_plan(
        questions, cells=["B_nokg"], limit=None, lang="zh",
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
        questions, cells=list(rig.CORPUS_CELLS), limit=None,
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


# --- ask 的必填澄清门 ------------------------------------------------------


def test_ask_does_not_submit_required_ambiguities_without_real_answers(
    tmp_path, monkeypatch, capsys,
):
    """Model options are not user confirmation; a blocked run stays observable."""
    out_dir = tmp_path / "t0"
    out_dir.mkdir()
    (out_dir / "rig-state.json").write_text(json.dumps({
         "token": "tok", "notebooks": {"A_nokg": "nb-a"},
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
    ]) == 1
    assert submitted == []
    assert "reason=clarification_required" in capsys.readouterr().out


# --- 导出(SQLite 侧) ------------------------------------------------------


def test_ask_counts_runs_without_a_final_frame_as_failures(tmp_path, monkeypatch, capsys):
    """执行阶段失败/取消走 HTTP 200 里的 `event="error"`/`"cancelled"` 帧;只看
    标签落库只证明 job 建了,整批全失败也会退出码 0(codex #700 R9 P2)。终帧不是
    `final` 的 run 计失败、整批退出码 1;日志只记事件名,不记 error 正文。"""
    out_dir = tmp_path / "t0"
    out_dir.mkdir()
    (out_dir / "rig-state.json").write_text(json.dumps({
         "token": "tok", "notebooks": {"A_nokg": "nb-a"},
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
        "kg_in_scope": True,
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
        _row(), _row(consumer="report_section", termination_inferred=False,
                     termination_reason="model_end"),
    ])
    md, js = tmp_path / "t0.md", tmp_path / "t0.json"
    assert analyze.main([
        str(source), "--group-by", "consumer",
        "--out-md", str(md), "--out-json", str(js),
    ]) == 0
    capsys.readouterr()
    rendered = md.read_text("utf-8")
    assert "检索轨迹聚合" in rendered
    assert "ask_single" in rendered and "report_section" in rendered
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
    from app.domain.reasoning_trace_stats import RUN_PROJECTION_KEYS

    covered = set(
        analyze.NUMERIC_METRICS + analyze.BOOLEAN_METRICS
        + analyze.CATEGORICAL_METRICS + analyze.COUNTER_METRICS)
    # 身份/分组维度(它们是分格依据,不是被聚合的指标)与自由文本身份。
    dimensions = {
        "consumer", "mode", "effort", "kg_in_scope",
        "corpus_cell", "question_key", "notebook_bucket", "section_index",
        "merge_key", "requested_mode",
    }
    # 结构化键:值是 dict/list,四组标量指标按定义都吃不下它们。
    structured = {"citation_contribution", "action_seq"}
    missing = RUN_PROJECTION_KEYS - covered - dimensions - structured
    assert not missing, sorted(missing)
    assert "scope_narrowed" in covered


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
    with pytest.raises(SystemExit, match="闭集投影行"):
        analyze.main([str(source)])


def test_analysis_refuses_an_unknown_group_by_dimension(tmp_path):
    source = _write_rows(tmp_path / "rows.jsonl", [_row()])
    with pytest.raises(SystemExit):
        analyze.main([str(source), "--group-by", "question"])


# --- search 子命令 -----------------------------------------------------------


def test_dry_run_search_enumerates_two_cells_two_efforts(capsys):
    assert rig.main(["--dry-run", "--limit", "5", "search"]) == 0
    printed = capsys.readouterr().out
    # 5 题 × 2 格 × 2 档 = 20。
    assert "planned runs  20" in printed
    assert "effort=standard" in printed
    assert "effort=deep" in printed
    # `search` 不建库也不建图,所以另外两格在主库上根本不存在,不许出现在计划里。
    assert "A_kg" not in printed and "B_nokg" not in printed
    # 主库连接必须显式给:`--database-url` 的默认值指向的是 ask/seed 的测试库。
    assert "<MISSING:必须显式给>" in printed
    # 调用量估计:每题一次意图 + 不调 plan(已确认意图直接作首轮种子)。
    assert "intent 10 + plan 0 + reflect ≤" in printed


def test_dry_run_search_charges_a_plan_call_per_run_without_intent(capsys):
    assert rig.main(["--dry-run", "--limit", "1", "--no-intent", "search"]) == 0
    printed = capsys.readouterr().out
    assert "intent 0 + plan 4 + reflect ≤" in printed


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


def test_search_plan_carries_no_idempotency_key():
    plan = rig.search_plan(
        rig.load_questions(), cells=list(rig.SEARCH_CELLS),
        limit=5, lang="zh", efforts=rig.EFFORTS,
    )
    assert len(plan) == 20
    assert all("client_request_id" not in item and "policy" not in item for item in plan)


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

#: **不写 `DATABASE_URL`**:那条命令结构上不连库,写任何占位值只是徒增一次
#: `Settings()` 校验失败的风险(`normalize_database_url("")` 直接抛)。三把注入


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
    backend = rig.backend_env(args)
    assert backend["EVENT_LOG_DIR"] == str((out / "events").resolve())
    assert backend["LLM_LOG_PATH"] == str((out / "llm" / "llm.jsonl").resolve())


def _search_item() -> dict:
    return {
        "question_key": "A-q01", "corpus_cell": "A_nokg",
        "effort": "standard", "question": "RTL到GDSII流程",
    }


def test_search_core_runs_legacy_on_one_repo_and_writes_nothing(rrepo, tmp_path):
    notebook = _seed_two_nodes(rrepo)
    database_url = f"sqlite:///{tmp_path / 't.db'}"
    before = rig._readonly_counts(database_url)
    assert before["knowledge_objects"]
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[{"next_action": "answer", "sufficient": True}],
    ))
    item = _search_item()
    steps = []
    rig.run_search_once(
        rrepo, rrepo.settings, notebook=notebook.id, item=item, prepared=None,
        on_step=lambda step: steps.append(rig.trace_step_row(step)),
        cancel_event=threading.Event(), actor_id="t0-owner",
    )
    row = rig.project_search_run(
        steps, effort=item["effort"], question_key=item["question_key"],
        corpus_cell=item["corpus_cell"], kg_in_scope=True,
    )
    assert row["reflect_turns"] >= 1
    assert row["trace_source"] == "in_process"
    assert row["consumer"] == "ask_single"
    assert row["anchors"] is None
    assert row["citation_contribution"] is None
    assert row["termination_inferred"] is True
    assert rig._readonly_counts(database_url) == before


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
        return types.SimpleNamespace()

    monkeypatch.setattr(ReasoningRetriever, "run", fake_run)
    rig.run_search_once(
        rrepo, rrepo.settings, notebook=notebook.id, item=_search_item(),
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
        return types.SimpleNamespace()

    monkeypatch.setattr(ReasoningRetriever, "run", fake_run)
    rig.run_search_once(
        rrepo, rrepo.settings, notebook=notebook.id, item=_search_item(),
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

    item = dict(_search_item())
    item["scope_source_titles"] = ("no-such-title.md",)
    out_dir = tmp_path / "out"
    runner = rig.Runner(dry_run=False, out_dir=out_dir)
    facts = {"A_nokg": {"notebook": notebook.id, "sources": 0,
                        "kg_in_scope": False}}
    rig._search_loop(
        _search_loop_args(database_url), runner, [item], facts, rrepo,
        rrepo.settings,
        actor_id="t0-owner", cancel_event=threading.Event(),
    )
    lines = (out_dir / "search.jsonl").read_text("utf-8").splitlines()
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
    env = rig.backend_env(args)
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
        return types.SimpleNamespace()

    monkeypatch.setattr(rig, "run_search_once", fake_run_search_once)
    first = dict(_search_item())
    second = dict(_search_item()); second["question_key"] = "A-q02"
    facts = {"A_nokg": {"notebook": notebook.id, "sources": 0, "kg_in_scope": False}}
    out_dir = tmp_path / "serial"
    runner = rig.Runner(dry_run=False, out_dir=out_dir)
    failed = rig._search_loop(
        _search_loop_args(f"sqlite:///{tmp_path / 't.db'}"), runner,
        [first, second], facts, rrepo,
        rrepo.settings,
        actor_id="t0-owner", cancel_event=threading.Event(),
    )
    assert failed == 1
    assert calls == ["A-q01", "A-q02"], "失败不该打断后面的 run"
    rows = [json.loads(l) for l in (out_dir / "search.jsonl").read_text().splitlines()]
    by_key = {r["question_key"]: r for r in rows}
    assert by_key["A-q01"]["status"] == "failed"
    assert by_key["A-q01"]["reflect_turns"] == 1, "失败前捕获的轨迹步不该被丢成 0"
    # 失败 run 也有 run 墙钟(计划 §3 T-PS5 验收):它崩在半路,但确实占了这么
    # 久,而「哪一侧更容易崩、崩之前烧了多少时间」正是要量的东西之一。
    assert isinstance(by_key["A-q01"]["run_wall_ms"], int)
    assert isinstance(by_key["A-q02"]["run_wall_ms"], int)
    assert by_key["A-q02"]["status"] != "failed"
    log_text = (out_dir / "search-runs.log").read_text("utf-8")
    assert "A-q01 A_nokg standard FAILED ValueError" in log_text
    assert "秘密原文" not in log_text

    def cancelled(*a, **k):
        raise AskCancelled()

    monkeypatch.setattr(rig, "run_search_once", cancelled)
    with pytest.raises(AskCancelled):
        rig._search_loop(
            _search_loop_args(f"sqlite:///{tmp_path / 't2.db'}"),
            rig.Runner(dry_run=False, out_dir=tmp_path / "serial2"),
            [dict(_search_item())], facts, rrepo,
            rrepo.settings,
            actor_id="t0-owner", cancel_event=threading.Event(),
        )


def test_failed_run_row_keeps_the_requested_effort_and_loops_count_it(
    rrepo, tmp_path, monkeypatch,
):
    fact = {"notebook": "nb", "sources": 1, "kg_in_scope": False}
    item = {"question_key": "B-q09", "corpus_cell": "B_kg",
            "effort": "deep", "question": "q"}
    assert rig._failed_run_row(item, fact)["effort"] == "deep"

    notebook = _seed_two_nodes(rrepo)

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("范围解析失败时不该调 run_search_once")

    monkeypatch.setattr(rig, "run_search_once", _boom)
    scoped = dict(_search_item())
    scoped["scope_source_titles"] = ("no-such-title.md",)
    facts = {"A_nokg": {"notebook": notebook.id, "sources": 0, "kg_in_scope": False}}
    runner = rig.Runner(dry_run=False, out_dir=tmp_path / "serial")
    failed = rig._search_loop(
        _search_loop_args(f"sqlite:///{tmp_path / 't.db'}"), runner, [scoped], facts,
        rrepo, rrepo.settings,
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
    item = dict(_search_item())
    item["scope_source_titles"] = ("paper-a.md", "paper-b.md")
    out_dir = tmp_path / "out"
    runner = rig.Runner(dry_run=False, out_dir=out_dir)
    facts = {"A_nokg": {"notebook": notebook.id, "sources": 2,
                        "kg_in_scope": True}}
    rig._search_loop(
        _search_loop_args(database_url), runner, [item], facts, rrepo,
        rrepo.settings,
        actor_id="t0-owner", cancel_event=threading.Event(),
    )
    lines = (out_dir / "search.jsonl").read_text("utf-8").splitlines()
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

    configured = True

    def __init__(self) -> None:
        self._local = threading.local()

    def bind(self, client: Any) -> None:  # noqa: ANN401 — 测试替身,类型从简
        self._local.client = client

    def chat_json(self, messages, schema_hint, **kwargs):
        return self._local.client.chat_json(messages, schema_hint, **kwargs)


def test_search_concurrent_two_questions_on_one_repo(rrepo, tmp_path):
    notebook = _seed_two_nodes(rrepo)
    dispatcher = _ThreadLocalLLM()
    bind_chat_client(rrepo, "reasoning_agent", dispatcher)
    plan = [dict(_search_item(), question_key=key) for key in ("A-q01", "A-q02")]
    database_url = f"sqlite:///{tmp_path / 't.db'}"
    before = rig._readonly_counts(database_url)
    assert before["knowledge_objects"]
    def run_one(item):
        dispatcher.bind(_SeqLLM(
            plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
            reflects=[{"next_action": "answer", "sufficient": True}],
        ))
        steps = []
        rig.run_search_once(
            rrepo, rrepo.settings, notebook=notebook.id, item=item, prepared=None,
            on_step=lambda step: steps.append(rig.trace_step_row(step)),
            cancel_event=threading.Event(), actor_id="t0-owner",
        )
        return rig.project_search_run(
            steps, effort=item["effort"], question_key=item["question_key"],
            corpus_cell=item["corpus_cell"], kg_in_scope=True,
        )
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(run_one, plan))
    assert {row["question_key"] for row in rows} == {"A-q01", "A-q02"}
    for row in rows:
        assert row["reflect_turns"] >= 1
        assert json.loads(json.dumps(row)) == row
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
        {"question_key": f"Q{i:02d}", "corpus_cell": "A_nokg",
         "effort": "standard", "question": "q"}
        for i in range(concurrency * 2)
    ]
    facts = {"A_nokg": {"notebook": "nb-a", "sources": 1, "kg_in_scope": True}}
    settings = object()
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
        return types.SimpleNamespace()

    monkeypatch.setattr(rig, "run_search_once", fake_run_search_once)

    runner = rig.Runner(dry_run=False, out_dir=tmp_path)
    profile = types.SimpleNamespace(id="t0-owner")
    rig._search_loop_concurrent(
        _concurrent_search_args(), runner, plan, facts, None,
        settings, actor_id="t0-owner", profile=profile,
        concurrency=concurrency,
    )

    assert state["peak"] == concurrency
    rows = [
        json.loads(line)
        for line in (tmp_path / "search.jsonl").read_text().splitlines()
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
        {"question_key": f"Q{i:02d}", "corpus_cell": "A_nokg",
         "effort": "standard", "question": "秘密原文不得进日志"}
        for i in range(4)
    ]
    facts = {"A_nokg": {"notebook": "nb-a", "sources": 1, "kg_in_scope": True}}
    settings = object()

    def fake_run_search_once(repo, settings, *, notebook, item, prepared,
                             on_step, cancel_event, actor_id,
                             scope_source_ids=None):
        if item["question_key"] == "Q02":
            # 失败前已经走了一轮反思:这一步不能从失败行里消失(codex #700 R13 P2)。
            on_step(types.SimpleNamespace(step_type="reflect", detail={}, duration_ms=5))
            raise ValueError("provider exploded")
        return types.SimpleNamespace()

    monkeypatch.setattr(rig, "run_search_once", fake_run_search_once)
    runner = rig.Runner(dry_run=False, out_dir=tmp_path)
    failed = rig._search_loop_concurrent(
        _concurrent_search_args(), runner, plan, facts, None,
        settings, actor_id="t0-owner",
        profile=types.SimpleNamespace(id="t0-owner"), concurrency=2,
    )

    assert failed == 1
    rows = [
        json.loads(line)
        for line in (tmp_path / "search.jsonl").read_text().splitlines()
    ]
    assert len(rows) == len(plan)
    by_key = {row["question_key"]: row for row in rows}
    assert by_key["Q02"]["status"] == "failed"
    assert by_key["Q02"]["scope_narrowed"] is None
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
    from app.services.cancellation import AskCancelled as rig_cancel_type

    plan = [
        {"question_key": "Q00", "corpus_cell": "A_nokg",
         "effort": "standard", "question": "q"},
        {"question_key": "Q01", "corpus_cell": "A_nokg",
         "effort": "standard", "question": "q"},
    ]
    facts = {"A_nokg": {"notebook": "nb-a", "sources": 1, "kg_in_scope": True}}
    settings = object()
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
        raise rig_cancel_type()

    real_say = rig.Runner.say

    def spy_say(self, action, detail=""):
        if action == "search aborted":
            aborted_seen.set()
        return real_say(self, action, detail)

    monkeypatch.setattr(rig, "_resolve_item_scope", slow_resolve)
    monkeypatch.setattr(rig, "run_search_once", fake_run_search_once)
    monkeypatch.setattr(rig.Runner, "say", spy_say)
    runner = rig.Runner(dry_run=False, out_dir=tmp_path)
    with pytest.raises(rig_cancel_type):
        rig._search_loop_concurrent(
            _concurrent_search_args(), runner, plan, facts, None,
            settings, actor_id="t0-owner",
            profile=types.SimpleNamespace(id="t0-owner"), concurrency=2,
        )
    assert executed == ["Q00"], "abort 之后才登记的 Q01 不该开始调模型"


def test_search_loop_concurrent_cancels_remaining_runs_on_cancellation(
    tmp_path, monkeypatch,
):
    from app.services.cancellation import AskCancelled

    total = 8
    from app.services.cancellation import AskCancelled as rig_cancel_type

    plan = [
        {"question_key": f"Q{i:02d}", "corpus_cell": "A_nokg",
         "effort": "standard",
         "question": "q"}
        for i in range(total)
    ]
    facts = {"A_nokg": {"notebook": "nb-a", "sources": 1, "kg_in_scope": True}}
    settings = object()
    executed: list[str] = []
    lock = threading.Lock()

    def fake_run_search_once(repo, settings, *, notebook, item, prepared,
                             on_step, cancel_event, actor_id,
                             scope_source_ids=None):
        with lock:
            executed.append(item["question_key"])
        if item["question_key"] == "Q00":
            raise rig_cancel_type()
        if cancel_event.wait(timeout=5):
            raise AskCancelled()
        raise TimeoutError("cancel_event 一直没被设置——abort() 没生效")

    monkeypatch.setattr(rig, "run_search_once", fake_run_search_once)

    runner = rig.Runner(dry_run=False, out_dir=tmp_path)
    profile = types.SimpleNamespace(id="t0-owner")
    started = time.monotonic()
    with pytest.raises(rig_cancel_type):
        rig._search_loop_concurrent(
            _concurrent_search_args(), runner, plan, facts, None,
            settings, actor_id="t0-owner", profile=profile,
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


def test_dry_run_search_only_question_and_only_cell_filter_to_two_runs(capsys):
    """`--only-question B-q03 --only-cell B_kg`:1 题 × 1 格 × 2 档 = 2。"""
    assert rig.main([
        "--dry-run", "--only-question", "B-q03", "--only-cell", "B_kg",
        "search",
    ]) == 0
    printed = capsys.readouterr().out
    assert "planned runs  2" in printed
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
        questions, cells=["B_kg"], limit=1,
        lang="zh", efforts=rig.EFFORTS, only_questions=["B-q03"],
    )
    assert plan  # 先筛后限:B-q03 没有被 limit=1 的前置切片挡掉
    assert {item["question_key"] for item in plan} == {"B-q03"}
    assert len(plan) == 2  # 两个检索档位


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
    notebook = _seed_two_nodes(rrepo)
    rrepo.settings.reasoning_stale_limit = 9
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[{"next_action": "answer", "sufficient": True}]))

    plan = [_search_item()]
    facts = {"A_nokg": {"notebook": notebook.id, "sources": 1,
                        "kg_in_scope": True}}
    settings = rrepo.settings

    args = rig.build_parser().parse_args(["search"])
    args.no_intent = True
    args.keep_raw_trace = False
    runner = rig.Runner(dry_run=False, out_dir=tmp_path)
    rig._search_loop(
        args, runner, plan, facts, rrepo, settings,
        actor_id="t0-owner", cancel_event=threading.Event(),
    )
    assert not (tmp_path / "raw").exists()

    # 同一个笔记本再跑一遍,这次打开开关。
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[{"next_action": "answer", "sufficient": True}]))
    args.keep_raw_trace = True
    rig._search_loop(
        args, runner, plan, facts, rrepo, settings,
        actor_id="t0-owner", cancel_event=threading.Event(),
    )
    raw_path = tmp_path / "raw" / "A-q01_A_nokg_standard.json"
    assert raw_path.exists()
    payload = json.loads(raw_path.read_text(encoding="utf-8"))
    assert payload["trace_steps"], "原始轨迹不该是空的"
    first = payload["trace_steps"][0]
    assert set(first) == {"step_type", "summary", "detail", "duration_ms"}
    assert isinstance(first["summary"], str) and first["summary"]
    assert set(payload) == {"trace_steps"}


def test_search_loop_concurrent_writes_raw_trace_under_the_write_lock(
    tmp_path, monkeypatch,
):
    """并发路径:`--keep-raw-trace` 的文件写在既有 `write_lock` 保护的那一段里,
    每题一份、互不覆盖。"""
    plan = [
        {"question_key": f"Q{i:02d}", "corpus_cell": "A_nokg",
         "effort": "standard", "question": "q"}
        for i in range(3)
    ]
    facts = {"A_nokg": {"notebook": "nb-a", "sources": 1, "kg_in_scope": True}}
    settings = object()

    def fake_run_search_once(repo, settings, *, notebook, item, prepared,
                             on_step, cancel_event, actor_id,
                             scope_source_ids=None):
        on_step(types.SimpleNamespace(
            step_type="reflect", summary="人话",
            detail={"next_action": "answer", "sufficient": True},
            duration_ms=5,
        ))
        return types.SimpleNamespace()

    monkeypatch.setattr(rig, "run_search_once", fake_run_search_once)

    args = _concurrent_search_args()
    args.keep_raw_trace = True
    runner = rig.Runner(dry_run=False, out_dir=tmp_path)
    profile = types.SimpleNamespace(id="t0-owner")
    rig._search_loop_concurrent(
        args, runner, plan, facts, None, settings,
        actor_id="t0-owner", profile=profile, concurrency=3,
    )

    raw_files = sorted((tmp_path / "raw").glob("*.json"))
    assert [p.name for p in raw_files] == [
        "Q00_A_nokg_standard.json", "Q01_A_nokg_standard.json",
        "Q02_A_nokg_standard.json",
    ]
    for path in raw_files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["trace_steps"][0]["summary"] == "人话"


@pytest.mark.parametrize("ambiguity", [
    {"id": "a1", "required": True, "options": ["全部来源", "仅本库"]},
    {"id": "a2", "required": True, "options": ["（请补充模型名称）"]},
    {"id": "a3", "required": True, "options": []},
    {"id": "a4", "options": ["默认选项"]},
    {"question": "缺少 id 的阻断项", "required": True},
])
def test_auto_clarification_answers_refuses_to_invent_required_answers(ambiguity):
    with pytest.raises(rig.ClarificationRequiredError, match="clarification_required"):
        rig.auto_clarification_answers({"ambiguities": [ambiguity]})


def test_auto_clarification_answers_only_auto_confirms_clear_intents():
    assert rig.auto_clarification_answers({}) == []
    assert rig.auto_clarification_answers({"ambiguities": [
        {"id": "optional", "required": False, "options": ["简述", "详述"]},
    ]}) == []
    with pytest.raises(rig.ClarificationRequiredError, match="clarification_required"):
        rig.auto_clarification_answers({"needs_clarification": True})


def test_plan_intent_does_not_freeze_a_model_placeholder_as_a_user_answer():
    class _AmbiguousClient:
        configured = True

        def chat_json(self, *args, **kwargs):
            return json.dumps({
                "normalized_question": "比较两个系统的性能。",
                "ambiguities": [{
                    "question": "比较哪个系统？",
                    "required": True,
                    "options": ["请填写系统名称"],
                }],
                "needs_clarification": True,
            })

    repo = types.SimpleNamespace(chat=lambda _: _AmbiguousClient())
    settings = types.SimpleNamespace(reasoning_max_subqueries=3)
    with pytest.raises(rig.ClarificationRequiredError, match="clarification_required"):
        rig._plan_intent(
            repo, settings, "性能与基准方案相比如何？",
            actor_id="fixture-owner", notebook="fixture-notebook",
        )


def test_intent_cache_rejects_old_confirmation_policy_without_rewriting_it(tmp_path):
    path = tmp_path / "intents.jsonl"
    old = json.dumps({
        "question_key": "A-q01", "contract": {
            "confirmed": True,
            "clarification_answers": [{"id": "a1", "answer": "请补充系统名称"}],
        },
    }) + "\n"
    path.write_text(old, encoding="utf-8")

    with pytest.raises(RuntimeError, match="intent_cache_policy_mismatch"):
        rig._IntentCache(path, enabled=True)

    assert path.read_text(encoding="utf-8") == old


def test_intent_cache_reuses_only_the_current_confirmation_policy(tmp_path, monkeypatch):
    calls = []
    contract = {"resolved_question": "question", "confirmed": True}
    monkeypatch.setattr(
        rig, "_plan_intent", lambda *a, **kw: calls.append(1) or contract,
    )
    path = tmp_path / "intents.jsonl"
    item = {"question_key": "A-q01", "question": "question"}
    for _ in range(2):
        assert rig._IntentCache(path, enabled=True).get(
            None, None, item, actor_id="owner", notebook="nb",
        ) == contract
    assert calls == [1]
    assert json.loads(path.read_text())["intent_cache_policy"] == rig.INTENT_CACHE_POLICY


@pytest.mark.parametrize("blocked", [False, True])
def test_intent_cache_same_question_shares_one_terminal_outcome(
    tmp_path, monkeypatch, blocked,
):
    from concurrent.futures import TimeoutError

    entered = threading.Event()
    release = threading.Event()
    second_started = threading.Event()
    calls = []
    contract = {"resolved_question": "question", "confirmed": True}

    def plan(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)
            if blocked:
                raise rig.ClarificationRequiredError("clarification_required")
            return contract
        # A second independent completion would disagree with the first. It
        # must never run while another effort of this question is being planned.
        if not blocked:
            raise rig.ClarificationRequiredError("clarification_required")
        return contract

    monkeypatch.setattr(rig, "_plan_intent", plan)
    cache = rig._IntentCache(tmp_path / "intents.jsonl", enabled=True)

    def get(second=False):
        if second:
            second_started.set()
        return cache.get(
            None, None, {"question_key": "A-q01", "question": "question"},
            actor_id="owner", notebook="nb",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(get)
        assert entered.wait(3)
        second = pool.submit(get, True)
        try:
            assert second_started.wait(3)
            with pytest.raises(TimeoutError):
                second.result(timeout=0.1)
        finally:
            release.set()
        for result in (first, second):
            if blocked:
                with pytest.raises(rig.ClarificationRequiredError):
                    result.result(timeout=3)
            else:
                assert result.result(timeout=3) == contract
    assert calls == [1]
    if blocked:
        with pytest.raises(rig.ClarificationRequiredError):
            get()
    else:
        assert get() == contract


def test_intent_cache_different_questions_still_plan_concurrently(tmp_path, monkeypatch):
    barrier = threading.Barrier(2)

    def plan(repo, settings, question, **kwargs):
        barrier.wait(timeout=3)
        return {"resolved_question": question, "confirmed": True}

    monkeypatch.setattr(rig, "_plan_intent", plan)
    cache = rig._IntentCache(tmp_path / "intents.jsonl", enabled=True)

    def get(key):
        return cache.get(
            None, None, {"question_key": key, "question": key},
            actor_id="owner", notebook="nb",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(get, ["A-q01", "B-q01"]))
    assert {row["resolved_question"] for row in results} == {"A-q01", "B-q01"}


@pytest.mark.parametrize("concurrency", [1, 2])
def test_search_clarification_failure_lands_each_effort_without_starting_retrieval(
    tmp_path, monkeypatch, concurrency,
):
    calls = []

    def blocked_intent(*args, **kwargs):
        calls.append(1)
        raise rig.ClarificationRequiredError("clarification_required")

    monkeypatch.setattr(rig, "_plan_intent", blocked_intent)
    monkeypatch.setattr(
        rig, "run_search_once",
        lambda *a, **kw: pytest.fail("unconfirmed request must not start retrieval"),
    )
    args = _concurrent_search_args(no_intent=False)
    plan = [_search_item(), dict(_search_item(), effort="deep")]
    facts = {"A_nokg": {"notebook": "nb", "sources": 1, "kg_in_scope": False}}
    runner = rig.Runner(dry_run=False, out_dir=tmp_path)
    common = (args, runner, plan, facts, None, None)
    if concurrency == 1:
        failed = rig._search_loop(
            *common, actor_id="owner", cancel_event=threading.Event(),
        )
    else:
        failed = rig._search_loop_concurrent(
            *common, actor_id="owner", profile=types.SimpleNamespace(id="owner"),
            concurrency=concurrency,
        )

    assert failed == 2
    assert calls == [1], "blocked intent is shared across efforts"
    for row in map(json.loads, (tmp_path / "search.jsonl").read_text().splitlines()):
        assert row["status"] == "failed"
        assert row["has_intent_contract"] is False
        assert row["run_wall_ms"] is None
        assert row["reflect_turns"] == 0
    assert "reason=clarification_required" in (tmp_path / "search-runs.log").read_text()


@pytest.mark.parametrize("concurrency", [1, 2])
def test_search_unexpected_intent_failure_still_propagates(
    tmp_path, monkeypatch, concurrency,
):
    def invalid_intent(*args, **kwargs):
        raise AssertionError("intent invariant")

    monkeypatch.setattr(rig, "_plan_intent", invalid_intent)
    common = (
        _concurrent_search_args(no_intent=False),
        rig.Runner(dry_run=False, out_dir=tmp_path), [_search_item()],
        {"A_nokg": {"notebook": "nb", "sources": 1, "kg_in_scope": False}},
        None, None,
    )
    with pytest.raises(AssertionError, match="intent invariant"):
        if concurrency == 1:
            rig._search_loop(*common, actor_id="owner", cancel_event=threading.Event())
        else:
            rig._search_loop_concurrent(
                *common, actor_id="owner", profile=types.SimpleNamespace(id="owner"),
                concurrency=concurrency,
            )


# ---------------------------------------------------------------------------
# export:按 merge_key 合并,不重复(codex #700 R2 P2)
# ---------------------------------------------------------------------------


def test_export_merge_dedupes_by_merge_key_keeping_the_traced_row(tmp_path):
    """`--reports` 导出的结果级独行与 rig 自己写的 `report-trace.jsonl`
    (同一个 `merge_key`,但带了轨迹)撞号时,合并只留一行——且是带轨迹
    (`trace_steps` 键)的那行,不是逐字节拼接文件、把一份报告的一节报成两个
    run(README 479–481 的「每节一行」不变量)。没有 `merge_key` 的行(纯 Ask
    run)不受影响,原样透传。"""
    ask_runs = tmp_path / "ask-runs.jsonl"
    report_trace = tmp_path / "report-trace.jsonl"
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
            "reflect_turns": 1,
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
    """结果级行如果没有轨迹半份可对(`report-trace.jsonl` 缺失或不含它),
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
# report:清晰契约确认重试 + 门控失败可观测
# ---------------------------------------------------------------------------


def _report_item(question: str) -> dict:
    return {
        "question_key": "t0-report-01", "corpus_cell": "B_kg",
         "depth": 1, "profile": "shallow",
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
    """A clear contract can retry the confirmation entry when auto-confirm fails
    open. No required ambiguity or invented clarification answer is involved.
    """
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

    assert gate_reason is None, "清晰契约确认重试成功，不该再判门控失败"
    detail = rrepo.get_report(nb.id, report_id)
    assert detail["status"] == "done", detail.get("error")

    rows = list(rig._report_rows(
        rrepo, nb.id, report_id, item, captured, gate_reason=gate_reason,
    ))
    assert rows, "确认成功后应该有真实的节可对"
    assert all(row.get("status") != "failed" for row in rows)
    # 报告的投影要带真实上下文,不是 None payload 投出来的 unknown/false
    # (codex #700 R6 P2):depth=1 ⇒ 生产映射 overview;契约已确认 ⇒ True。
    assert all(row["effort"] == "overview" for row in rows)
    assert all(row["has_intent_contract"] is True for row in rows)


def test_report_generate_keeps_required_clarification_blocked(rrepo, monkeypatch):
    from app.services.model_work import ModelPriority, model_work_scope
    from app.services.report_engine import ReportEngine
    from app.services.source_scope import source_scope_context
    from tests.test_report_engine import _AutoRunLLM, _bind_report_llm, _mk_nb

    nb = _mk_nb(rrepo)
    _bind_report_llm(rrepo, _AutoRunLLM(ambiguities=[{
        "id": "load", "question": "使用哪种负载？", "required": True,
        "options": ["请填写负载值"],
    }]))
    _stub_report_generation_at_class_level(monkeypatch)
    monkeypatch.setattr(rrepo.retrieval, "federated_retrieve", lambda a, q: [])
    question = "分析 PLL 稳定性"
    report_id = rrepo.create_report(nb.id, question, depth=1)
    item = _report_item(question)

    captured, gate_reason = rig._generate_report(
        rrepo, ReportEngine, types.SimpleNamespace(id="rig-owner"),
        nb.id, report_id, item,
        model_work_scope=model_work_scope, model_priority=ModelPriority.REPORT,
        source_scope_context=source_scope_context,
    )

    assert gate_reason == "clarification_gate"
    detail = rrepo.get_report(nb.id, report_id)
    assert detail["status"] == "intent_ready"
    assert not detail.get("sections")
    rows = list(rig._report_rows(
        rrepo, nb.id, report_id, item, captured, gate_reason=gate_reason,
    ))
    assert rows and all(row["status"] == "failed" for row in rows)


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


def test_export_joins_only_its_run_logs_and_keeps_retries_separate(tmp_path, monkeypatch):
    from tests.test_reflect_context_bench import _event, _log

    out = tmp_path / "run"
    (out / "llm").mkdir(parents=True)
    (out / "events").mkdir()
    _write_rows(out / "llm" / "llm.jsonl", [
        _log("mdl-one", status="retry", usage=None),
        _log("mdl-one"), _log("mdl-missing", usage=None),
    ])
    _write_rows(out / "events" / "events-today.jsonl", [_event("mdl-one")])
    _write_rows(tmp_path / "unrelated.jsonl", [_log("outside")])
    _write_rows(out / "report-trace.jsonl", [{
        "consumer": "report_section", "merge_key": "section0",
        "trace_steps": 3,
    }])

    def fake_export(argv):
        target = Path(argv[argv.index("--out") + 1])
        _write_rows(target, [{
            "consumer": "report_section", "merge_key": "section0", "attempted": 0,
        }])
        return 0

    monkeypatch.setattr(export, "main", fake_export)
    assert rig.main(["--out-dir", str(out), "export"]) == 0
    rows = [json.loads(line) for line in (out / "calls.jsonl").read_text().splitlines()]
    assert len(rows) == 3
    assert {row["support_id"] for row in rows} == {"mdl-one", "mdl-missing"}
    retry = next(row for row in rows if row["join"] == "retry")
    joined = next(row for row in rows if row["join"] == "joined")
    missing = next(row for row in rows if row["join"] == "log_only")
    assert retry["call_index"] is None
    assert {row["call_index"] for row in rows if row["call_index"] is not None} == {0, 1}
    assert joined["queue_latency_ms"] == 35 and joined["attempts"] == 2
    assert missing["queue_latency_ms"] is None and missing["cached_tokens"] is None
    assert all(row["question_key"] is None for row in rows)
    assert "只看 Qwen" not in (out / "calls.jsonl").read_text()
    merged = [json.loads(line) for line in (out / "t0-dataset.jsonl").read_text().splitlines()]
    assert len(merged) == 1 and merged[0]["trace_steps"] == 3


def test_export_without_logs_writes_no_call_samples(tmp_path, monkeypatch):
    def fake_export(argv):
        _write_rows(Path(argv[argv.index("--out") + 1]), [])
        return 0
    monkeypatch.setattr(export, "main", fake_export)
    (tmp_path / "calls.jsonl").write_text("stale output\n")
    assert rig.main(["--out-dir", str(tmp_path), "export"]) == 0
    assert (tmp_path / "calls.jsonl").read_text() == ""
