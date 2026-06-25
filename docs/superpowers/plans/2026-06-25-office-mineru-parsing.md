# Office（docx/pptx）经 MinerU 解析 + 上传类型锁定 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 docx/pptx 上传走 MinerU 高保真解析（未配置/失败时回退现有轻量解析器），并把前端文件选择从"静默丢弃不支持类型"改为"明确拒绝并提示"。

**Architecture:** 在 `parsers.py` 复刻 PDF 的 MinerU-优先 + 回退模式：`parse_docx`/`parse_pptx` 先试 `MinerUClient.parse()` → 通用 mapper（带 `label_prefix`）→ 失败回退到现有 python-docx/XML 实现。MinerU http 模式发送 office 字节即可（`/file_parse` 原生支持），cli 模式新增 `mineru` CLI 分支。前端把允许列表收敛成单一常量并在 `stageFiles` 做显式拒绝。

**Tech Stack:** Python / FastAPI / pytest（后端），Next.js / React / TypeScript（前端），MinerU（http/cli），python-docx。

参考设计：`docs/superpowers/specs/2026-06-25-office-mineru-parsing-design.md`

---

## 关键文件

- `backend/app/services/parsers.py` — 解析分发与各格式解析器（**主改**）
- `backend/app/services/mineru_client.py` — MinerU http/cli 客户端（cli 加 office 分支）
- `backend/tests/test_parsers_office.py` — 新增，office 解析单测
- `backend/tests/test_mineru_client_cli.py` — 新增，cli 命令路由单测
- `frontend/app/page.tsx` — 上传 UI 与 `stageFiles` 校验
- 调用链（无需改）：`sqlite_repository.py:1311` 已把 `self.mineru_client` 传入 `parse_source_file`，office 元素的 `last_error` 会在 :1314 被捕获。

---

## Task 1: 泛化 `mineru_content_list_to_elements` 的 location 前缀

让 mapper 不再把所有元素硬编码标成 `PDF p.X`，office 可传 `DOCX`/`PPTX`。默认 `"PDF"` 保持现有行为。

**Files:**
- Modify: `backend/app/services/parsers.py`（函数 `mineru_content_list_to_elements`，当前 369-471；其内 5 处 `f"PDF p.{page} ..."`）
- Test: `backend/tests/test_parsers_office.py`（新建）

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/test_parsers_office.py`：

```python
from app.services.parsers import mineru_content_list_to_elements


def _content_list():
    return [
        {"type": "title", "text": "Heading", "text_level": 1, "page_idx": 0},
        {"type": "text", "text": "Body text.", "page_idx": 0},
    ]


def test_mapper_default_prefix_is_pdf():
    els = mineru_content_list_to_elements("s1", _content_list())
    assert els[0].location_label.startswith("PDF p.1")
    assert all(e.metadata.get("parser") == "mineru" for e in els)


def test_mapper_custom_prefix_for_office():
    els = mineru_content_list_to_elements("s1", _content_list(), label_prefix="DOCX")
    assert els[0].location_label.startswith("DOCX p.1")
    assert els[0].metadata.get("source_format") == "docx"
    assert els[0].element_type == "heading"
    assert els[1].element_type == "paragraph"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_parsers_office.py -v`
Expected: FAIL（`mineru_content_list_to_elements() got an unexpected keyword argument 'label_prefix'`）

- [ ] **Step 3: 实现 — 加 `label_prefix` 参数并替换 5 处前缀**

把签名改为：

```python
def mineru_content_list_to_elements(
    source_id: str,
    content_list: List[dict],
    label_prefix: str = "PDF",
) -> List[SourceElement]:
```

将函数体内 5 处 location label 的 `"PDF p.{page} ..."` 改为 `f"{label_prefix} p.{page} ..."`：
- `f"PDF p.{page} block {ordinal}"`（text/title 分支）→ `f"{label_prefix} p.{page} block {ordinal}"`
- `f"PDF p.{page} formula {ordinal}"` → `f"{label_prefix} p.{page} formula {ordinal}"`
- `f"PDF p.{page} table {ordinal}"` → `f"{label_prefix} p.{page} table {ordinal}"`
- `f"PDF p.{page} image {ordinal}"` → `f"{label_prefix} p.{page} image {ordinal}"`
- else 分支的 `f"PDF p.{page} block {ordinal}"` → `f"{label_prefix} p.{page} block {ordinal}"`

并在 **每个** `_element(...)` 调用的 metadata 字典里加入 `"source_format": label_prefix.lower()`（5 处 metadata：text/title、equation、table、image、else，均补这一键）。

- [ ] **Step 4: 跑测试确认通过 + 不回归既有 PDF/云端解析**

Run: `cd backend && python -m pytest tests/test_parsers_office.py tests/test_mineru_cloud_client.py -v`
Expected: PASS（云端测试不传 `label_prefix`，默认 `PDF`，元素 label 不变）

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/parsers.py backend/tests/test_parsers_office.py
git commit -m "feat(parsers): mineru mapper accepts label_prefix for office docs"
```

---

## Task 2: `parse_docx` 走 MinerU（回退 python-docx）+ 分发转发

**Files:**
- Modify: `backend/app/services/parsers.py`（`parse_source_file` 21-22 行；`parse_docx` 134-172）
- Test: `backend/tests/test_parsers_office.py`

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_parsers_office.py` 追加 fake client 与 docx 测试：

```python
from pathlib import Path

from docx import Document

from app.services.parsers import parse_docx, parse_source_file


class FakeMineru:
    """模式无关的假 MinerU 客户端：按构造参数返回 content_list / 抛错 / 报未配置。"""

    def __init__(self, configured=True, content_list=None, raises=None, mode="http"):
        self.configured = configured
        self._content_list = content_list if content_list is not None else []
        self._raises = raises
        self.mode = mode
        self.last_error = ""

    def parse(self, file_path, file_name):
        self.last_error = ""
        if self._raises is not None:
            self.last_error = str(self._raises)
            raise self._raises
        return self._content_list


def _make_docx(path: Path) -> Path:
    doc = Document()
    doc.add_paragraph("Hello from docx.")
    table = doc.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "A"
    table.rows[0].cells[1].text = "B"
    doc.save(str(path))
    return path


def test_docx_uses_mineru_when_configured(tmp_path):
    path = _make_docx(tmp_path / "a.docx")
    client = FakeMineru(content_list=[{"type": "text", "text": "From MinerU.", "page_idx": 0}])
    els = parse_docx("s1", path, "a.docx", client)
    assert any(e.metadata.get("parser") == "mineru" for e in els)
    assert els[0].location_label.startswith("DOCX p.1")


def test_docx_falls_back_when_not_configured(tmp_path):
    path = _make_docx(tmp_path / "a.docx")
    client = FakeMineru(configured=False)
    els = parse_docx("s1", path, "a.docx", client)
    assert all(e.metadata.get("parser") == "docx" for e in els)
    assert any("Hello from docx." in e.text for e in els)


def test_docx_falls_back_on_mineru_error(tmp_path):
    path = _make_docx(tmp_path / "a.docx")
    client = FakeMineru(raises=RuntimeError("mineru boom"))
    els = parse_docx("s1", path, "a.docx", client)  # 不应冒泡异常
    assert all(e.metadata.get("parser") == "docx" for e in els)
    assert client.last_error == "mineru boom"


def test_docx_falls_back_when_mineru_empty(tmp_path):
    path = _make_docx(tmp_path / "a.docx")
    client = FakeMineru(content_list=[])
    els = parse_docx("s1", path, "a.docx", client)
    assert all(e.metadata.get("parser") == "docx" for e in els)


def test_parse_source_file_forwards_client_to_docx(tmp_path):
    path = _make_docx(tmp_path / "a.docx")
    client = FakeMineru(content_list=[{"type": "text", "text": "From MinerU.", "page_idx": 0}])
    els = parse_source_file("s1", str(path), "a.docx", client)
    assert any(e.metadata.get("parser") == "mineru" for e in els)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_parsers_office.py -k docx -v`
Expected: FAIL（`parse_docx()` 目前签名为 `(source_id, path)`，多传参数报 TypeError；且未走 MinerU）

- [ ] **Step 3: 实现 — 把现有 `parse_docx` 改名为回退、新增 MinerU 优先包装**

在 `parsers.py` 中，把当前 `def parse_docx(source_id, path):` 整段（134-172）**改名**为：

```python
def parse_docx_basic(source_id: str, path: Path) -> List[SourceElement]:
```

（函数体不变。）然后**新增**包装函数（放在 `parse_docx_basic` 之前），完全镜像 `parse_pdf`：

```python
def parse_docx(
    source_id: str,
    path: Path,
    file_name: str = "",
    mineru_client: Any = None,
) -> List[SourceElement]:
    """Parse a DOCX via MinerU when configured, else fall back to python-docx.

    MinerU (3.1+) natively parses DOCX with layout/tables/formulas. When it is
    not configured or fails, we degrade to python-docx so local/no-GPU dev works.
    """
    if mineru_client is not None and getattr(mineru_client, "configured", False):
        try:
            content_list = mineru_client.parse(str(path), file_name or path.name)
            elements = mineru_content_list_to_elements(
                source_id, content_list, label_prefix="DOCX"
            )
            if elements:
                return elements
            if hasattr(mineru_client, "last_error"):
                mineru_client.last_error = "MinerU content_list mapped to zero source elements"
        except Exception as exc:
            if hasattr(mineru_client, "last_error") and not getattr(
                mineru_client, "last_error", ""
            ):
                mineru_client.last_error = str(exc)
            # Fall through to python-docx so a MinerU outage never blocks ingestion.
            pass
    return parse_docx_basic(source_id, path)
```

并修改 `parse_source_file`（21-22 行）把 client 与 file_name 转发给 docx：

```python
    if suffix == ".docx":
        return parse_docx(source_id, Path(file_path), file_name, mineru_client)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_parsers_office.py -k docx -v`
Expected: PASS（5 个 docx/forward 用例全绿）

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/parsers.py backend/tests/test_parsers_office.py
git commit -m "feat(parsers): route docx through MinerU with python-docx fallback"
```

---

## Task 3: `parse_pptx` 走 MinerU（回退 XML）+ 分发转发

**Files:**
- Modify: `backend/app/services/parsers.py`（`parse_source_file` 23-24 行；`parse_pptx` 175-254）
- Test: `backend/tests/test_parsers_office.py`

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_parsers_office.py` 追加 pptx 测试（含最小 .pptx 构造，复用 Task 2 的 `FakeMineru`）：

```python
import zipfile

from app.services.parsers import parse_pptx

_SLIDE_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
    ' xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
    "<p:cSld><p:spTree><p:sp><p:txBody>"
    "<a:p><a:r><a:t>{text}</a:t></a:r></a:p>"
    "</p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
)


def _make_pptx(path: Path, slide_texts) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for index, text in enumerate(slide_texts, start=1):
            archive.writestr(f"ppt/slides/slide{index}.xml", _SLIDE_XML.format(text=text))
    return path


def test_pptx_uses_mineru_when_configured(tmp_path):
    path = _make_pptx(tmp_path / "a.pptx", ["Slide one body"])
    client = FakeMineru(content_list=[{"type": "text", "text": "From MinerU.", "page_idx": 0}])
    els = parse_pptx("s1", path, "a.pptx", client)
    assert any(e.metadata.get("parser") == "mineru" for e in els)
    assert els[0].location_label.startswith("PPTX p.1")


def test_pptx_falls_back_when_not_configured(tmp_path):
    path = _make_pptx(tmp_path / "a.pptx", ["Slide one body"])
    client = FakeMineru(configured=False)
    els = parse_pptx("s1", path, "a.pptx", client)
    assert all(e.metadata.get("parser") == "pptx" for e in els)
    assert any("Slide one body" in e.text for e in els)


def test_pptx_falls_back_on_mineru_error(tmp_path):
    path = _make_pptx(tmp_path / "a.pptx", ["Slide one body"])
    client = FakeMineru(raises=RuntimeError("mineru boom"))
    els = parse_pptx("s1", path, "a.pptx", client)
    assert all(e.metadata.get("parser") == "pptx" for e in els)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_parsers_office.py -k pptx -v`
Expected: FAIL（`parse_pptx()` 当前签名 `(source_id, path)`，多传参数 TypeError）

- [ ] **Step 3: 实现 — 镜像 Task 2**

把当前 `def parse_pptx(source_id, path):`（175-254）**改名**为：

```python
def parse_pptx_basic(source_id: str, path: Path) -> List[SourceElement]:
```

（函数体不变。）**新增**包装（放在 `parse_pptx_basic` 之前）：

```python
def parse_pptx(
    source_id: str,
    path: Path,
    file_name: str = "",
    mineru_client: Any = None,
) -> List[SourceElement]:
    """Parse a PPTX via MinerU when configured, else fall back to XML extraction.

    MinerU (3.0+) natively parses PPTX. When it is not configured or fails, we
    degrade to the raw-XML slide/notes extractor so local/no-GPU dev works.
    """
    if mineru_client is not None and getattr(mineru_client, "configured", False):
        try:
            content_list = mineru_client.parse(str(path), file_name or path.name)
            elements = mineru_content_list_to_elements(
                source_id, content_list, label_prefix="PPTX"
            )
            if elements:
                return elements
            if hasattr(mineru_client, "last_error"):
                mineru_client.last_error = "MinerU content_list mapped to zero source elements"
        except Exception as exc:
            if hasattr(mineru_client, "last_error") and not getattr(
                mineru_client, "last_error", ""
            ):
                mineru_client.last_error = str(exc)
            # Fall through to XML extraction so a MinerU outage never blocks ingestion.
            pass
    return parse_pptx_basic(source_id, path)
```

并修改 `parse_source_file`（23-24 行）：

```python
    if suffix == ".pptx":
        return parse_pptx(source_id, Path(file_path), file_name, mineru_client)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_parsers_office.py -v`
Expected: PASS（docx + pptx + mapper 全部）

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/parsers.py backend/tests/test_parsers_office.py
git commit -m "feat(parsers): route pptx through MinerU with XML fallback"
```

---

## Task 4: MinerU cli 模式支持 office（走 `mineru` CLI）

http 模式发送 office 字节到 `/file_parse` 已原生支持，**无需改动**（部署默认路径）。cli 模式现有 `_DO_PARSE_SCRIPT` 是 PDF 语义，office 改走确认支持的 `mineru -p <file> -o <dir>` 命令，复用现有 `*_content_list.json` 发现与超时逻辑。

**Files:**
- Modify: `backend/app/services/mineru_client.py`（`_parse_cli` 92-140）
- Test: `backend/tests/test_mineru_client_cli.py`（新建）

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/test_mineru_client_cli.py`，monkeypatch `subprocess.Popen`，断言 .docx 走 `mineru` CLI、.pdf 仍走 do_parse 脚本，且都能从 out_dir 读回 content_list：

```python
import json
import subprocess
from pathlib import Path

from app.core.config import Settings
from app.services.mineru_client import MinerUClient


def _cli_client(monkeypatch):
    # 仓库约定：用 setenv + Settings() 构造（pydantic-settings 读 env，非 kwargs）。
    monkeypatch.setenv("MINERU_MODE", "cli")
    monkeypatch.setenv("MINERU_BACKEND", "pipeline")
    monkeypatch.setenv("MINERU_LANG", "ch")
    monkeypatch.setenv("MINERU_MODEL_SOURCE", "huggingface")
    monkeypatch.setenv("MINERU_TIMEOUT_SECONDS", "30")
    return MinerUClient(Settings())


class FakePopen:
    """假子进程：把 content_list.json 写进命令里 -o / out_dir 指向的目录。"""

    captured_cmd = []

    def __init__(self, cmd, **kwargs):
        FakePopen.captured_cmd = cmd
        out_dir = Path(cmd[cmd.index("-o") + 1]) if "-o" in cmd else _out_dir_from_pdf_cmd(cmd)
        (out_dir / "doc_content_list.json").write_text(
            json.dumps([{"type": "text", "text": "ok", "page_idx": 0}]), encoding="utf-8"
        )

    def communicate(self, timeout=None):
        return (b"", b"")

    @property
    def returncode(self):
        return 0


def _out_dir_from_pdf_cmd(cmd):
    # PDF 路径命令是 [python, script, config.json]；out_dir 写在 config 里。
    config = json.loads(Path(cmd[-1]).read_text(encoding="utf-8"))
    return Path(config["out_dir"])


def test_cli_office_uses_mineru_command(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    docx = tmp_path / "doc.docx"
    docx.write_bytes(b"PK\x03\x04stub")
    out = _cli_client(monkeypatch).parse(str(docx), "doc.docx")
    assert FakePopen.captured_cmd[0] == "mineru"
    assert "-p" in FakePopen.captured_cmd and "-o" in FakePopen.captured_cmd
    assert out == [{"type": "text", "text": "ok", "page_idx": 0}]


def test_cli_pdf_still_uses_do_parse_script(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 stub")
    out = _cli_client(monkeypatch).parse(str(pdf), "doc.pdf")
    assert FakePopen.captured_cmd[0] != "mineru"  # [python, run_mineru_parse.py, config.json]
    assert FakePopen.captured_cmd[1].endswith("run_mineru_parse.py")
    assert out == [{"type": "text", "text": "ok", "page_idx": 0}]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_mineru_client_cli.py -v`
Expected: FAIL（office 当前也走 do_parse 脚本，`captured_cmd[0]` 不是 `"mineru"`，且 PDF 命名的脚本对 .docx 不会产出 content_list → RuntimeError）

- [ ] **Step 3: 实现 — `_parse_cli` 按后缀选择命令，共用 Popen/超时/发现逻辑**

把 `_parse_cli`（92-140）重构为：先按后缀决定 `command`，再共用其余逻辑。将原本写 `run_mineru_parse.py` + config 并返回 `[sys.executable, script, config]` 的部分抽到 `_pdf_cli_command`，新增 `_office_cli_command`：

```python
    def _parse_cli(self, file_path: str, file_name: str) -> List[dict]:
        suffix = Path(file_name).suffix.lower()
        with tempfile.TemporaryDirectory(prefix="mineru-") as out_dir:
            if suffix in {".docx", ".pptx"}:
                command = self._office_cli_command(file_path, out_dir)
            else:
                command = self._pdf_cli_command(file_path, file_name, out_dir)
            env = {**os.environ}
            if self.settings.mineru_model_source:
                env["MINERU_MODEL_SOURCE"] = self.settings.mineru_model_source
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                start_new_session=(os.name != "nt"),
            )
            try:
                stdout, stderr = process.communicate(
                    timeout=self.settings.mineru_timeout_seconds
                )
            except subprocess.TimeoutExpired as exc:
                stdout, stderr = _terminate_process(process)
                detail = _tail_process_output(stdout, stderr)
                suffix_msg = f": {detail}" if detail else ""
                raise RuntimeError(
                    f"MinerU Python API timed out after "
                    f"{self.settings.mineru_timeout_seconds}s{suffix_msg}"
                ) from exc
            if process.returncode != 0:
                detail = _tail_process_output(stdout, stderr)
                raise RuntimeError(f"MinerU Python API failed: {detail}")
            matches = sorted(Path(out_dir).rglob("*_content_list.json"))
            if not matches:
                raise RuntimeError("MinerU Python API produced no content_list.json")
            return json.loads(matches[0].read_text(encoding="utf-8"))

    def _office_cli_command(self, file_path: str, out_dir: str) -> List[str]:
        # MinerU CLI 原生解析 office：mineru -p <file> -o <dir> -l <lang> -b <backend>
        return [
            "mineru",
            "-p", str(file_path),
            "-o", str(out_dir),
            "-l", self.settings.mineru_lang or "ch",
            "-b", self.settings.mineru_backend or "pipeline",
        ]

    def _pdf_cli_command(self, file_path: str, file_name: str, out_dir: str) -> List[str]:
        config = {
            "file_path": file_path,
            "file_name": file_name,
            "out_dir": out_dir,
            "backend": self.settings.mineru_backend,
            "parse_method": self.settings.mineru_parse_method or "auto",
            "lang": self.settings.mineru_lang or "ch",
            "formula_enable": self.settings.mineru_formula_enable,
            "table_enable": self.settings.mineru_table_enable,
            "model_source": self.settings.mineru_model_source,
            "vlm_server_url": self.settings.mineru_vlm_server_url,
        }
        script_path = Path(out_dir) / "run_mineru_parse.py"
        config_path = Path(out_dir) / "mineru_config.json"
        script_path.write_text(_DO_PARSE_SCRIPT, encoding="utf-8")
        config_path.write_text(json.dumps(config), encoding="utf-8")
        return [sys.executable, str(script_path), str(config_path)]
```

注：保留原 `_DO_PARSE_SCRIPT` 常量不动。PDF 行为完全不变（仍走 do_parse 脚本）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_mineru_client_cli.py -v`
Expected: PASS（office→mineru CLI；pdf→do_parse 脚本）

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/mineru_client.py backend/tests/test_mineru_client_cli.py
git commit -m "feat(mineru): cli mode parses docx/pptx via mineru CLI"
```

---

## Task 5: 前端单一允许列表 + `stageFiles` 显式拒绝

**Files:**
- Modify: `frontend/app/page.tsx`（模块常量插在 `API_BASE` 后即 35 行后；`accept` 在 2366 与 2739；`stageFiles` 1424-1449）

- [ ] **Step 1: 加模块级常量（单一事实来源）**

在 `frontend/app/page.tsx` 第 35 行 `const API_BASE = ...` 之后插入：

```ts
// 上传支持的扩展名（单一事实来源）：accept 串与 stageFiles 校验都从此派生。
const SUPPORTED_SOURCE_EXTENSIONS: string[] = [
  "pdf", "md", "markdown", "docx", "pptx", "csv", "xlsx", "xlsm",
];
const SUPPORTED_SOURCE_ACCEPT = SUPPORTED_SOURCE_EXTENSIONS.map((ext) => `.${ext}`).join(",");
// 旧版二进制 Office 不被 MinerU 支持，给专门提示引导用户另存为 OOXML。
const LEGACY_OFFICE_EXTENSIONS = ["doc", "ppt", "xls"];

function fileExtension(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot >= 0 ? name.slice(dot + 1).toLowerCase() : "";
}
```

- [ ] **Step 2: 两处 `accept` 改用常量**

`page.tsx:2366` 和 `page.tsx:2739` 的：

```tsx
<input type="file" multiple accept=".pdf,.md,.markdown,.docx,.pptx,.csv,.xlsx,.xlsm" onChange={stageFiles} />
```

均改为：

```tsx
<input type="file" multiple accept={SUPPORTED_SOURCE_ACCEPT} onChange={stageFiles} />
```

- [ ] **Step 3: 重写 `stageFiles`（1424-1449）为显式拒绝**

```tsx
  function stageFiles(event: ChangeEvent<HTMLInputElement>) {
    const all = Array.from(event.target.files || []);
    event.target.value = "";
    const picked = all.filter((file) => SUPPORTED_SOURCE_EXTENSIONS.includes(fileExtension(file.name)));
    const rejected = all.filter((file) => !SUPPORTED_SOURCE_EXTENSIONS.includes(fileExtension(file.name)));
    if (rejected.length > 0) {
      const names = rejected.map((file) => file.name).join("、");
      const hasLegacy = rejected.some((file) => LEGACY_OFFICE_EXTENSIONS.includes(fileExtension(file.name)));
      const hint = hasLegacy
        ? "旧版 Office 格式请另存为 .docx / .pptx / .xlsx"
        : "支持：PDF / Word(.docx) / PPT(.pptx) / Excel(.xlsx,.xlsm) / Markdown / CSV";
      setToast(`已跳过不支持的文件：${names}。${hint}`);
    }
    if (picked.length === 0) {
      return;
    }
    // 追加而非覆盖（"继续添加文件"语义）；按 name+size 去重，避免重复入列。
    const merged = [...stagedFiles];
    const mergedTypes = [...stagedDocTypes];
    const added: File[] = [];
    for (const file of picked) {
      if (!merged.some((existing) => existing.name === file.name && existing.size === file.size)) {
        merged.push(file);
        mergedTypes.push("");
        added.push(file);
      }
    }
    setStagedFiles(merged);
    setStagedDocTypes(mergedTypes);
    setSourceModalOpen(true);
    // 对新增的文本类文件做内容检测，预填类型下拉（异步，不阻塞 UI；用户仍可改）。
    void detectStagedTypes(added, merged);
  }
```

- [ ] **Step 4: 类型检查 + 构建**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无报错。

- [ ] **Step 5: 提交**

```bash
git add frontend/app/page.tsx
git commit -m "feat(upload): single source-ext list + explicit rejection of unsupported files"
```

---

## Task 6: 全量验证（后端测试 + 前端类型 + 真机/preview 走查）

**Files:** 无（仅运行与人工验证）

- [ ] **Step 1: 后端全量测试**

Run: `cd backend && python -m pytest -q`
Expected: 全绿（含新增 office / cli 测试，无既有回归）

- [ ] **Step 2: 前端类型检查**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无报错。

- [ ] **Step 3: 前端拒绝提示走查（preview，按 preview_* 工作流）**

启动 preview，在"添加来源"上传弹窗中：
- 选一个 `.txt`/`.zip` → 期望 toast「已跳过不支持的文件：…。支持：…」，不入列。
- 选一个 `.doc` → 期望 toast 含「旧版 Office 格式请另存为 .docx / .pptx / .xlsx」。
- 混选 `.docx` + `.zip` → `.docx` 入列、`.zip` 被跳过并提示。
用 preview_snapshot / preview_screenshot 留证。

- [ ] **Step 4: office→MinerU 真机验证（需已配置 MinerU 的部署，人工）**

> 本机不启服务、不本地起模型（见用户偏好）。此步在已配置 `MINERU_MODE=http` 且 `mineru-api` 可达的部署上，由用户或具备环境的 agent 执行：上传一个真实 .docx 与 .pptx，确认 `source_elements` 的 `parser=="mineru"`、`location_label` 前缀为 `DOCX/PPTX`；MinerU 关闭时确认回退为 `parser=="docx"/"pptx"`。
> cli 模式 office 命令（`mineru -p ... -o ...`）的真机产出亦在此步确认（单测已锁命令路由，但未跑真实 MinerU）。

- [ ] **Step 5: 收尾提交（如走查中有微调）**

```bash
git add -A && git commit -m "test: office mineru parsing verification notes"  # 若无改动可跳过
```

---

## 自检对照（spec 覆盖）

- spec ① 后端解析流程 → Task 1（mapper 前缀）、Task 2（docx）、Task 3（pptx）、分发转发在 Task 2/3。
- spec ② MinerU 客户端 → Task 4（http 无需改、cli 加 office 分支、云端不动）。
- spec ③ 前端锁定 → Task 5（单一常量 + 两处 accept + stageFiles 显式拒绝 + 遗留提示）。
- spec ④ 错误与回退 → Task 2/3 的 try/except 回退 + `last_error`；后端 400 既有；前端提示在 Task 5。
- spec ⑤ 测试 → Task 1-4 单测 + Task 6 全量与走查。
- spec ⑥ 不做项 → 计划未触碰 xlsx/图片/.doc 解析/office-URL/异步/新开关。

## 实现者注意

- 回退函数命名：`parse_docx_basic` / `parse_pptx_basic`（与 `parse_pdf_pypdf` 同构）。包装函数保留原名 `parse_docx` / `parse_pptx`，供 `parse_source_file` 调用。
- mapper 默认 `label_prefix="PDF"`，云端/本地 PDF 路径行为不变。
- cli office 命令首元素是字符串 `"mineru"`（控制台脚本，随 MinerU 包安装在同环境 PATH）；PDF 路径首元素是 `sys.executable`。
- 前端 `SUPPORTED_SOURCE_EXTENSIONS` 须与后端 `SUPPORTED_SOURCE_SUFFIXES`（`backend/app/api/routes.py:71`，去掉点号）保持一致——若改一处记得同步另一处。
