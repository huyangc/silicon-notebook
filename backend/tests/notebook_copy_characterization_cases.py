"""Characterization rig for ``NotebookCopyService.copy_notebook`` — the deep copy.

**两端吃同一份世界、同一套观测、同一组场景**:``tests/test_notebook_copy_characterization.py``
(SQLite)与 ``tests/postgres/test_notebook_copy_characterization_pg.py``(PostgreSQL)各自只写
一个 ``Backend`` 适配器(怎么插行、有哪些表、怎么录 SQL)。不是测试模块(没有 ``test_``
前缀),pytest 不会收集它。

两层,按 AGENTS.md「不做只为重构服务的金标快照」分开:

* **行为层**(``Scenario.expect``,长期保留):用户与下游可见的事实——副本里有哪些行、引用
  是否都落在副本自己身上、哪些东西不随副本走(Memory、成员、授权边、分享口令、同步来源、
  对新主人无效的挂载边)、失败时只补偿目的地、写库调用按外键安全的冻结顺序发生。
* **拆分 oracle 层**(``observe`` 的完整观测 + 逐字节快照):拷贝服务与外界交换的一切,按
  发生顺序——拷贝服务对 store / catalog / 投影调度 / 文件系统的每一次调用(规范化后的全部
  参数与返回值)、id 铸造与取时钟、本线程发出的每一条 SQL(带参数),外加整库逐表的增删
  差分、存储目录的增删差分、日志与返回值/异常。它只为 ``copy_notebook`` 的零行为变化拆分
  服务:拆分前采集、拆分后逐字节不变,拆分完成后随快照目录一起退役。

确定性:行 id 来自计数器(``<前缀>-c<序号>``,只在拷贝期间替换 ``_new_id``),拷贝时钟固定为
``COPY_NOW``;世界里的时间戳都取自 ``FIXED_INSTANTS``,其余时间戳(收割的截止时刻、store
自己取的墙钟)一律打码成 ``<ts>``;存储根目录打码成 ``<storage>``。没有浮点(向量是字节)。
SQLite 的 trace 只记应用自己发的语句(跳过 FTS5 的嵌套语句,它们随 SQLite 版本变);
PostgreSQL 跳过连接池自己的借还探测。只记拷贝所在线程。
"""
from __future__ import annotations

import collections
import dataclasses
import datetime as dt
import hashlib
import json
import logging
import re
import shutil as real_shutil
import threading
from pathlib import Path
from typing import Any, Callable, ContextManager, Optional

from app.services import notebook_sharing
from app.services.knowhow.ids import cell_chunk_id, element_id
from tests import copy_memory_cases as memory_cases
from tests.copy_memory_cases import Row

NB = "nb-ccw"
NB_PLAIN = "nb-ccw-plain"
NB_MISSING = "nb-ccw-missing"
OWNER = memory_cases.OWNER
RECIPIENT = "u-recipient"
OTHER = "u-other"

NOW = memory_cases.NOW
GEN_EARLY = "2026-09-29T01:00:00+00:00"
GEN_LATE = "2026-09-29T02:00:00+00:00"
COPY_NOW = "2026-10-01T08:00:00+00:00"
STALE = "2020-01-01T00:00:00+00:00"
FRESH = "2999-01-01T00:00:00+00:00"
FIXED_INSTANTS = frozenset(
    dt.datetime.fromisoformat(value).astimezone(dt.timezone.utc)
    for value in (NOW, GEN_EARLY, GEN_LATE, COPY_NOW, STALE, FRESH)
)

#: Columns stored as JSON (SQLite text, PostgreSQL jsonb).
JSON_COLUMNS = memory_cases.JSON_COLUMNS | {"fields", "list_fields"}
STORAGE = "<storage>"

#: The frozen, FK-safe order in which the copy writes its tables (one
#: ``insert_copy_rows`` call per table, the sentinel first).
COPY_TABLE_ORDER = (
    "notebooks", "sources", "source_paper_meta", "source_authors",
    "notebook_object_schemas", "knowhow_tables", "knowhow_columns", "knowhow_rows",
    "notebook_assets", "knowhow_cells", "knowhow_cell_code", "source_elements", "chunks",
    "knowledge_objects", "extraction_runs", "knowledge_source_facts",
    "knowledge_source_fact_elements", "knowledge_source_fact_backfills",
    "knowledge_relations", "chunk_questions", "chunk_embeddings", "element_embeddings",
    "knowledge_embeddings", "relation_embeddings", "concept_clusters",
)


def published_store_sequence(*, mounts: bool) -> list[str]:
    """The copy's store calls, in order, for a copy that reaches publish: the sentinel
    commits before the mount re-judgement reads it on another connection, the knowhow
    genesis follows the last knowhow table, the FTS mirrors precede validation."""
    return [
        "sweep_stale_copies", "snapshot_copy_rows", "insert_copy_rows:notebooks",
        *(("valid_copied_mount_base_ids", "insert_copied_mount_edges") if mounts else ()),
        *(f"insert_copy_rows:{t}" for t in COPY_TABLE_ORDER[1:11]),
        "seed_copied_knowhow_genesis",
        *(f"insert_copy_rows:{t}" for t in COPY_TABLE_ORDER[11:]),
        "insert_fts_rows", "insert_fts_rows", "validate_copy", "publish_copy",
    ]

PNG_A = b"\x89PNG\r\n\x1a\n" + b"ccw-paste" * 6
PNG_B = b"\x89PNG\r\n\x1a\n" + b"ccw-figure-one" * 6
PNG_C = b"\x89PNG\r\n\x1a\n" + b"ccw-figure-two" * 6

# ----------------------------------------------------------------- the world


def _user(uid: str) -> Row:
    return Row("users", {
        "id": uid, "email": f"{uid}@example.test", "display_name": uid, "role": "user",
        "created_at": NOW, "updated_at": NOW,
    })


def _notebook(nb: str, owner: str, *, status: str = "draft", created_at: str = NOW,
              **extra: Any) -> Row:
    return Row("notebooks", {
        "id": nb, "name": f"book {nb}", "purpose": "", "primary_domain": "Semiconductor",
        "status": status, "created_by": owner, "created_at": created_at,
        "updated_at": created_at, **extra,
    })


def _kh_meta(table_id: str, row_id: str, column_id: str) -> dict:
    return {"knowhow": {"table_id": table_id, "row_id": row_id, "column_id": column_id}}


#: (table, hidden source, title, columns[(id, name, role)], rows[(id, {column: text})])
KNOWHOW = (
    ("kt-1", "src-kh-1", "时序修复",
     (("kc-1a", "违例类型", "anchor"), ("kc-1b", "现象识别", "procedure")),
     (("krow-1a", {"kc-1a": "过冲", "kc-1b": "见图 ![波形](asset://asset-paste-1) 与 "
                                             "asset://asset-not-here"}),
      ("krow-1b", {"kc-1a": "欠冲"}))),
    ("kt-2", "src-kh-2", "工具清单",
     (("kc-2a", "名称", "anchor"),),
     (("krow-2a", {"kc-2a": "示波器"}),)),
)


def _knowhow_rows(nb: str, *, chunk_without_elements: bool) -> list[Row]:
    rows: list[Row] = []
    for table_id, hidden, title, columns, table_rows in KNOWHOW:
        rows.append(Row("sources", {
            "id": hidden, "notebook_id": nb, "title": title, "source_type": "knowhow",
            "status": "ready", "parse_status": "ready", "created_at": NOW, "updated_at": NOW,
        }))
        rows.append(Row("knowhow_tables", {
            "id": table_id, "notebook_id": nb, "title": title, "description": "d",
            "mutation_seq": 4, "hidden_source_id": hidden, "created_by": OWNER,
            "created_at": NOW, "updated_at": NOW,
        }))
        for position, (column_id, name, role) in enumerate(columns):
            rows.append(Row("knowhow_columns", {
                "id": column_id, "table_id": table_id, "name": name, "role": role,
                "position": position,
            }))
        for position, (row_id, cells) in enumerate(table_rows):
            rows.append(Row("knowhow_rows", {
                "id": row_id, "table_id": table_id, "position": position,
                "projection_status": "done", "created_at": NOW, "updated_at": NOW,
            }))
            for part, (column_id, text) in enumerate(cells.items()):
                rows.append(Row("knowhow_cells", {
                    "id": f"kcell-{row_id}-{column_id}", "row_id": row_id,
                    "column_id": column_id, "content_md": text, "updated_at": NOW,
                }))
                eid = element_id(row_id, column_id)
                rows.append(Row("source_elements", {
                    "id": eid, "source_id": hidden, "element_type": "knowhow_cell",
                    "location_label": f"{title}/{row_id}", "text": text,
                    "metadata": _kh_meta(table_id, row_id, column_id), "created_at": NOW,
                }))
                chunk = cell_chunk_id(row_id, part)
                empty = chunk_without_elements and row_id == "krow-1b"
                rows.append(Row("chunks", {
                    "id": chunk, "notebook_id": nb, "source_id": hidden, "text": text,
                    "section_path": f"{title} > {row_id}",
                    "element_ids": [] if empty else [eid], "created_at": NOW,
                }))
                rows.append(Row("chunk_embeddings", {
                    "chunk_id": chunk, "notebook_id": nb, "vector": f"vec-{chunk}".encode(),
                    "created_at": NOW,
                }))
    rows.append(Row("knowhow_cell_code", {
        "id": "kcode-1", "row_id": "krow-1a", "column_id": "kc-1b", "code_text": "print(1)",
        "language": "python", "updated_by": OWNER, "cell_content_hash": "h-1",
        "created_at": NOW, "updated_at": NOW,
    }))
    # A KG object projected from a knowhow table: rebuilt by projection, never copied.
    rows.append(Row("knowledge_objects", {
        "id": "ko-kh-projected", "notebook_id": nb, "object_type": "违例类型",
        "status": "approved", "source_id": "src-kh-1", "payload": {"name": "过冲"},
        "evidence": [], "created_at": NOW, "updated_at": NOW,
    }))
    return rows


def _web_source_rows(nb: str) -> list[Row]:
    """A second ordinary source: image element naming an asset, a two-element chunk,
    objects whose payload carries ids, deprecated/name-less objects (no FTS row), and
    two fact generations without a backfill ledger (the newest one wins although its
    name sorts first)."""
    evidence = memory_cases.evidence("src-web", "el-web-1", memory=False)
    rows = [
        Row("sources", {
            "id": "src-web", "notebook_id": nb, "title": "web page", "source_type": "web",
            "source_url": "https://example.test/a", "status": "ready", "parse_status": "ready",
            "uploaded_by": OWNER, "created_at": NOW, "updated_at": NOW,
        }),
        Row("source_elements", {
            "id": "el-web-1", "source_id": "src-web", "element_type": "paragraph",
            "location_label": "p1", "text": "web paragraph", "metadata": {"page": 1},
            "created_at": NOW,
        }),
        Row("source_elements", {
            "id": "el-web-img", "source_id": "src-web", "element_type": "image",
            "location_label": "p2", "text": "web figure",
            "metadata": {"asset_id": "asset-img-1", "caption": "图一", "page": 2},
            "created_at": NOW,
        }),
        Row("element_embeddings", {
            "element_id": "el-web-1", "source_id": "src-web", "notebook_id": nb,
            "vector": b"vec-el-web-1", "created_at": NOW,
        }),
        Row("chunks", {
            "id": "ck-web-1", "notebook_id": nb, "source_id": "src-web", "text": "web chunk",
            "element_ids": ["el-web-1", "el-web-img"], "created_at": NOW,
        }),
        Row("chunk_embeddings", {
            "chunk_id": "ck-web-1", "notebook_id": nb, "vector": b"vec-ck-web-1",
            "created_at": NOW,
        }),
        Row("chunk_questions", {
            "id": "cq-web-1", "chunk_id": "ck-web-1", "notebook_id": nb, "source_id": "src-web",
            "question": "what is on the web?", "vector": b"vec-cq-web-1", "created_at": NOW,
        }),
        Row("knowledge_objects", {
            "id": "ko-web-1", "notebook_id": nb, "object_type": "concept", "status": "approved",
            "source_id": "src-web", "source_candidate_id": "src-doc",
            "payload": {"name": "web thing", "object_id": "ko-shared-1", "source_id": "src-doc",
                        "element_ids": ["el-web-1", "el-missing"]},
            "evidence": [evidence], "created_at": NOW, "updated_at": NOW,
        }),
        Row("knowledge_embeddings", {
            "object_id": "ko-web-1", "notebook_id": nb, "vector": b"vec-ko-web-1",
            "created_at": NOW,
        }),
        Row("knowledge_objects", {
            "id": "ko-web-blank", "notebook_id": nb, "object_type": "concept",
            "status": "approved", "source_id": "src-web", "payload": {"name": "   "},
            "evidence": [], "created_at": NOW, "updated_at": NOW,
        }),
        Row("knowledge_objects", {
            "id": "ko-web-old", "notebook_id": nb, "object_type": "concept",
            "status": "deprecated", "source_id": "src-web", "payload": {"name": "old name"},
            "evidence": [], "created_at": NOW, "updated_at": NOW,
        }),
        Row("knowledge_relations", {
            "id": "kr-web", "notebook_id": nb, "source_id": "src-web",
            "source_object_id": "ko-web-1", "target_object_id": "ko-shared-1",
            "edge_type": "related_to", "evidence": [evidence], "created_at": NOW,
        }),
        Row("relation_embeddings", {
            "relation_id": "kr-web", "notebook_id": nb, "vector": b"vec-kr-web",
            "created_at": NOW,
        }),
    ]
    for tag, generation, stamp in (("a", "gen-web-z", GEN_EARLY), ("b", "gen-web-a", GEN_LATE)):
        rows.append(Row("knowledge_source_facts", {
            "id": f"fact-web-{tag}", "notebook_id": nb, "source_id": "src-web",
            "source_generation": generation, "local_object_id": f"local-web-{tag}",
            "global_object_id": "ko-web-1", "object_type": "concept",
            "payload": {"name": f"web fact {tag}", "object_id": "ko-web-1"},
            "evidence": [evidence], "created_at": stamp, "updated_at": stamp,
        }))
    rows.append(Row("knowledge_source_fact_elements", {
        "fact_id": "fact-web-a", "notebook_id": nb, "source_id": "src-web",
        "source_generation": "gen-web-z", "element_id": "el-web-1", "created_at": GEN_EARLY,
    }))
    return rows


def full_world(*, knowhow_chunk_without_elements: bool = False) -> list[Row]:
    """The copy-with-Memory world of ``copy_memory_cases`` plus everything else a deep copy
    touches: files, mounts, members, a group grant, a share token, a sync origin, object
    schemas, a second ordinary source, two knowhow tables with assets, stale sentinels."""
    rows: list[Row] = [_user(RECIPIENT), _user(OTHER)]
    for row in memory_cases.world(NB):
        values = dict(row.values)
        if row.table == "notebooks":
            values.update(
                name="Shared", tier="personal", is_shared=1, share_token="tok-ccw",
                sync_origin="env-upstream", name_auto=1, indexing_pipeline="plugin-x",
                indexing_pipeline_version="v9", indexing_pipeline_generation="gen-pipe-1",
                indexing_pipeline_job_id="job-pipe-1", metadata_generation=3,
            )
        elif row.table == "sources" and values["id"] == "src-doc":
            values.update(
                file_name="doc.md", file_path=f"{STORAGE}/notebooks/{NB}/doc.md",
                uploaded_by=OWNER,
            )
        elif row.table == "knowledge_source_fact_backfills" and values["source_id"] == "src-doc":
            values.update(after_object_id="ko-shared-2", objects_scanned=7)
        rows.append(Row(row.table, values, row.memory))
    rows += [
        Row("notebook_members", {"notebook_id": NB, "user_id": uid, "role": "reader",
                                 "added_at": NOW})
        for uid in memory_cases.USERS
    ]
    rows += [
        Row("groups", {"id": "grp-ccw", "name": "G", "description": "", "created_by": OWNER,
                       "created_at": NOW, "updated_at": NOW}),
        Row("group_members", {"group_id": "grp-ccw", "user_id": "u-alice", "added_at": NOW}),
        Row("notebook_grants", {"id": "ng-ccw-group", "notebook_id": NB,
                                "principal_type": "group", "principal_id": "grp-ccw",
                                "role": "reader", "created_by": OWNER, "created_at": NOW}),
        Row("notebook_object_schemas", {
            "notebook_id": NB, "object_type": "device", "plural": "devices",
            "fields": [{"name": "name", "type": "text"}], "primary_field": "name",
            "list_fields": ["name"], "created_by": OWNER, "created_at": NOW, "updated_at": NOW,
        }),
        # Mounts: a public library and the recipient's own travel; a private library of
        # someone else does not; an `everyone` grant makes another private one travel.
        _notebook("nb-base-public", OWNER, tier="base"),
        _notebook("nb-base-private", OTHER),
        _notebook("nb-base-recipient", RECIPIENT),
        _notebook("nb-base-everyone", OTHER),
        Row("notebook_grants", {"id": "ng-ccw-everyone", "notebook_id": "nb-base-everyone",
                                "principal_type": "everyone", "principal_id": "",
                                "role": "reader", "created_by": OTHER, "created_at": NOW}),
        *(
            Row("notebook_bases", {"notebook_id": NB, "base_notebook_id": base,
                                   "created_at": NOW, "created_by": OWNER})
            for base in ("nb-base-public", "nb-base-private", "nb-base-recipient",
                         "nb-base-everyone")
        ),
        Row("extraction_runs", {"id": "run-doc-1", "notebook_id": NB, "source_id": "src-doc",
                                "run_type": "kg", "status": "completed", "created_at": NOW,
                                "updated_at": NOW}),
    ]
    rows += _web_source_rows(NB)
    rows += _knowhow_rows(NB, chunk_without_elements=knowhow_chunk_without_elements)
    rows += [
        Row("notebook_assets", {"id": "asset-paste-1", "notebook_id": NB,
                                "filename": "paste.png", "mime": "image/png",
                                "size": len(PNG_A), "created_by": OWNER, "created_at": NOW}),
        Row("notebook_assets", {"id": "asset-img-1", "notebook_id": NB,
                                "filename": "fig1.png", "mime": "image/png",
                                "size": len(PNG_B), "created_by": OWNER, "created_at": NOW,
                                "source_id": "src-web"}),
        Row("notebook_assets", {"id": "asset-img-2", "notebook_id": NB,
                                "filename": "fig2.png", "mime": "image/png",
                                "size": len(PNG_C), "created_by": OWNER, "created_at": NOW,
                                "source_id": "src-doc"}),
        # The source is long gone; the file is gone too (no copy, row kept as-is).
        Row("notebook_assets", {"id": "asset-gone", "notebook_id": NB,
                                "filename": "gone.gif", "mime": "image/gif", "size": 1,
                                "created_by": OWNER, "created_at": NOW,
                                "source_id": "src-gone"}),
    ]
    rows += [
        # The recipient's own crashed half-copy: reaped (rows and both directories).
        _notebook("nb-stale-recipient", RECIPIENT, status="copying", created_at=STALE),
        Row("sources", {"id": "src-stale", "notebook_id": "nb-stale-recipient",
                        "title": "half", "source_type": "document", "status": "ready",
                        "parse_status": "ready", "created_at": STALE, "updated_at": STALE}),
        # The recipient's copy still in flight, and somebody else's stale one: untouched.
        _notebook("nb-fresh-recipient", RECIPIENT, status="copying", created_at=FRESH),
        _notebook("nb-stale-other", OTHER, status="copying", created_at=STALE),
    ]
    return rows


FULL_FILES = {
    f"notebooks/{NB}/doc.md": b"doc body",
    f"notebooks/{NB}/extra/notes.txt": b"nested notes",
    f"assets/{NB}/asset-paste-1.png": PNG_A,
    f"assets/{NB}/asset-img-1.png": PNG_B,
    f"assets/{NB}/asset-img-2.png": PNG_C,
    "notebooks/nb-stale-recipient/old.md": b"half copied",
    "assets/nb-stale-recipient/asset-x.png": PNG_A,
    "notebooks/nb-fresh-recipient/inflight.md": b"in flight",
}


def plain_world() -> list[Row]:
    """No Memory, no files, no knowhow, no assets, no mounts: the pre-M2 statement set."""
    return [
        _user(RECIPIENT),
        _notebook(NB_PLAIN, OWNER),
        Row("sources", {"id": "src-plain", "notebook_id": NB_PLAIN, "title": "plain",
                        "source_type": "document", "status": "ready", "parse_status": "ready",
                        "created_at": NOW, "updated_at": NOW}),
        Row("source_elements", {"id": "el-plain", "source_id": "src-plain",
                                "element_type": "paragraph", "location_label": "p1",
                                "text": "plain element", "created_at": NOW}),
        Row("chunks", {"id": "ck-plain", "notebook_id": NB_PLAIN, "source_id": "src-plain",
                       "text": "plain chunk", "element_ids": ["el-plain"], "created_at": NOW}),
        Row("knowledge_objects", {
            "id": "ko-plain", "notebook_id": NB_PLAIN, "object_type": "concept",
            "status": "approved", "source_id": "src-plain", "payload": {"name": "plain"},
            "evidence": [memory_cases.evidence("src-plain", "el-plain", memory=False)],
            "created_at": NOW, "updated_at": NOW,
        }),
    ]


# ------------------------------------------------------------- normalisation

_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:[+-]\d{2}:?\d{2}|Z)?"
)


def _instant(value: dt.datetime) -> str:
    if value.tzinfo is None:
        return "<ts>"
    utc = value.astimezone(dt.timezone.utc)
    return utc.isoformat() if utc in FIXED_INSTANTS else "<ts>"


def _timestamp_text(match: "re.Match[str]") -> str:
    try:
        return _instant(dt.datetime.fromisoformat(match.group(0)))
    except ValueError:
        return "<ts>"


class Canon:
    """The stable projection of every recorded value."""

    def __init__(self, storage_dir: Path) -> None:
        self.storage = str(storage_dir)

    def text(self, value: str) -> str:
        return _TIMESTAMP.sub(_timestamp_text, value.replace(self.storage, STORAGE))

    def __call__(self, value: Any) -> Any:
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            return f"float:{value!r}"
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, (bytes, bytearray, memoryview)):
            return "bytes:" + bytes(value).hex()
        if isinstance(value, dt.datetime):
            return _instant(value)
        if isinstance(value, (dt.date, dt.time)):
            return value.isoformat()
        if isinstance(value, Path):
            return self.text(str(value))
        if isinstance(value, dict):
            return {str(k): self(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
        if isinstance(value, (list, tuple)):
            return [self(v) for v in value]
        if isinstance(value, (set, frozenset)):
            return sorted((self(v) for v in value), key=_dumps)
        if hasattr(value, "model_dump"):
            return self(value.model_dump(mode="json"))
        if hasattr(value, "obj") and type(value).__name__ == "Jsonb":
            return {"jsonb": self(value.obj)}
        if callable(value):
            return "<callable>"
        return f"<{type(value).__name__}>"


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


# ------------------------------------------------------------------ recorder


class Recorder:
    """One scenario's ordered timeline; ``active`` gates every channel."""

    def __init__(self, canon: Canon) -> None:
        self.canon = canon
        self.active = False
        self.thread = threading.get_ident()
        self.timeline: list[dict] = []

    def on_thread(self) -> bool:
        return self.active and threading.get_ident() == self.thread

    def note(self, entry: dict) -> dict:
        if self.on_thread():
            self.timeline.append(entry)
        return entry


class _Spy:
    """Transparent recording proxy for one collaborator of the copy service."""

    def __init__(self, label: str, target: Any, recorder: Recorder,
                 faults: "dict[tuple, BaseException]") -> None:
        self._label, self._target, self._recorder, self._faults = label, target, recorder, faults

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._target, name)
        if name.startswith("_") or not callable(attr):
            return attr
        recorder, canon = self._recorder, self._recorder.canon

        def call(*args: Any, **kwargs: Any) -> Any:
            entry = recorder.note({
                "call": f"{self._label}.{name}", "args": canon(args), "kwargs": canon(kwargs),
            })
            key = (name, args[0] if args and isinstance(args[0], str) else None)
            fault = self._faults.get(key) or self._faults.get((name, None))
            if fault is not None:
                entry["fault"] = repr(fault)
                raise fault
            try:
                result = attr(*args, **kwargs)
            except BaseException as exc:
                entry["raised"] = f"{type(exc).__name__}: {canon(str(exc))}"
                raise
            if name == "snapshot_copy_rows":
                entry["ret"] = {table: len(rows) for table, rows in sorted(result.items())}
            else:
                entry["ret"] = canon(result)
            return result

        return call


class _ShutilSpy:
    """``notebook_sharing.shutil`` stand-in: records copytree/copy2/rmtree (the copy's
    only filesystem side effects besides one ``mkdir``) and can fail the n-th copy2."""

    def __init__(self, recorder: Recorder, copy2_fault_at: Optional[int]) -> None:
        self._recorder = recorder
        self._copy2_fault_at = copy2_fault_at
        self._copy2_calls = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(real_shutil, name)

    def _note(self, name: str, *args: Any, **kwargs: Any) -> dict:
        canon = self._recorder.canon
        return self._recorder.note({"fs": name, "args": canon(args), "kwargs": canon(kwargs)})

    def copytree(self, *args: Any, **kwargs: Any) -> Any:
        entry = self._note("copytree", *args, **kwargs)
        try:
            return real_shutil.copytree(*args, **kwargs)
        except BaseException as exc:
            entry["raised"] = type(exc).__name__
            raise

    def copy2(self, *args: Any, **kwargs: Any) -> Any:
        entry = self._note("copy2", *args, **kwargs)
        self._copy2_calls += 1
        if self._copy2_calls == self._copy2_fault_at:
            entry["fault"] = "OSError"
            raise OSError("injected copy2 failure")
        return real_shutil.copy2(*args, **kwargs)

    def rmtree(self, *args: Any, **kwargs: Any) -> Any:
        self._note("rmtree", *args, **kwargs)
        return real_shutil.rmtree(*args, **kwargs)


# ----------------------------------------------------------------- scenarios


@dataclasses.dataclass(frozen=True)
class Scenario:
    name: str
    world: Callable[[], list[Row]]
    expect: Callable[["Observation"], None]
    files: dict = dataclasses.field(default_factory=dict)
    copy_of: str = NB
    new_name: Optional[str] = None
    actor_label: Optional[str] = None
    chunk_size: Optional[int] = None
    env: dict = dataclasses.field(default_factory=dict)
    store_faults: dict = dataclasses.field(default_factory=dict)
    copy2_fault_at: Optional[int] = None
    schedule_fault_at: Optional[int] = None
    pre_dirs: tuple = ()


@dataclasses.dataclass
class Backend:
    """What differs between SQLite and PostgreSQL: everything else is shared."""

    name: str
    insert: Callable[[Any, str, dict], None]
    tables: Callable[[Any], list[str]]
    record_sql: Callable[[Any, Recorder, Any], ContextManager]
    invalidate_caches: Callable[[], None]


@dataclasses.dataclass
class Observation:
    copy_id: str
    outcome: dict
    timeline: list
    db_diff: dict
    fs_diff: dict
    logs: list

    def oracle(self) -> dict:
        return {
            "copy_id": self.copy_id, "outcome": self.outcome, "timeline": self.timeline,
            "db_diff": self.db_diff, "fs_diff": self.fs_diff, "logs": self.logs,
        }

    def added(self, table: str) -> list[dict]:
        return self.db_diff.get(table, {}).get("added", [])

    def removed(self, table: str) -> list[dict]:
        return self.db_diff.get(table, {}).get("removed", [])

    def store_calls(self) -> list[str]:
        out = []
        for entry in self.timeline:
            call = entry.get("call", "")
            if call.startswith("store."):
                method = call[len("store."):]
                if method == "insert_copy_rows":
                    method += f":{entry['args'][0]}"
                out.append(method)
        return out


def _seed(repo: Any, backend: Backend, rows: list[Row], storage_dir: Path) -> None:
    with repo._runtime.database.write() as db:
        for row in rows:
            values = {
                k: (v.replace(STORAGE, str(storage_dir)) if isinstance(v, str) else v)
                for k, v in row.values.items()
            }
            backend.insert(db, row.table, values)


def _write_files(storage_dir: Path, files: dict) -> None:
    for relative, content in files.items():
        path = storage_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def _dump_db(repo: Any, backend: Backend, canon: Canon) -> dict[str, collections.Counter]:
    out: dict[str, collections.Counter] = {}
    with repo._runtime.database.connect() as db:
        for table in backend.tables(repo):
            rows = db.execute(f"SELECT * FROM {table}").fetchall()
            out[table] = collections.Counter(_dumps(canon(dict(row))) for row in rows)
    return out


def _dump_fs(storage_dir: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for top in ("notebooks", "assets"):
        root = storage_dir / top
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            key = path.relative_to(storage_dir).as_posix()
            out[key] = (
                "dir" if path.is_dir()
                else "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()[:16]
            )
    return out


def _db_diff(before: dict, after: dict) -> dict:
    diff = {}
    for table in sorted(set(before) | set(after)):
        old, new = before.get(table, collections.Counter()), after.get(table, collections.Counter())
        added = sorted((new - old).elements())
        removed = sorted((old - new).elements())
        if added or removed:
            diff[table] = {
                "added": [json.loads(r) for r in added],
                "removed": [json.loads(r) for r in removed],
            }
    return diff


def _fs_diff(before: dict, after: dict) -> dict:
    return {
        "added": {k: v for k, v in sorted(after.items()) if before.get(k) != v},
        "removed": sorted(k for k in before if k not in after),
    }


class _LogCapture(logging.Handler):
    def __init__(self, recorder: Recorder) -> None:
        super().__init__(logging.DEBUG)
        self.recorder = recorder
        self.records: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        entry = {"level": record.levelname, "message": self.recorder.canon(record.getMessage())}
        if record.exc_info and record.exc_info[0] is not None:
            entry["exc"] = record.exc_info[0].__name__
        self.records.append(entry)
        self.recorder.note({"log": entry["level"]})


def _seam_modules(repo: Any) -> list[Any]:
    """Where the compatibility seams resolve (``RepositoryFacade.__init__``): the concrete
    repository's module first, the facade module's own names as the fallback."""
    import sys

    from app.services import repository_facade

    modules = [repository_facade]
    concrete = sys.modules.get(type(repo).__module__)
    if concrete is not None and concrete is not repository_facade:
        modules.append(concrete)
    return modules


def observe(repo: Any, backend: Backend, scenario: Scenario, storage_dir: Path,
            monkeypatch: Any) -> Observation:
    """Seed the scenario's world, run one copy with every collaborator recorded, and
    return the whole observation (behaviour facts and split oracle alike)."""
    canon = Canon(storage_dir)
    recorder = Recorder(canon)
    _seed(repo, backend, scenario.world(), storage_dir)
    _write_files(storage_dir, scenario.files)
    for relative in scenario.pre_dirs:
        (storage_dir / relative).mkdir(parents=True, exist_ok=True)
        (storage_dir / relative / "keep.txt").write_bytes(b"pre-existing")
    backend.invalidate_caches()
    db_before, fs_before = _dump_db(repo, backend, canon), _dump_fs(storage_dir)

    counter = iter(range(1, 1_000_000))

    def new_id(prefix: str) -> str:
        value = f"{prefix}-c{next(counter):04d}"
        recorder.note({"new_id": prefix, "id": value})
        return value

    def now() -> str:
        recorder.note({"now": COPY_NOW})
        return COPY_NOW

    for module in _seam_modules(repo):
        monkeypatch.setattr(module, "_new_id", new_id, raising=False)
        monkeypatch.setattr(module, "_now", now, raising=False)
        if scenario.chunk_size is not None:
            monkeypatch.setattr(module, "_COPY_CHUNK", scenario.chunk_size, raising=False)

    copies = repo._runtime.notebook_copies
    scheduled = {"n": 0}

    def schedule(table_id: str) -> None:
        recorder.note({"schedule": table_id})
        scheduled["n"] += 1
        if scheduled["n"] == scenario.schedule_fault_at:
            raise RuntimeError("injected schedule failure")

    monkeypatch.setattr(copies, "_store", _Spy("store", copies._store, recorder,
                                               scenario.store_faults))
    monkeypatch.setattr(copies, "_catalog", _Spy("catalog", copies._catalog, recorder, {}))
    monkeypatch.setattr(copies, "_schedule_projection", schedule)
    monkeypatch.setattr(notebook_sharing, "shutil",
                        _ShutilSpy(recorder, scenario.copy2_fault_at))

    logger = logging.getLogger("silicon_notebook.sharing")
    capture = _LogCapture(recorder)
    previous_level = logger.level
    logger.addHandler(capture)
    logger.setLevel(logging.DEBUG)
    try:
        with backend.record_sql(repo, recorder, monkeypatch):
            recorder.active = True
            try:
                summary = copies.copy_notebook(
                    scenario.copy_of, new_owner_id=RECIPIENT,
                    actor_label=scenario.actor_label, new_name=scenario.new_name,
                )
                outcome = {"returned": canon(summary)}
            except Exception as exc:  # noqa: BLE001 — the raise IS the observation
                outcome = {"raised": type(exc).__name__, "message": canon(str(exc))}
            finally:
                recorder.active = False
    finally:
        logger.removeHandler(capture)
        logger.setLevel(previous_level)
    db_after, fs_after = _dump_db(repo, backend, canon), _dump_fs(storage_dir)
    minted = [e["id"] for e in recorder.timeline if e.get("new_id") == "nb"]
    return Observation(
        copy_id=minted[0] if minted else "",
        outcome=outcome,
        timeline=recorder.timeline,
        db_diff=_db_diff(db_before, db_after),
        fs_diff=_fs_diff(fs_before, fs_after),
        logs=capture.records,
    )


# --------------------------------------------------------- the oracle layer


def oracle_text(observation: Observation) -> str:
    return json.dumps(observation.oracle(), indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def assert_matches_oracle(observation: Observation, path: Path, *, regen: bool) -> None:
    text = oracle_text(observation)
    if regen:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return
    assert path.is_file(), f"missing split oracle {path}"
    expected = path.read_text(encoding="utf-8")
    if text != expected:
        import difflib

        delta = "".join(list(difflib.unified_diff(
            expected.splitlines(True), text.splitlines(True), "oracle", "observed", n=2,
        ))[:200])
        raise AssertionError(f"copy_notebook split oracle drifted ({path.name}):\n{delta}")


# ------------------------------------------------------- the behaviour layer


def _ids(rows: list[dict], column: str = "id") -> list:
    return [row[column] for row in rows]


def _json(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"jsonb"}:
        return value["jsonb"]
    if isinstance(value, str) and value[:1] in "[{":
        return json.loads(value)
    return value


def _assert_only_stale_reaped(obs: Observation) -> None:
    """The start-of-copy sweep reaps the recipient's own stale half-copy (rows and both
    directories) and nothing else; no other existing row anywhere is removed."""
    removed = {table: _ids(diff["removed"], "id") if table in ("notebooks", "sources") else
               diff["removed"] for table, diff in obs.db_diff.items() if diff["removed"]}
    assert removed.get("notebooks") == ["nb-stale-recipient"], removed
    assert removed.get("sources") == ["src-stale"], removed
    unexpected = {t: r for t, r in removed.items()
                  if t not in ("notebooks", "sources", "sync_change_log")}
    assert not unexpected, unexpected
    assert set(obs.fs_diff["removed"]) == {
        "notebooks/nb-stale-recipient", "notebooks/nb-stale-recipient/old.md",
        "assets/nb-stale-recipient", "assets/nb-stale-recipient/asset-x.png",
    }, obs.fs_diff["removed"]


def _assert_compensated(obs: Observation, raised: str, *, swept: bool = True) -> None:
    """A failed copy leaves no destination row and no destination directory behind."""
    assert obs.outcome.get("raised") == raised, obs.outcome
    if swept:
        _assert_only_stale_reaped(obs)
    added = {t: d["added"] for t, d in obs.db_diff.items()
             if d["added"] and t != "sync_change_log"}
    assert not added, added
    created = [p for p in obs.fs_diff["added"] if obs.copy_id and obs.copy_id in p]
    assert not created, created


def _assert_published_copy(obs: Observation, *, expected_name: str, name_auto: int,
                           mounts: bool = True) -> None:
    copy_id = obs.copy_id
    assert obs.outcome["returned"]["id"] == copy_id
    assert obs.outcome["returned"]["name"] == expected_name
    assert obs.store_calls() == published_store_sequence(mounts=mounts), obs.store_calls()
    (row,) = obs.added("notebooks")
    assert (row["id"], row["created_by"], row["status"], row["tier"]) == (
        copy_id, RECIPIENT, "draft", "personal")
    assert (row["is_shared"], row["share_token"], row["sync_origin"]) == (0, None, "")
    assert (row["indexing_pipeline"], row["indexing_pipeline_generation"],
            row["indexing_pipeline_job_id"], row["metadata_generation"]) == (None, "", "", 0)
    assert row["name_auto"] == name_auto
    for table in ("notebook_members", "notebook_grants", "memory_items"):
        assert not obs.added(table), table


def expect_full_copy(obs: Observation, *, expected_name: str = "Shared (副本)",
                     name_auto: int = 1) -> None:
    copy_id = obs.copy_id
    _assert_published_copy(obs, expected_name=expected_name, name_auto=name_auto)
    _assert_only_stale_reaped(obs)
    # Every copied content row belongs to the copy.
    for table in ("sources", "chunks", "knowledge_objects", "knowledge_relations",
                  "chunk_questions", "chunk_embeddings", "element_embeddings",
                  "knowledge_embeddings", "relation_embeddings", "concept_clusters",
                  "knowledge_source_facts", "knowhow_tables", "notebook_assets",
                  "notebook_bases", "extraction_runs", "notebook_object_schemas"):
        assert obs.added(table), table
        assert {row["notebook_id"] for row in obs.added(table)} == {copy_id}, table
    sources = {row["title"]: row for row in obs.added("sources")}
    assert set(sources) == {"doc src-doc", "web page", "时序修复", "工具清单"}, set(sources)
    for row in sources.values():
        assert (row["memory_id"], row["agent_profile_id"], row["uploaded_by"]) == ("", None, None)
    assert sources["doc src-doc"]["file_path"] == f"{STORAGE}/notebooks/{copy_id}/doc.md"
    source_ids = {row["id"] for row in sources.values()}
    for table in ("chunks", "knowledge_source_facts", "chunk_questions", "element_embeddings"):
        assert {row["source_id"] for row in obs.added(table)} <= source_ids, table
    # Mounts are re-judged for the recipient.
    assert sorted(_ids(obs.added("notebook_bases"), "base_notebook_id")) == [
        "nb-base-everyone", "nb-base-public", "nb-base-recipient"]
    assert [log["message"] for log in obs.logs if log["level"] == "INFO"] == [
        f"copy_notebook: 副本 {copy_id} 携带 3 条挂载边，丢弃 1 条对新 owner {RECIPIENT} "
        "无效的边（接收者可在副本里重新挂载他读得到的库）"]
    # Knowhow: fresh ids, stable derived element/chunk ids, asset refs rewritten.
    tables = _ids(obs.added("knowhow_tables"))
    assert [e["schedule"] for e in obs.timeline if "schedule" in e] == tables
    assert len(tables) == 2 and not set(tables) & {"kt-1", "kt-2"}
    rows_by_id = {row["id"]: row for row in obs.added("knowhow_rows")}
    assert {row["projection_status"] for row in rows_by_id.values()} == {"pending"}
    kh_sources = {sources["时序修复"]["id"], sources["工具清单"]["id"]}
    kh_chunks = [row for row in obs.added("chunks") if row["source_id"] in kh_sources]
    assert len(kh_chunks) == 4
    for chunk in kh_chunks:
        (eid,) = _json(chunk["element_ids"])
        element = next(e for e in obs.added("source_elements") if e["id"] == eid)
        meta = _json(element["metadata"])["knowhow"]
        assert meta["row_id"] in rows_by_id and meta["table_id"] in tables
        assert eid == element_id(meta["row_id"], meta["column_id"])
        assert chunk["id"] == cell_chunk_id(meta["row_id"], int(chunk["id"].rsplit("-", 1)[-1]))
    assets = {row["filename"]: row for row in obs.added("notebook_assets")}
    assert assets["fig1.png"]["source_id"] == sources["web page"]["id"]
    assert assets["paste.png"]["source_id"] is None
    assert assets["gone.gif"]["source_id"] == "src-gone"
    figure = next(e for e in obs.added("source_elements") if e["text"] == "web figure")
    assert _json(figure["metadata"])["asset_id"] == assets["fig1.png"]["id"]
    cell = next(c for c in obs.added("knowhow_cells") if "波形" in c["content_md"])
    assert cell["content_md"] == (
        f"见图 ![波形](asset://{assets['paste.png']['id']}) 与 asset://asset-not-here")
    assert len(obs.added("knowhow_changes")) == 2
    copied_files = sorted(p for p, kind in obs.fs_diff["added"].items() if kind != "dir")
    assert copied_files == sorted(
        [f"notebooks/{copy_id}/doc.md", f"notebooks/{copy_id}/extra/notes.txt"]
        + [f"assets/{copy_id}/{assets[name]['id']}.png"
           for name in ("paste.png", "fig1.png", "fig2.png")]), copied_files
    # One copy-local completed generation per source with facts; the newest wins.
    runs = {row["source_id"]: row for row in obs.added("extraction_runs")}
    assert set(runs) == {sources["doc src-doc"]["id"], sources["web page"]["id"]}
    assert runs[sources["doc src-doc"]["id"]]["error_message"] == (
        "kg objects=7 relations=0 copied_generation=1")
    facts = {_json(f["payload"])["name"]: f for f in obs.added("knowledge_source_facts")}
    assert facts["web fact b"]["source_generation"] == runs[sources["web page"]["id"]]["id"]
    assert facts["web fact a"]["source_generation"] != facts["web fact b"]["source_generation"]
    # Ids inside stored JSON follow the copy: an object named in a payload that was
    # copied earlier, sources and elements; an id the copy does not hold stays as-is.
    objects = {_json(o["payload"])["name"]: o for o in obs.added("knowledge_objects")}
    web = _json(objects["web thing"]["payload"])
    assert (web["object_id"], web["source_id"]) == (
        objects["shared ko-shared-1"]["id"], sources["doc src-doc"]["id"])
    assert web["element_ids"][1] == "el-missing" and web["element_ids"][0] != "el-web-1"
    for name in ("web fact a", "web fact b"):
        assert _json(facts[name]["payload"])["object_id"] == objects["web thing"]["id"]
        assert facts[name]["global_object_id"] == objects["web thing"]["id"]
    # Memory stays behind and the copy starts dirty (its clustering is rebuilt).
    (state,) = obs.added("unified_kg_state")
    assert (state["notebook_id"], state["dirty"]) == (copy_id, 1)


def expect_plain_copy(obs: Observation) -> None:
    _assert_published_copy(obs, expected_name="手动命名", name_auto=0, mounts=False)
    assert not obs.added("unified_kg_state")  # no Memory, clean source: no state row
    assert [e["kwargs"]["actor"] for e in obs.timeline
            if e.get("call") == "store.seed_copied_knowhow_genesis"] == ["分享链接"]
    assert not obs.fs_diff["added"] and not obs.fs_diff["removed"]
    assert not any(e.get("fs") for e in obs.timeline)


def expect_small_chunks(obs: Observation) -> None:
    expect_full_copy(obs, expected_name="手动命名", name_auto=0)
    assert {c["kwargs"]["chunk_size"] for c in obs.timeline
            if c.get("call") == "store.insert_copy_rows"} == {2}
    (genesis,) = [e for e in obs.timeline
                  if e.get("call") == "store.seed_copied_knowhow_genesis"]
    assert genesis["kwargs"]["actor"] == "agent:importer"


def expect_compensated(raised: str) -> Callable[[Observation], None]:
    def expect(obs: Observation) -> None:
        _assert_compensated(obs, raised)
        assert [e["call"] for e in obs.timeline if e.get("call") == "store.compensate_copy"]
    return expect


def expect_asset_file_failure(obs: Observation) -> None:
    _assert_compensated(obs, "OSError")
    assert not any(p.startswith(f"assets/{obs.copy_id}") for p in obs.fs_diff["added"])
    rmtree = [e["args"][0] for e in obs.timeline if e.get("fs") == "rmtree"]
    assert rmtree[-2:] == [f"{STORAGE}/notebooks/{obs.copy_id}",
                           f"{STORAGE}/assets/{obs.copy_id}"], rmtree


def expect_destination_exists(obs: Observation) -> None:
    """copytree refused (the destination already exists): nothing was written by this
    copy, so compensation must not delete that directory."""
    _assert_compensated(obs, "FileExistsError")
    assert not [e for e in obs.timeline if e.get("fs") == "rmtree"
                and obs.copy_id in e["args"][0]]
    assert f"notebooks/{obs.copy_id}/keep.txt" not in obs.fs_diff["removed"]


def expect_schedule_failure(obs: Observation) -> None:
    expect_full_copy(obs)
    warnings = [log for log in obs.logs if log["level"] == "WARNING"]
    assert len(warnings) == 1 and warnings[0]["exc"] == "RuntimeError", warnings


def expect_missing(obs: Observation) -> None:
    _assert_compensated(obs, "KeyError")
    assert not any(e.get("fs") == "copytree" for e in obs.timeline)


SCENARIOS = (
    Scenario("full_world", full_world, expect_full_copy, files=FULL_FILES),
    Scenario("full_world_small_chunks", full_world, expect_small_chunks, files=FULL_FILES,
             chunk_size=2, new_name="手动命名", actor_label="agent:importer"),
    Scenario("plain_notebook", plain_world, expect_plain_copy, copy_of=NB_PLAIN,
             new_name="手动命名", actor_label="分享链接"),
    Scenario("fails_inserting_knowledge_objects", full_world,
             expect_compensated("RuntimeError"), files=FULL_FILES,
             store_faults={("insert_copy_rows", "knowledge_objects"):
                           RuntimeError("injected insert failure")}),
    Scenario("fails_copying_second_asset_file", full_world, expect_asset_file_failure,
             files=FULL_FILES, copy2_fault_at=2),
    Scenario("fails_validation", full_world, expect_compensated("RuntimeError"),
             files=FULL_FILES,
             store_faults={("validate_copy", None): RuntimeError("injected validation")}),
    Scenario("fails_publishing", full_world, expect_compensated("RuntimeError"),
             files=FULL_FILES,
             store_faults={("publish_copy", None): RuntimeError("injected publish")}),
    Scenario("knowhow_chunk_without_element_ids",
             lambda: full_world(knowhow_chunk_without_elements=True),
             expect_compensated("ValueError"), files=FULL_FILES),
    Scenario("destination_directory_exists", full_world, expect_destination_exists,
             files=FULL_FILES, pre_dirs=("notebooks/nb-c0001",)),
    Scenario("projection_scheduling_fails", full_world, expect_schedule_failure,
             files=FULL_FILES, schedule_fault_at=1),
    Scenario("missing_notebook", full_world, expect_missing, files=FULL_FILES,
             copy_of=NB_MISSING),
    Scenario("too_large_to_copy", full_world, expect_compensated("NotebookTooLargeToCopyError"),
             files=FULL_FILES, env={"NOTEBOOK_COPY_MAX_ROWS": "1"}),
)
