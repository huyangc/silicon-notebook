from __future__ import annotations

import json
import logging
import re
import secrets
import shutil
from pathlib import Path
from typing import Callable, TYPE_CHECKING

from app.models.notebooks import NotebookSummary
from app.domain.repository import RepositoryCompatibilitySeams
from app.repositories.ports import (
    AgentObservationStorePort,
    AgentProfileStorePort,
    NotebookTooLargeToCopyError,
    RepositoryDatabasePort,
    SharingStorePort,
)
from app.services.knowhow.assets import ALLOWED_MIME_EXTENSIONS
from app.services.knowhow.ids import cell_chunk_id, element_id
from app.services.notebook_catalog import NotebookCatalogService, NotebookSummaryQuery

_log = logging.getLogger("silicon_notebook.sharing")


# PR-2+3 Task 13: a knowhow cell's content_md embeds pasted images as
# ``![alt](asset://<asset_id>)`` (design doc §①) — a deep copy mints a fresh
# asset id per copied ``notebook_assets`` row (see copy_notebook), so every
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
    copy_notebook's ordering) is left exactly as-is rather than dropped."""
    if "asset://" not in text:
        return text
    return _ASSET_REF_RE.sub(
        lambda m: f"asset://{asset_map.get(m.group(1), m.group(1))}", text
    )


class NotebookCopyService:
    """Deep-copy orchestration: ID remapping, chunked transactions through the
    store, filesystem copy and compensation ordering.

    Compatibility seams are read during EVERY operation — ``seams.new_id()``
    (sqlite_repository._new_id), ``seams.copy_chunk_size()``
    (sqlite_repository._COPY_CHUNK) and ``seams.remap_json_ids`` — so patches
    applied after repository construction stay authoritative, and the per-row
    insert seat (facade ``_insert_row``) is honoured inside the store.

    ``schedule_projection`` (PR-2+3 Task 13) is the facade's
    ``knowhow_api.get_scheduler(repo).schedule`` callable — copy_notebook
    calls it once per copied knowhow table, right after publish, so the
    structural KO/edge graph gets rebuilt (dynamic column-name types) without
    the caller ever blocking on it (same debounced background scheduler every
    editing endpoint already goes through — see ``ProjectionScheduler``).
    """

    def __init__(
        self,
        *,
        store: SharingStorePort,
        catalog: NotebookCatalogService,
        seams: "RepositoryCompatibilitySeams",
        storage_dir: Callable[[], Path],
        schedule_projection: Callable[[str], None],
    ) -> None:
        self._store = store
        self._catalog = catalog
        self._seams = seams
        self._storage_dir = storage_dir
        self._schedule_projection = schedule_projection

    def sweep_stuck_copies(self, created_by: "str | None" = None) -> int:
        return self._store.sweep_stale_copies(created_by=created_by)

    def copy_notebook(
        self,
        source_notebook_id: str,
        *,
        new_owner_id: str,
        actor_label: "str | None" = None,
        new_name: "str | None" = None,
    ) -> NotebookSummary:
        """Deep-copy a notebook with remapped IDs and a hidden copy sentinel.

        Phase contract (frozen): sweep only stale own copies → copy source
        directory → insert copying sentinel → copy each table in configured
        chunks → validate counts and references → publish original status.
        Failure compensates ONLY the destination rows/files.
        """
        self._store.sweep_stale_copies(created_by=new_owner_id)
        source_notebook = self._catalog.get_notebook(source_notebook_id)
        new_id = self._seams.new_id("nb")
        now = self._seams.now()
        name = new_name or f"{source_notebook.name} (副本)"
        chunk_size = self._seams.copy_chunk_size()

        def remapped_id(old: str) -> str:
            prefix = old.split("-", 1)[0] if old else "id"
            return self._seams.new_id(prefix)

        source_dir = self._storage_dir() / "notebooks" / source_notebook_id
        destination_dir = self._storage_dir() / "notebooks" / new_id
        # PR-2+3 Task 13: notebook_assets' on-disk files live in a SEPARATE
        # tree from source_dir (storage_dir/assets/<nb>/, not
        # storage_dir/notebooks/<nb>/ — see app.services.knowhow.assets.
        # AssetService.path_for), so they need their own copied-flag +
        # destination path for the same "compensate only what we actually
        # wrote" discipline as destination_dir below.
        assets_dest_dir = self._storage_dir() / "assets" / new_id
        copied_files = False
        assets_copied = False
        try:
            if source_dir.exists():
                shutil.copytree(source_dir, destination_dir)
                copied_files = True

            # snapshot_copy_rows enforces the copyable-row bound ATOMICALLY inside
            # its own stable-snapshot read transaction (raises
            # NotebookTooLargeToCopyError before materialising any rows), so a
            # concurrent ingestion cannot slip an over-limit notebook past the
            # cached pre-checks into the fetchall (codex PR#353 r3). copytree
            # above is disk, not memory; the except below rmtree's it on a raise.
            snapshot = self._store.snapshot_copy_rows(source_notebook_id)

            notebook_row = snapshot["notebooks"][0]
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
            )
            self._store.insert_copy_rows(
                "notebooks", [notebook_row], chunk_size=chunk_size
            )

            source_map: dict = {}
            element_map: dict = {}
            chunk_map: dict = {}
            object_map: dict = {}
            fact_map: dict = {}
            generation_map: dict[tuple[str, str], str] = {}
            relation_map: dict = {}
            # PR-2+3 Task 13: knowhow business-table + hidden-source-content
            # remap maps. ``knowhow_source_ids_old`` is captured from the
            # RAW (not-yet-mutated) snapshot, before anything below rewrites
            # snapshot["knowhow_tables"]' own dicts in place — it is how the
            # elements/chunks loops further down tell a knowhow row apart
            # from an ordinary document row (both share the same snapshot
            # queries now that neither is excluded).
            khtbl_map: dict = {}
            khcol_map: dict = {}
            khrow_map: dict = {}
            khcel_map: dict = {}
            khcode_map: dict = {}
            asset_map: dict = {}
            knowhow_source_ids_old = {
                t["hidden_source_id"]
                for t in snapshot["knowhow_tables"]
                if t.get("hidden_source_id")
            }
            # old element id -> NEW row id, populated by the elements_out
            # loop below; the chunks_out loop reads it to recompute each
            # knowhow chunk's id from ITS row (a chunk has no row_id column
            # of its own — only its element_ids[0] indirectly names one).
            element_row_new: dict = {}

            sources_out = []
            for data in snapshot["sources"]:
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
                data["agent_profile_id"] = None
                sources_out.append(data)
            self._store.insert_copy_rows("sources", sources_out, chunk_size=chunk_size)

            paper_meta_out = []
            for data in snapshot["source_paper_meta"]:
                data["source_id"] = source_map[data["source_id"]]
                data["notebook_id"] = new_id
                paper_meta_out.append(data)
            self._store.insert_copy_rows(
                "source_paper_meta", paper_meta_out, chunk_size=chunk_size
            )

            authors_out = []
            for data in snapshot["source_authors"]:
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

            schema_rows = []
            for data in snapshot["notebook_object_schemas"]:
                data["notebook_id"] = new_id
                data["created_by"] = new_owner_id
                schema_rows.append(data)
            self._store.insert_copy_rows(
                "notebook_object_schemas", schema_rows, chunk_size=chunk_size
            )

            # --- PR-2+3 Task 13: knowhow business tables ------------------
            # Order is FK-safe (each leg's remap map is fully populated
            # before the next leg that depends on it runs): tables (needs
            # source_map for hidden_source_id) -> columns/rows (need
            # khtbl_map) -> assets (independent; builds asset_map) -> cells
            # (needs khrow_map + khcol_map + asset_map, for the content_md
            # asset:// rewrite) -> cell_code (needs khrow_map + khcol_map).
            tables_out = []
            for data in snapshot["knowhow_tables"]:
                data["id"] = khtbl_map.setdefault(data["id"], remapped_id(data["id"]))
                data["notebook_id"] = new_id
                data["created_by"] = new_owner_id
                old_hidden = data.get("hidden_source_id")
                data["hidden_source_id"] = (
                    source_map.get(old_hidden, old_hidden) if old_hidden else old_hidden
                )
                tables_out.append(data)
            self._store.insert_copy_rows("knowhow_tables", tables_out, chunk_size=chunk_size)

            columns_out = []
            for data in snapshot["knowhow_columns"]:
                data["id"] = khcol_map.setdefault(data["id"], remapped_id(data["id"]))
                data["table_id"] = khtbl_map[data["table_id"]]
                columns_out.append(data)
            self._store.insert_copy_rows("knowhow_columns", columns_out, chunk_size=chunk_size)

            rows_out = []
            for data in snapshot["knowhow_rows"]:
                data["id"] = khrow_map.setdefault(data["id"], remapped_id(data["id"]))
                data["table_id"] = khtbl_map[data["table_id"]]
                # The copy has not been (re)projected yet regardless of what
                # the SOURCE row's own status was — 'pending' is the schema
                # default for exactly this "not yet projected" state (see
                # migrations.py _migration_16), and the scheduling loop at
                # the end of this method is what will settle it.
                data["projection_status"] = "pending"
                rows_out.append(data)
            self._store.insert_copy_rows("knowhow_rows", rows_out, chunk_size=chunk_size)

            assets_out = []
            asset_files: list[tuple[str, str, str]] = []  # (old_id, new_id, mime)
            for data in snapshot["notebook_assets"]:
                old_asset_id = data["id"]
                new_asset_id = asset_map.setdefault(old_asset_id, remapped_id(old_asset_id))
                data["id"] = new_asset_id
                data["notebook_id"] = new_id
                asset_files.append((old_asset_id, new_asset_id, data["mime"]))
                assets_out.append(data)
            self._store.insert_copy_rows("notebook_assets", assets_out, chunk_size=chunk_size)

            if asset_files:
                assets_src_dir = self._storage_dir() / "assets" / source_notebook_id
                assets_dest_dir.mkdir(parents=True, exist_ok=True)
                assets_copied = True
                for old_asset_id, new_asset_id, mime in asset_files:
                    ext = ALLOWED_MIME_EXTENSIONS.get(mime, "bin")
                    src_path = assets_src_dir / f"{old_asset_id}.{ext}"
                    if src_path.is_file():
                        shutil.copy2(src_path, assets_dest_dir / f"{new_asset_id}.{ext}")

            cells_out = []
            for data in snapshot["knowhow_cells"]:
                data["id"] = khcel_map.setdefault(data["id"], remapped_id(data["id"]))
                data["row_id"] = khrow_map[data["row_id"]]
                data["column_id"] = khcol_map[data["column_id"]]
                data["content_md"] = _rewrite_asset_refs(
                    data.get("content_md") or "", asset_map
                )
                cells_out.append(data)
            self._store.insert_copy_rows("knowhow_cells", cells_out, chunk_size=chunk_size)

            cell_code_out = []
            for data in snapshot["knowhow_cell_code"]:
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
                note=f"随笔记本《{source_notebook.name}》复制而来",
            )
            # --- end knowhow business tables -------------------------------

            elements_out = []
            for data in snapshot["source_elements"]:
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
                data["source_id"] = source_map[data["source_id"]]
                elements_out.append(data)
            self._store.insert_copy_rows(
                "source_elements", elements_out, chunk_size=chunk_size
            )

            json_maps = {
                "element_id": element_map,
                "element_ids": element_map,
                "source_id": source_map,
                "object_id": object_map,
            }

            chunks_out = []
            for data in snapshot["chunks"]:
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
            # unified_kg_state 不在 _COPY_SNAPSHOT_QUERIES 的表集合内，所以副本
            # 的 chunk_elements_indexed 恒缺失（读作 0），读路径走 legacy 全量
            # 扫描——反查行缺席是正确且自洽的。
            # ⚠ 若将来把 unified_kg_state 加进深拷贝表集合，**必须**同时复制
            # chunk_elements（或显式把副本的 chunk_elements_indexed 归零）：
            # 否则副本继承一个为真的标记而反查表是空的，点查路径会静默返回空
            # 证据——不报错、无自愈路径。knowhow 单表 copy_table 的目标是**已存在**
            # 的 notebook，所以那条路不能豁免，见 knowhow_transfer_store。
            # 与同为派生表的 chunk_questions（本方法下方确实复制）非对称，理由
            # 就在这里：那张表自描述（有行即可用），而 chunk_elements 的可用性
            # 由一个**不随拷贝走**的标记声明，所以「不拷贝」才是自洽的那一侧。
            self._store.insert_copy_rows("chunks", chunks_out, chunk_size=chunk_size)

            objects_out = []
            for data in snapshot["knowledge_objects"]:
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

            # Source-local facts are generation-bound: maintenance and the
            # read-only audit accept a projection only when its generation is
            # backed by a successful KG extraction run in the SAME notebook.
            # Operational extraction history deliberately does not travel
            # with a deep copy, so mint one copy-local completed generation
            # for every source represented by the copied terminal ledger (or,
            # for an older projection without a ledger, its newest fact
            # generation).  Facts, bindings and ledger rows below all remap
            # through the same (source,generation) key.
            generations_by_source: dict[str, dict[str, str]] = {}
            fact_counts_by_generation: dict[tuple[str, str], int] = {}
            for data in snapshot["knowledge_source_facts"]:
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
            for data in snapshot["knowledge_source_fact_backfills"]:
                source_id = str(data["source_id"])
                generation = str(data.get("source_generation") or "")
                if not generation:
                    continue
                active_generation_by_source[source_id] = generation
                scanned_by_source[source_id] = int(data.get("objects_scanned") or 0)
                generations_by_source.setdefault(source_id, {})[generation] = str(
                    data.get("updated_at") or data.get("created_at") or ""
                )
            for data in snapshot["knowledge_source_fact_elements"]:
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

            source_facts_out = []
            for data in snapshot["knowledge_source_facts"]:
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

            source_fact_elements_out = []
            for data in snapshot["knowledge_source_fact_elements"]:
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

            source_fact_backfills_out = []
            for data in snapshot["knowledge_source_fact_backfills"]:
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

            relations_out = []
            for data in snapshot["knowledge_relations"]:
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

            chunk_questions_out = []
            for data in snapshot["chunk_questions"]:
                data["id"] = remapped_id(data["id"])
                data["chunk_id"] = chunk_map[data["chunk_id"]]
                data["notebook_id"] = new_id
                data["source_id"] = source_map[data["source_id"]]
                chunk_questions_out.append(data)
            self._store.insert_copy_rows(
                "chunk_questions", chunk_questions_out, chunk_size=chunk_size
            )

            chunk_embeddings_out = []
            for data in snapshot["chunk_embeddings"]:
                data["chunk_id"] = chunk_map[data["chunk_id"]]
                data["notebook_id"] = new_id
                chunk_embeddings_out.append(data)
            self._store.insert_copy_rows(
                "chunk_embeddings", chunk_embeddings_out, chunk_size=chunk_size
            )

            element_embeddings_out = []
            for data in snapshot["element_embeddings"]:
                data["element_id"] = element_map[data["element_id"]]
                data["source_id"] = source_map[data["source_id"]]
                data["notebook_id"] = new_id
                element_embeddings_out.append(data)
            self._store.insert_copy_rows(
                "element_embeddings", element_embeddings_out, chunk_size=chunk_size
            )

            knowledge_embeddings_out = []
            for data in snapshot["knowledge_embeddings"]:
                data["object_id"] = object_map[data["object_id"]]
                data["notebook_id"] = new_id
                knowledge_embeddings_out.append(data)
            self._store.insert_copy_rows(
                "knowledge_embeddings", knowledge_embeddings_out, chunk_size=chunk_size
            )

            relation_embeddings_out = []
            for data in snapshot["relation_embeddings"]:
                data["relation_id"] = relation_map[data["relation_id"]]
                data["notebook_id"] = new_id
                relation_embeddings_out.append(data)
            self._store.insert_copy_rows(
                "relation_embeddings", relation_embeddings_out, chunk_size=chunk_size
            )

            clusters_out = []
            for data in snapshot["concept_clusters"]:
                data["id"] = remapped_id(data["id"])
                data["notebook_id"] = new_id
                data["canonical_id"] = object_map.get(data["canonical_id"], data["canonical_id"])
                data["member_object_id"] = object_map.get(
                    data["member_object_id"], data["member_object_id"]
                )
                clusters_out.append(data)
            self._store.insert_copy_rows(
                "concept_clusters", clusters_out, chunk_size=chunk_size
            )

            kg_fts_rows = [
                (
                    data["id"],
                    new_id,
                    (json.loads(data["payload"]).get("name") or "").strip(),
                )
                for data in objects_out
                if data.get("status") != "deprecated"
            ]
            kg_fts_rows = [row for row in kg_fts_rows if row[2]]
            self._store.insert_fts_rows(
                "INSERT INTO kg_objects_fts(object_id, notebook_id, name) VALUES (?, ?, ?)",
                kg_fts_rows,
                chunk_size=chunk_size,
            )

            chunk_fts_rows = [
                (data["id"], new_id, data.get("text") or "") for data in chunks_out
            ]
            self._store.insert_fts_rows(
                "INSERT INTO chunks_fts(chunk_id, notebook_id, text) VALUES (?, ?, ?)",
                chunk_fts_rows,
                chunk_size=chunk_size,
            )

            self._store.validate_copy(source_notebook_id, new_id)
            self._store.publish_copy(new_id, source_notebook.status)
        except Exception:
            self._store.compensate_copy(new_id)
            if copied_files:
                shutil.rmtree(destination_dir, ignore_errors=True)
            if assets_copied:
                shutil.rmtree(assets_dest_dir, ignore_errors=True)
            raise
        # Success-only path — publish_copy above flipped the sentinel off
        # 'copying', and compensate_copy can no longer reap the notebooks row
        # (its DELETE is `WHERE status = 'copying'` only, sharing_store.py).
        # NOTHING fallible past publish may live inside the try/except: a
        # raise here used to run compensation that deleted the published
        # copy's chunks_fts/kg_objects_fts/knowledge_embeddings rows and
        # rmtree'd its files while the (already published) row SURVIVED — a
        # persistent, silently-corrupted copy sweep_stale_copies never reaps.
        #
        # PR-2+3 Task 13: schedule ONE structural reprojection per copied
        # knowhow table (khtbl_map.values() are all NEW table ids, still in
        # scope from the completed try; empty for a notebook with no knowhow
        # tables, so this is a zero-cost no-op for the common case). The
        # chunks/vectors already sitting in the copy are byte-identical to
        # what project_table will independently recompute, so this rebuilds
        # the KO/edge graph (dynamic column-name types) without a single
        # additional embedder call — see element_id/cell_chunk_id above. A
        # scheduling failure (realistic: threading.Timer.start() RuntimeError
        # under thread/fd exhaustion — this codebase has documented history
        # there) is LOGGED, never compensated: the copy itself is complete
        # and valid; the table's manual 重建投影 button (or any later edit,
        # which schedules the same full pass) covers a missed schedule.
        for new_table_id in khtbl_map.values():
            try:
                self._schedule_projection(new_table_id)
            except Exception:  # noqa: BLE001 — published copy must survive a scheduling failure
                _log.warning(
                    "copy_notebook: 副本 %s 的 knowhow 表 %s 投影调度失败"
                    "（副本数据完整，可在表内手动重建投影）",
                    new_id,
                    new_table_id,
                    exc_info=True,
                )
        return self._catalog.get_notebook(new_id)


class NotebookSharingService:
    """Sharing, membership and read-access orchestration over SharingStore.

    ``copy_stats`` is a late-bound callback to the facade's
    ``notebook_copy_stats`` (the cross-domain scale-profile memo) so instance
    patches and live Settings mutations keep being observed.  ``database`` +
    ``summaries`` hydrate join_shared's NotebookSummary over one connection,
    exactly like the former mixin's ``_notebook_from_row`` path.
    """

    def __init__(
        self,
        *,
        store: SharingStorePort,
        copies: NotebookCopyService,
        catalog: NotebookCatalogService,
        summaries: NotebookSummaryQuery,
        database: RepositoryDatabasePort,
        copy_stats: Callable[[str], dict],
        profiles: "AgentProfileStorePort | None" = None,
        observations: "AgentObservationStorePort | None" = None,
    ) -> None:
        self._store = store
        self._copies = copies
        self._catalog = catalog
        self._summaries = summaries
        self._database = database
        self._copy_stats = copy_stats
        # Agentic Memory P1 (T5): the member's private "understanding" overlay
        # for this notebook. Losing access is the one event that makes those
        # blocks orphaned data, so removal cleans them up here. ``None`` = not
        # wired (older composition roots / test doubles): membership removal
        # must never fail because of it.
        self._profiles = profiles
        # codex #535 R6 P2 (Agentic Memory P3): the member's Agent observation
        # queue for this notebook follows the same blank-slate contract as the
        # overlay blocks above — rows are derived entirely from that person's
        # own Agents' use of this library, so removal clears them too. ``None``
        # = not wired, same fail-open posture as ``profiles``.
        self._observations = observations

    # ---------------------------------------------------------------- share
    def share_notebook(self, notebook_id: str) -> dict:
        self._catalog.get_notebook(notebook_id)  # raises KeyError if missing
        token = self._store.set_share_token(
            notebook_id, f"shr-{secrets.token_urlsafe(16)}"
        )
        stats = self.notebook_copy_stats(notebook_id)
        return {
            "share_token": token,
            "copyable": stats["copyable"],
            "size": stats["size"],
        }

    def share_state(self, notebook_id: str) -> dict:
        """当前的分享链接状态(P1-T4)。**只读**:不铸 token、无 token 时不算规模。

        存在性检查刻意用 `notebook_row` 而不是 `self._catalog.get_notebook`:后者是
        一次完整的 summary 水合(逐类计数、挂载参考库、KG 探针……),而这里只需要
        「这本库在不在、它的 token 是什么」两个字段。路由上的 `notebook:manage` 守卫
        已经解析过这本库,这里再水合一遍纯属白付。
        """
        row = self._store.notebook_row(notebook_id)
        if row is None:
            raise KeyError(notebook_id)
        token = str(row["share_token"] or "") if row["is_shared"] else ""
        if not token:
            # 没有链接就没有「可不可拷贝 / 多大」这回事 —— 也不为它跑一次规模统计。
            return {"share_token": "", "copyable": False, "size": {}}
        stats = self.notebook_copy_stats(notebook_id)
        return {
            "share_token": token,
            "copyable": stats["copyable"],
            "size": stats["size"],
        }

    def unshare_notebook(self, notebook_id: str) -> None:
        self._catalog.get_notebook(notebook_id)  # raises KeyError if missing
        self._store.clear_share(notebook_id)

    def find_notebook_by_share_token(self, token: str) -> "str | None":
        if not token:
            return None
        return self._store.find_by_token(token)

    def notebook_copy_stats(self, notebook_id: str) -> dict:
        # Share-routing copyability (copy vs read-only join — read by the
        # copy/join/preview routes and every share path below). ``self._copy_stats``
        # is the KG-version-cached bytes+chunks+nodes verdict; re-check the deep-copy
        # total-materialisation bound FRESH, because assets / sources / paper_meta
        # grow WITHOUT bumping that cache's version key. Otherwise a stale-copyable
        # notebook offers a copy that 409s at the guard while join rejects it as
        # small — a dead end (codex PR#354 r2 P2). Short-circuited to notebooks
        # already copyable by bytes+chunks+nodes (large libraries never pay the
        # count); retrieval reads scale_artifacts' cached stats directly and never
        # enters this path.
        stats = self._copy_stats(notebook_id)
        if stats.get("copyable") and not self._store.snapshot_copy_within_limits(notebook_id):
            stats = {**stats, "copyable": False}
        return stats

    def shared_preview(self, notebook_id: str) -> dict:
        notebook = self._catalog.get_notebook(notebook_id)
        stats = self.notebook_copy_stats(notebook_id)
        owner_display, titles = self._store.shared_preview_rows(notebook_id)
        return {
            "name": notebook.name,
            "owner_display": owner_display,
            "source_count": int(notebook.counts.get("sources", 0)),
            "node_count": stats["size"]["nodes"],
            "edge_count": stats["size"]["edges"],
            "source_titles": titles,
            "mode": "copy" if stats["copyable"] else "readonly",
            "size": stats["size"],
        }

    def shared_by_me(self, user_id: str) -> list:
        """owner 的「已分享」总览:只读共享 ∨ 共享给群组(P1-T4)。

        ⚠ **没有分享链接的行不算规模、不查成员**。`mode` / `size` / `members` 三个
        字段说的全是「这条**链接**是怎么回事」——纯群组共享的行没有链接,消费方也不
        渲染它们。而 `notebook_copy_stats` 每次都要新鲜复核一次深拷贝上限
        (`snapshot_copy_within_limits`,一次真实统计),`list_members` 又是一次查询:
        50 本只共享给群组的库会白付 100 次。所以这两笔只在真有 token 时才付。
        """
        out = []
        for row in self._store.list_shared_by_owner(user_id):
            token = row["share_token"] or ""
            if token:
                stats = self.notebook_copy_stats(row["id"])
                readonly = not stats["copyable"]
                mode = "readonly" if readonly else "copy"
                size = stats["size"]
                members = self.list_members(row["id"]) if readonly else []
            else:
                # 中性值:没有链接就没有「可不可拷贝 / 多大 / 谁加入了」这回事。
                mode, size, members = "readonly", {}, []
            out.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "share_token": token,
                    "mode": mode,
                    "size": size,
                    "members": members,
                    # 共享给了几个**不同的**群组(P1-T4)。为 0 时这一行就是一条
                    # 纯只读共享,与改动前逐字一致;非 0 而 share_token 为空时,
                    # 这一行只因群组共享而存在——消费方据此不渲染分享链接。
                    "group_count": int(row["group_count"] or 0),
                }
            )
        return out

    # ----------------------------------------------------------------- copy
    def copy_notebook(
        self,
        source_notebook_id: str,
        *,
        new_owner_id: str,
        actor_label: "str | None" = None,
        new_name: "str | None" = None,
    ) -> NotebookSummary:
        # Fast early reject (defense-in-depth): the deep copy materialises every
        # table (objects / relations / chunks / all vector tables) into one
        # in-memory snapshot — 300GB+ at 8M-object scale. This cached copy_stats
        # check rejects an already-large source cheaply (before copytree) and
        # covers non-route callers. It does NOT need to be race-free: the
        # authoritative, race-proof bound is NotebookCopyService.copy_notebook's
        # within_copy_row_limit(), checked FRESH on the snapshot's own connection
        # immediately before the fetchall (OOM audit P2-7).
        if not self.notebook_copy_stats(source_notebook_id)["copyable"]:
            raise NotebookTooLargeToCopyError(
                f"notebook {source_notebook_id} is too large to deep-copy "
                f"(exceeds notebook_copy_max_bytes/rows); share read-only instead"
            )
        return self._copies.copy_notebook(
            source_notebook_id, new_owner_id=new_owner_id,
            actor_label=actor_label, new_name=new_name,
        )

    def sweep_stuck_copies(self, created_by: "str | None" = None) -> int:
        return self._copies.sweep_stuck_copies(created_by)

    # -------------------------------------------------------- access guards
    def user_can_access_notebook(self, notebook_id: str, user_id: str) -> bool:
        """写权:仅 owner(安全边界,勿放宽)。"""
        return self._store.user_can_access_notebook(notebook_id, user_id)

    def is_member(self, notebook_id: str, user_id: str) -> bool:
        return self._store.is_member(notebook_id, user_id)

    def user_can_admin_notebook(self, notebook_id: str, user_id: str) -> bool:
        """管理权:owner ∪ `role='admin'` 的有效授权边(P2 能力翻转,裁决 P2-1)。

        谓词的唯一定义点在两个后端的 `access_sql.NOTEBOOK_ADMIN_SQL`,这里一跳直接
        委托 store —— 与 `user_can_read_notebook` 同款,理由也一样(手写复刻不会跟随
        谓词扩展)。**不**取代 `user_can_access_notebook`:删库与 Agent/MCP 面仍恒 owner。
        """
        return self._store.user_can_admin_notebook(notebook_id, user_id)

    def user_can_read_notebook(self, notebook_id: str, user_id: str) -> bool:
        """读权:owner ∪ 只读成员 ∪ 有效授权边(user/group/group_admins/everyone)。

        谓词的唯一定义点在两个后端的 `access_sql.NOTEBOOK_READ_SQL`,故这里一跳直接
        委托 store。曾经写成 `写权 or 成员`——语义相同但要发两次查询,而且是一份手写
        复刻;P1 群组授权扩展读权时,那份复刻不会跟随(正是这次委托要防的)。
        """
        return self._store.user_can_read_notebook(notebook_id, user_id)

    def user_can_read_source(self, source_id: str, user_id: str) -> bool:
        notebook_id = self._store.source_notebook_id(source_id)
        return bool(notebook_id) and self.user_can_read_notebook(notebook_id, user_id)

    def user_can_read_answer(self, answer_id: str, user_id: str) -> bool:
        notebook_id = self._store.answer_notebook_id(answer_id)
        return bool(notebook_id) and self.user_can_read_notebook(notebook_id, user_id)

    # ------------------------------------------------------------ membership
    def add_member(self, notebook_id: str, user_id: str) -> None:
        return self._store.add_member(notebook_id, user_id)

    def remove_member(self, notebook_id: str, user_id: str) -> None:
        """Drop the membership row, then discard that member's private
        understanding overlay for this notebook.

        Agentic Memory P1 (T5). The blocks are derived ENTIRELY from that
        person's own use of this library, so the moment they lose access the
        rows are orphaned data about a notebook they can no longer open — and
        if access is ever granted again, starting from a blank slate is the
        correct behaviour, not a regression.

        This is defence in depth, not the access control: reading is already
        gated (injection takes an explicit owner, the API reads through the
        notebook read guard), so a row that outlived a removal would not be
        exposed. The physical delete is what makes "revoked" also mean "gone".

        Order matters — membership first. If the clear fails, the removal has
        still happened (an access change must never depend on a cleanup), and
        the read-side gate covers the leftover rows. The reverse order could
        delete a live member's notes and then leave them a member.

        The SHARED base is untouched by construction: ``clear_all`` scopes to
        one ``owner_id``, and ``user_id`` here is never the ``''`` sentinel.

        This method only removes the explicit per-user membership row — it
        does not, and cannot, revoke access a person still holds through a
        group grant (``notebook_grants`` with a group/group_admins/everyone
        principal). That authorization path does not run through here at
        all; whether the caller can still open this notebook after this call
        is decided entirely by the read-side gate
        (``user_can_read_notebook``/``NOTEBOOK_READ_SQL``), same as every
        other access decision in this service. If a group grant still gives
        them read access, this call still resets their overlay — that is a
        blank slate, not a leak, and is the same "start over" outcome as any
        other rejoin.
        """
        self._store.remove_member(notebook_id, user_id)
        self._clear_member_profile(notebook_id, user_id)

    def _clear_member_profile(self, notebook_id: str, user_id: str) -> None:
        """Discard both halves of this member's overlay: the block rows
        (``clear_all``) AND the job/status row (``clear_job_row``).

        Agentic Memory P1 (T5 repair round). Clearing only the blocks left a
        stale ``agent_profile_jobs`` row behind — most visibly its
        ``pending_signal`` counter, which is derived entirely from this
        member's own asks/reports in THIS notebook. Without this, a member
        removed and then re-added would not start from a blank slate (the
        "rejoin" contract this method exists to guarantee): their counter
        would already be partway to the next consolidation run, or their row
        could still show a stale ``failed``/``running`` status from before
        they left. Both deletes are independently fail-open — a job-row
        failure must not re-raise past an already-committed access change,
        the same reasoning that already applied to the block-row delete.

        ⚠ Order matters (codex #520 R3 P1): the job row goes FIRST. The
        overlay worker's revocation guards are keyed off that row — it
        re-reads it before writing, and a ``settle`` that finds it gone
        triggers the post-write wipe. Blocks-first left a window where an
        in-flight worker passed its pre-write check, recreated the just-
        cleared blocks AND settled successfully, all before the job row
        vanished — every guard green, private data resurrected. Deleting the
        marker first means any worker still in flight either skips its writes
        (pre-check) or fails its settle and wipes what it wrote (post-guard);
        the block delete below then covers the fully-settled-before-removal
        case."""
        if not user_id:
            return
        if self._profiles is not None:
            self._clear_profile_halves(notebook_id, user_id)
        # codex #535 R6 P2:观察行同批清空——它们是这个人自己的 Agent 在本库
        # 留下的使用痕迹,移出后残留会经 GET .../agent-observations 立即复活,
        # 还会喂进重新加入后的第一次覆盖层巡固,破坏「空白起点」契约。座位与
        # profiles 各自独立判 None(旧组合根/替身可能只接了一半)。
        if self._observations is not None:
            try:
                self._observations.clear_observations(notebook_id, user_id)
            except Exception:  # noqa: BLE001 — access change already committed
                _log.exception(
                    "failed to clear agent observations for notebook %s",
                    notebook_id,
                )

    def _clear_profile_halves(self, notebook_id: str, user_id: str) -> None:
        try:
            self._profiles.clear_job_row(notebook_id, user_id)
        except Exception:  # noqa: BLE001 — access change already committed
            _log.exception(
                "failed to clear agent profile job row for notebook %s",
                notebook_id,
            )
        try:
            self._profiles.clear_all(notebook_id, user_id)
        except Exception:  # noqa: BLE001 — access change already committed
            _log.exception(
                "failed to clear agent profile overlay for notebook %s",
                notebook_id,
            )

    def kick_all_members(self, notebook_id: str) -> None:
        """Drop every membership row for this notebook.

        ⚠ Deliberately does NOT clear the removed members' overlays, and the
        reason is that it cannot do so honestly: the store method removes the
        rows in one statement, so this layer has no list of who was removed,
        and reconstructing one with a read-then-delete would be racing the very
        rows it is deleting. Those blocks stay unreadable through the read-side
        gate (identical to the ``remove_member`` case before its clear runs),
        and rejoining the notebook is what makes them visible again — which is
        the same person's own notes, so that is correct rather than a leak.
        The single-member path is the one users actually take, and it cleans up.
        """
        return self._store.kick_all_members(notebook_id)

    def list_members(self, notebook_id: str) -> list:
        return self._store.list_members(notebook_id)

    def join_shared(self, notebook_id: str, user_id: str) -> NotebookSummary:
        self.add_member(notebook_id, user_id)
        with self._database.connect() as db:
            row = self._store.notebook_row_on(db, notebook_id)
            notebook = self._summaries.from_row(db, row)
        notebook.access = "reader"
        return notebook

    def leave_notebook(self, notebook_id: str, user_id: str) -> None:
        self.remove_member(notebook_id, user_id)

    # -------------------------------------------------------------- ownership
    def source_owner(self, source_id: str) -> "str | None":
        return self._store.source_owner(source_id)

    def source_notebook_id(self, source_id: str) -> "str | None":
        return self._store.source_notebook_id(source_id)

    def conversation_owner(self, conversation_id: str) -> "str | None":
        return self._store.conversation_owner(conversation_id)

    def answer_owner(self, answer_id: str) -> "str | None":
        return self._store.answer_owner(answer_id)
