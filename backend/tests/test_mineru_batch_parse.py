"""冒烟测试：`scripts/mineru_batch_parse.py`（部署侧独立工具，不属于 app 包）。

按文件路径直接 import 该脚本（它不是包的一部分），用可编排的 fake session
（.post/.get 返回按 URL 后缀脚本化的 fake response）驱动 submit→poll→fetch
三段式，全程不打真实网络。
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys
import threading

import pytest
import requests

_SCRIPT_PATH = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "mineru_batch_parse.py"
_spec = importlib.util.spec_from_file_location("mineru_batch_parse", _SCRIPT_PATH)
mbp = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
# 注册进 sys.modules：Python 3.13 下 `from __future__ import annotations` 配合
# dataclasses 的类型检查会反查 sys.modules[cls.__module__]，不注册会 AttributeError。
sys.modules["mineru_batch_parse"] = mbp
_spec.loader.exec_module(mbp)

# 轮询/退避不真睡
mbp._sleep = lambda seconds: None


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeResponse:
    """最小 requests.Response 替身：.json()/.raise_for_status()/.status_code/.text"""

    def __init__(self, json_data=None, status_code=200, text=None):
        self._json_data = json_data
        self.status_code = status_code
        self.text = text if text is not None else json.dumps(json_data or {})

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error for url")


class FakeSession:
    """按 URL 后缀脚本化响应；每条记录可以是单个 FakeResponse（每次调用重复返回）
    或一个 list（按序弹出，弹到只剩一个后粘住最后一个，方便"轮询多次终态"场景）。"""

    def __init__(self, script):
        self.script = script
        self.calls = []

    def post(self, url, files=None, data=None, timeout=None):
        self.calls.append(("POST", url, data))
        return self._resolve(url)

    def get(self, url, timeout=None):
        self.calls.append(("GET", url))
        return self._resolve(url)

    def _resolve(self, url):
        for suffix, resp in self.script.items():
            if url.endswith(suffix):
                if isinstance(resp, list):
                    if len(resp) > 1:
                        return resp.pop(0)
                    return resp[0]
                return resp
        raise AssertionError(f"unscripted call: {url}")


def _make_server(session, **overrides):
    kwargs = dict(
        capacity=2,
        submit_timeout=30,
        result_timeout=30,
        poll_interval=0,
        max_poll_seconds=5,
        form_fields={
            "backend": "pipeline",
            "lang_list": "ch",
            "formula_enable": "true",
            "table_enable": "true",
            "return_md": "true",
        },
    )
    kwargs.update(overrides)
    return mbp.MinerUServer("http://mineru-host:8000", session, **kwargs)


# ---------------------------------------------------------------------------
# 1. extract_md —— 纯函数表驱动测试
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload,expected",
    [
        # results 是 dict：{key: item}，item 带 md_content
        ({"results": {"a.pdf": {"md_content": "# Hello dict"}}}, "# Hello dict"),
        # results 是 list：[item]，item 带 md
        ({"results": [{"md": "# Hello list"}]}, "# Hello list"),
        # 整个 payload 是 JSON 字符串，需要先 json.loads
        (json.dumps({"results": {"a.pdf": {"md_content": "# From string"}}}), "# From string"),
        # 空/缺失 → ""
        ({}, ""),
        ({"results": {}}, ""),
        ({"results": []}, ""),
        ({"results": {"a.pdf": {"foo": "bar"}}}, ""),
        (None, ""),
    ],
)
def test_extract_md_table(payload, expected):
    assert mbp.extract_md(payload) == expected


# ---------------------------------------------------------------------------
# 2. load_dotenv
# ---------------------------------------------------------------------------


def test_load_dotenv_parses_and_respects_existing_env(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# 这是注释\n"
        "\n"
        "MINERU_BATCH_LANG=ch\n"
        "MINERU_BATCH_BACKEND=\"pipeline\"\n"
        "MINERU_BATCH_SRC_DIR='/data/pdfs'\n"
        "ALREADY_SET=from_file\n",
        encoding="utf-8",
    )
    for key in ("MINERU_BATCH_LANG", "MINERU_BATCH_BACKEND", "MINERU_BATCH_SRC_DIR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ALREADY_SET", "from_env")

    mbp.load_dotenv(env_file)

    assert os.environ["MINERU_BATCH_LANG"] == "ch"
    assert os.environ["MINERU_BATCH_BACKEND"] == "pipeline"
    assert os.environ["MINERU_BATCH_SRC_DIR"] == "/data/pdfs"
    assert os.environ["ALREADY_SET"] == "from_env"  # 真实环境变量优先，不被覆盖


def test_load_dotenv_missing_file_is_noop(tmp_path):
    mbp.load_dotenv(tmp_path / "does_not_exist.env")  # 不应抛异常


@pytest.mark.parametrize(
    "line,key,expected",
    [
        # 无引号值 + 行内注释（# 前有空格）→ 注释被截掉
        ("MINERU_BATCH_CONCURRENCY_PER_SERVER=0   # 0 = auto from health", "MINERU_BATCH_CONCURRENCY_PER_SERVER", "0"),
        # 值全是空白 + 注释（等号右边只有注释）→ 空字符串，而不是字面 "# ..."
        ("MINERU_BATCH_MANIFEST=   # empty -> default path", "MINERU_BATCH_MANIFEST", ""),
        # 带引号的值内部的 # 必须原样保留，不当注释处理
        ('MINERU_BATCH_LANG="a # b"', "MINERU_BATCH_LANG", "a # b"),
        # 不带引号但 # 前面没有空白（紧贴在其他字符上，如 URL fragment）→ 保留原样
        ("MINERU_BATCH_SRC_DIR=http://h/x#frag", "MINERU_BATCH_SRC_DIR", "http://h/x#frag"),
    ],
)
def test_load_dotenv_strips_inline_comments(tmp_path, monkeypatch, line, key, expected):
    env_file = tmp_path / ".env"
    env_file.write_text(line + "\n", encoding="utf-8")
    monkeypatch.delenv(key, raising=False)

    mbp.load_dotenv(env_file)

    assert os.environ[key] == expected


# ---------------------------------------------------------------------------
# 3/4/5. process_file —— happy path / skip / fail
# ---------------------------------------------------------------------------


def test_process_file_happy_path(tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake pdf content")
    out_md = tmp_path / "out" / "a.md"

    script = {
        "/tasks": FakeResponse({"task_id": "task-1"}, status_code=202),
        "/tasks/task-1": [
            FakeResponse({"status": "running", "error": ""}),
            FakeResponse({"status": "completed", "error": ""}),
        ],
        "/tasks/task-1/result": FakeResponse({"results": {"a.pdf": {"md_content": "# Parsed OK"}}}),
    }
    session = FakeSession(script)
    server = _make_server(session)
    cfg = mbp.Config(src_dir=str(tmp_path), retry_max=3)

    record = mbp.process_file(server, pdf, cfg, out_md)

    assert record["status"] == "ok"
    assert record["attempts"] == 1
    assert record["task_id"] == "task-1"
    assert record["rel"] == "a.pdf"
    assert record["error"] == ""
    assert out_md.read_text(encoding="utf-8") == "# Parsed OK"
    # submit once, poll twice, fetch result once
    posts = [c for c in session.calls if c[0] == "POST"]
    gets = [c for c in session.calls if c[0] == "GET"]
    assert len(posts) == 1
    assert len(gets) == 3


def test_process_file_skip_when_output_already_exists(tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake pdf content")
    out_md = tmp_path / "out" / "a.md"
    out_md.parent.mkdir(parents=True)
    out_md.write_text("x" * 200, encoding="utf-8")  # > 100 bytes

    session = FakeSession({})  # 任何调用都应报错——不该发生
    server = _make_server(session)
    cfg = mbp.Config(src_dir=str(tmp_path))

    record = mbp.process_file(server, pdf, cfg, out_md)

    assert record["status"] == "skip"
    assert session.calls == []


def test_process_file_fails_after_retry_max_exhausted(tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake pdf content")
    out_md = tmp_path / "out" / "a.md"

    script = {
        "/tasks": FakeResponse({"task_id": "task-x"}),
        "/tasks/task-x": FakeResponse({"status": "failed", "error": "解析引擎崩溃"}),
    }
    session = FakeSession(script)
    server = _make_server(session)
    cfg = mbp.Config(src_dir=str(tmp_path), retry_max=3)

    record = mbp.process_file(server, pdf, cfg, out_md)

    assert record["status"] == "fail"
    assert record["attempts"] == 3
    assert "解析引擎崩溃" in record["error"]
    assert not out_md.exists()


def test_process_file_fail_record_does_not_leak_stale_task_id(tmp_path):
    """attempt 1 拿到 task_id 后轮询报 failed；attempt 2 的 submit() 本身抛异常
    （从未拿到新 task_id）。最终 fail 记录的 task_id 必须是空串，不能残留
    attempt 1 的旧 task_id——否则账本会误导人去查一个早已作废的 task。"""
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake pdf content")
    out_md = tmp_path / "out" / "a.md"

    script = {
        "/tasks": [
            FakeResponse({"task_id": "task-1"}, status_code=202),
            FakeResponse({}, status_code=500, text="boom"),
        ],
        "/tasks/task-1": FakeResponse({"status": "failed", "error": "engine crashed"}),
    }
    session = FakeSession(script)
    server = _make_server(session)
    cfg = mbp.Config(src_dir=str(tmp_path), retry_max=2)

    record = mbp.process_file(server, pdf, cfg, out_md)

    assert record["status"] == "fail"
    assert record["attempts"] == 2
    assert record["task_id"] == ""


def test_process_file_completed_with_empty_markdown_fails_without_burning_retries(tmp_path):
    """server 报 terminal completed，但 extract_md 只拿到空白 markdown（扫描件/
    图片 PDF，或未识别的结果形状）：这是确定性结果——必须立刻判 fail、不写文件、
    不再重试消耗剩余 attempts（重试不会让扫描件突然出现文字）。"""
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake pdf content")
    out_md = tmp_path / "out" / "a.md"

    script = {
        "/tasks": FakeResponse({"task_id": "task-empty"}, status_code=202),
        "/tasks/task-empty": FakeResponse({"status": "completed", "error": ""}),
        "/tasks/task-empty/result": FakeResponse(
            {"results": {"a.pdf": {"md_content": "   \n  "}}}
        ),
    }
    session = FakeSession(script)
    server = _make_server(session)
    cfg = mbp.Config(src_dir=str(tmp_path), retry_max=3)

    record = mbp.process_file(server, pdf, cfg, out_md)

    assert record["status"] == "fail"
    assert record["attempts"] == 1  # 确定性失败：不应该烧掉 retry_max=3 的其余尝试
    assert "empty markdown" in record["error"]
    assert not out_md.exists()
    posts = [c for c in session.calls if c[0] == "POST"]
    assert len(posts) == 1  # 只 submit 了一次，没有重试


# ---------------------------------------------------------------------------
# 6. _select_files —— 续跑过滤："done" 只等于 {"ok"}，fail/cancelled 都要重跑
# ---------------------------------------------------------------------------


def test_select_files_retries_cancelled_and_fail_but_not_ok(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    for name in ("a.pdf", "b.pdf", "c.pdf"):
        (src / name).write_bytes(b"%PDF-1.4 fake pdf content")
    out = tmp_path / "out"
    out.mkdir()
    manifest = out / "_manifest.jsonl"
    manifest.write_text(
        "\n".join(
            json.dumps(rec)
            for rec in (
                {"rel": "a.pdf", "status": "ok"},
                {"rel": "b.pdf", "status": "cancelled"},
                {"rel": "c.pdf", "status": "fail"},
            )
        )
        + "\n",
        encoding="utf-8",
    )
    cfg = mbp.Config(src_dir=str(src), out_dir=str(out))

    files = mbp._select_files(cfg)

    rels = {mbp._rel(p, cfg.src_dir) for p in files}
    assert rels == {"b.pdf", "c.pdf"}  # 'ok' 被排除续跑队列；cancelled/fail 都要重跑


# ---------------------------------------------------------------------------
# 7. run() —— 编排级覆盖（补 M4 缺口，同时钉住 Important #1 / #2）
# ---------------------------------------------------------------------------


def test_run_processes_multiple_files_and_writes_manifest_and_md(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    for name in ("a.pdf", "b.pdf", "c.pdf"):
        (src / name).write_bytes(b"%PDF-1.4 fake pdf content")
    out = tmp_path / "out"

    # 所有文件共享同一个（stateless、线程安全的）fake task：三个文件并发跑，
    # 谁先谁后不重要，每次 submit/poll/fetch 都返回同样的终态响应。
    script = {
        "/tasks": FakeResponse({"task_id": "task-shared"}, status_code=202),
        "/tasks/task-shared": FakeResponse({"status": "completed", "error": ""}),
        "/tasks/task-shared/result": FakeResponse(
            {"results": {"x": {"md_content": "# Parsed OK"}}}
        ),
    }
    session = FakeSession(script)
    server = _make_server(session, capacity=2)
    cfg = mbp.Config(src_dir=str(src), out_dir=str(out), retry_max=2)

    stats = mbp.run(cfg, [server])

    assert stats == {"total": 3, "ok": 3, "skip": 0, "fail": 0, "cancelled": 0}

    manifest_lines = (out / "_manifest.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(manifest_lines) == 3
    records = [json.loads(line) for line in manifest_lines]
    assert {r["rel"] for r in records} == {"a.pdf", "b.pdf", "c.pdf"}
    assert {r["status"] for r in records} == {"ok"}

    for name in ("a.pdf", "b.pdf", "c.pdf"):
        md_path = (out / name).with_suffix(".md")
        assert md_path.read_text(encoding="utf-8") == "# Parsed OK"

    assert not (out / "failed.txt").exists()


def test_run_with_preset_stop_event_dispatches_no_new_work(tmp_path):
    """stop_event 在 run() 开跑前就已经 set()（模拟 Ctrl-C 已经落地）：所有文件
    都必须被短路成 cancelled——绝不发起任何 HTTP 调用、绝不写任何 .md 输出，但
    manifest 里每个文件仍然要有一条 cancelled 记录（可审计）。"""
    src = tmp_path / "src"
    src.mkdir()
    for name in ("a.pdf", "b.pdf"):
        (src / name).write_bytes(b"%PDF-1.4 fake pdf content")
    out = tmp_path / "out"

    session = FakeSession({})  # 任何 HTTP 调用都应报错——不该发生
    server = _make_server(session, capacity=2)
    cfg = mbp.Config(src_dir=str(src), out_dir=str(out), retry_max=2)

    stop_event = threading.Event()
    stop_event.set()

    stats = mbp.run(cfg, [server], stop_event=stop_event)

    assert stats == {"total": 2, "ok": 0, "skip": 0, "fail": 0, "cancelled": 2}
    assert session.calls == []  # 没有发起任何 HTTP 调用

    manifest_lines = (out / "_manifest.jsonl").read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in manifest_lines]
    assert len(records) == 2
    assert {r["rel"] for r in records} == {"a.pdf", "b.pdf"}
    assert {r["status"] for r in records} == {"cancelled"}

    assert not list(out.rglob("*.md"))  # 没有写出任何输出文件
    assert not (out / "failed.txt").exists()  # cancelled 不是 failure，不入账 failed.txt


def test_run_marks_completed_empty_markdown_as_fail_in_manifest_and_failed_txt(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.pdf").write_bytes(b"%PDF-1.4 fake pdf content")
    out = tmp_path / "out"

    script = {
        "/tasks": FakeResponse({"task_id": "task-empty"}, status_code=202),
        "/tasks/task-empty": FakeResponse({"status": "completed", "error": ""}),
        "/tasks/task-empty/result": FakeResponse({"results": {"a.pdf": {"md_content": "   "}}}),
    }
    session = FakeSession(script)
    server = _make_server(session, capacity=1)
    cfg = mbp.Config(src_dir=str(src), out_dir=str(out), retry_max=3)

    stats = mbp.run(cfg, [server])

    assert stats == {"total": 1, "ok": 0, "skip": 0, "fail": 1, "cancelled": 0}

    manifest_lines = (out / "_manifest.jsonl").read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in manifest_lines]
    assert len(records) == 1
    assert records[0]["status"] == "fail"
    assert "empty markdown" in records[0]["error"]
    assert records[0]["attempts"] == 1  # 确定性失败，没有烧掉 retry_max=3

    assert not (out / "a.md").exists()

    failed_txt = (out / "failed.txt").read_text(encoding="utf-8")
    assert "a.pdf" in failed_txt


# ---------------------------------------------------------------------------
# 8. Config.from_env_and_args —— 非法整数环境变量给清晰报错，而不是裸 traceback
# ---------------------------------------------------------------------------


def test_from_env_and_args_bad_int_env_reports_key_and_exits(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MINERU_BATCH_POLL_INTERVAL", "not-a-number")
    args = mbp._parse_args(["--src", str(tmp_path / "src"), "--out", str(tmp_path / "out")])

    with pytest.raises(SystemExit) as exc_info:
        mbp.Config.from_env_and_args(args)

    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "MINERU_BATCH_POLL_INTERVAL" in err
    assert "not-a-number" in err
