"""`batch_ingest backfill-images` 阶段的编排（离线、外科式、零模型）。

纯逻辑（读 markdown、对齐既有元素、算出该插入什么）在
`app.services.image_backfill`；本模块只负责"按来源翻页、落资产、开写事务、记账"。

## 刻意不做（红线，代码与文档同记）

* **不重分块**：`chunks.text` 与 chunk id 逐字节不变，只往
  `chunks.element_ids` **尾部** append 新图片元素 id。重分块会换 chunk id，而
  `chunk_embeddings` 以 ``ON DELETE CASCADE`` 挂在 chunk 行上——那等于把整本库的
  向量删掉重算。
* **零 embedding 重算**：本阶段一次 embedder 都不调，也不删改任何
  ``chunk_embeddings`` / ``element_embeddings`` 行。新插入的图片元素**确实**会被
  体检 H5（"缺少元素向量"）数进去，那是诚实信号而不是回归：它的修复动作是既有
  的"补齐向量"（纯追加）。H2（零元素）只可能被清掉、H3（``chunked_at IS NULL``）
  不受影响——本阶段不碰 ``chunked_at``，所以这批来源**不会**被判成需要重新解析
  或重新分块。
* **零 KG 变动**：不碰 ``knowledge_objects`` / ``extraction_runs`` /
  ``unified_kg_state``；``kg_mutation_seq`` 不动，检索索引与社区划分因此都不会
  被判陈旧。
* **不修改既有元素行**，只插入；不下载远程图片，不处理 data URI（后者归在线
  解析路径的 `_persist_markdown_data_uri`）；不新增前端（离线运维 CLI，与
  ``backfill-chunk-elements`` 等阶段同例）。
* 已持久化的旧答案不回溯补图——payload 冻结是既有语义。

## 刻意登记的偏离

标准分块管线（`chunking.build_chunks`）对既无图注又无描述的 image 元素一律跳过，
而本阶段会把（可能无图注的）图片元素 append 进 chunk 的 ``element_ids``。这是
一次针对"历史数据修复"的定向偏离：判据是 markdown 里图片引用与该段文字的物理
相邻关系（原始 PDF 的版面顺序）。答案带图的准入只有两条
（`evidence_context._citation_image`：``element_type='image'`` 且
``metadata.asset_id`` 非空），图注**不是**显示的必要条件。
"""
from __future__ import annotations

import json
import mimetypes
import time
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Sequence

from app.services.image_backfill import (
    ElementView,
    ImageIndex,
    PlannedImage,
    SourcePlan,
    build_image_index,
    normalize_text,
    plan_source_images,
)
from app.services.knowhow.assets import (
    ALLOWED_MIME_EXTENSIONS,
    AssetService,
    AssetValidationError,
)

#: 候选来源的 keyset 页大小。与 `backfill-source-facts` 的目标翻页同量级；纯
#: 分页几何，不改变任何结果。
SOURCE_PAGE = 500


class ImageBackfillPort(Protocol):
    """离线回填要的四个仓储口（SQLite / PostgreSQL 双后端逐字对等）。

    刻意声明在这里而不是 `app/repositories/ports.py`：那个文件的 Protocol 方法
    总数钉在零余量上限上（只许降），而这四个方法只有本模块一个消费方。"""

    def image_backfill_source_page(
        self, notebook_id: str, after_id: str, limit: int
    ) -> list[dict]: ...

    def image_backfill_source_state(self, source_id: str) -> dict: ...

    def apply_image_backfill(
        self,
        notebook_id: str,
        source_id: str,
        elements: Sequence[dict],
        chunk_element_ids: dict[str, list[str]],
        *,
        created_at: Any,
        updated_at: Any,
    ) -> None: ...

    def image_backfill_discard_assets(
        self, asset_ids: Sequence[str]
    ) -> list[dict]: ...


class ImageBackfillRepository(Protocol):
    """本阶段用到的 facade 面（`BatchIngestRepository` 的窄子集 + 资产三件套）。"""

    settings: Any
    storage_dir: Path
    maintenance: ImageBackfillPort

    def get_notebook(self, notebook_id: str) -> Any: ...
    def current_user(self) -> Any: ...
    def insert_notebook_asset(
        self,
        notebook_id: str,
        filename: str,
        mime: str,
        size: int,
        created_by: str,
        source_id: str | None = None,
    ) -> str: ...
    def get_notebook_asset(self, asset_id: str) -> dict | None: ...


class ImageBackfillDisabled(RuntimeError):
    """部署明确关掉了来源图片（``MINERU_RETURN_IMAGES=false``）。"""


def _totals() -> dict[str, int]:
    return {
        "sources_scanned": 0,
        "sources_changed": 0,
        "sources_failed": 0,
        "images_inserted": 0,
        "captions": 0,
    }


def _element_views(elements: Sequence[dict]) -> list[ElementView]:
    return [
        ElementView(
            id=element["id"],
            element_type=element["element_type"],
            norm=normalize_text(element["text"]),
        )
        for element in elements
    ]


def _existing_image_srcs(elements: Sequence[dict]) -> list[str]:
    """已经补过图的引用目标。

    判据是"image 元素 + ``metadata.asset_id`` 非空"——与显示准入
    （`evidence_context._citation_image`）逐字同一条，所以"界面上看得见的图"
    与"本阶段认为已补过的图"永远是同一批。"""
    out: list[str] = []
    for element in elements:
        if element["element_type"] != "image":
            continue
        metadata = element["metadata"]
        if not metadata.get("asset_id"):
            continue
        src = metadata.get("src")
        if isinstance(src, str) and src:
            out.append(src)
    return out


def _chunk_by_element(chunks: Sequence[dict]) -> dict[str, str]:
    """element_id -> chunk_id。

    一个元素理论上可以出现在多个 chunk 里（`build_chunks` 不会这么产出，但历史
    行不保证）；取**第一个**（chunks 按 id 序读回，也就是文档序），确定性优先。"""
    mapping: dict[str, str] = {}
    for chunk in chunks:
        for element_id in chunk["element_ids"]:
            mapping.setdefault(element_id, chunk["id"])
    return mapping


def plan_for_source(
    repo: ImageBackfillRepository,
    source: dict,
    image_index: ImageIndex,
) -> tuple[SourcePlan, dict]:
    """读盘 + 读库 + 纯计算，零写入（``--dry-run`` 只跑到这里为止）。"""
    source_id = source["id"]
    plan = SourcePlan(source_id=source_id)
    path_text = source.get("file_path") or ""
    if not path_text:
        plan.skip("no_file_path")
        return plan, {}
    path = Path(path_text)
    try:
        markdown = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        plan.skip("file_unreadable")
        return plan, {}

    state = repo.maintenance.image_backfill_source_state(source_id)
    elements = state["elements"]
    settings = repo.settings
    return (
        plan_source_images(
            source_id=source_id,
            markdown=markdown,
            elements=_element_views(elements),
            existing_image_srcs=_existing_image_srcs(elements),
            existing_element_ids=[element["id"] for element in elements],
            chunk_by_element=_chunk_by_element(state["chunks"]),
            image_index=image_index,
            max_images=int(settings.mineru_max_images_per_source),
            max_bytes=int(settings.mineru_max_image_bytes),
        ),
        state,
    )


def _element_row(image: PlannedImage, asset_id: str) -> dict:
    """一条待插入的图片元素。

    ``location_label`` 与 metadata 的键名以 `parsers.parse_markdown_text` 的
    image 分支为准（``Markdown image <ordinal>``；``caption``/``src``/
    ``asset_id``/``parser``），不自造拼写。刻意与解析路径**不同**的只有一处：
    解析路径在成功落资产时丢掉 ``src``，而这里两者都写——``src`` 就是重跑的
    增量判据（见 `_existing_image_srcs`），丢了它每次重跑都会把同一张图再补一
    遍。``parser`` 写 ``image_backfill`` 而不是 ``markdown``，这样这批行的出处
    在库里一眼可辨。"""
    metadata: dict[str, Any] = {
        "parser": "image_backfill",
        "src": image.src,
        "asset_id": asset_id,
    }
    if image.caption:
        metadata["caption"] = image.caption
    return {
        "id": image.element_id,
        "element_type": "image",
        "location_label": f"Markdown image {image.ordinal}",
        "text": image.caption,
        "metadata": metadata,
    }


def _guess_mime(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "image/jpeg"


def apply_plan(
    repo: ImageBackfillRepository,
    notebook_id: str,
    source_id: str,
    plan: SourcePlan,
    state: dict,
    *,
    now: Callable[[], str],
) -> int:
    """落资产 → 一个写事务落元素/chunk/反查行/updated_at。返回插入图片数。

    两段式是被 `AssetService.save_source_image` 的形状逼出来的（它自带事务并写
    盘）。中间的失败窗口由回滚补上：元素事务失败时，本次刚写的资产行连同磁盘
    文件一起删掉（`sweep_orphan_assets` 明确不扫 ``source_id`` 非空的行，孤儿
    留着没人回收），于是重跑从一个干净状态重新开始——重跑幂等因此是"重建"而
    不是"复用孤儿"。"""
    assets = AssetService(repo)
    created_by = repo.current_user().id
    written: list[str] = []
    rows: list[dict] = []
    max_bytes = int(repo.settings.mineru_max_image_bytes)
    try:
        for image in plan.images:
            try:
                data = image.source_path.read_bytes()
            except OSError:
                plan.skip("image_unreadable")
                continue
            mime = _guess_mime(image.source_path.name)
            if mime not in ALLOWED_MIME_EXTENSIONS:
                plan.skip("mime_rejected")
                continue
            try:
                asset = assets.save_source_image(
                    notebook_id,
                    source_id,
                    image.source_path.name,
                    mime,
                    data,
                    created_by,
                    max_bytes=max_bytes,
                )
            except (AssetValidationError, RuntimeError, OSError):
                plan.skip("asset_write_failed")
                continue
            written.append(asset["id"])
            rows.append(_element_row(image, asset["id"]))

        if not rows:
            return 0

        planned_by_id = {image.element_id: image for image in plan.images}
        chunk_element_ids: dict[str, list[str]] = {}
        for chunk in state["chunks"]:
            chunk_element_ids.setdefault(chunk["id"], list(chunk["element_ids"]))
        for row in rows:
            chunk_id = planned_by_id[row["id"]].chunk_id
            chunk_element_ids[chunk_id].append(row["id"])
        touched = {planned_by_id[row["id"]].chunk_id for row in rows}
        stamp = now()
        repo.maintenance.apply_image_backfill(
            notebook_id,
            source_id,
            rows,
            {
                chunk_id: element_ids
                for chunk_id, element_ids in chunk_element_ids.items()
                if chunk_id in touched
            },
            created_at=state["element_created_at"] or stamp,
            updated_at=stamp,
        )
    except BaseException:
        _discard_assets(repo, assets, written)
        raise
    return len(rows)


def _discard_assets(
    repo: ImageBackfillRepository, assets: AssetService, asset_ids: Sequence[str]
) -> None:
    """Best-effort 回滚：先删行（拿回路径）再 unlink 文件，两步都不抛。"""
    if not asset_ids:
        return
    try:
        removed = repo.maintenance.image_backfill_discard_assets(list(asset_ids))
    except Exception:  # pragma: no cover - 回滚本身失败不得掩盖原始异常
        return
    for row in removed:
        try:
            path = assets.path_for(row)
            if path.is_file():
                path.unlink()
        except OSError:  # pragma: no cover
            continue


def run_backfill_images(
    repo: ImageBackfillRepository,
    notebook_id: str,
    *,
    mineru_outputs: Sequence[Path],
    source_id: Optional[str] = None,
    limit: Optional[int] = None,
    dry_run: bool = False,
    report_path: Optional[Path] = None,
    now: Optional[Callable[[], str]] = None,
) -> dict[str, Any]:
    if not repo.settings.mineru_return_images:
        raise ImageBackfillDisabled(
            "MINERU_RETURN_IMAGES=false：部署已关闭来源图片，backfill-images 拒绝运行"
        )
    repo.get_notebook(notebook_id)  # KeyError if missing
    clock = now or (lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))

    started = time.perf_counter()
    image_index = build_image_index([Path(root) for root in mineru_outputs])
    print(
        f"backfill-images: notebook={notebook_id} images_indexed={len(image_index)} "
        f"dry_run={dry_run}",
        flush=True,
    )
    if image_index.duplicates:
        print(
            "  warning: 同名不同大小的图片(按先见者取): "
            f"{len(image_index.duplicates)}",
            flush=True,
        )

    totals = _totals()
    skipped: dict[str, int] = {}
    report = report_path.open("a", encoding="utf-8") if report_path else None
    try:
        for source in _iter_sources(repo, notebook_id, source_id, limit):
            totals["sources_scanned"] += 1
            try:
                plan, state = plan_for_source(repo, source, image_index)
                inserted = (
                    0
                    if dry_run or not plan.images
                    else apply_plan(
                        repo, notebook_id, source["id"], plan, state, now=clock
                    )
                )
            except Exception as exc:  # 单源失败隔离，不掀翻整跑
                totals["sources_failed"] += 1
                _write_report(
                    report,
                    {
                        "source_id": source["id"],
                        "status": "failed",
                        "error": type(exc).__name__,
                    },
                )
                print(
                    f"  FAILED {source['id']}: {type(exc).__name__}",
                    flush=True,
                )
                continue
            if inserted:
                totals["sources_changed"] += 1
            totals["images_inserted"] += inserted
            totals["captions"] += plan.captions
            for reason, count in plan.skipped.items():
                skipped[reason] = skipped.get(reason, 0) + count
            _write_report(
                report,
                {
                    "source_id": source["id"],
                    "status": "planned" if dry_run else "applied",
                    "file_name": source.get("file_name", ""),
                    "candidates": len(plan.images),
                    "inserted": inserted,
                    "captions": plan.captions,
                    "coverage": round(plan.coverage, 4),
                    "skipped": dict(plan.skipped),
                },
            )
    finally:
        if report is not None:
            report.close()

    result: dict[str, Any] = dict(totals)
    result["skipped"] = skipped
    result["images_indexed"] = len(image_index)
    result["duplicate_names"] = len(image_index.duplicates)
    result["elapsed_s"] = round(time.perf_counter() - started, 1)
    print(f"backfill-images done: {result}", flush=True)
    return result


def _iter_sources(
    repo: ImageBackfillRepository,
    notebook_id: str,
    source_id: Optional[str],
    limit: Optional[int],
):
    if source_id:
        # 页按 id 升序，所以一旦整页都排在目标之后就不必再翻（试点用的单源
        # 开关不值得为它新开一条查询，但也不该白扫完整本库）。
        for page in _source_pages(repo, notebook_id):
            for row in page:
                if row["id"] == source_id:
                    yield row
                    return
            if page[-1]["id"] > source_id:
                return
        return
    seen = 0
    for page in _source_pages(repo, notebook_id):
        for row in page:
            if limit is not None and seen >= limit:
                return
            seen += 1
            yield row


def _source_pages(repo: ImageBackfillRepository, notebook_id: str):
    after = ""
    while True:
        page = repo.maintenance.image_backfill_source_page(
            notebook_id, after, SOURCE_PAGE
        )
        if not page:
            return
        yield page
        after = page[-1]["id"]


def _write_report(handle, entry: dict) -> None:
    """逐源明细。只写计数、稳定 reason code 与来源身份——绝不写图片字节或正文。"""
    if handle is None:
        return
    handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
