"""单张 knowhow 表跨 notebook 的复制/移动编排。

复用整本拷贝（notebook_sharing.copy_notebook）验证过的 K-1 稳定-id 派生物方案：
source_elements 用 element_id(new_row,new_col) 重算、chunks 用 cell_chunk_id 重算、
chunk_embeddings 随 chunk id 原样搬 → 拷完调度 project_table，text/section_path 未变
→ 零重嵌入。业务表 id 全新映射；cells 的 asset:// 引用改写；资产磁盘文件随迁。

routes 直接调本模块函数（沿用 knowhow_api 的「routes→模块函数(repo)」惯例）。
"""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

from app.services.knowhow.assets import ALLOWED_MIME_EXTENSIONS
from app.services.knowhow.api import build_projector, get_scheduler
from app.services.knowhow.projection import cell_chunk_id, element_id
from app.services.notebook_sharing import _ASSET_REF_RE, _rewrite_asset_refs

_log = logging.getLogger("silicon_notebook.knowhow.transfer")


def _remap(
    repo: Any, snapshot: dict, target_notebook_id: str, actor_id: str
) -> tuple[dict, list]:
    seams = repo._runtime.seams
    new = seams.new_id
    now = seams.now()

    src_table = snapshot["table"]
    src_notebook_id = src_table["notebook_id"]

    khtbl_map = {src_table["id"]: new("khtbl")}
    khcol_map: dict[str, str] = {}
    khrow_map: dict[str, str] = {}
    asset_map: dict[str, str] = {}
    source_map: dict[str, str] = {}

    # 隐藏源（可能不存在：源表从未投影）
    source_out = None
    if snapshot["source"]:
        src_source = dict(snapshot["source"])
        new_source_id = new("src")
        source_map[src_source["id"]] = new_source_id
        src_source["id"] = new_source_id
        src_source["notebook_id"] = target_notebook_id
        src_source["memory_id"] = ""  # 防御：knowhow 隐藏源无 memory 关联
        source_out = src_source

    table_out = dict(src_table)
    table_out["id"] = khtbl_map[src_table["id"]]
    table_out["notebook_id"] = target_notebook_id
    table_out["created_by"] = actor_id
    table_out["created_at"] = now
    table_out["updated_at"] = now
    old_hidden = src_table.get("hidden_source_id")
    table_out["hidden_source_id"] = source_map.get(old_hidden) if old_hidden else None

    columns_out = []
    for col in snapshot["columns"]:
        col = dict(col)
        new_col = new("khcol")
        khcol_map[col["id"]] = new_col
        col["id"] = new_col
        col["table_id"] = khtbl_map[col["table_id"]]
        columns_out.append(col)

    rows_out = []
    for row in snapshot["rows"]:
        row = dict(row)
        new_row = new("khrow")
        khrow_map[row["id"]] = new_row
        row["id"] = new_row
        row["table_id"] = khtbl_map[row["table_id"]]
        row["projection_status"] = "pending"
        rows_out.append(row)

    # 收集本表 cells 引用到的资产（仅这些随迁）
    referenced: set[str] = set()
    for cell in snapshot["cells"]:
        for match in _ASSET_REF_RE.findall(cell["content_md"] or ""):
            referenced.add(match)
    asset_files = []
    assets_out = []
    for old_asset_id in sorted(referenced):
        asset = repo.get_notebook_asset(old_asset_id)
        if asset is None:
            continue
        new_asset_id = new("asset")
        asset_map[old_asset_id] = new_asset_id
        asset_files.append((old_asset_id, new_asset_id, asset["mime"], src_notebook_id))
        row = dict(asset)
        row["id"] = new_asset_id
        row["notebook_id"] = target_notebook_id
        # 独立副本：源资产行的 source_id 指向源 notebook 里的某个 source
        # （MinerU 派生资产才非空；粘贴上传本就是 NULL）。原样带过去的话，
        # knowhow_store.delete_source_asset_rows 的
        # `DELETE FROM notebook_assets WHERE source_id=?` 不按 notebook 收窄——
        # 源那边删/重解析该 source 会把这条已经跑到别的 notebook 里的副本行
        # 也一并删掉。spec §4.3 承诺的是「独立副本」，副本不该继续挂在源
        # notebook 任何 source 的生命周期上。
        row["source_id"] = None
        assets_out.append(row)

    cells_out = []
    for cell in snapshot["cells"]:
        cell = dict(cell)
        cell["id"] = new("khcel")
        cell["row_id"] = khrow_map[cell["row_id"]]
        cell["column_id"] = khcol_map[cell["column_id"]]
        cell["content_md"] = _rewrite_asset_refs(cell.get("content_md") or "", asset_map)
        cells_out.append(cell)

    cell_code_out = []
    for code in snapshot["cell_code"]:
        code = dict(code)
        code["id"] = new("khcode")
        code["row_id"] = khrow_map[code["row_id"]]
        code["column_id"] = khcol_map[code["column_id"]]
        cell_code_out.append(code)

    # 派生产物：稳定 id 重算（零重嵌入的关键）
    element_map: dict[str, str] = {}
    element_row_new: dict[str, str] = {}
    elements_out = []
    for el in snapshot["elements"]:
        el = dict(el)
        old_id = el["id"]
        metadata = json.loads(el.get("metadata") or "{}")
        kh_meta = dict(metadata.get("knowhow") or {})
        new_row = khrow_map[kh_meta["row_id"]]
        new_col = khcol_map[kh_meta["column_id"]]
        new_el = element_id(new_row, new_col)
        element_map[old_id] = new_el
        element_row_new[old_id] = new_row
        kh_meta["table_id"] = khtbl_map.get(kh_meta.get("table_id"), kh_meta.get("table_id"))
        kh_meta["row_id"] = new_row
        kh_meta["column_id"] = new_col
        metadata["knowhow"] = kh_meta
        el["metadata"] = json.dumps(metadata, ensure_ascii=False)
        el["id"] = new_el
        el["source_id"] = source_map[el["source_id"]]
        elements_out.append(el)

    chunk_map: dict[str, str] = {}
    chunks_out = []
    for chunk in snapshot["chunks"]:
        chunk = dict(chunk)
        old_chunk_id = chunk["id"]
        old_element_ids = json.loads(chunk.get("element_ids") or "[]")
        if not old_element_ids:
            raise ValueError(f"knowhow chunk {old_chunk_id} 缺 element_ids，无法重算稳定 id")
        new_row = element_row_new[old_element_ids[0]]
        part = int(old_chunk_id.rsplit("-", 1)[-1])
        new_chunk_id = cell_chunk_id(new_row, part)
        chunk_map[old_chunk_id] = new_chunk_id
        chunk["id"] = new_chunk_id
        chunk["notebook_id"] = target_notebook_id
        chunk["source_id"] = source_map[chunk["source_id"]]
        chunk["element_ids"] = json.dumps(
            [element_map.get(e, e) for e in old_element_ids], ensure_ascii=False
        )
        chunks_out.append(chunk)

    vectors_out = []
    for vec in snapshot["chunk_embeddings"]:
        vec = dict(vec)
        vec["chunk_id"] = chunk_map[vec["chunk_id"]]
        vec["notebook_id"] = target_notebook_id
        vectors_out.append(vec)

    payload = {
        "table": table_out,
        "columns": columns_out,
        "rows": rows_out,
        "cells": cells_out,
        "cell_code": cell_code_out,
        "assets": assets_out,
        "source": source_out,
        "elements": elements_out,
        "chunks": chunks_out,
        "chunk_embeddings": vectors_out,
    }
    return payload, asset_files


def copy_table(
    repo: Any, source_table_id: str, target_notebook_id: str, actor_id: str
) -> str:
    store = repo._runtime.knowhow_transfer_store
    snapshot = store.snapshot_table(source_table_id)
    payload, asset_files = _remap(repo, snapshot, target_notebook_id, actor_id)
    expected_counts = {
        "columns": len(snapshot["columns"]),
        "rows": len(snapshot["rows"]),
        "cells": len(snapshot["cells"]),
        "cell_code": len(snapshot["cell_code"]),
    }
    store.insert_transfer(payload, expected_counts)  # 单事务 + 提交前校验

    # 事务提交后落资产磁盘文件（设计文档 §4.1/§7：单文件 copy2 失败逐一记事件
    # 并跳过，不回滚已提交的 DB）。best-effort 是硬要求而非风格选择：DB 事务此
    # 时已 COMMIT，若某个文件的 copy2 抛出（磁盘满/权限/竞态删除）而异常向上冒
    # 泡，下面的 schedule() 就永远不会被调用——副本表会带着一堆 pending 行永久
    # 卡住、KG 一次都不会建，而 DB 里它看起来完全正常。丢一张图片远比丢整张表
    # 的投影轻。mkdir 一并纳入保护，理由相同（它失败同样会跳过 schedule）。
    if asset_files:
        try:
            assets_root = Path(repo.storage_dir) / "assets"
            dest_dir = assets_root / target_notebook_id
            dest_dir.mkdir(parents=True, exist_ok=True)
            for old_id, new_id, mime, src_nb in asset_files:
                ext = ALLOWED_MIME_EXTENSIONS.get(mime, "bin")
                src = assets_root / src_nb / f"{old_id}.{ext}"
                if not src.is_file():
                    continue
                try:
                    shutil.copy2(src, dest_dir / f"{new_id}.{ext}")
                except Exception:  # noqa: BLE001 — 单个资产失败不许拖垮已提交的拷贝
                    _log.warning(
                        "copy_table：副本 %s 的资产 %s→%s 落盘失败，跳过该文件",
                        payload["table"]["id"], old_id, new_id, exc_info=True,
                    )
        except Exception:  # noqa: BLE001 — 同上，资产目录准备失败也不许跳过 schedule
            _log.warning(
                "copy_table：副本 %s 的资产目录准备失败，跳过全部资产落盘",
                payload["table"]["id"], exc_info=True,
            )

    new_table_id = payload["table"]["id"]
    try:
        get_scheduler(repo).schedule(new_table_id)  # 后台重建 KG objects/relations
    except Exception:  # noqa: BLE001 — 同上一段：DB 已提交，这里也不许把一次
        # 已成功的拷贝变成调用方看到的裸异常。schedule() 本身能抛
        # （ProjectionScheduler.schedule → threading.Timer.start() 在线程耗尽
        # 时会抛 RuntimeError）；不 guard 的话，move_table 会因为这个函数没能
        # 正常返回而整个跳过它自己的 SourceCleanupFailed→409 包装（那段 try
        # 根本没机会进入），退化成路由层的裸 500——用户分不清"什么都没发生"
        # 和"副本已经落地"，重试只会在目标堆更多重复表。副本本身完整（KG
        # 重建只是留在 pending，「重建投影」逃生口可以手动补），不值得为它
        # 牺牲整次调用的诚实返回。
        _log.exception(
            "copy_table：副本 %s 已提交，但调度 KG 重建失败，表会带 pending 行",
            new_table_id,
        )
    return new_table_id


class SourceCleanupFailed(Exception):
    """移动时复制已成功、但删源失败：副本已在目标存在，源仍在，可安全重试删源。"""

    def __init__(self, new_table_id: str, cause: Exception) -> None:
        super().__init__(str(cause))
        self.new_table_id = new_table_id
        self.cause = cause


def move_table(
    repo: Any, source_table_id: str, target_notebook_id: str, actor_id: str
) -> str:
    # 四步顺序是这个函数的全部要害，三条边界都不能动：
    # ① 先复制、复制成功了才碰源——copy_table 抛异常时源必须分毫未动。
    # ② hidden_source_id 在复制**之后**、删表行之前才读。约束只是「读要早于
    #    DELETE」，不是「早于复制」；而放到复制之前会让这个读跨越整个复制窗口
    #    （快照+事务+资产落盘）形成 TOCTOU：若源表在这期间**首次**被投影，
    #    读到的 hidden 是 None → 拆投影被跳过、表行照删 → 又变成 ③ 说的那种
    #    不可回收的幽灵；若并发的 ensure_hidden_source 顶替了 id
    #    （projection.py:275-282），拿着旧 id 去拆会在 projection.py:702-704
    #    静默 no-op，把新的那份漏成孤儿。读得越晚，窗口越窄。
    # ③ 拆源投影在删源表之前。反过来的话，拆投影一旦失败（copy_table 紧邻
    #    调度了后台重投影，此刻必然有并发 SQLite 写者，busy timeout 上吃到
    #    OperationalError 是现实可能），源表行已经没了，而 hidden_source_id
    #    只存在于那条被删掉的行里：源库的 chunks/chunks_fts/chunk_embeddings/
    #    KO 会永久且不可回收地活着（源 notebook 继续检索到已搬走的内容），
    #    重试也无门——copy_table→snapshot_table 会因表已删而抛 KeyError。
    #    按现在的顺序，任何一步失败都只留下「两边都在」的可恢复重复：用户
    #    可以重试搬迁，也可以照常删掉源表。宁可重复，绝不丢失。
    new_table_id = copy_table(repo, source_table_id, target_notebook_id, actor_id)
    # A3 评审附加需求：copy_table 已经 COMMIT，从这里往下任何一步再炸，副本
    # 都已经是既成事实——裸异常冒泡上去只会变成路由层的通用 500，调用方分不清
    # "整个搬迁没发生"和"副本已经在、只是源没删干净"，naive 重试会在目标侧
    # 越堆越多重复副本。把这一段(读 hidden_source_id + 拆源投影 + 删源表行)
    # 整体收进一个类型化错误，让路由能把 new_table_id 一并带回给调用方。
    # 注意 copy_table(...) 本身特意留在这个 try 之外——复制失败必须原样冒泡
    # （源分毫未动、什么都没创建），不能被这里的包装误伤。
    try:
        hidden = repo.get_knowhow_table(source_table_id).get("hidden_source_id")
        if hidden:
            build_projector(repo).delete_table_projection(hidden)
        repo.delete_knowhow_table(source_table_id)
    except Exception as exc:  # noqa: BLE001 — 副本已提交，必须转成可辨识的类型化错误
        # 必须在这里落日志：`from exc` 保住了 __cause__，但路由会把它转成
        # HTTPException，而 FastAPI 渲染 HTTPException 是不带 traceback 的——
        # 真在生产触发时（用户手上留下一份需要人工对账的重复副本），服务端
        # 将没有任何关于「为什么失败」的记录。_log.exception 带上原始堆栈。
        _log.exception(
            "move_table：副本 %s 已提交，但清理源表 %s 失败", new_table_id, source_table_id
        )
        raise SourceCleanupFailed(new_table_id, exc) from exc
    return new_table_id


def transfer_table(
    repo: Any,
    source_table_id: str,
    target_notebook_id: str,
    actor_id: str,
    mode: str,
) -> str:
    if mode == "copy":
        return copy_table(repo, source_table_id, target_notebook_id, actor_id)
    if mode == "move":
        return move_table(repo, source_table_id, target_notebook_id, actor_id)
    raise ValueError(f"unknown transfer mode: {mode}")
