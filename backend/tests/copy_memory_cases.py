"""笔记本拷贝「不带 Memory」(M2)的场景与断言 —— **两端吃同一份世界**。

沿用 ``memory_sql_cases.py`` 的理由:SQLite 与 PostgreSQL 各写一套夹具,就等于让每一端
迎合自己那份实现。这里把「世界里有什么、拷贝后该剩什么」写成与后端无关的行表,
``tests/test_notebook_share_copy.py``(SQLite,走真路由)与
``tests/postgres/test_copy_memory_exclusion_pg.py``(PostgreSQL,走真路由)各自 import,
各自只写一个把行插进库、把行读出来的薄适配器。

世界(一个共享笔记本,owner 为 seeded admin,这样 ``/share`` 与 ``/shared/{token}/copy`` 走真路由):

* 两位成员 alice、bob 各有一条已确认 Memory,各自派生出一个 Memory 来源(带元素、元素向量、
  KG 对象、对象向量、关系、关系向量、来源级事实三表)。另有一条**孤儿** Memory 来源
  (``memory_id`` 为空)。
* 共享内容:一个普通文档来源,带元素、元素向量、一个分块(+向量+问题)、三个 KG 对象(+向量)、
  一条关系(+向量)、事实三表、一个纯净概念簇。
* **混合端点关系**:一端是 Memory 对象、另一端是共享对象,关系自己的 ``source_id`` 是共享
  来源(或 NULL)——它不是 Memory 关系,却不能进拷贝(拷贝里没有那个 Memory 对象可指)。
* **存量混合簇**:成员里既有共享对象又有 Memory 对象的簇,簇名取自 Memory;以及
  ``canonical_id`` 本身就是某个 Memory 对象 id 的簇。
* **一条不该存在的 Memory 分块**(Memory 来源从不建分块,不变量如此;这里造出来钉住防御:没有
  这层排除,拷贝会在来源映射上 KeyError)。

每行带一个 ``memory`` 旗标:``True`` = 拷贝里绝不许出现。期望的副本内容就是旗标为 ``False``
的行。所有 Memory 行的文本都带 ``MARK``,副本里任何一张表的任何一列出现它都是泄漏。

不是测试模块(没有 ``test_`` 前缀),pytest 不会收集它。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

NOW = "2026-09-29T00:00:00+00:00"
NOTEBOOK = "nb-copy-mem"
NOTEBOOK_PLAIN = "nb-copy-plain"
OWNER = "user-local"
MARK = "MEMSECRET"

USERS = ("u-alice", "u-bob")

#: 需要 JSON 编码的列(SQLite 存文本,PostgreSQL 存 jsonb)。
JSON_COLUMNS = frozenset({"payload", "evidence", "element_ids", "metadata"})

#: 副本里逐表核对的表(键是表名,值是「按笔记本取全部行」的 SQL,``{p}`` 是占位符)。
COPY_TABLE_QUERIES: dict[str, str] = {
    "sources": "SELECT * FROM sources WHERE notebook_id = {p}",
    "source_paper_meta": "SELECT * FROM source_paper_meta WHERE notebook_id = {p}",
    "source_authors": "SELECT * FROM source_authors WHERE notebook_id = {p}",
    "source_elements": (
        "SELECT e.* FROM source_elements e JOIN sources s ON s.id = e.source_id "
        "WHERE s.notebook_id = {p}"
    ),
    "chunks": "SELECT * FROM chunks WHERE notebook_id = {p}",
    "chunk_embeddings": "SELECT * FROM chunk_embeddings WHERE notebook_id = {p}",
    "chunk_questions": "SELECT * FROM chunk_questions WHERE notebook_id = {p}",
    "element_embeddings": "SELECT * FROM element_embeddings WHERE notebook_id = {p}",
    "knowledge_objects": "SELECT * FROM knowledge_objects WHERE notebook_id = {p}",
    "knowledge_embeddings": "SELECT * FROM knowledge_embeddings WHERE notebook_id = {p}",
    "knowledge_relations": "SELECT * FROM knowledge_relations WHERE notebook_id = {p}",
    "relation_embeddings": "SELECT * FROM relation_embeddings WHERE notebook_id = {p}",
    "knowledge_source_facts": "SELECT * FROM knowledge_source_facts WHERE notebook_id = {p}",
    "knowledge_source_fact_elements": (
        "SELECT * FROM knowledge_source_fact_elements WHERE notebook_id = {p}"
    ),
    "knowledge_source_fact_backfills": (
        "SELECT * FROM knowledge_source_fact_backfills WHERE notebook_id = {p}"
    ),
    "concept_clusters": "SELECT * FROM concept_clusters WHERE notebook_id = {p}",
}


@dataclass(frozen=True)
class Row:
    table: str
    values: dict[str, Any]
    memory: bool = False


def _vec(tag: str) -> bytes:
    return f"vec-{tag}".encode()


def _source(nb: str, sid: str, source_type: str, *, memory_id=None, memory=False) -> Row:
    title = f"{MARK} {sid}" if memory else f"doc {sid}"
    return Row(
        "sources",
        {
            "id": sid, "notebook_id": nb, "title": title, "source_type": source_type,
            "memory_id": memory_id, "status": "ready", "parse_status": "ready",
            "created_at": NOW, "updated_at": NOW,
        },
        memory,
    )


def _paper_meta(nb: str, sid: str, *, memory=False) -> list[Row]:
    """Paper metadata + one author. A Memory source has none in practice; the stray rows
    pin the defensive exclusion on the per-source join."""
    label = f"{MARK} {sid}" if memory else f"paper {sid}"
    return [
        Row("source_paper_meta", {
            "source_id": sid, "notebook_id": nb, "is_paper": 1, "paper_title": label,
            "model": "m", "created_at": NOW, "updated_at": NOW,
        }, memory),
        Row("source_authors", {
            "id": f"auth-{sid}", "source_id": sid, "notebook_id": nb, "position": 0,
            "name": label, "created_at": NOW,
        }, memory),
    ]


def _element(sid: str, eid: str, *, memory=False) -> list[Row]:
    text = f"{MARK} element {eid}" if memory else f"shared element {eid}"
    return [
        Row("source_elements", {
            "id": eid, "source_id": sid, "element_type": "para", "location_label": "p1",
            "text": text, "created_at": NOW,
        }, memory),
    ]


def _element_vec(nb: str, sid: str, eid: str, *, memory=False) -> Row:
    return Row("element_embeddings", {
        "element_id": eid, "source_id": sid, "notebook_id": nb,
        "vector": _vec(eid), "created_at": NOW,
    }, memory)


def evidence(sid: str, eid: str, *, memory: bool) -> dict:
    """A full Evidence entry (``app.models.common.Evidence``): the quoted text, the source
    title and the locator travel with it — a Memory's entry carries the Memory's text."""
    label = MARK if memory else "shared"
    return {
        "source_id": sid, "source_title": f"{label} title {sid}", "element_id": eid,
        "element_type": "paragraph", "location_label": f"{label} p1",
        "quoted_span": f"{label} quoted {eid}", "confidence": 0.9,
    }


def _object(
    nb: str, oid: str, sid: str, eid: str, *, memory=False, lent: tuple = (), own=True,
) -> list[Row]:
    """``lent``: Memory evidence entries a manual merge appended to this (shared) object.
    ``own=False``: the object's own evidence is empty (a manually created object), so
    after the strip its evidence is ``[]``."""
    name = f"{MARK} {oid}" if memory else f"shared {oid}"
    own_entries = [evidence(sid, eid, memory=memory)] if own else []
    return [
        Row("knowledge_objects", {
            "id": oid, "notebook_id": nb, "object_type": "concept", "status": "approved",
            "source_id": sid, "payload": {"name": name},
            "evidence": own_entries + [evidence(s, e, memory=True) for s, e in lent],
            "created_at": NOW, "updated_at": NOW,
        }, memory),
        Row("knowledge_embeddings", {
            "object_id": oid, "notebook_id": nb, "vector": _vec(oid), "created_at": NOW,
        }, memory),
    ]


def _relation(
    nb: str, rid: str, sid, src: str, dst: str, *, memory: bool, ev: list | None = None,
) -> list[Row]:
    return [
        Row("knowledge_relations", {
            "id": rid, "notebook_id": nb, "source_id": sid, "source_object_id": src,
            "target_object_id": dst, "edge_type": "related_to", "evidence": ev or [],
            "created_at": NOW,
        }, memory),
        Row("relation_embeddings", {
            "relation_id": rid, "notebook_id": nb, "vector": _vec(rid), "created_at": NOW,
        }, memory),
    ]


def _facts(
    nb: str, sid: str, tag: str, obj: str, eid: str, *, memory: bool, ev: list | None = None,
) -> list[Row]:
    generation = f"gen-{tag}"
    fact_id = f"fact-{tag}"
    return [
        Row("knowledge_source_facts", {
            "id": fact_id, "notebook_id": nb, "source_id": sid, "source_generation": generation,
            "local_object_id": f"local-{tag}", "global_object_id": obj, "object_type": "concept",
            "payload": {"name": f"{MARK} {tag}" if memory else f"shared {tag}"},
            "evidence": ev or [], "created_at": NOW, "updated_at": NOW,
        }, memory),
        Row("knowledge_source_fact_elements", {
            "fact_id": fact_id, "notebook_id": nb, "source_id": sid,
            "source_generation": generation, "element_id": eid, "created_at": NOW,
        }, memory),
        Row("knowledge_source_fact_backfills", {
            "source_id": sid, "notebook_id": nb, "source_generation": generation,
            "status": "complete", "created_at": NOW, "updated_at": NOW,
        }, memory),
    ]


def _cluster(nb: str, cid: str, name: str, members: Iterable[str], *, memory: bool) -> list[Row]:
    return [
        Row("concept_clusters", {
            "id": f"cc-{cid}-{member}", "notebook_id": nb, "canonical_id": cid,
            "member_object_id": member, "canonical_name": name, "object_type": "concept",
            "created_at": NOW, "generation": 0,
        }, memory)
        for member in members
    ]


def world(nb: str = NOTEBOOK, *, with_memory: bool = True) -> list[Row]:
    """世界的全部行,按外键安全的顺序;``with_memory=False`` 只留共享内容。

    共享行里夹带的 Memory 证据(手工合并留下的,带 Memory 原文、标题与定位)只在
    ``with_memory`` 时出现;``LENT_EVIDENCE`` 登记它们,副本里必须一条不剩。"""
    lent = LENT_EVIDENCE if with_memory else {}
    rows: list[Row] = [
        Row("notebooks", {
            "id": nb, "name": "Shared", "purpose": "", "primary_domain": "Semiconductor",
            "status": "draft", "created_by": OWNER, "created_at": NOW, "updated_at": NOW,
        }),
        # -- shared content -------------------------------------------------
        _source(nb, "src-doc", "document"),
        *_paper_meta(nb, "src-doc"),
        *_element("src-doc", "el-doc-1"),
        *_element("src-doc", "el-doc-2"),
        _element_vec(nb, "src-doc", "el-doc-1"),
        _element_vec(nb, "src-doc", "el-doc-2"),
        Row("chunks", {
            "id": "ck-doc-1", "notebook_id": nb, "source_id": "src-doc", "text": "shared chunk",
            "element_ids": ["el-doc-1"], "created_at": NOW,
        }),
        Row("chunk_embeddings", {
            "chunk_id": "ck-doc-1", "notebook_id": nb, "vector": _vec("ck-doc-1"),
            "created_at": NOW,
        }),
        Row("chunk_questions", {
            "id": "cq-doc-1", "chunk_id": "ck-doc-1", "notebook_id": nb, "source_id": "src-doc",
            "question": "what is shared?", "vector": _vec("cq-doc-1"), "created_at": NOW,
        }),
        *_object(nb, "ko-shared-1", "src-doc", "el-doc-1"),
        *_object(nb, "ko-shared-2", "src-doc", "el-doc-1"),
        # 手工合并过 alice 的 Memory 对象:自己的证据 + alice 的一条(带原文)。
        *_object(nb, "ko-shared-3", "src-doc", "el-doc-2", lent=lent.get("ko-shared-3", ())),
        *_object(nb, "ko-shared-4", "src-doc", "el-doc-2"),
        # 自己没有证据(手工建的对象),合并进了 bob 的 Memory 对象:剥离后证据为 []。
        *_object(nb, "ko-shared-5", "src-doc", "el-doc-2", own=False,
                 lent=lent.get("ko-shared-5", ())),
        *_object(nb, "ko-shared-6", "src-doc", "el-doc-2"),
        *_relation(nb, "kr-shared", "src-doc", "ko-shared-1", "ko-shared-2", memory=False,
                   ev=[evidence("src-doc", "el-doc-1", memory=False)]
                   + [evidence(s, e, memory=True) for s, e in lent.get("kr-shared", ())]),
        *_facts(nb, "src-doc", "doc", "ko-shared-1", "el-doc-1", memory=False,
                ev=[evidence("src-doc", "el-doc-1", memory=False)]
                + [evidence(s, e, memory=True) for s, e in lent.get("fact-doc", ())]),
        *_cluster(nb, "K-shared", "shared topic", ("ko-shared-1", "ko-shared-2"), memory=False),
    ]
    if with_memory:
        rows += [
            Row("users", {"id": u, "email": f"{u}@example.test", "display_name": u,
                          "role": "user", "created_at": NOW, "updated_at": NOW})
            for u in USERS
        ]
        for who, uid in (("alice", "u-alice"), ("bob", "u-bob")):
            sid, mem = f"src-mem-{who}", f"mem-{who}"
            rows += [
                Row("memory_items", {
                    "id": mem, "notebook_id": nb, "created_by": uid, "origin": "ask_answer",
                    "status": "confirmed", "title": f"{MARK} {who}",
                    "content_md": f"{MARK} body", "created_at": NOW, "updated_at": NOW,
                }, True),
                _source(nb, sid, "memory", memory_id=mem, memory=True),
                *_paper_meta(nb, sid, memory=True),
                *_element(sid, f"el-{who}-1", memory=True),
                _element_vec(nb, sid, f"el-{who}-1", memory=True),
                *_object(nb, f"ko-{who}-1", sid, f"el-{who}-1", memory=True),
                *_object(nb, f"ko-{who}-2", sid, f"el-{who}-1", memory=True),
                *_relation(nb, f"kr-{who}", sid, f"ko-{who}-1", f"ko-{who}-2", memory=True),
                *_facts(nb, sid, who, f"ko-{who}-1", f"el-{who}-1", memory=True),
            ]
        rows += [
            # 孤儿 Memory 来源:memory_id 为空,仍是 Memory 派生。
            _source(nb, "src-mem-orphan", "memory", memory=True),
            *_element("src-mem-orphan", "el-orphan-1", memory=True),
            _element_vec(nb, "src-mem-orphan", "el-orphan-1", memory=True),
            # 混合端点关系:自己的来源是共享来源 / NULL,一端却是 Memory 对象。
            *_relation(nb, "kr-mixed-src", "src-doc", "ko-alice-1", "ko-shared-1", memory=True),
            *_relation(nb, "kr-mixed-null", None, "ko-shared-2", "ko-bob-1", memory=True),
            # 存量混合簇:成员里有 Memory 对象,簇名取自 Memory;整簇不进拷贝。
            *_cluster(nb, "K-mixed", f"{MARK} topic", ("ko-shared-3", "ko-alice-1"), memory=True),
            # 纯 Memory 簇。
            *_cluster(nb, "K-bob", f"{MARK} bob topic", ("ko-bob-1", "ko-bob-2"), memory=True),
            # 真名种子的簇,alice 的 Memory 对象还在簇里(成员臂),共享成员一起整簇不带。
            *_cluster(nb, "K-alice-topic", f"{MARK} topic of alice",
                      ("ko-shared-4", "ko-alice-2"), memory=True),
            # canonical_id 铸自 Memory 对象 id(``K-~<对象 id>``,名字退化的种子),成员是
            # 共享对象:簇名/描述可能取自那个 Memory 对象。
            *_cluster(nb, "K-~ko-bob-2", f"{MARK} minted", ("ko-shared-5",), memory=True),
            # canonical_id 铸自一个已经不存在的对象(种它的 Memory 已被删除):无从证明
            # 不是 Memory 的,拷贝不带。
            *_cluster(nb, "KL-~ko-deleted-1", f"{MARK} stale", ("ko-shared-6",), memory=True),
            # 一条不该存在的 Memory 分块(+向量+问题)。
            Row("chunks", {
                "id": "ck-mem-stray", "notebook_id": nb, "source_id": "src-mem-alice",
                "text": f"{MARK} stray chunk", "element_ids": ["el-alice-1"], "created_at": NOW,
            }, True),
            Row("chunk_embeddings", {
                "chunk_id": "ck-mem-stray", "notebook_id": nb, "vector": _vec("ck-mem-stray"),
                "created_at": NOW,
            }, True),
            Row("chunk_questions", {
                "id": "cq-mem-stray", "chunk_id": "ck-mem-stray", "notebook_id": nb,
                "source_id": "src-mem-alice", "question": f"{MARK}?",
                "vector": _vec("cq-mem-stray"), "created_at": NOW,
            }, True),
        ]
    return rows


#: 共享行里夹带的 Memory 证据:行 -> ``(Memory 来源, Memory 元素)``。对象的是手工合并留下的
#: (``merge_objects_in_transaction`` 整条追加);关系与事实按构造不会有,造出来钉住剥离的防御面。
LENT_EVIDENCE: dict[str, tuple] = {
    "ko-shared-3": (("src-mem-alice", "el-alice-1"),),
    "ko-shared-5": (("src-mem-bob", "el-bob-1"), ("src-mem-orphan", "el-orphan-1")),
    "kr-shared": (("src-mem-alice", "el-alice-1"),),
    "fact-doc": (("src-mem-bob", "el-bob-1"),),
}


def seed(insert: Callable[[str, dict], None], nb: str = NOTEBOOK, *, with_memory: bool = True) -> None:
    for row in world(nb, with_memory=with_memory):
        insert(row.table, row.values)


def expected_copy_counts(nb: str = NOTEBOOK) -> dict[str, int]:
    """副本里每张表应有多少行 = 世界里该表旗标为 ``False`` 的行数。"""
    counts = {table: 0 for table in COPY_TABLE_QUERIES}
    for row in world(nb):
        if row.table in counts and not row.memory:
            counts[row.table] += 1
    return counts


def _key(values: dict) -> Any:
    return values.get("id") or values.get("object_id") or values.get("relation_id") \
        or values.get("element_id") or values.get("chunk_id") or values.get("fact_id") \
        or values.get("source_id")


def memory_ids() -> dict[str, set[str]]:
    """世界里每张表的 Memory 行 id(取各表的主键列),用来断言副本没有引用它们。"""
    out: dict[str, set[str]] = {}
    for row in world():
        if row.memory:
            out.setdefault(row.table, set()).add(_key(row.values))
    return out


#: 世界里的 Memory 来源(任一成员的,含孤儿)。
MEMORY_SOURCES = frozenset(
    row.values["id"] for row in world() if row.table == "sources" and row.memory
)
#: 拷贝保留的知识对象,及每个对象自己(非夹带)的证据条数。
SHARED_OBJECT_OWN_EVIDENCE = {
    "ko-shared-1": 1, "ko-shared-2": 1, "ko-shared-3": 1, "ko-shared-4": 1,
    "ko-shared-5": 0, "ko-shared-6": 1,
}
SHARED_OBJECTS = frozenset(SHARED_OBJECT_OWN_EVIDENCE)


def _evidence_list(value: Any) -> list:
    value = _decoded(value)
    return value if isinstance(value, list) else []


def assert_kept_rows_lost_only_the_lent_evidence(view: "CopyView") -> None:
    """副本里保留下来的对象 / 关系 / 事实:夹带的 Memory 证据条目一条不剩,自己的证据原样在
    (条数与文本);自己没有证据的对象以空数组留下,不被丢弃。"""
    copy_sources = {row["id"] for row in view.rows["sources"]}
    by_name = {
        _decoded(row["payload"])["name"]: row for row in view.rows["knowledge_objects"]
    }
    assert set(by_name) == {f"shared {oid}" for oid in SHARED_OBJECTS}, set(by_name)
    for oid, own in SHARED_OBJECT_OWN_EVIDENCE.items():
        entries = _evidence_list(by_name[f"shared {oid}"]["evidence"])
        assert len(entries) == own, (oid, entries)
        for entry in entries:
            assert entry["source_id"] in copy_sources, (oid, entry)
            assert entry["quoted_span"].startswith("shared quoted"), (oid, entry)
    for table in ("knowledge_relations", "knowledge_source_facts"):
        for row in view.rows[table]:
            entries = _evidence_list(row["evidence"])
            assert len(entries) == 1 and entries[0]["source_id"] in copy_sources, (table, row)


def assert_snapshot_is_legacy_minus_memory(now: dict, legacy: dict) -> None:
    """现行快照 = M2 之前的原文快照去掉 Memory 行、再去掉共享行里夹带的 Memory 证据条目;
    其余每一列逐字相同(没有被剥的行,证据 JSON 文本也逐字不变)。"""
    memory = memory_ids()
    stripped = 0
    expected: dict[str, list] = {}
    for table, rows in legacy.items():
        kept = []
        for row in rows:
            if _key(row) in memory.get(table, ()):
                continue
            row = dict(row)
            entries = _evidence_list(row.get("evidence")) if "evidence" in row else []
            clean = [e for e in entries if e.get("source_id") not in MEMORY_SOURCES]
            if len(clean) != len(entries):
                stripped += len(entries) - len(clean)
                row["evidence"] = clean
            kept.append(row)
        expected[table] = kept
    assert stripped == sum(len(v) for v in LENT_EVIDENCE.values()), stripped

    def canon(row):
        return json.dumps(
            {k: (_decoded(v) if k == "evidence" else v) for k, v in row.items()},
            default=str, sort_keys=True,
        )

    assert set(now) == set(expected)
    for table in now:
        if table == "notebooks":
            continue  # the root row carries the dirty tag
        assert sorted(map(canon, now[table])) == sorted(map(canon, expected[table])), table
    # 没被剥的行:证据 JSON 文本逐字不变(不是重新序列化出来的)。
    legacy_text = {
        r["id"]: r["evidence"] for r in legacy["knowledge_objects"]
    }
    for row in now["knowledge_objects"]:
        if row["id"] not in LENT_EVIDENCE:
            assert row["evidence"] == legacy_text[row["id"]], row["id"]


def assert_share_sizes_exclude_memory(share: dict, preview: dict) -> None:
    """分享响应、公开预览与主人的分享弹窗读同一份 size:来源数 / 节点数 / 边数都不含任何成员
    的 Memory 及其派生行;``size.sources`` 与 ``source_count`` 相等,链接持有人无法从两者之差
    算出 Memory 条数。"""
    shared_sources = sum(
        1 for row in world() if row.table == "sources" and not row.memory
    )
    shared_nodes = len(SHARED_OBJECTS)
    shared_edges = sum(
        1 for row in world() if row.table == "knowledge_relations" and not row.memory
    )
    for size in (share["size"], preview["size"]):
        assert (size["sources"], size["nodes"], size["edges"]) == (
            shared_sources, shared_nodes, shared_edges,
        ), size
    assert preview["source_count"] == preview["size"]["sources"] == shared_sources
    assert (preview["node_count"], preview["edge_count"]) == (shared_nodes, shared_edges)


@dataclass
class CopyView:
    """副本(或源库)在每张表里的行。``fetch(sql, params) -> list[dict]`` 由适配器提供。"""

    counts: dict[str, int] = field(default_factory=dict)
    dump: str = ""
    rows: dict[str, list[dict]] = field(default_factory=dict)
    leaves: list[str] = field(default_factory=list)


def _decoded(value: Any) -> Any:
    """A JSON text column (SQLite stores JSON as text; PostgreSQL returns jsonb already
    parsed) decoded to its structure, so the scan below sees the same leaves on both
    backends — scanning the dumped text would miss an id inside SQLite's escaped quotes."""
    if isinstance(value, str) and value[:1] in "[{":
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _leaves(value: Any) -> Iterable[str]:
    """Every string in a (decoded) value, recursively. Vector bytes are not text and are
    guarded by the row counts."""
    value = _decoded(value)
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _leaves(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _leaves(item)


def read_copy(fetch: Callable[[str, tuple], list[dict]], placeholder: str, nb_id: str) -> CopyView:
    view = CopyView()
    parts: list[str] = []
    leaves: list[str] = []
    for table, query in COPY_TABLE_QUERIES.items():
        rows = fetch(query.format(p=placeholder), (nb_id,))
        view.counts[table] = len(rows)
        decoded = [{k: _decoded(v) for k, v in row.items()} for row in rows]
        view.rows[table] = decoded
        parts.append(json.dumps(decoded, default=str, sort_keys=True, ensure_ascii=False))
        for row in decoded:
            leaves.extend(_leaves(list(row.values())))
    view.dump = "\n".join(parts)
    view.leaves = leaves
    return view


def assert_copy_has_no_memory(view: CopyView) -> None:
    """副本的行数等于共享内容,且任何表的任何列(JSON 列先解析、逐个字符串叶子比对)都没有
    Memory 的文本或 id。"""
    assert view.counts == expected_copy_counts(), view.counts
    leaked = [leaf for leaf in view.leaves if MARK in leaf]
    assert not leaked, f"a Memory text leaked into the copy: {leaked}"
    for table, ids in memory_ids().items():
        if table == "memory_items":
            continue
        for value in ids:
            hits = [leaf for leaf in view.leaves if value in leaf]
            assert not hits, f"Memory row {table}:{value} referenced by the copy: {hits}"


# --------------------------------------------------------------------------
# 拷贝快照在 M2 之前的原文(master @ e02c8fa8),只列被本次改动触及的表。
# 用来证明「没有 Memory 的笔记本,快照与之前逐行逐序一致」:同一份数据上跑原文与现行
# `_COPY_SNAPSHOT_QUERIES`,结果必须相同。占位符按各后端原样保留。
# --------------------------------------------------------------------------
_KH_PG = "SELECT id FROM sources WHERE source_type='knowhow'"
_KH_SQLITE = "SELECT id FROM sources WHERE source_type = 'knowhow'"

LEGACY_SNAPSHOT_PG: dict[str, str] = {
    "sources": "SELECT * FROM sources WHERE notebook_id=%s",
    "source_paper_meta": (
        "SELECT m.* FROM source_paper_meta m JOIN sources s ON s.id=m.source_id "
        "WHERE s.notebook_id=%s AND s.source_type<>'knowhow'"
    ),
    "source_authors": (
        "SELECT a.* FROM source_authors a JOIN sources s ON s.id=a.source_id "
        "WHERE s.notebook_id=%s AND s.source_type<>'knowhow'"
    ),
    "source_elements": (
        "SELECT e.* FROM source_elements e JOIN sources s ON s.id=e.source_id "
        "WHERE s.notebook_id=%s ORDER BY e.ordinal"
    ),
    "chunks": "SELECT * FROM chunks WHERE notebook_id=%s ORDER BY ordinal",
    "knowledge_objects": (
        f"SELECT * FROM knowledge_objects WHERE notebook_id=%s AND source_id NOT IN ({_KH_PG}) "
        "ORDER BY ordinal"
    ),
    "knowledge_source_facts": (
        f"SELECT * FROM knowledge_source_facts WHERE notebook_id=%s AND source_id NOT IN ({_KH_PG})"
    ),
    "knowledge_source_fact_elements": (
        "SELECT * FROM knowledge_source_fact_elements WHERE notebook_id=%s "
        f"AND source_id NOT IN ({_KH_PG})"
    ),
    "knowledge_source_fact_backfills": (
        "SELECT * FROM knowledge_source_fact_backfills WHERE notebook_id=%s "
        f"AND status IN ('complete','incomplete') AND source_id NOT IN ({_KH_PG})"
    ),
    "knowledge_relations": (
        "SELECT * FROM knowledge_relations WHERE notebook_id=%s "
        f"AND (source_id IS NULL OR source_id NOT IN ({_KH_PG}))"
    ),
    "chunk_embeddings": "SELECT * FROM chunk_embeddings WHERE notebook_id=%s",
    "chunk_questions": "SELECT * FROM chunk_questions WHERE notebook_id=%s",
    "element_embeddings": (
        "SELECT e.* FROM element_embeddings e JOIN sources s ON s.id=e.source_id "
        "WHERE s.notebook_id=%s AND s.source_type<>'knowhow'"
    ),
    "knowledge_embeddings": "SELECT * FROM knowledge_embeddings WHERE notebook_id=%s",
    "relation_embeddings": "SELECT * FROM relation_embeddings WHERE notebook_id=%s",
    "concept_clusters": (
        "SELECT * FROM concept_clusters WHERE notebook_id=%s AND generation = COALESCE("
        "(SELECT cluster_generation FROM unified_kg_state u "
        "WHERE u.notebook_id = concept_clusters.notebook_id), 0)"
    ),
}

LEGACY_SNAPSHOT_SQLITE: dict[str, str] = {
    "sources": "SELECT * FROM sources WHERE notebook_id = ?",
    "source_paper_meta": (
        "SELECT spm.* FROM source_paper_meta spm JOIN sources s ON s.id = spm.source_id "
        "WHERE s.notebook_id = ? AND s.source_type != 'knowhow'"
    ),
    "source_authors": (
        "SELECT sa.* FROM source_authors sa JOIN sources s ON s.id = sa.source_id "
        "WHERE s.notebook_id = ? AND s.source_type != 'knowhow'"
    ),
    "source_elements": (
        "SELECT se.* FROM source_elements se JOIN sources s ON s.id = se.source_id "
        "WHERE s.notebook_id = ?"
    ),
    "chunks": "SELECT * FROM chunks WHERE notebook_id = ?",
    "knowledge_objects": (
        f"SELECT * FROM knowledge_objects WHERE notebook_id = ? AND source_id NOT IN ({_KH_SQLITE})"
    ),
    "knowledge_source_facts": (
        "SELECT * FROM knowledge_source_facts WHERE notebook_id = ? "
        f"AND source_id NOT IN ({_KH_SQLITE})"
    ),
    "knowledge_source_fact_elements": (
        "SELECT * FROM knowledge_source_fact_elements WHERE notebook_id = ? "
        f"AND source_id NOT IN ({_KH_SQLITE})"
    ),
    "knowledge_source_fact_backfills": (
        "SELECT * FROM knowledge_source_fact_backfills WHERE notebook_id = ? "
        f"AND status IN ('complete','incomplete') AND source_id NOT IN ({_KH_SQLITE})"
    ),
    "knowledge_relations": (
        "SELECT * FROM knowledge_relations WHERE notebook_id = ? "
        f"AND (source_id IS NULL OR source_id NOT IN ({_KH_SQLITE}))"
    ),
    "chunk_embeddings": "SELECT * FROM chunk_embeddings WHERE notebook_id = ?",
    "chunk_questions": "SELECT * FROM chunk_questions WHERE notebook_id = ?",
    "element_embeddings": (
        "SELECT ee.* FROM element_embeddings ee JOIN sources s ON s.id = ee.source_id "
        "WHERE s.notebook_id = ? AND s.source_type != 'knowhow'"
    ),
    "knowledge_embeddings": "SELECT * FROM knowledge_embeddings WHERE notebook_id = ?",
    "relation_embeddings": "SELECT * FROM relation_embeddings WHERE notebook_id = ?",
    "concept_clusters": (
        "SELECT * FROM concept_clusters WHERE notebook_id = ? AND generation = COALESCE("
        "(SELECT cluster_generation FROM unified_kg_state u "
        "WHERE u.notebook_id = concept_clusters.notebook_id), 0)"
    ),
}

#: 快照里每张表在 Memory 之前的行顺序是否有 ORDER BY 保证(PostgreSQL 三张)。其余表两侧
#: 都没有顺序保证,只比较行的多重集合;有保证的逐序比较。
PG_ORDERED_TABLES = frozenset({"source_elements", "chunks", "knowledge_objects"})

#: 深拷贝快照的全部表名:新增/删除一张表必须回到这里登记它会不会带 Memory 派生内容。
SNAPSHOT_TABLES = frozenset({
    "notebooks", "notebook_bases", "sources", "source_paper_meta", "source_authors",
    "source_elements", "chunks", "knowledge_objects", "knowledge_source_facts",
    "knowledge_source_fact_elements", "knowledge_source_fact_backfills",
    "knowledge_relations", "chunk_embeddings", "chunk_questions", "element_embeddings",
    "knowledge_embeddings", "relation_embeddings", "concept_clusters",
    "notebook_object_schemas", "knowhow_tables", "knowhow_columns", "knowhow_rows",
    "knowhow_cells", "knowhow_cell_code", "notebook_assets",
})

#: 会带 Memory 派生内容、且**必须不在**快照里的表(靠「不在快照」保证不被拷贝)。
MEMORY_CARRIERS_NOT_COPIED = frozenset({
    "memory_items", "memory_embeddings", "memory_provenance", "memory_revisions",
    "promotion_candidates", "knowledge_object_sources", "mention_edges", "canonical_relations",
    "communities", "community_members", "kg_community_edges", "kg_analysis_artifacts",
    "concept_comentions", "concept_merge_candidates", "kg_conflict_candidates",
    "kg_source_profiles", "chunk_elements", "conversations", "answers", "reports",
    "retrieval_experiences", "unified_kg_state", "extraction_runs",
})


def legacy_snapshot_queries(current: tuple, legacy: dict[str, str]) -> tuple:
    """`_COPY_SNAPSHOT_QUERIES` with the M2-touched entries swapped back to their pre-M2 text."""
    return tuple((table, legacy.get(table, query)) for table, query in current)


def assert_snapshots_equal(new: dict, old: dict, ordered: Iterable[str] | None = None) -> None:
    """Row-for-row equality of two snapshots. Tables in ``ordered`` must also agree on order;
    the rest (no ORDER BY in either text) are compared as multisets."""
    ordered_tables = frozenset(new if ordered is None else ordered)
    assert new.keys() == old.keys()
    for table in new:
        a = [json.dumps(row, default=str, sort_keys=True) for row in new[table]]
        b = [json.dumps(row, default=str, sort_keys=True) for row in old[table]]
        if table in ordered_tables:
            assert a == b, f"{table}: rows or their order changed"
        else:
            assert sorted(a) == sorted(b), f"{table}: rows changed"


# --------------------------------------------------------------------------
# 评审复现的两个存量形态(spec review B1 / B2),走真实的合并与删除路径造出来。
# --------------------------------------------------------------------------
PROBE_NOTEBOOK = "nb-copy-probe"
PROBE_MEMORY_TEXT = f"{MARK}: my salary is 123"


def probe_world(nb: str = PROBE_NOTEBOOK, *, cluster_canonical: str | None = None) -> list[Row]:
    """alice 的一条 Memory 派生出来源 ``src-mem-p``(元素原文带 ``MARK``)与对象 ``ko-mem-p``;
    共享文档来源 ``src-doc-p`` 与对象 ``ko-shared-p``。两个对象的证据都是完整条目(原文、
    标题、定位)。``cluster_canonical`` 给出时,再造一个由 Memory 做种子的簇:成员是两个对象,
    簇名与描述取自 Memory。"""
    rows = [
        Row("notebooks", {
            "id": nb, "name": "Shared", "purpose": "", "primary_domain": "Semiconductor",
            "status": "draft", "created_by": OWNER, "created_at": NOW, "updated_at": NOW,
        }),
        Row("users", {"id": "u-alice", "email": "u-alice@example.test", "display_name": "a",
                      "role": "user", "created_at": NOW, "updated_at": NOW}),
        Row("memory_items", {
            "id": "mem-p", "notebook_id": nb, "created_by": "u-alice", "origin": "ask_answer",
            "status": "confirmed", "title": f"{MARK} title", "content_md": PROBE_MEMORY_TEXT,
            "created_at": NOW, "updated_at": NOW,
        }, True),
        _source(nb, "src-doc-p", "document"),
        _source(nb, "src-mem-p", "memory", memory_id="mem-p", memory=True),
        Row("source_elements", {
            "id": "el-doc-p", "source_id": "src-doc-p", "element_type": "paragraph",
            "location_label": "p1", "text": "shared text", "created_at": NOW,
        }),
        Row("source_elements", {
            "id": "el-mem-p", "source_id": "src-mem-p", "element_type": "paragraph",
            "location_label": "p1", "text": PROBE_MEMORY_TEXT, "created_at": NOW,
        }, True),
    ]
    for oid, sid, eid, memory in (
        ("ko-shared-p", "src-doc-p", "el-doc-p", False),
        ("ko-mem-p", "src-mem-p", "el-mem-p", True),
    ):
        entry = evidence(sid, eid, memory=memory)
        if memory:
            entry["quoted_span"] = PROBE_MEMORY_TEXT
        rows.append(Row("knowledge_objects", {
            "id": oid, "notebook_id": nb, "object_type": "concept", "status": "approved",
            "source_id": sid, "payload": {"name": "salary", "definition": "d"},
            "evidence": [entry], "created_at": NOW, "updated_at": NOW,
        }, memory))
        rows.append(Row("knowledge_object_sources", {
            "object_id": oid, "notebook_id": nb, "source_id": sid,
        }, memory))
    if cluster_canonical is not None:
        for member in ("ko-shared-p", "ko-mem-p"):
            rows.append(Row("concept_clusters", {
                "id": f"cc-p-{member}", "notebook_id": nb, "canonical_id": cluster_canonical,
                "member_object_id": member, "canonical_name": f"{MARK} plan",
                "object_type": "concept", "canonical_description": f"{MARK} description",
                "created_at": NOW, "generation": 0,
            }, True))
    return rows


def seed_probe(insert: Callable[[str, dict], None], nb: str = PROBE_NOTEBOOK, **kw) -> None:
    for row in probe_world(nb, **kw):
        insert(row.table, row.values)


#: 被删 Memory 做种子的簇:名字种子判不出来源(拷贝照带,靠标脏让副本主人重建);按对象 id
#: 铸的种子(``K-~<对象 id>``)能认出种子对象已不存在,拷贝不带。
STALE_CLUSTER_CASES = {
    "K-alice-private-plan": True,   # canonical -> cluster rows still arrive in the copy
    "K-~ko-mem-p": False,
}


def assert_copy_objects_carry_no_memory_text(view: "CopyView") -> None:
    leaked = [leaf for leaf in view.leaves if MARK in leaf or "src-mem-p" in leaf
              or "el-mem-p" in leaf]
    assert not leaked, leaked


def poisoned(entries: tuple) -> tuple:
    """The same (table, text) shape with every text replaced by a statement that fails
    when executed: patched in for the Memory-aware set, it proves a path never ran it."""
    return tuple((table, "SELECT memory_aware_statement_must_not_run(") for table, _ in entries)


_KH_PG_V = "SELECT id FROM sources WHERE source_type='knowhow'"
_KH_SQLITE_V = "SELECT id FROM sources WHERE source_type = 'knowhow'"
_CLUSTER_GEN_EXTRA = (
    "AND generation = COALESCE((SELECT cluster_generation "
    "FROM unified_kg_state u WHERE u.notebook_id = concept_clusters.notebook_id), 0)"
)


def _legacy_validated(kh: str) -> dict[str, str]:
    """`_COPY_VALIDATED_TABLES` as it was before M2 (master @ e02c8fa8), per backend."""
    return {
        "sources": "",
        "source_paper_meta": f"AND source_id NOT IN ({kh})",
        "source_authors": f"AND source_id NOT IN ({kh})",
        "chunks": "",
        "chunk_questions": "",
        "knowledge_objects": f"AND source_id NOT IN ({kh})",
        "knowledge_source_facts": f"AND source_id NOT IN ({kh})",
        "knowledge_source_fact_elements": f"AND source_id NOT IN ({kh})",
        "knowledge_source_fact_backfills": (
            f"AND status IN ('complete','incomplete') AND source_id NOT IN ({kh})"
        ),
        "knowledge_relations": f"AND (source_id IS NULL OR source_id NOT IN ({kh}))",
        "concept_clusters": _CLUSTER_GEN_EXTRA,
        "notebook_object_schemas": "",
        "knowhow_tables": "",
        "notebook_assets": "",
    }


LEGACY_VALIDATED_PG = _legacy_validated(_KH_PG_V)
LEGACY_VALIDATED_SQLITE = _legacy_validated(_KH_SQLITE_V)
