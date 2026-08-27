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
* 不下载远程图片，不处理 data URI（后者归在线解析路径的
  `_persist_markdown_data_uri`）；不新增前端（离线运维 CLI，与
  ``backfill-chunk-elements`` 等阶段同例）。
* 已持久化的旧答案不回溯补图——payload 冻结是既有语义。

## 对原规格「只插入、不修改既有元素行」的一次显式修订

生产解析路径对**带 alt** 的相对路径图片会产出一条 image 元素并写下
``metadata.src``，但拿不到 ``asset_id``（单文件 markdown 路径不传
`resolve_image`）。这类行既不在"已补过"的集合里（那条判据是 asset_id 非空），
又与本次引用指向同一个 src——按"只插入"处理就会给同一张图造出第二条元素行，
两条都进 chunk、两条都能被引用带图取到。所以对**这一类**元素改为**就地补齐**：
落资产之后只 UPDATE 它的 ``metadata``（补 ``asset_id``），``text``/``caption``/
``id``/``created_at`` 一律不动。除此之外仍然只插入，不改任何既有元素行。

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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol, Sequence

from app.services.image_backfill import (
    ElementView,
    EnrichedImage,
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

    def image_backfill_resolve_source_path(self, file_path: str) -> str: ...

    def apply_image_backfill(
        self,
        notebook_id: str,
        source_id: str,
        elements: Sequence[dict],
        chunk_element_ids: dict[str, list[str]],
        metadata_updates: Sequence[dict],
        *,
        created_at: Any,
    ) -> None: ...

    def image_backfill_source_asset_ids(
        self, notebook_id: str, source_id: str
    ) -> list[str]: ...

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


class ImageBackfillRefused(RuntimeError):
    """整跑的前置条件不成立，一个来源都不该扫。CLI 把它翻成退出码 2。"""


class ImageBackfillDisabled(ImageBackfillRefused):
    """部署明确关掉了来源图片（``MINERU_RETURN_IMAGES=false``）。"""


class ImageBackfillIndexEmpty(ImageBackfillRefused):
    """一个 ``--mineru-output`` 都没索引到图片。

    这几乎一定是路径给错了（拼错、挂载点没挂上、给成了 `auto/` 下一层）。带着
    空索引跑下去的结果是一次**看起来正常**的全库扫描：每一张图都记
    ``image_not_found``，汇总一片零，而运维会读成"原图确实找不回来了"。宁可
    早退报错。"""


def _totals() -> dict[str, int]:
    return {
        "sources_scanned": 0,
        "sources_changed": 0,
        "sources_failed": 0,
        # 计划出来的候选（dry-run 唯一能报的"能补多少张"）。插入与就地补齐分列：
        # 两者的运维含义不同——前者新增元素行，后者只补既有行的 asset_id。
        "candidates_insert": 0,
        "candidates_enrich": 0,
        "images_inserted": 0,
        "images_enriched": 0,
        # 实际落地的图里带图注的张数（不是计划里的——被 mime/读盘/资产校验拦下
        # 的那些不该算进"收割到多少图注"）。dry-run 一张都不落，那时改取计划命中
        # 数，否则这个数恒为 0、把 dry-run 的估算用途废掉。
        "captions": 0,
        # 本趟新出现、却没有任何元素引用的资产行被扫掉了几条（见
        # `_sweep_pass_orphans`）。正常跑恒为 0，非零本身就是信号。
        "orphan_assets_removed": 0,
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
    与"本阶段认为已补过的图"永远是同一批。

    这一条判据**不可省**：删掉 ``asset_id`` 那半，带 alt 的既有 image 元素
    （它有 src、没有 asset_id）就会被算成"已补过"而整批漏掉；反过来，只按 src
    去重、不看 asset_id，重跑就不再幂等。另一半（有 src、无 asset_id）由
    `_unassigned_image_srcs` 接手就地补齐。"""
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


def _unassigned_image_srcs(elements: Sequence[dict]) -> dict[str, str]:
    """``src -> element_id``：有 ``metadata.src``、但 ``asset_id`` 为空的 image
    元素（见 `image_backfill.EnrichedImage`）。

    ``elements`` 已按 id 序读回，所以 ``setdefault`` 取的就是"同 src 多条时的
    id 序第一条"——其余同 src 元素一律不动（多条同 src 是历史畸形数据，把每条
    都指到同一个资产只会让引用带图重复显示同一张图）。"""
    out: dict[str, str] = {}
    for element in elements:
        if element["element_type"] != "image":
            continue
        metadata = element["metadata"]
        if metadata.get("asset_id"):
            continue
        src = metadata.get("src")
        if isinstance(src, str) and src:
            out.setdefault(src, element["id"])
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
    # 路径解析走产品统一的那一条（`SourceFileStore.resolve_path`，也就是数据库
    # 边界的 `resolve_path`：绝对路径原样、相对路径按仓库根解析），**绝不**裸
    # `Path(path_text)`。历史来源的 `sources.file_path` 可以是仓库根相对路径，
    # 而这条离线命令从任意 CWD 启动都合法——裸 Path 会按进程 CWD 解析，于是
    # 整批历史来源被误报成 `file_unreadable`，看起来像"文件都没了"。
    path = Path(repo.maintenance.image_backfill_resolve_source_path(path_text))
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
            existing_unassigned_srcs=_unassigned_image_srcs(elements),
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
    """按扩展名猜 mime；猜不出返回空串，调用方按 ``mime_rejected`` 跳过。

    刻意**不**兜底成 ``image/jpeg``：MinerU 只产 jpg/png，所以兜底一次都救不
    了真图片，只会把一个扩展名不认识的文件（``.bin``/``.tmp``/被截断的名字）
    当成 JPEG 写进资产表——落库的 mime 是错的，而错 mime 会让浏览器拿到一张
    永远渲染不出来的图。"""
    return mimetypes.guess_type(name)[0] or ""


def _metadata_update_row(image: EnrichedImage, existing: dict, asset_id: str) -> dict:
    """就地补齐用的 metadata：既有键原样保留，只补 ``asset_id``。

    ``src`` 刻意留着（在线解析路径在成功落资产时会丢掉它，这里不能丢——它是
    重跑的增量判据）；``parser`` 也保持既有值（``markdown``），这条行确实是解析
    路径产出的，改写它等于伪造出处。"""
    metadata = dict(existing)
    metadata["asset_id"] = asset_id
    return {"id": image.element_id, "metadata": metadata}


@dataclass(frozen=True)
class ApplyOutcome:
    inserted: int = 0
    enriched: int = 0
    captions: int = 0
    orphan_assets_removed: int = 0


def _referenced_asset_ids(
    state: dict, rows: Sequence[dict] = (), updates: Sequence[dict] = ()
) -> set[str]:
    """这个来源的元素当前引用着哪些资产 id。

    ``rows``/``updates`` 只有在它们**已经提交**之后才该传进来——回滚路径传空，
    正是那样才让本次刚写下的资产行落进"没人引用"那一边、被扫掉。"""
    referenced: set[str] = set()
    sources: list[Any] = [element.get("metadata") for element in state.get("elements", ())]
    sources.extend(row.get("metadata") for row in rows)
    sources.extend(update.get("metadata") for update in updates)
    for metadata in sources:
        if not isinstance(metadata, dict):
            continue
        asset_id = metadata.get("asset_id")
        if isinstance(asset_id, str) and asset_id:
            referenced.add(asset_id)
    return referenced


def apply_plan(
    repo: ImageBackfillRepository,
    notebook_id: str,
    source_id: str,
    plan: SourcePlan,
    state: dict,
) -> ApplyOutcome:
    """落资产 → 一个写事务落元素/metadata/chunk/反查行/updated_at。

    两段式是被 `AssetService.save_source_image` 的形状逼出来的（它自带事务并写
    盘）。中间有两个失败窗口，都由**本趟范围内的孤儿清扫**收口：

    1. 元素事务失败——本次写下的资产行连同磁盘文件一起删掉；
    2. `save_source_image` **先提交 `notebook_assets` 行、后写盘**
       （`assets.py`），写盘/落盘校验失败时它抛异常，调用方**拿不到 asset id**，
       于是那一行既不在 ``written`` 里、又永远没人引用。

    `sweep_orphan_assets` 明确不扫 ``source_id`` 非空的行，所以这两类孤儿没有任何
    后台会回收——必须在这里扫。判据是「本趟**新出现**的、且不被该来源任何元素
    引用的资产行」：先在动手之前快照一次该来源的资产 id，收尾时只考虑差集。

    **刻意不用更宽的判据**（"该来源全部无人引用的资产行"，也就是 docs 里写给人工
    清理用的那条）：深拷贝会为 `notebook_assets` 铸新 id，却**不**重映射
    `source_elements.metadata.asset_id`（`notebook_sharing.py` 的 json_maps 只含
    element/source/object 三类 id）——实测一本副本里每一条来源图片资产行都"无人
    引用"。广义清扫会把副本的资产行连同磁盘文件全删掉，那是本工具不该碰的数据。
    历史 kill-9 残留仍按 docs 的判据人工清理。
    """
    assets = AssetService(repo)
    created_by = repo.current_user().id
    written: list[str] = []
    rows: list[dict] = []
    updates: list[dict] = []
    metadata_by_id = {element["id"]: element["metadata"] for element in state["elements"]}
    max_bytes = int(repo.settings.mineru_max_image_bytes)
    # 动手之前的快照：清扫只作用于这之后新出现的行。
    assets_before = set(
        repo.maintenance.image_backfill_source_asset_ids(notebook_id, source_id)
    )

    def _store(image) -> str:
        """读盘 + 落资产；失败时记账并返回空串。"""
        try:
            data = image.source_path.read_bytes()
        except OSError:
            plan.skip("image_unreadable")
            return ""
        mime = _guess_mime(image.source_path.name)
        if mime not in ALLOWED_MIME_EXTENSIONS:
            plan.skip("mime_rejected")
            return ""
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
            return ""
        written.append(asset["id"])
        return asset["id"]

    try:
        for image in plan.images:
            asset_id = _store(image)
            if asset_id:
                rows.append(_element_row(image, asset_id))
        for enriched in plan.enriched:
            asset_id = _store(enriched)
            if asset_id:
                updates.append(
                    _metadata_update_row(
                        enriched, metadata_by_id.get(enriched.element_id, {}), asset_id
                    )
                )

        if not rows and not updates:
            # 一张都没落成，但 `_store` 可能已经在 `asset_write_failed` 上留下了
            # 提交过的资产行——这条路同样要扫。
            return ApplyOutcome(
                orphan_assets_removed=_sweep_pass_orphans(
                    repo, assets, notebook_id, source_id, assets_before,
                    _referenced_asset_ids(state),
                )
            )
        created_at = state["element_created_at"]
        if rows and not created_at:
            # 该来源一条元素都没有——对齐因此不可能匹配到任何锚点，走到这里说明
            # 上游算错了。宁可整源失败也不要用"现在"当元素批次时间戳（那会把
            # 图片在详情分页里全部沉底，并让命令目录的来源代次平白漂移）。
            raise RuntimeError(
                f"image backfill for {source_id} has no element generation stamp"
            )

        planned_by_id = {image.element_id: image for image in plan.images}
        enriched_by_id = {item.element_id: item for item in plan.enriched}
        chunk_element_ids: dict[str, list[str]] = {}
        for chunk in state["chunks"]:
            chunk_element_ids.setdefault(chunk["id"], list(chunk["element_ids"]))
        touched: set[str] = set()
        for row in rows:
            chunk_id = planned_by_id[row["id"]].chunk_id
            chunk_element_ids[chunk_id].append(row["id"])
            touched.add(chunk_id)
        for update in updates:
            # 就地补齐的元素多半已经在某个 chunk 里（带图注的图会进分块），那时
            # `chunk_id` 是空串、chunk 零改动。
            chunk_id = enriched_by_id[update["id"]].chunk_id
            if not chunk_id:
                continue
            chunk_element_ids[chunk_id].append(update["id"])
            touched.add(chunk_id)
        repo.maintenance.apply_image_backfill(
            notebook_id,
            source_id,
            rows,
            {
                chunk_id: element_ids
                for chunk_id, element_ids in chunk_element_ids.items()
                if chunk_id in touched
            },
            updates,
            created_at=created_at,
        )
        # `try` 块必须**到此为止**：再往里放代码，一次提交成功之后才抛的异常会
        # 走下面那条回滚，而回滚用的引用集不含 rows/updates，刚提交的资产会被
        # 当成孤儿删掉。
    except BaseException:
        # 什么都没提交，所以 rows/updates 不算引用——本次写下的资产行（含
        # `asset_write_failed` 那些拿不到 id 的）全落进差集，被扫干净。
        _sweep_pass_orphans(
            repo, assets, notebook_id, source_id, assets_before,
            _referenced_asset_ids(state),
        )
        raise
    landed = {row["id"] for row in rows}
    captions = sum(
        1
        for image in plan.images
        if image.element_id in landed and image.caption
    )
    return ApplyOutcome(
        inserted=len(rows),
        enriched=len(updates),
        captions=captions,
        orphan_assets_removed=_sweep_pass_orphans(
            repo, assets, notebook_id, source_id, assets_before,
            _referenced_asset_ids(state, rows, updates),
        ),
    )


def _sweep_pass_orphans(
    repo: ImageBackfillRepository,
    assets: AssetService,
    notebook_id: str,
    source_id: str,
    assets_before: set[str],
    referenced: set[str],
) -> int:
    """删掉本趟新出现、却没有任何元素引用的资产行与文件；返回删除条数。

    Best-effort：清扫自身的失败绝不掩盖调用路径上的原始异常，也绝不掀翻一次
    本来成功的来源。差集 `assets_before` 是硬性的——没有它，判据就会误伤深拷贝
    留下的资产行（见 `apply_plan` 的 docstring）。"""
    try:
        current = set(
            repo.maintenance.image_backfill_source_asset_ids(notebook_id, source_id)
        )
    except Exception:  # pragma: no cover - 读不到就不扫，绝不猜
        return 0
    orphans = sorted((current - assets_before) - referenced)
    if not orphans:
        return 0
    try:
        removed = repo.maintenance.image_backfill_discard_assets(orphans)
    except Exception:  # pragma: no cover - 清扫失败不得掩盖原始异常
        return 0
    for row in removed:
        try:
            path = assets.path_for(row)
            if path.is_file():
                path.unlink()
        except OSError:  # pragma: no cover
            continue
    return len(removed)


def run_backfill_images(
    repo: ImageBackfillRepository,
    notebook_id: str,
    *,
    mineru_outputs: Sequence[Path],
    source_id: Optional[str] = None,
    after_id: str = "",
    limit: Optional[int] = None,
    dry_run: bool = False,
    report_path: Optional[Path] = None,
) -> dict[str, Any]:
    if not repo.settings.mineru_return_images:
        raise ImageBackfillDisabled(
            "MINERU_RETURN_IMAGES=false：部署已关闭来源图片，backfill-images 拒绝运行"
        )
    repo.get_notebook(notebook_id)  # KeyError if missing

    started = time.perf_counter()
    roots = [Path(root) for root in mineru_outputs]
    missing = [root for root in roots if not root.is_dir()]
    if missing and len(missing) == len(roots):
        # 单个 root 缺失仍然容忍（多路径运行里一条挂载点没上来不该废掉整跑），
        # 全部缺失才拒绝——那不是"图找不回来"，那是路径给错了。
        raise ImageBackfillIndexEmpty(
            f"backfill-images：全部 {len(roots)} 个 --mineru-output 都不是目录，"
            "拒绝以空索引做一次全库空扫"
        )
    image_index = build_image_index(roots)
    if not len(image_index):
        raise ImageBackfillIndexEmpty(
            "backfill-images：--mineru-output 树里一个 images/ 下的文件都没索引到，"
            "拒绝以空索引做一次全库空扫（检查路径是否指向 output 根目录）"
        )
    print(
        f"backfill-images: notebook={notebook_id} images_indexed={len(image_index)} "
        f"dry_run={dry_run}",
        flush=True,
    )
    if missing:
        print(f"  warning: {len(missing)} 个 --mineru-output 不是目录，已跳过", flush=True)
    if image_index.duplicates:
        print(
            "  warning: 同名不同大小的图片(按先见者取): "
            f"{len(image_index.duplicates)}",
            flush=True,
        )

    totals = _totals()
    skipped: dict[str, int] = {}
    matched_source = False
    if report_path is not None:
        # append 语义（见 docs/operations）：目录可能还不存在，一次运维跑不该
        # 在扫完整本库之后才因为少一个目录而丢掉全部明细。
        report_path.parent.mkdir(parents=True, exist_ok=True)
    report = report_path.open("a", encoding="utf-8") if report_path else None
    try:
        for source in _iter_sources(repo, notebook_id, source_id, after_id, limit):
            matched_source = True
            totals["sources_scanned"] += 1
            try:
                plan, state = plan_for_source(repo, source, image_index)
                if dry_run:
                    # 图注命中数取**计划**里的（`plan.captions`）。真跑时它按实际
                    # 落地的图重算（被 mime/读盘/资产校验拦下的不算），但 dry-run
                    # 一张都不落——照那条口径就恒为 0，逐源行、JSONL 与汇总会齐刷刷
                    # 报「图注 0」，恰好废掉 dry-run 唯一的估算用途。
                    outcome = ApplyOutcome(captions=plan.captions)
                elif not (plan.images or plan.enriched):
                    outcome = ApplyOutcome()
                else:
                    outcome = apply_plan(
                        repo, notebook_id, source["id"], plan, state
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
            if outcome.inserted or outcome.enriched:
                totals["sources_changed"] += 1
            totals["candidates_insert"] += len(plan.images)
            totals["candidates_enrich"] += len(plan.enriched)
            totals["images_inserted"] += outcome.inserted
            totals["images_enriched"] += outcome.enriched
            totals["captions"] += outcome.captions
            totals["orphan_assets_removed"] += outcome.orphan_assets_removed
            for reason, count in plan.skipped.items():
                skipped[reason] = skipped.get(reason, 0) + count
            entry = {
                "source_id": source["id"],
                "status": "planned" if dry_run else "applied",
                "file_name": source.get("file_name", ""),
                "candidates": len(plan.images) + len(plan.enriched),
                "candidates_insert": len(plan.images),
                "candidates_enrich": len(plan.enriched),
                "inserted": outcome.inserted,
                "enriched": outcome.enriched,
                "captions": outcome.captions,
                "orphan_assets_removed": outcome.orphan_assets_removed,
                "coverage": round(plan.coverage, 4),
                "skipped": dict(plan.skipped),
            }
            _write_report(report, entry)
            if dry_run:
                # dry-run 的**唯一**产出就是这几行：不逐源打出来，运维就永远拿不
                # 到"这一跑能补多少张"（汇总只有一个总数，看不出是哪些源、也看
                # 不出锚定失败集中在哪）。
                _print_dry_run_line(entry)
    finally:
        if report is not None:
            report.close()

    if source_id and not matched_source:
        # 区分性提示：`--source-id` 一个都没命中有两种完全不同的原因，而汇总
        # 里的 `sources_scanned=0` 两种都长得一样。
        print(
            f"  note: --source-id {source_id} 未命中候选——它要么不属于 "
            f"notebook {notebook_id}/不存在，要么不是 .md/.markdown 来源"
            "（本阶段只处理 markdown 来源）",
            flush=True,
        )

    result: dict[str, Any] = dict(totals)
    result["skipped"] = skipped
    result["images_indexed"] = len(image_index)
    result["duplicate_names"] = len(image_index.duplicates)
    result["elapsed_s"] = round(time.perf_counter() - started, 1)
    print(f"backfill-images done: {result}", flush=True)
    return result


def _print_dry_run_line(entry: dict) -> None:
    """逐源一行：候选 / 锚定失败 / 缺图 / 图注命中（docs 承诺的那四个数）。"""
    skipped = entry["skipped"]
    anchor_failed = sum(
        count
        for reason, count in skipped.items()
        if reason in ("no_anchor", "no_chunk", "anchor_stale", "alignment_drifted")
    )
    print(
        f"  {entry['source_id']} cov={entry['coverage']:.2f} "
        f"候选={entry['candidates']}(新增 {entry['candidates_insert']}/"
        f"补齐 {entry['candidates_enrich']}) "
        f"锚定失败={anchor_failed} 缺图={skipped.get('image_not_found', 0)} "
        f"图注={entry['captions']}",
        flush=True,
    )


def _iter_sources(
    repo: ImageBackfillRepository,
    notebook_id: str,
    source_id: Optional[str],
    after_id: str,
    limit: Optional[int],
):
    if source_id:
        # 页按 id 升序，所以一旦整页都排在目标之后就不必再翻（试点用的单源
        # 开关不值得为它新开一条查询，但也不该白扫完整本库）。
        for page in _source_pages(repo, notebook_id, after_id):
            for row in page:
                if row["id"] == source_id:
                    yield row
                    return
            if page[-1]["id"] > source_id:
                return
        return
    seen = 0
    for page in _source_pages(repo, notebook_id, after_id):
        for row in page:
            if limit is not None and seen >= limit:
                return
            seen += 1
            yield row


def _source_pages(repo: ImageBackfillRepository, notebook_id: str, after_id: str = ""):
    after = after_id or ""
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
