"""The deep copy's row writer: every table of a notebook's copy closure, phase by
phase, with each id remapped onto the copy.

``NotebookCopyService.copy_notebook`` (``notebook_sharing``) owns the run around it
— the start-of-copy sweep, the source directory, the snapshot read, validation,
publish, the compensation and the post-publish projections, and every file on
disk. This module owns what happens between the snapshot and the validation: the
hidden ``copying`` sentinel, the mounts carried for the recipient, the remapped
rows of all the copied tables in their frozen FK-safe order, and the SQLite FTS
mirrors.

The writer reads its rows through a ``CopyRowSource``, table by table, in
batches. ``SnapshotRowSource`` (the only source today) wraps the dict
``store.snapshot_copy_rows`` returns and gives every table as ONE batch, so the
store sees exactly one ``insert_copy_rows`` per table, in the frozen order the
copy characterization tests pin. ``NotebookCopyWriter``'s docstring lists the
phases that need more than one batch at a time.

Logs go to the ``silicon_notebook.sharing`` logger, the same one the copy service
uses.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Protocol

from app.domain.indexing_pipeline import BUILTIN_INDEXING_PIPELINE_VERSION
from app.domain.repository import RepositoryCompatibilitySeams
from app.models.notebooks import NotebookSummary
from app.repositories.ports import SharingStorePort
from app.services.knowhow.ids import cell_chunk_id, element_id

_log = logging.getLogger("silicon_notebook.sharing")


# PR-2+3 Task 13: a knowhow cell's content_md embeds pasted images as
# ``![alt](asset://<asset_id>)`` (design doc §①) — a deep copy mints a fresh
# asset id per copied ``notebook_assets`` row (see
# ``NotebookCopyWriter._copy_notebook_assets``), so every
# cell that references one must have its markdown rewritten to point at the
# NEW id instead of the source's. Asset ids are ``new_id("asset")`` = a
# lowercase prefix + a dash + a 32-hex uuid4 (sqlite_repository._new_id) —
# ``[\w-]+`` safely captures the whole thing up to the closing ``)`` the
# markdown image syntax always supplies, with no dependency on that exact
# format (a wider match just means "not in asset_map -> left unchanged",
# never a wrong rewrite).
_ASSET_REF_RE = re.compile(r"asset://([\w-]+)")


def _rewrite_asset_refs(text: str, asset_map: dict) -> str:
    """Rewrite every ``asset://<old_id>`` in ``text`` to ``asset://<new_id>``
    via ``asset_map`` (old id -> new id); an id with no entry (should not
    happen — every asset referenced by a copied cell was itself copied, see
    ``NotebookCopyWriter``'s phase order) is left exactly as-is rather than
    dropped."""
    if "asset://" not in text:
        return text
    return _ASSET_REF_RE.sub(
        lambda m: f"asset://{asset_map.get(m.group(1), m.group(1))}", text
    )


def _remap_element_asset_id(raw, asset_map: dict):
    """Point a copied element's top-level ``metadata.asset_id`` at the copy's
    own ``notebook_assets`` row (old id -> new id via ``asset_map``).

    Writers only ever put ``asset_id`` at the top level of an element's
    metadata (the parser image elements and ``backfill-images``), so only that
    key is rewritten; a nested ``asset_id`` is somebody else's data. Every
    other shape is returned AS-IS, byte for byte: no ``"asset_id"`` substring
    (the common case, never parsed), text that is not JSON, JSON that is not an
    object, an id that is unhashable or has no entry in ``asset_map`` (same
    "unknown id -> unchanged" rule as ``_rewrite_asset_refs``). Raising here
    would compensate a copy that succeeds today, so nothing in this function
    raises on bad metadata. Only a value that actually changes is re-serialized,
    with the same ``ensure_ascii=False`` as the knowhow-element branch; on
    SQLite that rewritten text may differ from the original in separators or
    escapes (JSON-equal apart from the id), PostgreSQL stores jsonb either way.
    """
    if not isinstance(raw, str) or '"asset_id"' not in raw:
        return raw
    try:
        metadata = json.loads(raw)
    except (ValueError, RecursionError):
        return raw
    if not isinstance(metadata, dict):
        return raw
    old_asset_id = metadata.get("asset_id")
    try:
        new_asset_id = asset_map.get(old_asset_id)
    except TypeError:  # unhashable value (a list/object) — not an asset id
        return raw
    if new_asset_id is None or new_asset_id == old_asset_id:
        return raw
    metadata["asset_id"] = new_asset_id
    return json.dumps(metadata, ensure_ascii=False)


def _copy_source_elements(
    rows: list[dict],
    *,
    knowhow_source_ids_old: set,
    khtbl_map: dict,
    khrow_map: dict,
    khcol_map: dict,
    source_map: dict,
    asset_map: dict,
    element_map: dict,
    element_row_new: dict,
    remapped_id: Callable[[str], str],
) -> list[dict]:
    """Remap the snapshot's ``source_elements`` rows for a deep copy, in place.

    Pure in-memory rewrite — no statement is issued here;
    ``NotebookCopyWriter._copy_elements_and_chunks`` inserts the returned rows,
    inside ``copy_notebook``'s compensating try.
    Fills ``element_map`` (old -> new element id) and ``element_row_new``
    (old knowhow element id -> NEW knowhow row id) for the chunk loop."""
    elements_out = []
    for data in rows:
        old_element_id = data["id"]
        if data["source_id"] in knowhow_source_ids_old:
            # A knowhow cell's element: recompute its id via the
            # SAME stable formula project_table itself uses
            # (app.services.knowhow.projection.element_id), keyed on
            # the ALREADY-remapped row/column ids, instead of an
            # arbitrary fresh id — this is what lets the post-copy
            # reprojection find this exact row already in place.
            metadata = json.loads(data.get("metadata") or "{}")
            kh_meta = dict(metadata.get("knowhow") or {})
            new_row_id = khrow_map[kh_meta["row_id"]]
            new_column_id = khcol_map[kh_meta["column_id"]]
            new_element_id = element_id(new_row_id, new_column_id)
            element_map[old_element_id] = new_element_id
            element_row_new[old_element_id] = new_row_id
            kh_meta["table_id"] = khtbl_map.get(
                kh_meta.get("table_id"), kh_meta.get("table_id")
            )
            kh_meta["row_id"] = new_row_id
            kh_meta["column_id"] = new_column_id
            metadata["knowhow"] = kh_meta
            data["metadata"] = json.dumps(metadata, ensure_ascii=False)
            data["id"] = new_element_id
        else:
            data["id"] = element_map.setdefault(
                old_element_id, remapped_id(old_element_id)
            )
            # An image element names its picture by notebook_assets id; the
            # copy minted new asset ids above, so without this the copy's
            # images point into the ORIGINAL notebook (404 through the asset
            # proxy, gone once the original deletes the source).
            if "metadata" in data:
                data["metadata"] = _remap_element_asset_id(data["metadata"], asset_map)
        data["source_id"] = source_map[data["source_id"]]
        elements_out.append(data)
    return elements_out


def _remap_copied_asset_row(data: dict, new_id: str, source_map: dict) -> None:
    """Move a copied ``notebook_assets`` row into the copy: its notebook, and
    its ``source_id`` onto the copy's own source.

    ``delete_source_asset_rows`` deletes by ``source_id`` alone (not scoped to a
    notebook), so a copied row left pointing at the ORIGINAL's source was
    deleted — file and all — when the original deleted or re-parsed that source,
    while deleting the copy's own source left it behind. A source id with no
    entry in ``source_map`` (a source already gone, or a copy-of-a-copy whose
    row still names the first original's source) keeps its value exactly as
    today: never NULL, because ``sweep_orphan_assets`` reaps NULL-source rows and
    their files right after the post-copy projection."""
    data["notebook_id"] = new_id
    old_source_id = data.get("source_id")
    if old_source_id is not None:
        data["source_id"] = source_map.get(old_source_id, old_source_id)


def _reset_copied_notebook_row(
    notebook_row: dict,
    *,
    new_id: str,
    name: str,
    new_owner_id: str,
    now: str,
    manual_name: bool = False,
) -> None:
    """Rewrite the copied notebooks row's identity/lifecycle/authority columns.

    索引管线四列一律复位成内建：published identity 住在 unified_kg_state 里而它
    刻意不进深拷贝（副本至多得到 insert_copy_rows 写的一条状态行——标脏行，或只带
    源库已发布概念种子空间的干净行（S1，见 sharing_store 的 _COPY_SEED_SPACE）——
    其余各列取默认值，published identity 即内建），照抄 desired 选择会让副本天生 desired≠published、每一次写入
    409 直到手动全库重建；照抄 generation/job_id 更会让副本的状态投影 join 到
    *源库*正在跑的 job 行。与「授权边/share_token/agent_profile_id 不随副本走」
    同一条论证——副本由新 owner 重新选择并显式重建（既有 chunk 是核心 schema，
    照常可读）。

    已登记接受（codex #602 R7 P2 驳回）：插件管线库的副本里，拷来的插件 chunk 与
    此后按内建增量分块的新 chunk 会共存。这不触犯全库原子发布不变量——那条不变量
    保护的是**切换操作**的身份消费方（scale manifest identity、published KG identity、
    fold/full 判定），而副本这些面全部从零开始（unified_kg_state 缺席或只有标脏行、scale 工件
    不拷、首次重建必然 full），没有任何消费方会据错误身份做决策；extraction_runs
    随行拷贝、逐 run 保留真实的 (pipeline_id, version) 出处。剩下的只是 chunk
    粒度异质——与「部署中途调 chunk_target_chars 后新旧来源粒度不同」这一既有
    合法情形同类，从来不要求全库重建。另两个选项都更糟：种 published identity
    需要给副本造一条带真实身份的 unified_kg_state 行（与 chunk_elements 标记语义的
    既有红线冲突；标脏行不带身份、chunk_elements_indexed 取默认 0，不在此列）；
    强制先重建再可写等于让每一次插件库拷贝都先付一轮全库模型钱。
    """
    if manual_name:
        notebook_row["name_auto"] = 0
    notebook_row.update(
        id=new_id,
        name=name,
        created_by=new_owner_id,
        tier="personal",
        is_shared=0,
        share_token=None,
        status="copying",
        created_at=now,
        updated_at=now,
        indexing_pipeline=None,
        indexing_pipeline_version=BUILTIN_INDEXING_PIPELINE_VERSION,
        indexing_pipeline_generation="",
        indexing_pipeline_job_id="",
        metadata_generation=0,
        # 副本是**本地**笔记本,即便被拷的是一本镜像(设计文档
        # docs/incremental-sync-design.md §5:「复制笔记本的产物是本地笔记本,允许;
        # 但复制不带 sync_origin」)。照抄源库这一列会让副本也被写入围栏挡住,而它
        # 根本不在任何同步关系里——下一次导入不会碰它,新 owner 却再也改不动它。
        sync_origin="",
    )


def _carry_valid_mount_edges(
    store,
    edges: list[dict],
    *,
    new_id: str,
    new_owner_id: str,
    now: str,
    chunk_size: int,
) -> None:
    """Carry the source's reference-library mounts that are still valid for the
    recipient, and drop (with a log line) the ones that are not.

    多领域基准库 followups spec A6。挂载边是**接收者的检索参与集**——一本带着
    参考库回答问题的库，副本若空手到达，同一个问题的答案会明显变差；但挂载边
    同时**不是授权凭证**（``mount_sql`` 模块 docstring：有效性每次解析实时判定），
    所以照抄源库的边会让副本挂着一批它的新 owner 永远读不到的死边。故：边随
    副本走，但**按目的地重判**——``store.valid_copied_mount_base_ids`` 用
    ``MOUNT_VALID_EXPR`` 以副本自身为挂载方求值，于是

    * ``tier='base'`` 的公共知识库、以及带 ``everyone`` 授权边的库，恒随副本走；
    * 私有库只在「副本的 owner 就是该库的 owner」时随走（自己拷自己的库）；
    * 借入边只在接收者**自己**对该库有读权时随走（副本此刻无成员/授权边，
      「未共享门」自然满足；分享给别人的那本副本一旦被再共享，边照常失效）。

    ``created_at``/``created_by`` 改写成副本自己的——这批边是这次拷贝为新 owner
    建立的配置，不是源库那次挂载的历史。无效边**丢弃**而不是像源库那样保留置灰：
    源库上的灰边是 owner 自己做过、还可能恢复的配置，副本上的灰边则是接收者
    从未做过、通常也永远恢复不了的配置，只会变成一行「已不可用的知识库」。

    丢弃条数落**日志**：``copy_notebook`` 对外返回的是 ``NotebookSummary``，没有
    可以扩展的拷贝结果形状，为一个诊断计数新开 API 字段不成比例。

    **前置条件**：副本的哨兵 ``notebooks`` 行必须已经**提交**（不只是"已插入"）。
    有效性判定跑在 store 自己的另一条连接上、且要 join 目的地这一行取
    ``a.created_by``——未提交则读不到，全部候选一律判无效、边一条不剩。

    判定与插入分属两条连接，所以中间存在一个跨库竞态：被挂库在这两步之间被删
    掉时插入会撞外键。这条边因此不走 ``insert_copy_rows``，而走
    ``store.insert_copied_mount_edges``——它逐行插入并把 ``notebook_bases`` 的
    外键违例当成**又一条被丢弃的边**（返回真正落库的条数），而不是让一次无关的
    删库把整本副本连同补偿回滚一起赔进去。
    """
    if not edges:
        return
    wanted = list(
        dict.fromkeys(
            row["base_notebook_id"] for row in edges if row.get("base_notebook_id")
        )
    )
    valid = store.valid_copied_mount_base_ids(new_id, wanted)
    carried = store.insert_copied_mount_edges(
        [
            {
                "notebook_id": new_id,
                "base_notebook_id": base_id,
                "created_at": now,
                "created_by": new_owner_id,
            }
            for base_id in wanted
            if base_id in valid
        ],
        chunk_size=chunk_size,
    )
    dropped = len(wanted) - carried
    if dropped:
        _log.info(
            "copy_notebook: 副本 %s 携带 %d 条挂载边，丢弃 %d 条对新 owner %s "
            "无效的边（接收者可在副本里重新挂载他读得到的库）",
            new_id, carried, dropped, new_owner_id,
        )


def _insert_copy_fts_mirrors(
    store,
    *,
    new_id: str,
    objects_out: list[dict],
    chunks_out: list[dict],
    chunk_size: int,
) -> None:
    """Fill the copy's SQLite FTS mirror tables (a no-op on PostgreSQL, whose
    GIN/trigram indexes are maintained from the base rows — see that backend's
    ``insert_fts_rows``).

    Deprecated knowledge objects and name-less ones are skipped exactly as the
    live KG write path skips them; a chunk mirrors its text verbatim.
    """
    kg_fts_rows = [
        (data["id"], new_id, (json.loads(data["payload"]).get("name") or "").strip())
        for data in objects_out
        if data.get("status") != "deprecated"
    ]
    store.insert_fts_rows(
        "INSERT INTO kg_objects_fts(object_id, notebook_id, name) VALUES (?, ?, ?)",
        [row for row in kg_fts_rows if row[2]],
        chunk_size=chunk_size,
    )
    store.insert_fts_rows(
        "INSERT INTO chunks_fts(chunk_id, notebook_id, text) VALUES (?, ?, ?)",
        [(data["id"], new_id, data.get("text") or "") for data in chunks_out],
        chunk_size=chunk_size,
    )


@dataclass
class NotebookCopyRun:
    """One deep copy's state, threaded through ``NotebookCopyWriter``'s phases and
    back to ``copy_notebook`` (compensation, post-publish projections).

    The identity values (``new_id``/``now``/``chunk_size``) are read from the seams
    ONCE, at the start of ``copy_notebook``, exactly as before the split. The remap
    maps are shared by identity: each phase fills the ones later phases read
    (``json_maps`` holds the very same dicts, so an object id remapped earlier in
    the objects loop is already visible to the next object's payload). The two
    ``*_copied`` flags record what this copy actually wrote to disk — set the moment
    it happens, by whichever step does it — so the compensation removes only
    those directories.
    """

    source_notebook_id: str
    source_notebook: NotebookSummary
    new_owner_id: str
    new_id: str
    now: str
    chunk_size: int
    destination_dir: Path
    # PR-2+3 Task 13: notebook_assets' on-disk files live in a SEPARATE
    # tree from source_dir (storage_dir/assets/<nb>/, not
    # storage_dir/notebooks/<nb>/ — see app.services.knowhow.assets.
    # AssetService.path_for), so they need their own copied-flag +
    # destination path for the same "compensate only what we actually
    # wrote" discipline as destination_dir.
    assets_dest_dir: Path
    copied_files: bool = False
    assets_copied: bool = False
    source_map: dict = field(default_factory=dict)
    element_map: dict = field(default_factory=dict)
    chunk_map: dict = field(default_factory=dict)
    object_map: dict = field(default_factory=dict)
    fact_map: dict = field(default_factory=dict)
    generation_map: "dict[tuple[str, str], str]" = field(default_factory=dict)
    relation_map: dict = field(default_factory=dict)
    # PR-2+3 Task 13: knowhow business-table + hidden-source-content
    # remap maps. ``knowhow_source_ids_old`` is captured from the
    # RAW (not-yet-mutated) snapshot, before anything rewrites
    # snapshot["knowhow_tables"]' own dicts in place — it is how the
    # elements/chunks loops tell a knowhow row apart from an ordinary
    # document row (both share the same snapshot queries now that
    # neither is excluded).
    khtbl_map: dict = field(default_factory=dict)
    khcol_map: dict = field(default_factory=dict)
    khrow_map: dict = field(default_factory=dict)
    khcel_map: dict = field(default_factory=dict)
    khcode_map: dict = field(default_factory=dict)
    asset_map: dict = field(default_factory=dict)
    knowhow_source_ids_old: set = field(default_factory=set)
    # old element id -> NEW row id, populated by the elements_out
    # loop; the chunks_out loop reads it to recompute each
    # knowhow chunk's id from ITS row (a chunk has no row_id column
    # of its own — only its element_ids[0] indirectly names one).
    element_row_new: dict = field(default_factory=dict)
    # The remap maps every stored-JSON rewrite reads (``seams.remap_json_ids``),
    # built from the very same dict objects as the fields above — never a copy —
    # so every phase sees the entries the earlier phases have added.
    json_maps: dict = field(init=False)

    def __post_init__(self) -> None:
        self.json_maps = {
            "element_id": self.element_map,
            "element_ids": self.element_map,
            "source_id": self.source_map,
            "object_id": self.object_map,
        }


class CopyRowSource(Protocol):
    """Where a copy's rows come from, table by table.

    ``batches(table)`` returns the table's rows as an iterable of row lists. The
    writer remaps each batch in place and hands it to ``insert_copy_rows``, one
    call per batch. A source must

    * answer every table of the copy closure;
    * allow a table to be asked for again, starting over from its first row: a
      few phases read a table twice (``NotebookCopyWriter`` lists them);
    * hand out row dicts the writer may rewrite in place.

    A table's WRITE pass is always its last read: every second read is a pure
    pre-scan that runs before the table is written. A source that re-reads from
    storage (fresh dicts holding source ids) and ``SnapshotRowSource`` (the same,
    by then remapped, dicts) only agree under that ordering, so a phase that
    wants to re-read a table after writing it must keep what it needs from the
    write pass instead.
    """

    def batches(self, table: str) -> Iterable[list[dict]]: ...


class SnapshotRowSource:
    """``CopyRowSource`` over the dict ``store.snapshot_copy_rows`` returns.

    Every table is exactly ONE batch: the snapshot's own list, the same object
    each time it is asked for, so the writer's in-place remaps land in the
    snapshot's dicts. An empty table is one empty batch, so the writer still
    issues that table's ``insert_copy_rows``: together these reproduce the
    frozen one-call-per-table write sequence the copy characterization pins. A
    table the snapshot does not hold raises ``KeyError`` when it is asked for."""

    def __init__(self, snapshot: "dict[str, list[dict]]") -> None:
        self._snapshot = snapshot

    def batches(self, table: str) -> "tuple[list[dict]]":
        return (self._snapshot[table],)


class NotebookCopyWriter:
    """Writes one copy's rows, phase by phase, remapping every id onto the copy.

    The tables are written in FK-safe order by the phase methods below, one
    ``insert_copy_rows`` per batch the row source gives (one per table with
    ``SnapshotRowSource``); each phase fills the remap maps on ``NotebookCopyRun``
    that the later phases read. Every statement goes through ``store``. The asset
    FILES are not the writer's: right after a batch of ``notebook_assets`` rows is
    inserted, ``materialize_assets`` receives that batch's ``(old_id, new_id,
    mime)`` triples and puts the files in place (the copy service copies them
    from the source notebook's asset tree), still inside the caller's
    compensating try.

    Seams are read during the write, never cached: ``seams.new_id()`` mints every
    remapped id, ``seams.remap_json_ids`` rewrites stored JSON.

    What needs more than the batch in hand. ``SnapshotRowSource`` holds every
    table whole, so none of this costs anything today; a streaming source (PR4)
    has to keep each point working:

    * ``notebooks`` (the root row) and ``notebook_bases`` are read whole: the mount
      edges are judged together, in one ``valid_copied_mount_base_ids`` call. Both
      are tiny (one row; a notebook's handful of mounts).
    * ``knowhow_source_ids_old`` is a pass over ``knowhow_tables`` before
      ``sources`` are written, so that table is read twice. The result is one id
      per knowhow table.
    * ``_insert_copy_generation_runs`` scans all three source-fact tables before any
      fact is written: each source's ``extraction_runs`` row needs every
      generation of that source, and its active one, first. The fact tables are
      read twice; a streaming source serves the first read as a pre-pass that
      keeps only the per-source aggregates.
    * The FTS mirrors are written after the last phase from every copied
      ``knowledge_objects`` and ``chunks`` row the writer keeps (``write``). A
      streaming source keeps the whole chunk text in memory that way; mirroring
      each batch as it lands instead moves the FTS calls inside the frozen store
      call sequence, which the copy characterization tests pin.
    * Every remap map (``source_map``, ``chunk_map``, ``object_map`` ...) holds one
      entry per copied row of its table for the whole copy.
    """

    def __init__(
        self,
        *,
        store: SharingStorePort,
        seams: "RepositoryCompatibilitySeams",
        rows: CopyRowSource,
        run: NotebookCopyRun,
        materialize_assets: Callable[[list[tuple[str, str, str]]], None],
    ) -> None:
        self._store = store
        self._seams = seams
        self._rows = rows
        self._run = run
        self._materialize_assets = materialize_assets

    def write(self, *, new_name: "str | None", actor_label: "str | None") -> None:
        """Every row of the copy in the frozen FK-safe order, then the FTS mirrors."""
        run = self._run
        self._insert_copy_sentinel(new_name=new_name)
        run.knowhow_source_ids_old = {
            t["hidden_source_id"]
            for batch in self._rows.batches("knowhow_tables")
            for t in batch
            if t.get("hidden_source_id")
        }
        self._copy_source_tables()
        self._copy_knowhow_tables(actor_label=actor_label)
        chunks_out = self._copy_elements_and_chunks()
        objects_out = self._copy_knowledge_objects()
        self._insert_copy_generation_runs()
        self._copy_source_facts()
        self._copy_relations()
        self._copy_chunk_and_vector_rows()

        _insert_copy_fts_mirrors(
            self._store, new_id=run.new_id, objects_out=objects_out,
            chunks_out=chunks_out, chunk_size=run.chunk_size,
        )

    def _remapped_id(self, old: str) -> str:
        """A fresh id for a copied row, keeping the old id's prefix."""
        prefix = old.split("-", 1)[0] if old else "id"
        return self._seams.new_id(prefix)

    def _whole_table(self, table: str) -> list[dict]:
        """All of ``table``'s rows in one list, for the phases that read a table
        whole (see the class docstring)."""
        rows: list[dict] = []
        for batch in self._rows.batches(table):
            rows.extend(batch)
        return rows

    def _insert_copy_sentinel(self, *, new_name: "str | None") -> None:
        """Insert the copy's hidden 'copying' notebooks row, then carry the mounts
        that are valid for the recipient."""
        run = self._run
        new_id, now = run.new_id, run.now
        new_owner_id, chunk_size = run.new_owner_id, run.chunk_size
        notebook_row = self._whole_table("notebooks")[0]
        _reset_copied_notebook_row(
            notebook_row,
            new_id=new_id,
            name=new_name or f"{run.source_notebook.name} (副本)",
            new_owner_id=new_owner_id,
            now=now,
            manual_name=bool(new_name),
        )
        # 哨兵行必须先**提交**（不只是"已插入"）：下一步的挂载有效性判定跑在
        # store 的另一条连接上，还要 join 目的地这一行取 created_by。
        self._store.insert_copy_rows("notebooks", [notebook_row], chunk_size=chunk_size)
        _carry_valid_mount_edges(
            self._store, self._whole_table("notebook_bases"), new_id=new_id,
            new_owner_id=new_owner_id, now=now, chunk_size=chunk_size,
        )

    def _copy_source_tables(self) -> None:
        """sources (fills ``source_map``), paper metadata, authors, object schemas."""
        run = self._run
        new_id, new_owner_id, chunk_size = run.new_id, run.new_owner_id, run.chunk_size
        source_map, destination_dir = run.source_map, run.destination_dir
        remapped_id = self._remapped_id

        for batch in self._rows.batches("sources"):
            sources_out = []
            for data in batch:
                data["id"] = source_map.setdefault(data["id"], remapped_id(data["id"]))
                data["notebook_id"] = new_id
                if data.get("file_path"):
                    data["file_path"] = str(destination_dir / Path(data["file_path"]).name)
                # Task 6: memory_id is NOT an id that gets remapped — it points
                # at a Memory row that is owner-private and never travels with
                # a copy. Force it empty (insert_source's own "no link"
                # default) so the copy doesn't dangle-reference a Memory the
                # recipient can't see, and doesn't collide with the global
                # partial unique index idx_sources_memory_id (which the
                # source row's own unchanged memory_id still occupies).
                data["memory_id"] = ""
                # v48 provenance is likewise not an identity that travels with a
                # copy: the copy row is created by the USER's copy action, so it
                # is user-added by definition. Force NULL — not "" — because the
                # projection derives ``agent_created`` from IS NOT NULL, and an
                # empty string would read as "an Agent added this". Clearing it
                # moves every copied row into the protected class (a source an
                # Agent may never delete), which is the conservative direction:
                # carrying the id over would hand an Agent delete rights on rows
                # in a notebook it never touched.
                data.update(agent_profile_id=None, uploaded_by=None)
                sources_out.append(data)
            self._store.insert_copy_rows("sources", sources_out, chunk_size=chunk_size)

        for batch in self._rows.batches("source_paper_meta"):
            paper_meta_out = []
            for data in batch:
                data["source_id"] = source_map[data["source_id"]]
                data["notebook_id"] = new_id
                paper_meta_out.append(data)
            self._store.insert_copy_rows(
                "source_paper_meta", paper_meta_out, chunk_size=chunk_size
            )

        for batch in self._rows.batches("source_authors"):
            authors_out = []
            for data in batch:
                new_source_id = source_map[data["source_id"]]
                # Deterministic id scheme mirrors SourceStore.upsert_paper_meta's
                # write-time convention exactly, so a copy's author rows are
                # indistinguishable from freshly-extracted ones.
                data["id"] = f"{new_source_id}:auth:{int(data['position']):03d}"
                data["source_id"] = new_source_id
                data["notebook_id"] = new_id
                authors_out.append(data)
            self._store.insert_copy_rows(
                "source_authors", authors_out, chunk_size=chunk_size
            )

        for batch in self._rows.batches("notebook_object_schemas"):
            schema_rows = []
            for data in batch:
                data["notebook_id"] = new_id
                data["created_by"] = new_owner_id
                schema_rows.append(data)
            self._store.insert_copy_rows(
                "notebook_object_schemas", schema_rows, chunk_size=chunk_size
            )

    def _copy_knowhow_tables(self, *, actor_label: "str | None") -> None:
        """PR-2+3 Task 13: the knowhow business tables, the notebook's assets, and
        each copied table's version-history genesis.

        Order is FK-safe (each leg's remap map is fully populated
        before the next leg that depends on it runs): tables (needs
        source_map for hidden_source_id) -> columns/rows (need
        khtbl_map) -> assets (independent; builds asset_map) -> cells
        (needs khrow_map + khcol_map + asset_map, for the content_md
        asset:// rewrite) -> cell_code (needs khrow_map + khcol_map).
        """
        run = self._run
        new_id, new_owner_id, chunk_size = run.new_id, run.new_owner_id, run.chunk_size
        source_map, khtbl_map = run.source_map, run.khtbl_map
        khcol_map, khrow_map, khcel_map = run.khcol_map, run.khrow_map, run.khcel_map
        khcode_map, asset_map = run.khcode_map, run.asset_map
        remapped_id = self._remapped_id

        for batch in self._rows.batches("knowhow_tables"):
            tables_out = []
            for data in batch:
                data["id"] = khtbl_map.setdefault(data["id"], remapped_id(data["id"]))
                data["notebook_id"] = new_id
                data["created_by"] = new_owner_id
                old_hidden = data.get("hidden_source_id")
                data["hidden_source_id"] = (
                    source_map.get(old_hidden, old_hidden) if old_hidden else old_hidden
                )
                tables_out.append(data)
            self._store.insert_copy_rows("knowhow_tables", tables_out, chunk_size=chunk_size)

        for batch in self._rows.batches("knowhow_columns"):
            columns_out = []
            for data in batch:
                data["id"] = khcol_map.setdefault(data["id"], remapped_id(data["id"]))
                data["table_id"] = khtbl_map[data["table_id"]]
                columns_out.append(data)
            self._store.insert_copy_rows("knowhow_columns", columns_out, chunk_size=chunk_size)

        for batch in self._rows.batches("knowhow_rows"):
            rows_out = []
            for data in batch:
                data["id"] = khrow_map.setdefault(data["id"], remapped_id(data["id"]))
                data["table_id"] = khtbl_map[data["table_id"]]
                # The copy has not been (re)projected yet regardless of what
                # the SOURCE row's own status was — 'pending' is the schema
                # default for exactly this "not yet projected" state (see
                # migrations.py _migration_16), and the post-publish scheduling
                # (NotebookCopyService._schedule_copied_projections in
                # notebook_sharing) is what will settle it.
                data["projection_status"] = "pending"
                rows_out.append(data)
            self._store.insert_copy_rows("knowhow_rows", rows_out, chunk_size=chunk_size)

        self._copy_notebook_assets()

        for batch in self._rows.batches("knowhow_cells"):
            cells_out = []
            for data in batch:
                data["id"] = khcel_map.setdefault(data["id"], remapped_id(data["id"]))
                data["row_id"] = khrow_map[data["row_id"]]
                data["column_id"] = khcol_map[data["column_id"]]
                data["content_md"] = _rewrite_asset_refs(
                    data.get("content_md") or "", asset_map
                )
                cells_out.append(data)
            self._store.insert_copy_rows("knowhow_cells", cells_out, chunk_size=chunk_size)

        for batch in self._rows.batches("knowhow_cell_code"):
            cell_code_out = []
            for data in batch:
                data["id"] = khcode_map.setdefault(data["id"], remapped_id(data["id"]))
                data["row_id"] = khrow_map[data["row_id"]]
                data["column_id"] = khcol_map[data["column_id"]]
                cell_code_out.append(data)
            self._store.insert_copy_rows(
                "knowhow_cell_code", cell_code_out, chunk_size=chunk_size
            )
        # 版本管理创世流水（codex 第 2 轮 P2）：knowhow 表内容已全部插完，
        # 为每个拷贝表补一条 table_create，让它有可回退到的拷贝态起点——
        # 单表 copy_table 早就这么做了，整本深拷贝这条路径此前漏了。指纹在
        # 此刻算才反映完整表状态，故必须放在 cell/cell_code 插入之后。
        self._store.seed_copied_knowhow_genesis(
            list(khtbl_map.values()),
            new_id=self._seams.new_id,
            now=self._seams.now,
            actor=new_owner_id if actor_label is None else actor_label,
            note=f"随笔记本《{run.source_notebook.name}》复制而来",
        )

    def _copy_notebook_assets(self) -> None:
        """notebook_assets rows (fills ``asset_map``), then their files.

        Each batch's files are handed to ``materialize_assets`` right after the
        batch's rows are inserted (only when the batch names any asset), so a
        failure there still compensates through the caller's try."""
        run = self._run
        new_id, chunk_size = run.new_id, run.chunk_size
        source_map, asset_map = run.source_map, run.asset_map
        remapped_id = self._remapped_id

        for batch in self._rows.batches("notebook_assets"):
            assets_out = []
            asset_files: list[tuple[str, str, str]] = []  # (old_id, new_id, mime)
            for data in batch:
                old_asset_id = data["id"]
                new_asset_id = asset_map.setdefault(old_asset_id, remapped_id(old_asset_id))
                data["id"] = new_asset_id
                _remap_copied_asset_row(data, new_id, source_map)
                asset_files.append((old_asset_id, new_asset_id, data["mime"]))
                assets_out.append(data)
            self._store.insert_copy_rows("notebook_assets", assets_out, chunk_size=chunk_size)

            if asset_files:
                self._materialize_assets(asset_files)

    def _copy_elements_and_chunks(self) -> list[dict]:
        """source_elements (fills ``element_map``), then chunks (fills
        ``chunk_map``); returns the copied chunk rows for the FTS mirror."""
        run = self._run
        new_id, chunk_size = run.new_id, run.chunk_size
        source_map, element_map, chunk_map = run.source_map, run.element_map, run.chunk_map
        knowhow_source_ids_old, element_row_new = run.knowhow_source_ids_old, run.element_row_new
        remapped_id = self._remapped_id

        for batch in self._rows.batches("source_elements"):
            elements_out = _copy_source_elements(
                batch,
                knowhow_source_ids_old=knowhow_source_ids_old,
                khtbl_map=run.khtbl_map,
                khrow_map=run.khrow_map,
                khcol_map=run.khcol_map,
                source_map=source_map,
                asset_map=run.asset_map,
                element_map=element_map,
                element_row_new=element_row_new,
                remapped_id=remapped_id,
            )
            self._store.insert_copy_rows(
                "source_elements", elements_out, chunk_size=chunk_size
            )

        json_maps = run.json_maps

        chunks_copied: list[dict] = []
        for batch in self._rows.batches("chunks"):
            chunks_out = []
            for data in batch:
                old_chunk_id = data["id"]
                if data["source_id"] in knowhow_source_ids_old:
                    # Recompute the SAME stable chunk id project_table would
                    # (app.services.knowhow.projection.cell_chunk_id): the
                    # trailing "part" number (column-position-derived) is
                    # unchanged by a copy (column position is copied as-is),
                    # so it is read straight off the OLD id; only the
                    # row-hash segment (a pure function of row_id) needs
                    # recomputing for the remapped row. This is the crux of
                    # the zero-re-embed contract — landing on this exact id
                    # with unchanged text+section_path is what makes the
                    # post-copy project_table pass see "nothing changed".
                    old_element_ids = json.loads(data.get("element_ids") or "[]")
                    if not old_element_ids:
                        # Defensive only — the projector always writes exactly
                        # one element id per knowhow chunk (_write_chunks).
                        # Failing loud INSIDE the try means a genuinely
                        # malformed source row compensates the whole copy
                        # instead of silently producing a chunk whose id the
                        # post-copy reprojection could never reconcile.
                        raise ValueError(
                            f"copy_notebook: knowhow chunk {old_chunk_id} "
                            "缺 element_ids，无法重算稳定 id"
                        )
                    new_row_id = element_row_new[old_element_ids[0]]
                    part = int(old_chunk_id.rsplit("-", 1)[-1])
                    new_chunk_id = cell_chunk_id(new_row_id, part)
                    chunk_map[old_chunk_id] = new_chunk_id
                    data["id"] = new_chunk_id
                else:
                    data["id"] = chunk_map.setdefault(
                        old_chunk_id, remapped_id(old_chunk_id)
                    )
                data["notebook_id"] = new_id
                data["source_id"] = source_map[data["source_id"]]
                element_ids = {"element_ids": json.loads(data.get("element_ids") or "[]")}
                data["element_ids"] = json.dumps(
                    self._seams.remap_json_ids(element_ids, json_maps)["element_ids"]
                )
                chunks_out.append(data)
            # chunk_elements（element→chunk 反查表，v46）刻意**不**随深拷贝复制：
            # unified_kg_state 不在拷贝表集合内（至多有 insert_copy_rows 写的一条
            # 状态行——标脏行或只带概念种子空间的干净行——该列取默认 0），所以副本的 chunk_elements_indexed 恒缺失或为 0，读路径
            # 走 legacy 全量扫描——反查行缺席是正确且自洽的。
            # ⚠ 若将来把 unified_kg_state 加进深拷贝表集合，**必须**同时复制
            # chunk_elements（或显式把副本的 chunk_elements_indexed 归零）：
            # 否则副本继承一个为真的标记而反查表是空的，点查路径会静默返回空
            # 证据——不报错、无自愈路径。knowhow 单表 copy_table 的目标是**已存在**
            # 的 notebook，所以那条路不能豁免，见 knowhow_transfer_store。
            # 与同为派生表的 chunk_questions（_copy_chunk_and_vector_rows 确实复制）非对称，
            # 理由就在这里：那张表自描述（有行即可用），而 chunk_elements 的可用性
            # 由一个**不随拷贝走**的标记声明，所以「不拷贝」才是自洽的那一侧。
            self._store.insert_copy_rows("chunks", chunks_out, chunk_size=chunk_size)
            chunks_copied.extend(chunks_out)
        return chunks_copied

    def _copy_knowledge_objects(self) -> list[dict]:
        """knowledge_objects (fills ``object_map``); returns the copied rows for the
        FTS mirror."""
        run = self._run
        new_id, chunk_size = run.new_id, run.chunk_size
        source_map, object_map, json_maps = run.source_map, run.object_map, run.json_maps
        remapped_id = self._remapped_id

        objects_copied: list[dict] = []
        for batch in self._rows.batches("knowledge_objects"):
            objects_out = []
            for data in batch:
                old_source_id = data["source_id"]
                old_candidate_id = data.get("source_candidate_id")
                data["id"] = object_map.setdefault(data["id"], remapped_id(data["id"]))
                data["notebook_id"] = new_id
                data["source_id"] = source_map.get(old_source_id, old_source_id)
                if old_candidate_id:
                    data["source_candidate_id"] = source_map.get(
                        old_candidate_id, old_candidate_id
                    )
                data["payload"] = json.dumps(
                    self._seams.remap_json_ids(json.loads(data.get("payload") or "{}"), json_maps)
                )
                data["evidence"] = json.dumps(
                    self._seams.remap_json_ids(json.loads(data.get("evidence") or "[]"), json_maps)
                )
                objects_out.append(data)
            self._store.insert_copy_rows(
                "knowledge_objects", objects_out, chunk_size=chunk_size
            )
            objects_copied.extend(objects_out)
        return objects_copied

    def _insert_copy_generation_runs(self) -> None:
        """Mint the copy-local completed KG generations (fills ``generation_map``).

        Source-local facts are generation-bound: maintenance and the
        read-only audit accept a projection only when its generation is
        backed by a successful KG extraction run in the SAME notebook.
        Operational extraction history deliberately does not travel
        with a deep copy, so mint one copy-local completed generation
        for every source represented by the copied terminal ledger (or,
        for an older projection without a ledger, its newest fact
        generation).  Facts, bindings and ledger rows all remap
        through the same (source,generation) key.

        Needs the three source-fact tables WHOLE, before any fact is written (the
        class docstring says how a streaming source serves that): this phase
        only reads them, ``_copy_source_facts`` reads them again to write them.
        """
        run = self._run
        new_id, now, chunk_size = run.new_id, run.now, run.chunk_size
        source_map, generation_map = run.source_map, run.generation_map
        remapped_id = self._remapped_id

        generations_by_source: dict[str, dict[str, str]] = {}
        fact_counts_by_generation: dict[tuple[str, str], int] = {}
        for batch in self._rows.batches("knowledge_source_facts"):
            for data in batch:
                source_id = str(data["source_id"])
                generation = str(data.get("source_generation") or "")
                if generation:
                    generations_by_source.setdefault(source_id, {})[generation] = str(
                        data.get("updated_at") or data.get("created_at") or ""
                    )
                    key = (source_id, generation)
                    fact_counts_by_generation[key] = (
                        fact_counts_by_generation.get(key, 0) + 1
                    )
        active_generation_by_source: dict[str, str] = {}
        scanned_by_source: dict[str, int] = {}
        for batch in self._rows.batches("knowledge_source_fact_backfills"):
            for data in batch:
                source_id = str(data["source_id"])
                generation = str(data.get("source_generation") or "")
                if not generation:
                    continue
                active_generation_by_source[source_id] = generation
                scanned_by_source[source_id] = int(data.get("objects_scanned") or 0)
                generations_by_source.setdefault(source_id, {})[generation] = str(
                    data.get("updated_at") or data.get("created_at") or ""
                )
        for batch in self._rows.batches("knowledge_source_fact_elements"):
            for data in batch:
                source_id = str(data["source_id"])
                generation = str(data.get("source_generation") or "")
                if generation:
                    generations_by_source.setdefault(source_id, {}).setdefault(
                        generation, str(data.get("created_at") or "")
                    )

        copy_generation_runs = []
        for old_source_id, generations in generations_by_source.items():
            for old_generation in generations:
                generation_map[(old_source_id, old_generation)] = remapped_id(
                    old_generation
                )
            active_generation = active_generation_by_source.get(old_source_id)
            if not active_generation:
                active_generation = max(
                    generations,
                    key=lambda generation: (generations[generation], generation),
                )
            new_generation = generation_map[(old_source_id, active_generation)]
            copied_fact_count = fact_counts_by_generation.get(
                (old_source_id, active_generation), 0
            )
            object_count = scanned_by_source.get(old_source_id, copied_fact_count)
            copy_generation_runs.append(
                {
                    "id": new_generation,
                    "notebook_id": new_id,
                    "source_id": source_map[old_source_id],
                    "run_type": "kg",
                    "status": "completed",
                    "error_message": (
                        f"kg objects={object_count} relations=0 copied_generation=1"
                    ),
                    "created_at": now,
                    "updated_at": now,
                }
            )
        self._store.insert_copy_rows(
            "extraction_runs", copy_generation_runs, chunk_size=chunk_size
        )

    def _copy_source_facts(self) -> None:
        """The three source-fact tables, onto the copy's own generations (fills
        ``fact_map``)."""
        run = self._run
        new_id, chunk_size = run.new_id, run.chunk_size
        source_map, element_map, object_map = run.source_map, run.element_map, run.object_map
        fact_map, generation_map, json_maps = run.fact_map, run.generation_map, run.json_maps
        remapped_id = self._remapped_id

        for batch in self._rows.batches("knowledge_source_facts"):
            source_facts_out = []
            for data in batch:
                old_fact_id = data["id"]
                old_source_id = str(data["source_id"])
                old_generation = str(data.get("source_generation") or "")
                data["id"] = fact_map.setdefault(old_fact_id, remapped_id(old_fact_id))
                data["notebook_id"] = new_id
                data["source_id"] = source_map[old_source_id]
                if old_generation:
                    data["source_generation"] = generation_map[
                        (old_source_id, old_generation)
                    ]
                data["global_object_id"] = object_map.get(
                    data.get("global_object_id") or "",
                    data.get("global_object_id") or "",
                )
                data["payload"] = json.dumps(
                    self._seams.remap_json_ids(
                        json.loads(data.get("payload") or "{}"), json_maps
                    )
                )
                data["evidence"] = json.dumps(
                    self._seams.remap_json_ids(
                        json.loads(data.get("evidence") or "[]"), json_maps
                    )
                )
                source_facts_out.append(data)
            self._store.insert_copy_rows(
                "knowledge_source_facts", source_facts_out, chunk_size=chunk_size
            )

        for batch in self._rows.batches("knowledge_source_fact_elements"):
            source_fact_elements_out = []
            for data in batch:
                old_source_id = str(data["source_id"])
                old_generation = str(data.get("source_generation") or "")
                data["fact_id"] = fact_map[data["fact_id"]]
                data["notebook_id"] = new_id
                data["source_id"] = source_map[old_source_id]
                if old_generation:
                    data["source_generation"] = generation_map[
                        (old_source_id, old_generation)
                    ]
                data["element_id"] = element_map[data["element_id"]]
                source_fact_elements_out.append(data)
            self._store.insert_copy_rows(
                "knowledge_source_fact_elements",
                source_fact_elements_out,
                chunk_size=chunk_size,
            )

        for batch in self._rows.batches("knowledge_source_fact_backfills"):
            source_fact_backfills_out = []
            for data in batch:
                old_source_id = str(data["source_id"])
                old_generation = str(data.get("source_generation") or "")
                data["source_id"] = source_map[old_source_id]
                data["notebook_id"] = new_id
                if old_generation:
                    data["source_generation"] = generation_map[
                        (old_source_id, old_generation)
                    ]
                old_after = data.get("after_object_id") or ""
                data["after_object_id"] = object_map.get(old_after, "")
                source_fact_backfills_out.append(data)
            self._store.insert_copy_rows(
                "knowledge_source_fact_backfills",
                source_fact_backfills_out,
                chunk_size=chunk_size,
            )

    def _copy_relations(self) -> None:
        """knowledge_relations (fills ``relation_map``)."""
        run = self._run
        new_id, chunk_size = run.new_id, run.chunk_size
        source_map, object_map = run.source_map, run.object_map
        relation_map, json_maps = run.relation_map, run.json_maps
        remapped_id = self._remapped_id

        for batch in self._rows.batches("knowledge_relations"):
            relations_out = []
            for data in batch:
                old_source_id = data["source_id"]
                data["id"] = relation_map.setdefault(data["id"], remapped_id(data["id"]))
                data["notebook_id"] = new_id
                data["source_id"] = source_map.get(old_source_id, old_source_id)
                data["source_object_id"] = object_map[data["source_object_id"]]
                data["target_object_id"] = object_map[data["target_object_id"]]
                data["evidence"] = json.dumps(
                    self._seams.remap_json_ids(json.loads(data.get("evidence") or "[]"), json_maps)
                )
                relations_out.append(data)
            self._store.insert_copy_rows(
                "knowledge_relations", relations_out, chunk_size=chunk_size
            )

    def _copy_chunk_and_vector_rows(self) -> None:
        """chunk_questions, the four vector tables and concept_clusters — rows that
        only point at already-copied rows."""
        run = self._run
        new_id, chunk_size = run.new_id, run.chunk_size
        source_map, element_map, chunk_map = run.source_map, run.element_map, run.chunk_map
        object_map, relation_map = run.object_map, run.relation_map
        remapped_id = self._remapped_id

        for batch in self._rows.batches("chunk_questions"):
            chunk_questions_out = []
            for data in batch:
                data["id"] = remapped_id(data["id"])
                data["chunk_id"] = chunk_map[data["chunk_id"]]
                data["notebook_id"] = new_id
                data["source_id"] = source_map[data["source_id"]]
                chunk_questions_out.append(data)
            self._store.insert_copy_rows(
                "chunk_questions", chunk_questions_out, chunk_size=chunk_size
            )

        for batch in self._rows.batches("chunk_embeddings"):
            chunk_embeddings_out = []
            for data in batch:
                data["chunk_id"] = chunk_map[data["chunk_id"]]
                data["notebook_id"] = new_id
                chunk_embeddings_out.append(data)
            self._store.insert_copy_rows(
                "chunk_embeddings", chunk_embeddings_out, chunk_size=chunk_size
            )

        for batch in self._rows.batches("element_embeddings"):
            element_embeddings_out = []
            for data in batch:
                data["element_id"] = element_map[data["element_id"]]
                data["source_id"] = source_map[data["source_id"]]
                data["notebook_id"] = new_id
                element_embeddings_out.append(data)
            self._store.insert_copy_rows(
                "element_embeddings", element_embeddings_out, chunk_size=chunk_size
            )

        for batch in self._rows.batches("knowledge_embeddings"):
            knowledge_embeddings_out = []
            for data in batch:
                data["object_id"] = object_map[data["object_id"]]
                data["notebook_id"] = new_id
                knowledge_embeddings_out.append(data)
            self._store.insert_copy_rows(
                "knowledge_embeddings", knowledge_embeddings_out, chunk_size=chunk_size
            )

        for batch in self._rows.batches("relation_embeddings"):
            relation_embeddings_out = []
            for data in batch:
                data["relation_id"] = relation_map[data["relation_id"]]
                data["notebook_id"] = new_id
                relation_embeddings_out.append(data)
            self._store.insert_copy_rows(
                "relation_embeddings", relation_embeddings_out, chunk_size=chunk_size
            )

        for batch in self._rows.batches("concept_clusters"):
            clusters_out = []
            for data in batch:
                data["id"] = remapped_id(data["id"])
                data["notebook_id"], data["generation"] = new_id, 0
                data["canonical_id"] = object_map.get(data["canonical_id"], data["canonical_id"])
                data["member_object_id"] = object_map.get(
                    data["member_object_id"], data["member_object_id"]
                )
                clusters_out.append(data)
            self._store.insert_copy_rows(
                "concept_clusters", clusters_out, chunk_size=chunk_size
            )
