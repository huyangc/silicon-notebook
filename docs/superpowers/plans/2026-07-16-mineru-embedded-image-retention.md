# 保留 pdf/word/ppt 内嵌图片（MinerU 抽图落盘 + 前端展示）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 MinerU 从 pdf/docx/pptx 抽出的内嵌图片落盘成可见资产并在前端来源正文里内联渲染，图注/文字继续进检索；不接受裸图上传、不做独立检索单元。

**Architecture:** 三种 MinerU 模式（http / cli / cloud）的 `parse` 除 content_list 外再带回 `{图片名: bytes}`；`mineru_content_list_to_elements` 的 image 分支停止丢图、经注入的 `persist_image` 回调把图片存进复用的 `AssetService` 资产层（`notebook_assets` 加 `source_id` 列关联来源）、在元素 metadata 里盖 `asset_id`；前端 `ElementBody` 加 image 分支，用带鉴权的 blob fetch 渲染 `<img>`。图片持久化工厂建在 facade 层（有 `AssetService(self)`），按源注入 `SourceIngestionService`，覆盖本地文件 / URL 本地 / URL 云端三条解析路径，并在重解析/删源时级联清理。

**Tech Stack:** Python 3.13 / FastAPI / SQLite（自研迁移）/ pydantic-settings v2 / Next.js + React（TS）/ pytest / node --test（*.test.mjs）。

## Global Constraints

- 图片保留是**纯增量**：任何抽图/写盘/解码失败都不得影响既有文本解析产出（pdf→pypdf、docx→python-docx、pptx→XML 的回退路径与文本行为不变）。
- `MINERU_RETURN_IMAGES=0` 时全链路退回「只文字/caption」，零图、零新写盘。
- 不改文件类型分派、不动 `SUPPORTED_SOURCE_SUFFIXES`、不接受裸图上传。
- schema 变更必须**追加** `_migration_19` 并 bump `SCHEMA_VERSION=19`（`backend/app/repositories/sqlite/migrations.py`），迁移循环 `for version in range(current+1, SCHEMA_VERSION+1)`（migrations.py:1516）自动调用；ALTER 带列存在性守卫可重入。禁止改已封版的 `_migration_1`。
- pydantic-settings v2：新配置项用 `validation_alias=`（`Field(env=)` 失效）。
- 新配置项须同步写进 README.md + README_zh.md（通用部署口径，无机器特定路径）。
- 动了 facade seam / 组合根签名后，必须跑 `backend/tests/test_architecture_documentation.py` 与 repository surface_manifest 测试；因增删行破坏行号断言时，按既有流程重生成 manifest（见 [[surface-manifest行号脆弱]]）。
- 每个 git commit 结尾附：`Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`（下方 commit step 为简洁省略，但必须带上）。
- 本机 Python 解释器：`/opt/homebrew/Caskroom/miniconda/base/bin/python`（或 `PYTHON_BIN`）；worktree 无 `.env`/`node_modules`，后端测试从主 checkout 根跑（见 [[backend-launch-and-port]]、[[multi-agent-shared-checkout]]）。

---

## Task 1: MinerU 图片配置 + 护栏

**Files:**
- Modify: `backend/app/core/config.py:438`（紧接 `mineru_table_enable` 之后）
- Test: `backend/tests/test_mineru_cloud_config.py`（追加，复用现有 MinerU 配置测试文件）

**Interfaces:**
- Produces: `Settings.mineru_return_images: bool`（默认 True）、`Settings.mineru_max_image_bytes: int`（默认 5MB）、`Settings.mineru_max_images_per_source: int`（默认 200）。

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_mineru_cloud_config.py 追加
def test_mineru_image_retention_defaults(monkeypatch):
    from app.core.config import Settings
    for k in ("MINERU_RETURN_IMAGES", "MINERU_MAX_IMAGE_BYTES", "MINERU_MAX_IMAGES_PER_SOURCE"):
        monkeypatch.delenv(k, raising=False)
    s = Settings()
    assert s.mineru_return_images is True
    assert s.mineru_max_image_bytes == 5 * 1024 * 1024
    assert s.mineru_max_images_per_source == 200


def test_mineru_return_images_env_off(monkeypatch):
    from app.core.config import Settings
    monkeypatch.setenv("MINERU_RETURN_IMAGES", "0")
    assert Settings().mineru_return_images is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_mineru_cloud_config.py::test_mineru_image_retention_defaults -v`
Expected: FAIL（`AttributeError: 'Settings' object has no attribute 'mineru_return_images'`）

- [ ] **Step 3: Add the settings fields**

在 `backend/app/core/config.py` 的 `mineru_table_enable` 行（:438）之后插入：

```python
    # MinerU 内嵌图片保留（pdf/docx/pptx 抽图落盘为可见资产）。默认开；护栏
    # 让内网 mineru-api 回传载荷与磁盘占用有界，可整体关停回到「只文字」。
    mineru_return_images: bool = Field(True, validation_alias="MINERU_RETURN_IMAGES")
    mineru_max_image_bytes: int = Field(5 * 1024 * 1024, validation_alias="MINERU_MAX_IMAGE_BYTES")
    mineru_max_images_per_source: int = Field(200, validation_alias="MINERU_MAX_IMAGES_PER_SOURCE")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_mineru_cloud_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/core/config.py backend/tests/test_mineru_cloud_config.py
git commit -m "feat(mineru): add image-retention settings + guardrails"
```

---

## Task 2: 迁移 `_migration_19`——notebook_assets 加 source_id

**Files:**
- Modify: `backend/app/repositories/sqlite/migrations.py:14`（`SCHEMA_VERSION`）+ 新增 `_migration_19` 方法（放在 `_migration_18` 之后，约 :1375 之后）
- Test: `backend/tests/test_schema_migrations.py`（若不存在则创建）

**Interfaces:**
- Produces: `notebook_assets.source_id TEXT`（可空；knowhow 粘贴图为 NULL、来源内嵌图存 source_id）+ 索引 `idx_notebook_assets_source`；`SCHEMA_VERSION == 19`。

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_schema_migrations.py（新建或追加）
import sqlite3
from app.repositories.sqlite.migrations import SCHEMA_VERSION, SqliteMigrator


def _cols(db, table):
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}


def test_migration_19_adds_source_id_on_fresh_db(tmp_path):
    path = tmp_path / "fresh.db"
    migrator = SqliteMigrator(str(path))
    migrator.migrate()
    with sqlite3.connect(str(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert SCHEMA_VERSION == 19
        assert "source_id" in _cols(db, "notebook_assets")


def test_migration_19_backfills_deployed_db(tmp_path):
    # 模拟停在 v18 的已部署库：跑全量后，把 source_id 列删掉并回退 user_version
    # 到 18，再跑一次迁移，_migration_19 的守卫应补回该列（可重入）。
    path = tmp_path / "deployed.db"
    SqliteMigrator(str(path)).migrate()  # 到当前 SCHEMA_VERSION(19)
    with sqlite3.connect(str(path)) as db:
        db.execute("ALTER TABLE notebook_assets DROP COLUMN source_id")  # SQLite>=3.35
        db.execute("PRAGMA user_version = 18")
        db.commit()
    SqliteMigrator(str(path)).migrate()
    with sqlite3.connect(str(path)) as db:
        assert "source_id" in _cols(db, "notebook_assets")


def test_migration_19_is_reentrant(tmp_path):
    # 列已存在时再跑迁移不报错（守卫生效）。
    path = tmp_path / "reentrant.db"
    SqliteMigrator(str(path)).migrate()
    with sqlite3.connect(str(path)) as db:
        db.execute("PRAGMA user_version = 18")
        db.commit()
    SqliteMigrator(str(path)).migrate()  # 不应抛「duplicate column name」
    with sqlite3.connect(str(path)) as db:
        assert "source_id" in _cols(db, "notebook_assets")
```

> 注：`SqliteMigrator` 的构造签名以 migrations.py 现有为准（可能是 `SqliteMigrator(connect_fn)` 而非路径）。执行者先 `grep -n "class SqliteMigrator" -A15 backend/app/repositories/sqlite/migrations.py` 对齐构造/`migrate()` 入口名，再据此改测试的构造两行；其余断言不变。

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_schema_migrations.py::test_migration_19_adds_source_id_on_fresh_db -v`
Expected: FAIL（`SCHEMA_VERSION == 18` 断言失败）

- [ ] **Step 3: Bump 版本 + 加迁移**

`migrations.py:14`：`SCHEMA_VERSION = 18` → `SCHEMA_VERSION = 19`

在 `_migration_18` 方法之后新增：

```python
    def _migration_19(self) -> None:
        """来源内嵌图片资产：notebook_assets 加可空 source_id 列 + 索引。

        MinerU 从 pdf/docx/pptx 抽出的内嵌图片以 source_id 关联到来源，供来源
        视图渲染 + 源删除/重解析级联清理；knowhow 粘贴图片 source_id 为 NULL。
        已部署库(user_version>=1 时 _migration_1 短路)靠本迁移 ALTER 补列——
        与 _migration_2/_migration_4 同款独立迁移；ALTER ADD COLUMN 若列已存在
        会报错，故带 PRAGMA table_info 列存在性守卫，保证可重入。"""
        with self._connect() as db:
            cols = {row[1] for row in db.execute("PRAGMA table_info(notebook_assets)").fetchall()}
            if "source_id" not in cols:
                db.execute("ALTER TABLE notebook_assets ADD COLUMN source_id TEXT")
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_notebook_assets_source "
                "ON notebook_assets(source_id)"
            )
```

> `self._connect()` 用法与 `_migration_16`/`_migration_18` 一致（同文件内既有写法）。

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_schema_migrations.py -v`
Expected: PASS（两个用例都过）

- [ ] **Step 5: Commit**

```bash
git add backend/app/repositories/sqlite/migrations.py backend/tests/test_schema_migrations.py
git commit -m "feat(db): _migration_19 add notebook_assets.source_id (SCHEMA 19)"
```

---

## Task 3: 资产存储层——source_id 写入 + 按源查/删

**Files:**
- Modify: `backend/app/repositories/sqlite/knowhow_store.py:646`（`insert_notebook_asset` 加 `source_id` 形参）+ 新增 `source_asset_ids` / `delete_source_asset_rows`
- Test: `backend/tests/test_knowhow_grid_parser.py` 同目录新增 `backend/tests/test_source_asset_store.py`

**Interfaces:**
- Consumes: Task 2 的 `source_id` 列。
- Produces:
  - `insert_notebook_asset(notebook_id, filename, mime, size, created_by, source_id: str | None = None) -> str`
  - `source_asset_ids(source_id: str) -> list[str]`
  - `delete_source_asset_rows(source_id: str) -> list[str]`（删行并返回被删 asset_id，供上层删盘）

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_source_asset_store.py
import pytest


@pytest.fixture
def store(tmp_path):
    # 复用仓库既有的 knowhow store 组测夹具方式；若已有 conftest fixture
    # 提供 KnowhowStore/facade，直接用之。此处示意用最小 facade。
    from tests.helpers import make_test_repository  # 仓库既有测试工厂（对齐实际名）
    return make_test_repository(tmp_path)


def test_insert_with_source_id_then_query_and_delete(store):
    nb = store.create_notebook_min("nb-1")  # 对齐仓库既有建 notebook 测试辅助
    a1 = store.insert_notebook_asset(nb, "fig1.png", "image/png", 10, "u", source_id="src-1")
    a2 = store.insert_notebook_asset(nb, "fig2.png", "image/png", 10, "u", source_id="src-1")
    _pasted = store.insert_notebook_asset(nb, "paste.png", "image/png", 10, "u")  # source_id NULL
    assert set(store.source_asset_ids("src-1")) == {a1, a2}
    deleted = store.delete_source_asset_rows("src-1")
    assert set(deleted) == {a1, a2}
    assert store.source_asset_ids("src-1") == []
    assert store.get_notebook_asset(_pasted) is not None  # 粘贴图不受影响
```

> 执行者先 `grep -rn "make_test_repository\|def.*repository\|conftest" backend/tests/test_knowhow_grid_parser.py backend/tests/conftest.py` 对齐实际测试夹具/建 notebook 辅助名，替换上面两处占位辅助（`make_test_repository`/`create_notebook_min`）。断言逻辑不变。

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_source_asset_store.py -v`
Expected: FAIL（`insert_notebook_asset() got an unexpected keyword argument 'source_id'`）

- [ ] **Step 3: Implement**

`knowhow_store.py`：改 `insert_notebook_asset`，新增两方法：

```python
    def insert_notebook_asset(
        self, notebook_id: str, filename: str, mime: str, size: int,
        created_by: str, source_id: str | None = None,
    ) -> str:
        asset_id = self.new_id("asset")
        with self.database.write() as db:
            db.execute(
                "INSERT INTO notebook_assets "
                "(id, notebook_id, filename, mime, size, created_by, created_at, source_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (asset_id, notebook_id, filename, mime, size, created_by, self.now(), source_id),
            )
        return asset_id

    def source_asset_ids(self, source_id: str) -> list[str]:
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT id FROM notebook_assets WHERE source_id = ?", (source_id,)
            ).fetchall()
        return [row[0] for row in rows]

    def delete_source_asset_rows(self, source_id: str) -> list[str]:
        ids = self.source_asset_ids(source_id)
        if ids:
            with self.database.write() as db:
                db.execute(
                    "DELETE FROM notebook_assets WHERE source_id = ?", (source_id,)
                )
        return ids
```

> 若这三个方法需经 facade 一跳委托暴露（本仓 facade 组合根约定，见 [[codex架构整改轨道]]），在 facade 对应 allowlist 处补一跳委托；执行者按 surface_manifest 测试提示补齐。

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_source_asset_store.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/repositories/sqlite/knowhow_store.py backend/tests/test_source_asset_store.py
git commit -m "feat(assets): source_id write + per-source query/delete on notebook_assets"
```

---

## Task 4: AssetService——存来源图片 + 删来源图片文件

**Files:**
- Modify: `backend/app/services/knowhow/assets.py`
- Test: `backend/tests/test_source_image_asset_service.py`（新建）

**Interfaces:**
- Consumes: Task 3 的 `insert_notebook_asset(..., source_id=)` / `delete_source_asset_rows` / `get_notebook_asset`。
- Produces:
  - `AssetService.save_source_image(notebook_id, source_id, filename, mime, data, created_by) -> dict`
  - `AssetService.delete_source_images(source_id) -> None`（删行 + unlink 盘上文件）

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_source_image_asset_service.py
import pytest
from app.services.knowhow.assets import AssetService, AssetValidationError


class _FakeRepo:
    def __init__(self, tmp_path):
        self.storage_dir = tmp_path
        self._rows, self._seq = {}, 0

    def insert_notebook_asset(self, notebook_id, filename, mime, size, created_by, source_id=None):
        self._seq += 1
        aid = f"asset-{self._seq}"
        self._rows[aid] = {"id": aid, "notebook_id": notebook_id, "filename": filename,
                           "mime": mime, "size": size, "created_by": created_by, "source_id": source_id}
        return aid

    def get_notebook_asset(self, aid):
        return self._rows.get(aid)

    def source_asset_ids(self, source_id):
        return [a["id"] for a in self._rows.values() if a["source_id"] == source_id]

    def delete_source_asset_rows(self, source_id):
        ids = self.source_asset_ids(source_id)
        for i in ids:
            self._rows.pop(i, None)
        return ids


def test_save_source_image_writes_disk_and_row(tmp_path):
    svc = AssetService(_FakeRepo(tmp_path))
    asset = svc.save_source_image("nb-1", "src-1", "images/fig.png", "image/png", b"\x89PNG..", "u")
    path = svc.path_for(asset)
    assert path.is_file() and path.read_bytes() == b"\x89PNG.."
    assert asset["source_id"] == "src-1"


def test_delete_source_images_unlinks(tmp_path):
    repo = _FakeRepo(tmp_path)
    svc = AssetService(repo)
    asset = svc.save_source_image("nb-1", "src-1", "fig.png", "image/png", b"x", "u")
    path = svc.path_for(asset)
    assert path.is_file()
    svc.delete_source_images("src-1")
    assert not path.exists()
    assert repo.source_asset_ids("src-1") == []


def test_save_source_image_rejects_bad_mime(tmp_path):
    svc = AssetService(_FakeRepo(tmp_path))
    with pytest.raises(AssetValidationError):
        svc.save_source_image("nb-1", "src-1", "fig.svg", "image/svg+xml", b"x", "u")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_source_image_asset_service.py -v`
Expected: FAIL（`AttributeError: 'AssetService' object has no attribute 'save_source_image'`）

- [ ] **Step 3: Implement**

`assets.py` 的 `AssetService` 类内新增（复用既有 `validate_asset`/`path_for`/落盘校验）：

```python
    def save_source_image(
        self, notebook_id: str, source_id: str, filename: str,
        mime: str, data: bytes, created_by: str,
    ) -> dict:
        """存 MinerU 从来源抽出的内嵌图片：与 save() 同款校验+落盘，但带 source_id
        关联，供来源视图渲染与按源级联清理。护栏(大小/张数)由调用方(persist_image
        工厂)先行把关，这里仍做 mime/尺寸兜底校验，绝不放行不合规写盘。"""
        validate_asset(mime, len(data))
        asset_id = self._repo.insert_notebook_asset(
            notebook_id, safe_filename(filename or "image"), mime, len(data),
            created_by, source_id=source_id,
        )
        asset = self._repo.get_notebook_asset(asset_id)
        if asset is None:  # pragma: no cover
            raise RuntimeError(f"source image asset {asset_id} missing after insert")
        path = self.path_for(asset)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if not path.is_file() or path.stat().st_size != len(data):
            raise RuntimeError(f"source image asset {asset_id} did not persist to {path}")
        return asset

    def delete_source_images(self, source_id: str) -> None:
        """删一个来源的全部内嵌图片：先读行拿到 asset 元数据算盘上路径，删行，
        再 unlink 文件。删盘 best-effort（文件先没了不阻塞行删除）。"""
        ids = self._repo.source_asset_ids(source_id)
        assets = [self._repo.get_notebook_asset(i) for i in ids]
        self._repo.delete_source_asset_rows(source_id)
        for asset in assets:
            if not asset:
                continue
            path = self.path_for(asset)
            try:
                if path.is_file():
                    path.unlink()
            except OSError:
                pass
```

需要 `from app.repositories.source_files import safe_filename`（文件已 import，确认即可）。

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_source_image_asset_service.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/knowhow/assets.py backend/tests/test_source_image_asset_service.py
git commit -m "feat(assets): AssetService.save_source_image + delete_source_images"
```

---

## Task 5: MinerUClient——parse_with_images（http return_images + cli 拷图）

**Files:**
- Modify: `backend/app/services/mineru_client.py`
- Test: `backend/tests/test_mineru_client_cli.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `settings.mineru_return_images`。
- Produces:
  - `MinerUClient.parse_with_images(file_path, file_name) -> tuple[list[dict], dict[str, bytes]]`（images 以**图片文件名 basename** 为键）
  - `MinerUClient.parse(...)` 保持返回 `list[dict]`（`return self.parse_with_images(...)[0]`，向后兼容既有调用/测试）
  - 模块级 `_extract_images(payload) -> dict[str, bytes]`、`_decode_data_uri(s) -> bytes | None`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_mineru_client_cli.py 追加
import base64
from app.services.mineru_client import _decode_data_uri, _extract_images


def test_decode_data_uri_roundtrip():
    raw = b"\x89PNG\r\n_binary_"
    uri = "data:image/png;base64," + base64.b64encode(raw).decode()
    assert _decode_data_uri(uri) == raw
    assert _decode_data_uri("not-a-data-uri") is None


def test_extract_images_from_http_payload():
    raw = b"JPEGBYTES"
    uri = "data:image/jpeg;base64," + base64.b64encode(raw).decode()
    payload = {"results": {"doc.pdf": {"content_list": [], "images": {"images/abc.jpg": uri}}}}
    images = _extract_images(payload)
    assert images == {"abc.jpg": raw}  # 以 basename 为键


def test_parse_with_images_http_gated_off(monkeypatch):
    # return_images=False 时不请求图片、images 为空，content_list 照常。
    from app.core.config import Settings
    from app.services.mineru_client import MinerUClient
    s = Settings(MINERU_MODE="http", MINERU_API_URL="http://x", MINERU_RETURN_IMAGES="0")
    client = MinerUClient(s)
    monkeypatch.setattr(client, "_post_file_parse",
                        lambda fields, fp, fn: {"results": {"d.pdf": {"content_list": [{"type": "text", "text": "hi"}]}}})
    cl, images = client.parse_with_images("/tmp/d.pdf", "d.pdf")
    assert images == {}
    assert cl and cl[0]["text"] == "hi"
```

> 说明：为可测，Task 把 `_parse_http` 里真正发 HTTP 的部分抽成一个 `_post_file_parse(fields, file_path, file_name) -> dict` 网络接缝（测试 monkeypatch 它），与 `mineru_cloud_client` 的 `_http_json`/`_http_bytes` 接缝同款风格。

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_mineru_client_cli.py::test_extract_images_from_http_payload -v`
Expected: FAIL（`ImportError: cannot import name '_extract_images'`）

- [ ] **Step 3: Implement**

`mineru_client.py`：

1) 加模块级 helper：

```python
import base64
from pathlib import Path


def _decode_data_uri(value: str) -> bytes | None:
    """data:image/xxx;base64,.... -> bytes；非 data-uri 返回 None。"""
    if not isinstance(value, str) or not value.startswith("data:"):
        return None
    _, _, b64 = value.partition(",")
    if not b64:
        return None
    try:
        return base64.b64decode(b64)
    except Exception:
        return None


def _extract_images(payload: object) -> dict[str, bytes]:
    """从 mineru-api 响应里抽图片，键归一为 basename（与 content_list 的
    img_path basename 对齐）。找不到就返回空 dict（不抛错——图片是增量）。"""
    images: dict[str, bytes] = {}

    def _harvest(images_field: object) -> None:
        if isinstance(images_field, dict):
            for path, val in images_field.items():
                data = _decode_data_uri(val) if isinstance(val, str) else None
                if data is None and isinstance(val, (bytes, bytearray)):
                    data = bytes(val)
                if data is not None:
                    images[Path(str(path)).name] = data

    if isinstance(payload, dict):
        results = payload.get("results")
        if isinstance(results, dict):
            for value in results.values():
                if isinstance(value, dict):
                    _harvest(value.get("images"))
        _harvest(payload.get("images"))
    return images
```

2) `parse()` 改为委托，新增 `parse_with_images()`；HTTP 抽出网络接缝 `_post_file_parse`；`return_images` 由配置驱动：

```python
    def parse(self, file_path: str, file_name: str) -> List[dict]:
        return self.parse_with_images(file_path, file_name)[0]

    def parse_with_images(self, file_path: str, file_name: str) -> tuple[List[dict], dict[str, bytes]]:
        """返回 (content_list, {basename: bytes})。图片抽取失败绝不影响 content_list。"""
        self.last_error = ""
        try:
            if self.mode == "http":
                return self._parse_http_with_images(file_path, file_name)
            if self.mode == "cli":
                return self._parse_cli_with_images(file_path, file_name)
            raise RuntimeError("MinerU is not configured")
        except Exception as exc:
            self.last_error = str(exc)
            raise
```

HTTP：把原 `_parse_http` 改为组 fields（`return_images` 用配置）+ 调 `_post_file_parse` + 抽 content_list/images：

```python
    def _post_file_parse(self, fields: dict, file_path: str, file_name: str) -> dict:
        url = self.settings.mineru_api_url.rstrip("/") + "/file_parse"
        content = Path(file_path).read_bytes()
        body, content_type = _encode_multipart(fields, "files", file_name, content)
        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header("Content-Type", content_type)
        with urllib.request.urlopen(request, timeout=self.settings.mineru_timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))

    def _parse_http_with_images(self, file_path: str, file_name: str) -> tuple[List[dict], dict[str, bytes]]:
        want_images = bool(self.settings.mineru_return_images)
        fields = {
            "backend": self.settings.mineru_backend,
            "parse_method": self.settings.mineru_parse_method,
            "return_content_list": "true",
            "return_md": "false",
            "return_middle_json": "false",
            "return_model_output": "false",
            "return_images": "true" if want_images else "false",
            "response_format_zip": "false",
            "formula_enable": "true" if self.settings.mineru_formula_enable else "false",
            "table_enable": "true" if self.settings.mineru_table_enable else "false",
        }
        if self.settings.mineru_lang:
            fields["lang_list"] = self.settings.mineru_lang
        if self.settings.mineru_vlm_server_url:
            fields["server_url"] = self.settings.mineru_vlm_server_url
        payload = self._post_file_parse(fields, file_path, file_name)
        content_list = _extract_content_list(payload)
        images = _extract_images(payload) if want_images else {}
        return content_list, images
```

CLI：原 `_parse_cli` 返回 content_list，新增 `_parse_cli_with_images`——`return_images` 开时在临时目录销毁前把 `images/` 读成 basename→bytes：

```python
    def _parse_cli_with_images(self, file_path: str, file_name: str) -> tuple[List[dict], dict[str, bytes]]:
        suffix = Path(file_name).suffix.lower()
        want_images = bool(self.settings.mineru_return_images)
        with tempfile.TemporaryDirectory(prefix="mineru-") as out_dir:
            command = (self._office_cli_command(file_path, out_dir)
                       if suffix in {".docx", ".pptx"}
                       else self._pdf_cli_command(file_path, file_name, out_dir))
            env = {**os.environ}
            if self.settings.mineru_model_source:
                env["MINERU_MODEL_SOURCE"] = self.settings.mineru_model_source
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       env=env, start_new_session=(os.name != "nt"))
            try:
                stdout, stderr = process.communicate(timeout=self.settings.mineru_timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                stdout, stderr = _terminate_process(process)
                detail = _tail_process_output(stdout, stderr)
                raise RuntimeError(
                    f"MinerU Python API timed out after {self.settings.mineru_timeout_seconds}s"
                    + (f": {detail}" if detail else "")) from exc
            if process.returncode != 0:
                raise RuntimeError(f"MinerU Python API failed: {_tail_process_output(stdout, stderr)}")
            matches = sorted(Path(out_dir).rglob("*_content_list.json"))
            if not matches:
                raise RuntimeError("MinerU Python API produced no content_list.json")
            content_list = json.loads(matches[0].read_text(encoding="utf-8"))
            images: dict[str, bytes] = {}
            if want_images:
                for img in Path(out_dir).rglob("*"):
                    if img.is_file() and img.parent.name == "images":
                        try:
                            images[img.name] = img.read_bytes()
                        except OSError:
                            pass
            return content_list, images
```

保留原 `_parse_cli`/`_parse_http` 也可（让 `_parse_cli` 委托 `_parse_cli_with_images(...)[0]`）以不破坏既有 CLI 测试；执行者按测试结果决定是否保留薄封装。

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_mineru_client_cli.py -v`
Expected: PASS（新用例 + 既有 CLI 用例均绿）

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/mineru_client.py backend/tests/test_mineru_client_cli.py
git commit -m "feat(mineru): parse_with_images (http return_images + cli image copy-out)"
```

---

## Task 6: MinerUCloudClient——parse_url_with_images（zip 抽图）

**Files:**
- Modify: `backend/app/services/mineru_cloud_client.py`
- Test: `backend/tests/test_mineru_cloud_client.py`（追加）

**Interfaces:**
- Produces:
  - `MinerUCloudClient.parse_url_with_images(url, data_id=None) -> tuple[list[dict], dict[str, bytes]]`
  - `parse_url(...)` 保持返回 `list[dict]`（委托 `[0]`）
  - `_images_from_zip(zip_bytes) -> dict[str, bytes]`（zip 内 `images/` 目录，basename 为键）

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_mineru_cloud_client.py 追加
import io, zipfile
from app.services.mineru_cloud_client import _images_from_zip


def test_images_from_zip_keys_by_basename():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("full_content_list.json", "[]")
        z.writestr("images/pic1.jpg", b"J1")
        z.writestr("images/sub/pic2.png", b"P2")
        z.writestr("readme.md", "x")
    imgs = _images_from_zip(buf.getvalue())
    assert imgs == {"pic1.jpg": b"J1", "pic2.png": b"P2"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_mineru_cloud_client.py::test_images_from_zip_keys_by_basename -v`
Expected: FAIL（`ImportError: cannot import name '_images_from_zip'`）

- [ ] **Step 3: Implement**

`mineru_cloud_client.py`：

```python
from pathlib import Path


def _images_from_zip(zip_bytes: bytes) -> dict[str, bytes]:
    images: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        for name in archive.namelist():
            if name.endswith("/"):
                continue
            parts = name.split("/")
            if "images" in parts[:-1]:
                images[Path(name).name] = archive.read(name)
    return images
```

主流程（现 `parse_url` 下载 zip → `_content_list_from_zip`）改为带图版本：

```python
    def parse_url(self, url: str, data_id: str | None = None) -> List[dict]:
        return self.parse_url_with_images(url, data_id=data_id)[0]

    def parse_url_with_images(self, url: str, data_id: str | None = None) -> tuple[List[dict], dict[str, bytes]]:
        # 复用既有：提交 URL → 轮询 → 下 zip。此处仅在拿到 zip_bytes 后多抽一次图片。
        task_id = self._submit(url, data_id=data_id)          # 对齐既有内部方法名
        zip_url = self._poll(task_id)
        zip_bytes = self._http_bytes(zip_url)
        content_list = self._content_list_from_zip(zip_bytes)
        images = _images_from_zip(zip_bytes) if self.settings.mineru_return_images else {}
        return content_list, images
```

> 执行者对齐既有 `parse_url` 内部真实调用（`_submit`/`_poll` 名称以文件现状为准，见 mineru_cloud_client.py:44-89），把「下载 zip」这段复用、仅在其后加 `_images_from_zip`。

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_mineru_cloud_client.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/mineru_cloud_client.py backend/tests/test_mineru_cloud_client.py
git commit -m "feat(mineru-cloud): parse_url_with_images (extract images/ from result zip)"
```

---

## Task 7: parsers.py——image 分支保图 + 三个 parse 函数透传

**Files:**
- Modify: `backend/app/services/parsers.py`（`parse_source_file` / `parse_pdf` / `parse_docx` / `parse_pptx` / `mineru_content_list_to_elements`）
- Test: `backend/tests/test_parsers_office.py`（追加）

**Interfaces:**
- Consumes: Task 5/6 的 `parse_with_images() -> (content_list, images)`。
- Produces（签名新增末位可选参数，向后兼容）：
  - `mineru_content_list_to_elements(source_id, content_list, label_prefix="PDF", images: dict[str, bytes] | None = None, persist_image: Callable[[bytes, str], str | None] | None = None)`
  - `parse_pdf/parse_docx/parse_pptx(source_id, path, file_name="", mineru_client=None, persist_image=None)`
  - `parse_source_file(source_id, file_path, file_name, mineru_client=None, persist_image=None)`
  - `persist_image(img_bytes, img_name) -> asset_id | None`（img_name = img_path 的 basename；mime 与护栏由调用方闭包内部处理）
  - image 元素：`element_type == "image"`，`metadata` 含 `asset_id`（成功落盘时）+ `caption`（有则）+ `page_number`/`source_format`；`text` = caption 或占位 `f"{label_prefix} p.{page} 图 {ordinal}"`（**不再丢无 caption 图**）

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_parsers_office.py 追加
from app.services.parsers import mineru_content_list_to_elements


def test_image_block_persists_asset_and_keeps_caption():
    content = [{"type": "image", "img_path": "images/fig1.jpg",
                "image_caption": ["Figure 1: layout"], "page_idx": 0}]
    saved = {}
    def persist(b, name):
        saved[name] = b
        return "asset-xyz"
    els = mineru_content_list_to_elements("s1", content, images={"fig1.jpg": b"J"}, persist_image=persist)
    img = [e for e in els if e.element_type == "image"]
    assert len(img) == 1
    assert img[0].metadata["asset_id"] == "asset-xyz"
    assert "Figure 1: layout" in img[0].text
    assert saved == {"fig1.jpg": b"J"}


def test_image_block_without_caption_is_not_dropped():
    content = [{"type": "image", "img_path": "images/x.png", "page_idx": 2}]
    els = mineru_content_list_to_elements("s1", content, images={"x.png": b"P"},
                                          persist_image=lambda b, n: "a1")
    img = [e for e in els if e.element_type == "image"]
    assert len(img) == 1                      # 旧行为会 continue 丢弃
    assert img[0].metadata["asset_id"] == "a1"
    assert img[0].text.strip()                # 占位文本非空


def test_image_block_no_persist_degrades_to_caption_text_only():
    content = [{"type": "image", "img_path": "images/x.png",
                "image_caption": ["cap"], "page_idx": 0}]
    els = mineru_content_list_to_elements("s1", content)   # 无 images/persist
    img = [e for e in els if e.element_type == "image"]
    assert len(img) == 1
    assert "asset_id" not in img[0].metadata
    assert "cap" in img[0].text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_parsers_office.py::test_image_block_persists_asset_and_keeps_caption -v`
Expected: FAIL（`persist_image` 参数不存在 / image 分支仍丢弃）

- [ ] **Step 3: Implement**

`parsers.py`：

1) `mineru_content_list_to_elements` 签名 + image 分支：

```python
def mineru_content_list_to_elements(
    source_id: str,
    content_list: List[dict],
    label_prefix: str = "PDF",
    images: Dict[str, bytes] | None = None,
    persist_image: Any = None,
) -> List[SourceElement]:
    ...
        elif block_type == "image":
            img_path = str(block.get("img_path", "")).strip()
            img_name = Path(img_path).name
            caption = " ".join(
                _as_list(block.get("image_caption")) + _as_list(block.get("image_footnote"))
            ).strip()
            asset_id = ""
            if img_name and images and persist_image and img_name in images:
                try:
                    asset_id = persist_image(images[img_name], img_name) or ""
                except Exception:
                    asset_id = ""   # 抽图/写盘失败绝不影响文本产出
            text = caption or f"{label_prefix} p.{page} 图 {ordinal}"
            metadata: Dict[str, Any] = {
                "parser": "mineru", "page_number": page,
                "source_format": label_prefix.lower(),
            }
            if asset_id:
                metadata["asset_id"] = asset_id
            if caption:
                metadata["caption"] = caption
            elements.append(
                _element(source_id, "image", f"{label_prefix} p.{page} image {ordinal}", text, metadata)
            )
```

（删掉原 image 分支里的 `if not caption: continue` 与 `image_caption` 元素类型。）

2) 三个 parse 函数：改为调 `parse_with_images`，透传 images + persist_image。以 `parse_pdf` 为例（docx/pptx 同款，各自 label_prefix）：

```python
def parse_pdf(source_id, path, file_name="", mineru_client=None, persist_image=None):
    if mineru_client is not None and getattr(mineru_client, "configured", False):
        try:
            content_list, images = mineru_client.parse_with_images(str(path), file_name or path.name)
            elements = mineru_content_list_to_elements(
                source_id, content_list, images=images, persist_image=persist_image)
            if elements:
                return elements
            if hasattr(mineru_client, "last_error"):
                mineru_client.last_error = "MinerU content_list mapped to zero source elements"
        except Exception as exc:
            if hasattr(mineru_client, "last_error") and not getattr(mineru_client, "last_error", ""):
                mineru_client.last_error = str(exc)
    return parse_pdf_pypdf(source_id, path)
```

`parse_docx` 用 `label_prefix="DOCX"`、`parse_pptx` 用 `"PPTX"`（其余结构照抄，回退各自原路径）。

3) `parse_source_file` 透传：

```python
def parse_source_file(source_id, file_path, file_name, mineru_client=None, persist_image=None):
    suffix = Path(file_name).suffix.lower()
    if suffix in {".md", ".markdown"}:
        return parse_markdown(source_id, Path(file_path))
    if suffix == ".docx":
        return parse_docx(source_id, Path(file_path), file_name, mineru_client, persist_image)
    if suffix == ".pptx":
        return parse_pptx(source_id, Path(file_path), file_name, mineru_client, persist_image)
    if suffix == ".pdf":
        return parse_pdf(source_id, Path(file_path), file_name, mineru_client, persist_image)
    if suffix == ".csv":
        return parse_csv(source_id, Path(file_path))
    if suffix in {".xlsx", ".xlsm"}:
        return parse_xlsx(source_id, Path(file_path))
    return parse_plain_text(source_id, Path(file_path), "text")
```

确认 `from pathlib import Path` 与 `Dict`/`Any` 已 import（文件顶已有）。

4) grep 确认无其它消费 `"image_caption"` element_type 的下游（预期仅本处产出）：
Run: `grep -rn "image_caption" backend/app frontend/app`

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_parsers_office.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/parsers.py backend/tests/test_parsers_office.py
git commit -m "feat(parsers): retain MinerU images as asset-backed elements (no more dropping)"
```

---

## Task 8: 接线——facade 图片持久化工厂 + 三条解析路径透传

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`（facade：`_make_persist_image` + parse_file seam 透传 persist_image）
- Modify: `backend/app/services/repository_runtime.py:492-573`（`wire_source_ingestion` 加 `make_persist_image` 形参 + 传入）
- Modify: `backend/app/services/source_ingestion.py`（`__init__` 收 `make_persist_image`；`process_source` 建 per-source 闭包并透传本地/URL本地/URL云端三路径）
- Test: `backend/tests/test_source_ingestion_service.py`（追加）

**Interfaces:**
- Consumes: Task 4 `AssetService.save_source_image`；Task 5/6 `parse_with_images`/`parse_url_with_images`；Task 7 `persist_image` 契约。
- Produces:
  - `SourceIngestionService.__init__(..., make_persist_image: Callable[[str, str, str], Optional[Callable[[bytes, str], Optional[str]]]])`（notebook_id, source_id, created_by → persist 闭包或 None）
  - facade `_make_persist_image(notebook_id, source_id, created_by)`：`mineru_return_images` 关时返回 None；否则返回带 per-source 张数计数 + 单图尺寸/mime 护栏的闭包，内部调 `AssetService(self).save_source_image`。

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_source_ingestion_service.py 追加
def test_make_persist_image_enforces_guardrails(monkeypatch, tmp_path):
    # facade 工厂：超尺寸/超张数被挡；关配置时整体返回 None。
    from app.core.config import Settings
    from app.services.knowhow.assets import AssetService

    class _Repo:
        storage_dir = tmp_path
        def __init__(self): self.saved = []
        def insert_notebook_asset(self, nb, fn, mime, size, by, source_id=None):
            aid = f"a{len(self.saved)}"; self.saved.append(aid); return aid
        def get_notebook_asset(self, aid): return {"id": aid, "notebook_id": "nb", "mime": "image/png"}

    # 直接测工厂逻辑：构造与 facade._make_persist_image 等价的闭包工厂
    from app.services.source_image_persist import make_persist_image_factory  # Task 引入的纯函数
    repo = _Repo()
    settings = Settings(MINERU_RETURN_IMAGES="1", MINERU_MAX_IMAGE_BYTES="10",
                        MINERU_MAX_IMAGES_PER_SOURCE="1")
    factory = make_persist_image_factory(settings, lambda: AssetService(repo))
    persist = factory("nb", "src-1", "u")
    assert persist(b"x" * 5, "a.png") == "a0"      # 通过
    assert persist(b"x" * 50, "b.png") is None      # 超尺寸被挡
    assert persist(b"x" * 5, "c.png") is None        # 超张数(上限1)被挡

    off = Settings(MINERU_RETURN_IMAGES="0")
    assert make_persist_image_factory(off, lambda: AssetService(repo))("nb", "s", "u") is None
```

> 为可测且低耦合，把工厂逻辑抽成纯函数模块 `backend/app/services/source_image_persist.py::make_persist_image_factory(settings, asset_service_provider)`；facade 的 `_make_persist_image` 只是 `make_persist_image_factory(self.settings, lambda: AssetService(self))` 的实例绑定。

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_source_ingestion_service.py::test_make_persist_image_enforces_guardrails -v`
Expected: FAIL（`ModuleNotFoundError: app.services.source_image_persist`）

- [ ] **Step 3: Implement**

1) 新建 `backend/app/services/source_image_persist.py`：

```python
"""来源内嵌图片持久化工厂（纯逻辑，便于单测）。

facade 的 _make_persist_image 绑定它到实例：per-source 一个闭包，带张数计数
与单图尺寸/mime 护栏；任何一步不合规返回 None（元素退化为 caption/占位文本，
绝不因图片问题阻塞文本解析）。"""
from __future__ import annotations

import mimetypes
from typing import Callable, Optional

from app.core.config import Settings
from app.services.knowhow.assets import ALLOWED_MIME_EXTENSIONS, AssetValidationError


def make_persist_image_factory(
    settings: Settings,
    asset_service_provider: Callable[[], object],
) -> Callable[[str, str, str], Optional[Callable[[bytes, str], Optional[str]]]]:
    def factory(notebook_id: str, source_id: str, created_by: str):
        if not settings.mineru_return_images:
            return None
        state = {"n": 0}

        def persist(img_bytes: bytes, img_name: str) -> Optional[str]:
            if len(img_bytes) > settings.mineru_max_image_bytes:
                return None
            if state["n"] >= settings.mineru_max_images_per_source:
                return None
            mime = mimetypes.guess_type(img_name)[0] or "image/jpeg"
            if mime not in ALLOWED_MIME_EXTENSIONS:
                return None
            try:
                asset = asset_service_provider().save_source_image(
                    notebook_id, source_id, img_name, mime, img_bytes, created_by)
            except (AssetValidationError, RuntimeError, OSError):
                return None
            state["n"] += 1
            return asset["id"]

        return persist

    return factory
```

2) facade `sqlite_repository.py`：加 `_make_persist_image`（用 `AssetService(self)`），并在构造 parse_file seam 处（:570 附近）让 seam 透传 `persist_image` kwarg 给 `parse_source_file`。示意：

```python
from app.services.knowhow.assets import AssetService
from app.services.source_image_persist import make_persist_image_factory

    def _make_persist_image(self, notebook_id, source_id, created_by):
        return make_persist_image_factory(self.settings, lambda: AssetService(self))(
            notebook_id, source_id, created_by)
```

parse_file seam（原 `lambda ...: parse_source_file(source_id, file_path, file_name, client)`）改为接受并透传 `persist_image`：

```python
        def _parse_file(source_id, file_path, file_name, client, persist_image=None):
            return parse_source_file(source_id, file_path, file_name, client, persist_image)
```

3) `repository_runtime.py`：`wire_source_ingestion` 形参表加 `make_persist_image: Callable[..., Any]`，并在 `SourceIngestionService(...)` 调用里加 `make_persist_image=make_persist_image`。facade 调 `wire_source_ingestion(..., make_persist_image=self._make_persist_image)`。

4) `source_ingestion.py`：
   - `__init__` 形参加 `make_persist_image: Callable[[str, str, str], Any]`，存 `self.make_persist_image = make_persist_image`。
   - `process_source` parse 阶段，在拿到 `source` 后建闭包并透传三路径：

```python
            persist_image = self.make_persist_image(
                source.notebook_id, source_id, getattr(source, "created_by", "") or "")
            if source.source_url:
                mineru_client = self.mineru_client()
                if mineru_client.configured:
                    elements = self.parse_url_via_local(
                        source_id, source.source_url, source.file_name, persist_image)
                    ...
                else:
                    cloud_client = self.mineru_cloud_client()
                    content_list, images = cloud_client.parse_url_with_images(
                        source.source_url, data_id=source_id)
                    elements = mineru_content_list_to_elements(
                        source_id, content_list, images=images, persist_image=persist_image)
                    ...
            else:
                mineru_client = self.mineru_client()
                elements = self.parse_file(
                    source_id, source.file_path, source.file_name, mineru_client,
                    persist_image=persist_image)
                ...
```

   - `parse_url_via_local(self, source_id, url, file_name, persist_image=None)`：把 `persist_image` 透传给它内部对 `parse_source_file`/`parse_pdf` 的调用（源在 source_ingestion.py:402-416）。

> 本 Task 动了 facade seam 与组合根签名：跑 `test_architecture_documentation.py` 与 surface_manifest 测试，按提示补一跳委托 allowlist / 重生成行号 manifest。

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_source_ingestion_service.py tests/test_source_ingestion_failure_boundaries.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/source_image_persist.py backend/app/services/sqlite_repository.py backend/app/services/repository_runtime.py backend/app/services/source_ingestion.py backend/tests/test_source_ingestion_service.py
git commit -m "feat(ingest): wire per-source image persistence across file/url/cloud parse paths"
```

---

## Task 9: 生命周期——重解析/删源级联清理图片

**Files:**
- Modify: `backend/app/services/source_ingestion.py`（`delete_source` :709 + 重解析清理点）
- Test: `backend/tests/test_source_ingestion_service.py`（追加）

**Interfaces:**
- Consumes: Task 4 `AssetService.delete_source_images(source_id)`。
- Produces: 删源 / 重新解析同一源前，先删旧图片资产（行+盘），避免孤儿累积。

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_source_ingestion_service.py 追加
def test_delete_source_cleans_images(ingestion_fixture):
    # ingestion_fixture 对齐仓库既有 SourceIngestionService 集成夹具。
    repo, svc = ingestion_fixture
    src = repo.add_parsed_source_with_image()   # 造一个含 image 资产的源(对齐辅助)
    asset_ids = repo.source_asset_ids(src)
    assert asset_ids
    svc.delete_source(src, repo.pipeline_hooks())
    assert repo.source_asset_ids(src) == []      # 行已删
```

> 执行者对齐 `test_source_ingestion_service.py` 现有夹具/辅助命名（建源、拿 hooks）。若无「造含图源」辅助，可先手插一条 `notebook_assets(source_id=src)` 行 + 落一个占位文件再断言删除。

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_source_ingestion_service.py::test_delete_source_cleans_images -v`
Expected: FAIL（删源未清图片，`source_asset_ids` 非空）

- [ ] **Step 3: Implement**

`source_ingestion.py`：
- `delete_source` 内（删元素/文件的同批）加：`AssetService(self._facade_or_repo).delete_source_images(source_id)`。为拿到 AssetService，给 SourceIngestionService 复用 Task 8 注入的 `make_persist_image` 同源 provider——更简单的做法：Task 8 顺带注入 `delete_source_images: Callable[[str], None]`（facade 绑定 `lambda sid: AssetService(self).delete_source_images(sid)`），此处直接 `self.delete_source_images(source_id)`。
- 重解析清理：`process_source` 在写新元素前（`clear_source_extraction_state` 调用附近，source_ingestion.py:502）先 `self.delete_source_images(source_id)`，保证 re-parse 不留旧图。

> 采用「注入 `delete_source_images` 回调」而非在 source_ingestion 里直接构造 AssetService，保持与 Task 8 一致的依赖注入风格。相应在 `wire_source_ingestion` + facade 各加一个 `delete_source_images` 形参/绑定。

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_source_ingestion_service.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/source_ingestion.py backend/app/services/sqlite_repository.py backend/app/services/repository_runtime.py backend/tests/test_source_ingestion_service.py
git commit -m "feat(ingest): cascade-delete source images on re-parse and source delete"
```

---

## Task 10: 前端——ElementBody 内联渲染图片（带鉴权 blob）

**Files:**
- Create: `frontend/app/source-image.ts`（纯 URL 助手 + test 目标）
- Modify: `frontend/app/page.tsx`（新增 `AuthedImage` 组件；`ElementBody` 加 image 分支 + 收 `notebookId`；调用处 :4499 传 `notebookId={currentNotebookId}`）
- Test: `frontend/app/source-image.test.mjs`

**Interfaces:**
- Consumes: 元素 `metadata.asset_id`；`API_BASE`（`frontend/app/auth.ts:1`）；`authHeaders()`（auth.ts:31）；serving 端点 `GET /api/notebooks/{nb}/assets/{id}`（routes.py:453，已存在，来源图片作为 notebook_assets 行同样被它服务）。
- Produces: `sourceImageAssetUrl(apiBase, notebookId, assetId) -> string`；`AuthedImage` 组件（fetch→blob→objectURL，卸载 revoke）。

> **引用侧无需改动（spec §3.E 方案 a）**：引用/证据卡沿用现有文字渲染（`quoted_span`/`element_text` = 含 caption 的 chunk 文本），点击引用经**既有** element 跳转（`highlightedElementId`，page.tsx:1442）落到来源正文，图片在场景一渲染出来。本 Task 只做来源正文内联；不碰 `EvidenceLine`/`KgEvidenceCard`/报告证据。

- [ ] **Step 1: Write the failing test**

```js
// frontend/app/source-image.test.mjs
import { test } from "node:test";
import assert from "node:assert";
import { sourceImageAssetUrl } from "./source-image.ts";

test("builds notebook-scoped asset url", () => {
  assert.equal(
    sourceImageAssetUrl("http://api", "nb-1", "asset-9"),
    "http://api/notebooks/nb-1/assets/asset-9",
  );
});

test("returns empty when asset id missing", () => {
  assert.equal(sourceImageAssetUrl("http://api", "nb-1", ""), "");
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && node --test app/source-image.test.mjs`
Expected: FAIL（`Cannot find module './source-image.ts'`）

- [ ] **Step 3: Implement**

`frontend/app/source-image.ts`：

```ts
export function sourceImageAssetUrl(apiBase: string, notebookId: string, assetId: string): string {
  if (!assetId || !notebookId) return "";
  return `${apiBase}/notebooks/${notebookId}/assets/${assetId}`;
}
```

`frontend/app/page.tsx`：新增带鉴权的图片组件（复用 `API_BASE`/`authHeaders`，与 report blob 下载同款 fetch→blob）：

```tsx
function AuthedImage({ url, alt }: { url: string; alt: string }) {
  const [src, setSrc] = useState<string>("");
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    if (!url) return;
    let revoked = "";
    let alive = true;
    fetch(url, { headers: authHeaders() })
      .then((r) => (r.ok ? r.blob() : Promise.reject(new Error(String(r.status)))))
      .then((blob) => {
        if (!alive) return;
        revoked = URL.createObjectURL(blob);
        setSrc(revoked);
      })
      .catch(() => alive && setFailed(true));
    return () => { alive = false; if (revoked) URL.revokeObjectURL(revoked); };
  }, [url]);
  if (failed) return <p className="tool-hint">图片加载失败</p>;
  if (!src) return <p className="tool-hint">图片加载中…</p>;
  return <img className="element-image" src={src} alt={alt} loading="lazy" />;
}
```

`ElementBody` 加 image 分支并接收 `notebookId`（同时改调用处传参）：

```tsx
function ElementBody({ element, notebookId }: { element: SourceElement; notebookId: string }) {
  if (element.element_type === "formula") return <FormulaView latex={element.text} />;
  if (element.element_type === "table") {
    const html = typeof element.metadata?.table_html === "string" ? element.metadata.table_html : "";
    if (html) return <div className="element-table" dangerouslySetInnerHTML={{ __html: sanitizeTableHtml(html) }} />;
  }
  if (element.element_type === "image") {
    const assetId = typeof element.metadata?.asset_id === "string" ? element.metadata.asset_id : "";
    const caption = typeof element.metadata?.caption === "string" ? element.metadata.caption : "";
    const url = sourceImageAssetUrl(API_BASE, notebookId, assetId);
    return (
      <figure className="element-image-figure">
        {url ? <AuthedImage url={url} alt={caption || "figure"} /> : null}
        {caption ? <figcaption>{caption}</figcaption> : null}
      </figure>
    );
  }
  return <p>{element.text}</p>;
}
```

调用处（page.tsx:4499）：`<ElementBody element={element} notebookId={currentNotebookId ?? ""} />`。

确认 `API_BASE` 已从 `./auth.ts` 导入、`sourceImageAssetUrl` 从 `./source-image.ts` 导入、`useEffect`/`useState` 在作用域（page.tsx 顶已用）。加轻量 CSS：`.element-image{max-width:100%;height:auto;border-radius:6px}`（放现有 source-element 样式附近）。

- [ ] **Step 4: Run test + typecheck**

Run: `cd frontend && node --test app/source-image.test.mjs && npx tsc --noEmit`
Expected: 测试 PASS；tsc 无新增错误

- [ ] **Step 5: Commit**

```bash
git add frontend/app/source-image.ts frontend/app/source-image.test.mjs frontend/app/page.tsx
git commit -m "feat(ui): render embedded source images inline via authed blob fetch"
```

---

## Task 11: 文档——README 记录图片保留 + 配置

**Files:**
- Modify: `README.md`、`README_zh.md`

**Interfaces:** 无代码接口；通用部署口径（见 [[committed-docs-stay-generic]]），无机器特定路径/端口。

- [ ] **Step 1: 更新 README_zh.md**

在 MinerU 相关小节补：pdf/word/ppt 文档中的内嵌图片会被 MinerU 抽取并保留、在来源正文内联展示；新增配置 `MINERU_RETURN_IMAGES`（默认开）、`MINERU_MAX_IMAGE_BYTES`（默认 5MB）、`MINERU_MAX_IMAGES_PER_SOURCE`（默认 200）；`MINERU_RETURN_IMAGES=0` 关闭仅保留文字/图注。裸图片文件不作为可上传源。

- [ ] **Step 2: 同步 README.md（英文，同款内容）**

- [ ] **Step 3: Commit**

```bash
git add README.md README_zh.md
git commit -m "docs: document embedded-image retention + MINERU_RETURN_IMAGES config"
```

---

## Task 12: 端到端验证 + 回归门

**Files:** 无（验证 + 记录）

- [ ] **Step 1: 全量后端测试**

Run: `cd backend && python -m pytest tests/ -q`
Expected: 全绿（重点关注 parsers / mineru_client / mineru_cloud_client / source_ingestion / schema_migrations / architecture_documentation / surface_manifest）。任何行号断言失败按 [[surface-manifest行号脆弱]] 重生成 manifest。

- [ ] **Step 2: 前端测试 + 构建**

Run: `cd frontend && node --test app/*.test.mjs && npx tsc --noEmit`
Expected: PASS / 无类型错误。

- [ ] **Step 3: 浏览器端到端验证（preview_start 起前端，配 MinerU 的环境）**

- 上传一个含插图的 PDF → 打开来源详情 → 确认图片元素内联渲染出 `<img>`（read_page 看到 `element-image` / figure；read_network_requests 看到 `/api/notebooks/.../assets/...` 200）。
- 重新解析同一源 → 确认旧图片资产被清理、无孤儿累积（`source_asset_ids` 前后一致、无翻倍）。
- 设 `MINERU_RETURN_IMAGES=0` 重解析 → 确认零图、文本照常（纯降级）。
- 若无法在本会话连真机 MinerU：至少用 http 模式 mock 或既有含图 fixture 跑通后端集成，前端用一条手插 `notebook_assets(source_id=…)` + 元素 `metadata.asset_id` 验证渲染路径；并在结论里如实标注「真机 MinerU 抽图待部署环境回归」。

- [ ] **Step 4: 记录已知后续（不在本 PR）**

- 分享/深拷贝时 source-image 的 `source_id` 重映射：当前拷贝按 asset_id/notebook_id 重映射，`source_id` 列会带旧值（展示走 element.asset_id 不受影响，仅「按源删除拷贝的图」会漏）——列为小型 fast-follow。
- 证据卡内嵌缩略图（方案 b）为可选跟进，非本次范围。

- [ ] **Step 5: 收尾 PR**

分支先 rebase 到 master 保持线性 → push → `gh pr create --base master`（见 [[dev-flow-finish-with-pr]]、[[pr-merge-is-rebase]]）。PR 描述附本 plan 与 spec 链接。
