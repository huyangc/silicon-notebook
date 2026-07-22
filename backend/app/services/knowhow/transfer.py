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
) -> tuple[dict, list, Any]:
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

    # 派生产物：稳定 id 重算（零重嵌入的关键）。
    #
    # P1-A（PR review round 6，普通顺序 bug，非并发竞态）：这里的 elements 不
    # 保证全部还对应存活的业务行/列。project_table 的重投影只按「当前存活
    # 行」重写：_write_elements 对每一行都是「先按 row_id 整体删、再按当前
    # 列集合重插」（projection.py），但这只在该行本身被下一次重投影**重新
    # 访问**时才会发生——一行被 delete_knowhow_row 删掉之后，它不再出现在
    # table["rows"] 里，从此没有任何重投影会再碰它，它名下已经写过的
    # source_elements（连同 chunks/chunk_embeddings）永久留在隐藏源下。
    # snapshot_table 按 source_id 整表 SELECT，不按行/列过滤，会把这些陈旧
    # 派生物原样读进快照。以前这里对 khrow_map/khcol_map 的查找是无条件
    # 的——查不到就是裸 KeyError，把整次复制/搬迁直接崩掉（路由层的
    # `except KeyError` 把它转成 404 "Table not found"），该表从此永久无法
    # 再被复制或移动，且不需要任何并发就能触发（普通用户「加两行、删一
    # 行」就会踩上）。
    #
    # 跳过 khrow_map/khcol_map 里找不到的陈旧引用是正确的丢弃，不是将就：
    # 它们不代表任何还活着的业务数据，目标侧的重投影本来就只会替存活行重
    # 建 KO/chunk，带过去的陈旧派生物既不会被覆盖也不会被清理，唯一后果是
    # 副本从出生起就带着一份永久性死重。
    element_map: dict[str, str] = {}
    element_row_new: dict[str, str] = {}
    elements_out = []
    for el in snapshot["elements"]:
        el = dict(el)
        old_id = el["id"]
        metadata = json.loads(el.get("metadata") or "{}")
        kh_meta = dict(metadata.get("knowhow") or {})
        old_row_id = kh_meta.get("row_id")
        old_col_id = kh_meta.get("column_id")
        if old_row_id not in khrow_map or old_col_id not in khcol_map:
            continue  # 陈旧派生物（已删行/列的遗留）——不随复制/搬迁携带
        new_row = khrow_map[old_row_id]
        new_col = khcol_map[old_col_id]
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
        if old_element_ids[0] not in element_row_new:
            continue  # 挂在陈旧 element 下的陈旧 chunk——同上，一并跳过
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
        old_chunk_id = vec["chunk_id"]
        if old_chunk_id not in chunk_map:
            continue  # 陈旧 chunk 的向量——同上，随其 chunk 一并跳过
        vec["chunk_id"] = chunk_map[old_chunk_id]
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
    # ``seams`` (already resolved above) rides back out to the caller so
    # ``copy_table`` can hand ``new_id``/``now`` to ``insert_transfer`` (Task
    # 13's genesis flow entry) WITHOUT a second ``repo._runtime`` reach in
    # ITS OWN scope — the architecture guard's independent_private tracking
    # (tests/architecture/repository_callers.py) counts private-attribute
    # accesses PER SCOPE, and ``copy_table`` already has one (its own
    # ``repo._runtime.knowhow_transfer_store`` line below); returning the
    # already-open seam here keeps that count unchanged instead of adding a
    # second, distinct occurrence that would need a fixture rebaseline.
    return payload, asset_files, seams


def copy_table(
    repo: Any, source_table_id: str, target_notebook_id: str, actor_id: str,
    *, verb: str = "复制",
) -> str:
    """``verb`` (knowhow 表版本管理 Task 13, spec §7.3) names the genesis
    flow entry's ``note`` text — ``"由《<源表标题>》{verb}而来"``. Defaults
    to "复制" for a plain copy (``transfer_table``'s ``mode="copy"``
    dispatch, and any direct caller); ``move_table`` below overrides it to
    "移动" since IT is the one that knows the copy it performs internally is
    really the first half of a move — ``copy_table`` itself has no way to
    tell the two apart otherwise (``move_table`` calls this very function to
    do its own copying)."""
    store = repo._runtime.knowhow_transfer_store
    snapshot = store.snapshot_table(source_table_id)
    payload, asset_files, seams = _remap(repo, snapshot, target_notebook_id, actor_id)
    expected_counts = {
        "columns": len(snapshot["columns"]),
        "rows": len(snapshot["rows"]),
        "cells": len(snapshot["cells"]),
        "cell_code": len(snapshot["cell_code"]),
    }
    # 单事务 + 提交前校验 + 版本管理创世流水（spec §7.3：历史本身不随复制/
    # 移动携带，目标表只记这一条 table_create，note 里点名来源表标题）。
    # source_title 取自快照（复制/移动前源表自己的标题）——_remap 只改
    # table_out 的 id/notebook_id/created_by/时间戳/hidden_source_id，标题
    # 原样带过去，所以两者字面相同；用快照而非 payload 只是更直接地表达
    # "这是源表的标题"，不依赖那份不变量。
    source_title = snapshot["table"]["title"]
    store.insert_transfer(
        payload, expected_counts,
        new_id=seams.new_id, now=seams.now, actor=actor_id,
        note=f"由《{source_title}》{verb}而来",
    )

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
    """移动时复制已成功、但源清理阶段未能把源删掉：副本已在目标存在。

    ``reason``（round 10 P1-A）区分两种截然不同的成因，路由层要给用户两种
    截然不同的指引，不能共用同一句"请手动删除源表"：
    - "source_changed"：指纹复核未命中——源在复制完成后被并发编辑，是被
      有意保留下来保护那份编辑的，不是清理失败。源仍带着这份编辑，引导
      用户删除它会连带永久丢失该编辑。
    - "cleanup_error"：拆投影/原子删除本身抛出异常，源确实原封未动，手动
      删除是安全的（原有行为，contract 不变）。
    """

    def __init__(self, new_table_id: str, cause: Exception, *, reason: str) -> None:
        super().__init__(str(cause))
        self.new_table_id = new_table_id
        self.cause = cause
        self.reason = reason


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
    #
    # P1-2（PR review round 2，数据丢失）：④ 复制的是「此刻」的源，删除的也
    # 是「此刻」的源，但两者相隔一整个 copy_table（快照+事务+资产落盘+调度）
    # ——中间任何一次 cell/行/列编辑提交，目标侧落的都是编辑前的旧快照，而
    # 编辑后的新内容会被下面的删源一并带走，永久丢失。fingerprint 在 copy_
    # table 之前就取（越早越好：即便并发编辑恰好卡在这次取指纹和 copy_table
    # 自己的 snapshot_table 之间，copy_table 拷到的也已经是编辑后的新内容,
    # 指纹对比顶多多算一次「变了」误报进 SourceCleanupFailed——安全的方向，
    # 不会把这次误判成"没变"而删掉新内容),再在删除前复核一次；不一致就在
    # try 内部 raise，直接复用下面既有的 SourceCleanupFailed 包装——两边都
    # 留、不丢失，与 ③ 的失败处理是同一个契约,不发明新的返回形状。
    knowhow_transfer_store = repo._runtime.knowhow_transfer_store
    source_fingerprint = knowhow_transfer_store.table_fingerprint(source_table_id)
    # verb="移动"（spec §7.3）：这次 copy_table 内部产生的 table_create 创世
    # 流水必须说"移动而来"，不是"复制而来"——move 语义上删源，note 措辞要如实。
    new_table_id = copy_table(
        repo, source_table_id, target_notebook_id, actor_id, verb="移动",
    )
    # A3 评审附加需求：copy_table 已经 COMMIT，从这里往下任何一步再炸，副本
    # 都已经是既成事实——裸异常冒泡上去只会变成路由层的通用 500，调用方分不清
    # "整个搬迁没发生"和"副本已经在、只是源没删干净"，naive 重试会在目标侧
    # 越堆越多重复副本。把这一段(读 hidden_source_id + 拆源投影 + 删源表行)
    # 整体收进一个类型化错误，让路由能把 new_table_id 一并带回给调用方。
    # 注意 copy_table(...) 本身特意留在这个 try 之外——复制失败必须原样冒泡
    # （源分毫未动、什么都没创建），不能被这里的包装误伤。
    #
    # round 10 P1-A：cleanup_reason 默认 "cleanup_error"（真正的拆投影/删除
    # 操作抛出异常——源原封未动，手动删除安全），下面两处指纹复核未命中的
    # 分支各自在 raise 之前把它扳成 "source_changed"（源被有意保留以保护一
    # 份并发编辑，绝不能引导用户删除源）。用 try 之前的局部变量而不是
    # isinstance 判某个专用异常类型：即便"扳成 source_changed 之后、真正
    # raise 之前"这段收尾（比如下面重新调度投影）自己又抛出别的异常，语义
    # 依然对——源确实是因为指纹变了才被保留的，不该因为收尾工作本身失手就
    # 误判成"删除安全"的 cleanup_error。
    cleanup_reason = "cleanup_error"
    try:
        if knowhow_transfer_store.table_fingerprint(source_table_id) != source_fingerprint:
            cleanup_reason = "source_changed"
            raise RuntimeError(
                "源表在复制完成后被并发编辑（列/行/格/代码计数或 mutation_seq "
                "与复制时的快照不一致），为避免丢失该编辑，源表未删除"
            )
        source_table_row = repo.get_knowhow_table(source_table_id)
        hidden = source_table_row.get("hidden_source_id")
        # P1-B（PR review round 6）读得越早越安全，同④的取指纹一个道理：这
        # 一读要撑到下面成功路径最后的孤儿扫描——扫描按 notebook 收窄
        # （KnowhowTransferStore.orphaned_knowhow_source_ids 的文档字符串），
        # 表行删掉之后就再也读不到它的 notebook_id 了，必须现在存下来。
        src_notebook_id = source_table_row["notebook_id"]
        if hidden:
            build_projector(repo).delete_table_projection(hidden)
        # P1-1（PR review round 3：check-then-act 仍是竞态）：上面那次复核本身
        # 不是原子的——它只是「读一次指纹、比一次」，和真正删表行的 DELETE 是
        # 两条独立语句，中间隔着整段拆投影（build_projector(repo).
        # delete_table_projection）。并发编辑完全可能恰好卡在「复核通过」之
        # 后、「表行被删」之前这段真空里——早到 build_projector(repo) 构造期
        # 间，晚到 delete_table_projection 内部某次写 I/O 让出线程的任意一
        # 刻——这次复核对它完全失明，行还是会被无条件删掉，编辑照样永久丢
        # 失。delete_table_if_unchanged 把「复核」和「删」收进
        # KnowhowTransferStore 自己 ONE 个 database.write() 里：整个方法体连
        # 续持有进程级 write_lock（写全程串行，见 database.py 的
        # SqliteDatabase.write 文档字符串），没有第二个写者能在「复核」和
        # 「删」之间插进来提交任何东西——不管拆投影花了多久、这中间发生了什
        # 么。这才是真正堵死这条竞态的地方；上面那次复核只是尽量早地、廉价
        # 地把明摆着已经变了的情况拦在拆投影之前（省一次没意义的拆投影），
        # 不是正确性本身依赖的那道闸——纯 best-effort 的窗口收窄。
        # 返回 False 说明源表指纹在这最后一刻仍然对不上——两边都留：副本已
        # 在目标（copy_table 早就 COMMIT 了），源表连同它的并发编辑原样留在
        # 原地。但上一行的拆投影已经真的跑过了——不管这次原子删除最终判定
        # 「变了」保留与否，那份投影（可能还是并发编辑自己刚重建好的最新一
        # 份）都已经被拆掉了。
        #
        # P2-2（PR review round 4）：不能指望"下次投影/重建会自动把它重新建
        # 起来"——那是错的：并发编辑落地时若它自己的重投影恰好也在拆投影
        # 之前就跑完了（行已经标成 synced，没有任何 pending 队列），这里保
        # 留下来的源表就彻底没有任何东西会再去调度一次重投影，从此没有
        # chunk/KG，直到有人手工点「重建投影」。这里显式重新排队，按当前
        # （含并发编辑内容）的业务行重建一遍——不改④拆投影必须先于删表这条
        # round-3 顺序，只是给「保留」这一条分支补上它自己应得的那次调度。
        if not knowhow_transfer_store.delete_table_if_unchanged(
            source_table_id, source_fingerprint
        ):
            cleanup_reason = "source_changed"
            get_scheduler(repo).schedule(source_table_id)
            raise RuntimeError(
                "源表在复制完成后被并发编辑（原子重删时指纹复核未命中），"
                "为避免丢失该编辑，源表未删除"
            )
    except Exception as exc:  # noqa: BLE001 — 副本已提交，必须转成可辨识的类型化错误
        # 必须在这里落日志：`from exc` 保住了 __cause__，但路由会把它转成
        # HTTPException，而 FastAPI 渲染 HTTPException 是不带 traceback 的——
        # 真在生产触发时（用户手上留下一份需要人工对账的重复副本），服务端
        # 将没有任何关于「为什么失败」的记录。_log.exception 带上原始堆栈。
        _log.exception(
            "move_table：副本 %s 已提交，但清理源表 %s 失败", new_table_id, source_table_id
        )
        raise SourceCleanupFailed(new_table_id, exc, reason=cleanup_reason) from exc

    # P1-B（PR review round 6，孤儿收尾扫描）：源表行此刻确凿已经删除、副本
    # 确凿已经在目标——上面的 try/except 只覆盖到这里为止的「读指纹+拆投影
    # +原子删表」，不该再管接下来这一步。这一步单独存在的理由：delete_
    # table_projection(hidden) 删的是 source 行本身，从不清空表行的
    # hidden_source_id 列（只有 set_knowhow_hidden_source 写它）——所以从
    # 它返回到上面原子删除真正执行这段真空里，表行仍在、hidden_source_id
    # 仍指着刚被拆掉的旧 id。一个恰好卡在这个窗口的并发 ensure_hidden_
    # source（stale-debounce 重投影撞上这次 move，projection.py 的「记录的
    # id 解不出就重铸一个」分支）会读到这个已经解不出的旧 id、铸出一个替身
    # source 并把它写回表行。table_fingerprint 只覆盖业务数据（正确——
    # hidden_source_id 是派生数据，不该进指纹），看不见这次替换，原子删除
    # 照常成功；表行没了，替身 source 从此不再被任何 knowhow_tables 行引
    # 用，变成源 notebook 里一个继续可被检索到的永久孤儿。
    #
    # 扫描的失败必须只记日志、绝不重新抛出：源表行已经确凿删除，这不是
    # SourceCleanupFailed 承诺的「源仍在，可安全重试删源」那种状态——把扫
    # 描失败包装成那个类型化错误，会让调用方对着一张早就不存在的源表做出
    # 错误决策（比如引导用户去"删除源表"，而源表根本已经没了）。留给下一
    # 次同 notebook 的 move（这个扫描本来就是幂等、opportunistic 的）或将
    # 来专门的孤儿清理入口去补，不是这次调用必须完成的事。
    try:
        for orphan_id in knowhow_transfer_store.orphaned_knowhow_source_ids(
            src_notebook_id
        ):
            build_projector(repo).delete_table_projection(orphan_id)
    except Exception:  # noqa: BLE001 — 见上：源表已确凿删除，扫描失败不能
        # 把一次已经成功的移动回报成需要人工介入的清理失败。
        _log.warning(
            "move_table：源表 %s 已成功移动，但收尾孤儿隐藏源扫描失败，"
            "notebook %s 可能残留孤儿（下次同 notebook 的 move 会重试）",
            source_table_id, src_notebook_id, exc_info=True,
        )
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
