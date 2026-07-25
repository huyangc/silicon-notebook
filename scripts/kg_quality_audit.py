#!/usr/bin/env python3
"""KG 抽取质量离线审计:回答「库里的节点都是些什么、有多少是噪声」。

只读、零 LLM、零写库。用法(从仓库根跑):

    PYTHONPATH=backend python3 scripts/kg_quality_audit.py --db .local/silicon_notebook.db
    PYTHONPATH=backend python3 scripts/kg_quality_audit.py --db <path> --notebook nb-xxxx
    PYTHONPATH=backend python3 scripts/kg_quality_audit.py --db <path> --notebook nb-xxxx --sources 0

它刻意 **不是** `scripts/diag.py` 的子命令:那七个命令共享一份「≤32 KiB、全脱敏、
可粘贴给他人」的报告信封,而本审计的价值就在于把真实概念名打给库主人自己看。两种
契约不相容,所以分开放,并在报告头部提醒不要外发。

判据直接 import 产品代码(`app.services.kg.filters` / `app.eval.probes`),不重实现
—— 否则「现有过滤器在真库上拦下多少」这个问题会因为口径漂移而失真。代价是需要
`PYTHONPATH=backend` 和后端的 Python 环境。

抽样口径(大库上必须的,绝不静默):默认随机抽 `--sources K` 个来源,读它们的全部
对象;报告每一节都标注这是抽样还是全量。`--sources 0` 走全量(千万级库会很慢)。
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote

_REPO_ROOT = Path(__file__).resolve().parent.parent

# 产品判据。缺 PYTHONPATH 时响亮失败,不退化成自己重写一套(那会让结论失真)。
try:
    from app.eval.probes import claim_degraded, classify_concept, formula_degraded
    from app.services.kg.filters import is_meta_claim, is_noise_concept
    from app.services.knowledge_contracts import USABLE_STATUSES
except ImportError as exc:  # pragma: no cover - 环境问题,不是逻辑分支
    sys.stderr.write(
        f"无法 import 产品判据模块({exc})。\n"
        "本脚本刻意复用 app.services.kg.filters / app.eval.probes,以免口径漂移。\n"
        f"请从仓库根用后端解释器运行,并带上 PYTHONPATH=backend,例如:\n"
        f"  PYTHONPATH=backend python3 scripts/kg_quality_audit.py --db .local/silicon_notebook.db\n"
    )
    raise SystemExit(2)

DEFAULT_DB = str(_REPO_ROOT / ".local" / "silicon_notebook.db")
DEFAULT_SOURCE_SAMPLE = 60
DEFAULT_DEGREE_SAMPLE = 4000
# 内容分析读进内存的对象行数上限。来源抽样本身**不**约束内存(来源数少于
# --sources 时一个都不会被抽掉,单个来源也可能自己就有几百万对象),这才是那道闸。
DEFAULT_MAX_OBJECTS = 300_000
SAMPLE_PRINT = 40
KG_TYPES = ("concept", "claim", "formula", "procedure")

# 审计口径必须与产品口径一致,否则结论回答的是另一个问题。
#   - 对象:只有 USABLE_STATUSES 里的状态会进检索(合并/弃用掉的历史对象仍留在表里)。
#   - 关系:产品的每一条图/检索查询都带 review_status != 'rejected'(knowledge_store
#     里 6 处),被评审否掉的边不属于有效图。
# 被排除的量单独报出来 —— 既不让它污染质量指标,也不让它凭空消失。
_USABLE_PLACEHOLDERS = ",".join("?" for _ in USABLE_STATUSES)
_NOT_REJECTED = "review_status != 'rejected'"

_CJK_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
# 与 filters._norm / kg_merge._norm 同族的显示归一(仅用于本报告的计数口径)。
_WS_RE = re.compile(r"[\s\-_]+")


def _norm(name: str) -> str:
    return _WS_RE.sub(" ", (name or "").strip().lower())


def _has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


def _cjk_len(text: str) -> int:
    """CJK 名字的「字数」口径:NFKC 折叠后去掉空白的字符数。"""
    return len(re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or "")))


# ---------------------------------------------------------------------------
# DB access — 只读,不改源库
# ---------------------------------------------------------------------------

def open_readonly(path: str) -> sqlite3.Connection:
    """以只读方式打开源库。query_only 是第二道闸:即便 URI 被误改也写不进去。

    刻意 **不用** `immutable=1` —— 生产库多半有活着的写者,immutable 会让读者忽略
    WAL,读到过期甚至撕裂的快照。mode=ro 在 WAL 下与写者并发安全。

    「只读」的准确边界:**产品数据一个字节都不动**(mode=ro + query_only 双闸)。
    但 WAL 模式下 SQLite 为了读到最新快照仍可能创建/触碰 `-wal` / `-shm` 辅助文件
    —— 服务在跑时这两个文件本就存在,不会有任何变化;只有在「服务已停、WAL 已被
    checkpoint 掉」的库上,首次只读打开会重建一个 0 字节 `-wal`。这不改动任何数据,
    但它不是「一个文件都不碰」,所以在这里写明,别把它说成后者。
    """
    resolved = Path(path).expanduser()
    if not resolved.is_file():
        raise SystemExit(f"库文件不存在: {resolved}")
    try:
        conn = sqlite3.connect(
            f"file:{quote(str(resolved), safe='/')}?mode=ro", uri=True, timeout=30.0
        )
    except sqlite3.OperationalError as exc:
        raise SystemExit(
            f"只读打开失败: {exc}\n"
            "常见原因:库处于 WAL 模式但 -shm 不存在,而 mode=ro 无权创建它。"
            "服务在跑时不会有这个问题;若服务已停,让后端起一次再跑本脚本。"
        ) from exc
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def require_schema(conn: sqlite3.Connection) -> None:
    """缺表就直接报错退出,不静默跳过整节(降级必须显式)。"""
    have = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    missing = {"notebooks", "sources", "knowledge_objects", "knowledge_relations"} - have
    if missing:
        raise SystemExit(f"库缺少必需的表: {sorted(missing)} —— 这不是一个 silicon-notebook 库?")


def list_notebooks(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return list(conn.execute(
        "SELECT n.id, n.name, n.tier, "
        "  (SELECT COUNT(*) FROM sources s WHERE s.notebook_id = n.id) AS n_sources "
        "FROM notebooks n ORDER BY n_sources DESC"
    ))


def type_composition(
    conn: sqlite3.Connection, notebook_id: str
) -> Tuple[List[Tuple[str, int]], int]:
    """有效对象的类型构成 + 被状态排除的行数。

    走 idx_knowledge_objects_nb_type_status,不读 payload。返回
    ([(object_type, n_usable)], n_excluded) —— 排除的是合并/弃用等不进检索的
    历史对象;把它们混进质量指标会让治理做得越多的库看起来质量越差。
    """
    rows = conn.execute(
        "SELECT object_type, COUNT(*) AS n FROM knowledge_objects "
        f"WHERE notebook_id = ? AND status IN ({_USABLE_PLACEHOLDERS}) "
        "GROUP BY object_type ORDER BY n DESC",
        (notebook_id, *USABLE_STATUSES),
    ).fetchall()
    excluded = conn.execute(
        "SELECT COUNT(*) FROM knowledge_objects "
        f"WHERE notebook_id = ? AND status NOT IN ({_USABLE_PLACEHOLDERS})",
        (notebook_id, *USABLE_STATUSES),
    ).fetchone()
    return ([(str(r["object_type"]), int(r["n"])) for r in rows],
            int(excluded[0] or 0) if excluded else 0)


def sample_source_ids(
    conn: sqlite3.Connection, notebook_id: str, k: int, seed: int
) -> Tuple[List[str], int]:
    """返回 (抽中的 source id, 该 notebook 的 source 总数)。k<=0 表示全量。"""
    all_ids = [
        str(r["id"])
        for r in conn.execute("SELECT id FROM sources WHERE notebook_id = ? ORDER BY id",
                              (notebook_id,))
    ]
    total = len(all_ids)
    if k <= 0 or k >= total:
        return all_ids, total
    return sorted(random.Random(seed).sample(all_ids, k)), total


def _as_item(row: sqlite3.Row) -> dict:
    try:
        payload = json.loads(row["payload"] or "{}")
    except (TypeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        n_ev = len(json.loads(row["evidence"] or "[]"))
    except (TypeError, ValueError):
        n_ev = 0
    return {
        "id": str(row["id"]),
        "object_type": str(row["object_type"] or ""),
        "source_id": str(row["source_id"] or ""),
        "name": str(payload.get("name") or "").strip(),
        "payload": payload,
        "n_evidence": n_ev,
    }


def count_sourceless(conn: sqlite3.Connection, notebook_id: str) -> int:
    """不挂任何来源的对象数。

    这些**不是**脏数据:晋升(personal→base)与 Memory→KG 的写路径刻意把
    `source_id` 写成字面量 ''(见 repositories/*/governance_store.py 的
    `VALUES (?,?,?,'approved','',?,?,?,'',?,?)`)。按来源抽样天生看不见它们,
    而第一节的全量构成又把它们算进去 —— 不显式报出来,一个晋升为主的 base 库
    会出现「构成有几十万行、内容分析却几乎是空的」而读者无从察觉。
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM knowledge_objects "
        "WHERE notebook_id = ? AND (source_id = '' OR source_id IS NULL) "
        f"AND status IN ({_USABLE_PLACEHOLDERS})",
        (notebook_id, *USABLE_STATUSES),
    ).fetchone()
    return int(row[0] or 0) if row else 0


def _drain_into(cursor, out: List[dict], budget: int) -> bool:
    """把游标追加进 out,受 budget(**总**行数上限,0 = 不限)约束。

    返回 True 当且仅当**确实还有没读的行** —— 判据是「已经攒够 budget 行、而游标
    又吐出了下一行」。恰好等于上限时不算截断:那种情况什么都没漏,报成截断会让读者
    以为覆盖不全(单来源时甚至会报出 0/1 这种荒谬数字)。

    留下的是**索引前缀,不是随机样本**。这一点被刻意保留,并由调用方在报告里显式
    声明(见 main 里的截断文案)——不是疏漏:

      * 蓄水池/ORDER BY RANDOM 能拿到无偏样本,但两者都要把全表的 payload 扫一遍。
        本工具的目标是 437GB / 千万行级的生产库,那等于几十分钟到几小时的冷 IO,
        会让「先抽样快速看一眼」这个唯一的使用姿势不成立。
      * `--sources K` 只把**来源**随机化,并不能消除偏差:截断必然落在某个来源
        中途,留下的仍是那个来源的库内顺序前缀(一个来源自己就超过上限时尤其明显)。
        所以两条路径触顶时都要发同一句偏差警告 —— 曾经只给全量模式发、还向抽样模式
        保证「不会系统性聚集」,那是个过强且错误的保证。
      * 真正让结论无偏的办法只有「别触顶」:调大 --max-objects,或用更小的
        --sources K 让全部抽中来源都能读完。

    换言之:不消除偏差,但绝不隐瞒偏差。test_kg_quality_audit.py 里有反向护栏钉住
    这句声明必须出现。
    """
    for row in cursor:
        if budget and len(out) >= budget:
            return True        # 这是第 budget+1 行,证明确有剩余
        out.append(_as_item(row))
    return False


def count_orphan_source_refs(conn: sqlite3.Connection, notebook_id: str) -> int:
    """source_id 非空、却在 sources 表里找不到对应行的对象数。

    `knowledge_objects.source_id` 没有外键约束(schema 里只是
    `source_id TEXT NOT NULL DEFAULT ''`),历史清理/部分删除都可能留下这种孤儿引用。
    按来源逐个查天然看不见它们 —— 不报出来,一次「全量」审计会悄悄漏掉一批有效对象。
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM knowledge_objects o "
        "WHERE o.notebook_id = ? AND o.source_id != '' AND o.source_id IS NOT NULL "
        f"AND o.status IN ({_USABLE_PLACEHOLDERS}) "
        "AND NOT EXISTS (SELECT 1 FROM sources s WHERE s.id = o.source_id)",
        (notebook_id, *USABLE_STATUSES),
    ).fetchone()
    return int(row[0] or 0) if row else 0


def load_all_usable_objects(
    conn: sqlite3.Connection, notebook_id: str, out: List[dict], budget: int
) -> bool:
    """全量模式:一条查询覆盖该 notebook 下**全部**有效对象。

    刻意不复用按来源逐个查的路径 —— 那条路只能看见 `sources` 表里还存在的 id,
    孤儿 source_id 的对象会被静默漏掉,而报告却写着「全量」。这里按 notebook_id
    直接查,挂来源的、不挂来源的、孤儿引用的一并覆盖,「全量」才名副其实。
    """
    return _drain_into(
        conn.execute(
            "SELECT id, object_type, source_id, payload, evidence "
            "FROM knowledge_objects "
            f"WHERE notebook_id = ? AND status IN ({_USABLE_PLACEHOLDERS})",
            (notebook_id, *USABLE_STATUSES),
        ),
        out,
        budget,
    )


def load_sourceless_objects(
    conn: sqlite3.Connection, notebook_id: str, out: List[dict], budget: int
) -> bool:
    """把不挂来源的对象(晋升 / Memory→KG 产物)追加进 ``out``,同样受行数上限约束。

    晋升为主的 base 库这一批本身就可能有几百万行 —— 只给挂来源的那条路径设闸,
    上限就是个摆设。**追加进调用方已有的 ``out``**(而不是自己开一个新列表)是刻意
    的:budget 衡量的是这次审计读进内存的**总**行数,给它一份独立预算等于把上限
    放宽一倍。返回是否被上限截断。
    """
    return _drain_into(
        conn.execute(
            "SELECT id, object_type, source_id, payload, evidence "
            "FROM knowledge_objects "
            "WHERE notebook_id = ? AND (source_id = '' OR source_id IS NULL) "
            f"AND status IN ({_USABLE_PLACEHOLDERS})",
            (notebook_id, *USABLE_STATUSES),
        ),
        out,
        budget,
    )


def load_objects(
    conn: sqlite3.Connection, notebook_id: str, source_ids: Sequence[str],
    max_objects: int,
) -> Tuple[List[dict], int, bool]:
    """读抽中来源下的 KG 对象,**按对象行数封顶**。

    按 source_id 逐个查,走 idx_knowledge_objects_source —— 不做一条巨型 IN
    (百万级 id 会撞 SQLite 变量上限,见部署机冻结那次教训)。

    为什么还需要行数上限:按来源抽样对内存**不构成任何约束**。来源数本来就少于
    --sources 时一个都不会被抽掉;单个来源(一整本手册)也可能自己就有几百万对象。
    这个工具的目标场景恰恰是千万级库,没有这道闸就会在读 payload/evidence 时把
    宿主机内存吃光。上限 0 = 不封顶(明确要全量时才用)。

    返回 (对象, 完整读完的来源数, 是否被上限截断)。截断必须由调用方显式报出 ——
    静默截断会让读者以为覆盖了全部抽样来源。恰好读满上限而后面再无数据时**不**算
    截断(见 _drain_into)。

    只覆盖挂了来源的对象;不挂来源的那部分见 load_sourceless_objects。"""
    out: List[dict] = []
    covered = 0
    for i, sid in enumerate(source_ids, 1):
        if _drain_into(
            conn.execute(
                "SELECT id, object_type, source_id, payload, evidence "
                "FROM knowledge_objects "
                f"WHERE source_id = ? AND notebook_id = ? "
                f"AND status IN ({_USABLE_PLACEHOLDERS})",
                (sid, notebook_id, *USABLE_STATUSES),
            ),
            out,
            max_objects,
        ):
            return out, covered, True     # 这个来源只读了一半,不计入 covered
        covered = i
        if i % 200 == 0:
            sys.stderr.write(f"  ... 已读 {i}/{len(source_ids)} 个来源\n")
            sys.stderr.flush()
    return out, covered, False


def degree_and_basis(
    conn: sqlite3.Connection, notebook_id: str, object_ids: Sequence[str]
) -> Tuple[Dict[str, int], Counter]:
    """对给定对象逐个查度数(两个方向各一次索引查找),并统计其边的 relink basis。

    刻意逐 id 查而非全表扫 knowledge_relations:千万级边的全扫会把这个诊断本身
    变成一次事故。代价是只覆盖抽样节点,报告里已标注。
    """
    # 两条字面量 SQL 而不是把列名插进 f-string:列名虽来自本地常量元组、没有注入
    # 面,但动态拼 SQL 在这个仓库里是需要理由的形态,写死更省事也更好读。
    #
    # JOIN 是为了检查**对端**节点是否可用:对象合并后源对象被标 deprecated,而它的
    # 关系仍留在表里;产品的图构建按可用节点集建图,那些边根本进不去。只过滤
    # review_status 的话,一个只连着 deprecated 节点的节点会被报成「非孤立」,它的
    # 边也会进标注统计 —— 而产品眼里它就是孤立的。被查节点自身已从可用集合里抽出,
    # 故只需约束对端。
    # 对端还必须属于**本 notebook**:关系的两个端点列都没有外键约束更没有 notebook
    # 归属约束,历史/损坏数据可能指向别的库;而产品的节点集就是本 notebook 的可用
    # 对象,那种边根本进不了图。少了这个条件,跨库的野边会被算成连通。
    outgoing = ("SELECT r.evidence FROM knowledge_relations r "
                "JOIN knowledge_objects o ON o.id = r.target_object_id "
                f"WHERE r.notebook_id = ? AND r.source_object_id = ? AND r.{_NOT_REJECTED} "
                "AND o.notebook_id = ? "
                f"AND o.status IN ({_USABLE_PLACEHOLDERS})")
    incoming = ("SELECT r.evidence FROM knowledge_relations r "
                "JOIN knowledge_objects o ON o.id = r.source_object_id "
                f"WHERE r.notebook_id = ? AND r.target_object_id = ? AND r.{_NOT_REJECTED} "
                "AND o.notebook_id = ? "
                f"AND o.status IN ({_USABLE_PLACEHOLDERS})")
    deg: Dict[str, int] = {}
    basis = Counter()
    for i, oid in enumerate(object_ids, 1):
        n = 0
        for statement in (outgoing, incoming):
            for row in conn.execute(
                    statement,
                    (notebook_id, oid, notebook_id, *USABLE_STATUSES)):
                n += 1
                try:
                    ev = json.loads(row["evidence"] or "[]")
                except (TypeError, ValueError):
                    ev = []
                # 只有 relink 会写 basis。没有 basis 的边**不能**记成「LLM 抽取」:
                # knowhow 投影写的 about 边就是 evidence='[]'(见
                # services/knowhow/projection.py 的 `..., "about", "[]", now`),
                # 与 LLM 抽取的边在这里不可区分。归到「未标注」,别替它认领出处。
                tag = ""
                if isinstance(ev, list) and ev and isinstance(ev[0], dict):
                    tag = str(ev[0].get("basis") or "")
                basis[tag or "untagged"] += 1
        deg[oid] = n
        if i % 1000 == 0:
            sys.stderr.write(f"  ... 已查 {i}/{len(object_ids)} 个节点的度数\n")
            sys.stderr.flush()
    return deg, basis


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------

def _pct(part: int, whole: int) -> str:
    return f"{part / whole:6.2%}" if whole else "   n/a"


def _bar(title: str) -> None:
    print()
    print(title)
    print("-" * max(20, len(title)))


def report_concepts(items: List[dict], whitelist: set, show_samples: bool,
                    seed: int) -> None:
    # 分母是**全部** concept 行,不是「有名字的行」。payload 坏掉 / 不是 dict /
    # name 缺失或空白的行,恰恰是这份审计要暴露的低质量行 —— 悄悄从分母里剔掉,
    # 既低估了噪声占比,极端情况还会报出「抽样里没有 concept」而类型构成里明明有。
    total = len(items)
    names = [it["name"] for it in items if it["name"]]
    n_nameless = total - len(names)
    _bar(f"[concept] 抽样 {total} 行")
    if not total:
        print("  (抽样里没有 concept)")
        return
    if n_nameless:
        print(f"  ⚠ 无名/payload 异常: {n_nameless} 行 ({_pct(n_nameless, total)})"
              " —— 本身就是质量信号;下面按名字算的指标只覆盖其余行")
    if not names:
        print("  所有 concept 行都没有可用的名字,按名字算的指标无从统计。")
        return

    norm_counts = Counter(_norm(n) for n in names)
    # 冗余行 = 总行数 − 唯一名数(等价于 sum(c-1))。一个名字出现 c 次,多出来的只有
    # c-1 行 —— 头一行不是冗余。早先写成 sum(c) 会把「参与重名的行」当成「冗余行」,
    # 系统性高估这个指标(9400 行 / 7412 唯一名:真值 21.1%,错算成 32.0%)。
    redundant = total - len(norm_counts)
    print(f"  唯一名 {len(norm_counts)} / 行数 {total}"
          f"  → 冗余 {redundant} 行 ({_pct(redundant, total)})")
    top_dup = [f"{n}×{c}" for n, c in norm_counts.most_common(6) if c > 1]
    if top_dup and show_samples:   # 概念名是内容,--no-samples 下一律不打
        print(f"  最重复: {', '.join(top_dup)}")

    # 文档频次:一个名字出现在多少个来源里。抽样口径下 DF 被系统性低估,已在结尾说明。
    # 不挂来源的对象(source_id='')不参与 —— 它们共享同一个空 source_id,算进去会
    # 被当成「同一篇文档」,那是个凭空造出来的文档频次。
    df: Dict[str, set] = defaultdict(set)
    mention: Counter = Counter()
    n_no_source = 0
    for it in items:
        if not it["name"]:
            continue
        if not it["source_id"]:
            n_no_source += 1
            continue
        key = _norm(it["name"])
        df[key].add(it["source_id"])
        mention[key] += max(1, it["n_evidence"])
    # 只有 DF 这一小节依赖 source_id。一个全部来自晋升的 base 库 DF 无从谈起,但
    # 噪声过滤器、probes、词数分布、CJK 诊断、样本对它一概不依赖 —— 不能因为 DF
    # 算不出来就把整节 return 掉(那正是 full 模式声称已纳入的那批对象)。
    if not df:
        print(f"  文档频次(DF):本批 {n_no_source} 个对象全部不挂来源,DF 无从统计;"
              "以下其余分析照常。")
    else:
        buckets = Counter(min(len(s), 5) for s in df.values())
        if n_no_source:
            print(f"  文档频次(DF)分桶 [按唯一名;已排除 {n_no_source} 个不挂来源的对象]:")
        else:
            print("  文档频次(DF)分桶 [按唯一名]:")
        for k in sorted(buckets):
            label = f"{k} 篇" if k < 5 else "≥5 篇"
            print(f"    DF = {label:<6} {buckets[k]:7d}  {_pct(buckets[k], len(df))}")
        singleton = [n for n, s in df.items() if len(s) == 1 and mention[n] <= 1]
        print(f"  只在 1 篇出现且只有 1 处证据: {len(singleton)} 个唯一名"
              f" ({_pct(len(singleton), len(df))}) —— 语料统计意义上的长尾")

    # 现有产品过滤器 + 质量探针
    filt = Counter()
    for n in names:
        noise, why = is_noise_concept(n, whitelist)
        if noise:
            filt[why] += 1
    n_filt = sum(filt.values())
    print(f"  现有 is_noise_concept 命中: {n_filt} ({_pct(n_filt, total)}) {dict(filt) or ''}")
    print("    注:入库前已过滤过一遍,所以这里接近 0 是预期的;它衡量的是"
          "「过滤规则改了以后还会再拦下多少存量」,不是「库里还剩多少噪声」。")

    probe = Counter()
    probe_samples: Dict[str, List[str]] = defaultdict(list)
    for n in names:
        for tag in classify_concept(n):
            probe[tag] += 1
            if show_samples and len(probe_samples[tag]) < 6:
                probe_samples[tag].append(n)
    print(f"  probes.classify_concept 疑似信号(比过滤器口径宽):")
    if not probe:
        print("    (无命中)")
    for tag, c in probe.most_common():
        example = f"   e.g. {probe_samples[tag]}" if show_samples else ""
        print(f"    {tag:<10} {c:7d}  {_pct(c, total)}{example}")

    words = Counter(min(len(n.split()), 6) for n in names)
    print("  按词数: " + ", ".join(
        f"{'≥6' if k == 6 else k}词={words[k]}" for k in sorted(words)))

    # CJK 名字长度分布 —— filters.is_noise_concept 的 `len(raw) <= 2` 会丢掉中文
    # 双字术语(栅极/沟道/阈值…)。存量里看不到被丢的那些,但 2 字档恒为 0 而 3 字档
    # 有量,就是这条规则在中文语料上生效的直接痕迹。
    cjk = [n for n in names if _has_cjk(n)]
    if cjk:
        lens = Counter(min(_cjk_len(n), 6) for n in cjk)
        print(f"  含中日韩字符的概念: {len(cjk)} ({_pct(len(cjk), total)})"
              f"  字数分布: " + ", ".join(
                  f"{'≥6' if k == 6 else k}字={lens[k]}" for k in sorted(lens)))
        if lens.get(2, 0) == 0 and lens.get(3, 0) > 0:
            # 措辞刻意停在「风险」而不是「已发生」:直方图证明不了丢弃 —— 语料可能
            # 天然没有双字术语,抽样也可能恰好没抽到。真正的损失量只能从过滤前的
            # 抽取产物里数,那不是这个只读工具能看到的东西。
            print("    ⚠ 风险提示:含中日韩字符的概念里 2 字档为 0、3 字档非 0。"
                  "filters.is_noise_concept\n      的 `len(raw) <= 2` 会把中文双字"
                  "术语(汇率/栅极/沟道)判成 too_short 丢掉,这个形状与该规则生效"
                  "相符。\n      但直方图不构成证据(语料可能本就没有双字术语,抽样"
                  "也可能漏掉);要定性\n      得对着原文查一批双字术语是否真的缺席。")

    if show_samples:
        rng = random.Random(seed)
        print(f"  随机样本({min(SAMPLE_PRINT, total)} 个,人工判断是否领域术语):")
        for n in rng.sample(names, min(SAMPLE_PRINT, total)):
            print(f"    {n}")


def report_claims(items: List[dict], show_samples: bool, seed: int) -> None:
    # 同 report_concepts:分母是全部 claim 行,无名/坏 payload 单独报,不静默剔除。
    total = len(items)
    names = [it["name"] for it in items if it["name"]]
    n_nameless = total - len(names)
    _bar(f"[claim] 抽样 {total} 行")
    if not total:
        print("  (抽样里没有 claim)")
        return
    if n_nameless:
        print(f"  ⚠ 无名/payload 异常: {n_nameless} 行 ({_pct(n_nameless, total)})"
              " —— 本身就是质量信号;下面按文本算的指标只覆盖其余行")
    if not names:
        print("  所有 claim 行都没有可用的文本,按文本算的指标无从统计。")
        return
    norm_counts = Counter(_norm(n) for n in names)
    redundant = total - len(norm_counts)   # 同 report_concepts:只有 c-1 行是冗余
    print(f"  唯一命题 {len(norm_counts)} / 行数 {total}"
          f"  → 冗余 {redundant} 行 ({_pct(redundant, total)})")
    meta = [n for n in names if is_meta_claim(n)[0]]
    print(f"  现有 is_meta_claim 命中(元叙述/导航): {len(meta)} ({_pct(len(meta), total)})")
    if meta and show_samples:      # 命题原文是内容
        print(f"    e.g. {meta[:3]}")
    degraded = [n for n in names if claim_degraded(n)]
    n_cjk = sum(1 for n in names if _has_cjk(n))
    print(f"  probes.claim_degraded 疑似退化: {len(degraded)} ({_pct(len(degraded), total)})")
    print("    注:这是宽口径「疑似信号」,已知会误报(动词表覆盖不全,如 'lacks');"
          "看趋势和样本,别当结论。")
    if n_cjk > total * 0.3:
        print(f"    ⚠ 本批 {_pct(n_cjk, total)} 的命题含中日韩字符,而 claim_degraded 的"
              "动词表与词数判据\n      只覆盖英文 —— 中文命题几乎必然被判 degraded。"
              "上面这个数字对本库无效,请忽略。")
    if show_samples and degraded:
        rng = random.Random(seed + 1)
        print(f"  degraded 样本({min(8, len(degraded))} 条):")
        for n in rng.sample(degraded, min(8, len(degraded))):
            print(f"    {n}")


def report_formulas_procedures(items: List[dict], show_samples: bool) -> None:
    formulas = [it for it in items if it["object_type"] == "formula"]
    procedures = [it for it in items if it["object_type"] == "procedure"]
    _bar(f"[formula / procedure] 抽样 {len(formulas)} / {len(procedures)} 行")
    if formulas:
        bad = [it["name"] for it in formulas if formula_degraded(it["name"])]
        example = f"  e.g. {bad[:4]}" if show_samples else ""   # 公式原文是内容
        print(f"  formula 无任何运算符(疑似不是公式): {len(bad)}"
              f" ({_pct(len(bad), len(formulas))}){example}")
    if procedures:
        empty = [it for it in procedures
                 if not (it["payload"].get("steps") or [])]
        print(f"  procedure 无 steps: {len(empty)} ({_pct(len(empty), len(procedures))})")


def report_degree(deg: Dict[str, int], basis: Counter, items_by_id: Dict[str, dict],
                  sampled: int, pool: int, show_samples: bool) -> None:
    _bar(f"[连通性] 度数子样本 {sampled} / 抽样节点 {pool}")
    if not deg:
        print("  (没有可查的节点)")
        return
    by_type_zero: Counter = Counter()
    by_type_total: Counter = Counter()
    for oid, d in deg.items():
        otype = items_by_id.get(oid, {}).get("object_type", "?")
        by_type_total[otype] += 1
        if d == 0:
            by_type_zero[otype] += 1
    print("  零度(无任何关系)占比:")
    # 同第一节:自定义 object_type 是用户表头,--no-samples 下按序号脱敏。
    custom_seen = 0
    for otype in sorted(by_type_total):
        if otype in KG_TYPES or show_samples:
            shown = otype
        else:
            custom_seen += 1
            shown = f"<自定义类型 #{custom_seen}>"
        print(f"    {shown:<20} {by_type_zero[otype]:6d} / {by_type_total[otype]:<6d}"
              f"  {_pct(by_type_zero[otype], by_type_total[otype])}")
    total_edges = sum(basis.values())
    if total_edges:
        print(f"  这些节点上的边按标注({total_edges} 条边端):")
        for tag, c in basis.most_common(6):
            label = {"untagged": "未标注(LLM 抽取或 knowhow 投影)"}.get(tag, tag)
            print(f"    {label:<30} {c:7d}  {_pct(c, total_edges)}")
        print("    注:只有 relink 会写 basis。未标注一档里 LLM 抽取的边与 knowhow"
              "投影的 about 边\n      不可区分(后者 evidence='[]'),别把这一档整个"
              "当成 LLM 的产出。")
        relink = sum(c for tag, c in basis.items() if tag.startswith("relink:"))
        if relink:
            print(f"    → relink 补出来的占 {_pct(relink, total_edges)};"
                  "gleaning 出的无边节点靠它连上,所以「度数>0」不等于「LLM 认为它有关系」。")


def _non_negative(raw: str) -> int:
    """解析期就拒绝负数。

    否则 `--sources -1` 会被 `k <= 0` 当成「全量」,在千万级生产库上**静默**执行
    那条我们特意警告过很慢的全扫;`--degree-sample -1` 则要等对象都读完了才在
    random.sample 里崩。手滑应该在打开库之前就失败。
    """
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"需要整数,收到 {raw!r}")
    if value < 0:
        raise argparse.ArgumentTypeError(f"不能为负数(收到 {value};0 表示全量)")
    return value


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="kg_quality_audit.py",
        description="KG 抽取质量离线审计(只读、零 LLM、零写库)。",
    )
    ap.add_argument("--db", default=DEFAULT_DB, help=f"SQLite 库路径(默认 {DEFAULT_DB})")
    ap.add_argument("--notebook", default=None,
                    help="要审计的 notebook id;不给则列出候选并选来源最多的那个")
    ap.add_argument("--sources", type=_non_negative, default=DEFAULT_SOURCE_SAMPLE,
                    metavar="K",
                    help=f"随机抽 K 个来源做内容分析(默认 {DEFAULT_SOURCE_SAMPLE};"
                         "0 = 全量,千万级库会很慢;不接受负数)")
    ap.add_argument("--degree-sample", type=_non_negative,
                    default=DEFAULT_DEGREE_SAMPLE, metavar="N",
                    help=f"对其中 N 个节点查度数与边标注(默认 {DEFAULT_DEGREE_SAMPLE};"
                         "0 = 全部抽样节点;不接受负数)")
    ap.add_argument("--max-objects", type=_non_negative,
                    default=DEFAULT_MAX_OBJECTS, metavar="N",
                    help=f"内容分析最多读入 N 个对象(默认 {DEFAULT_MAX_OBJECTS};"
                         "0 = 不封顶。这是唯一的内存闸 —— 来源抽样不约束内存)")
    ap.add_argument("--seed", type=int, default=20260725, help="抽样随机种子(可复现)")
    ap.add_argument("--no-samples", action="store_true",
                    help="不打印具体名称样本(只出统计数字)")
    args = ap.parse_args(list(argv) if argv is not None else None)

    conn = open_readonly(args.db)
    try:
        require_schema(conn)
        notebooks = list_notebooks(conn)
        if not notebooks:
            raise SystemExit("库里没有 notebook。")

        print("=" * 78)
        print("KG 抽取质量审计 —— 只读、零 LLM、零写库")
        print(f"库: {args.db}")
        show_samples = not args.no_samples
        if show_samples:
            print("⚠ 本报告含知识库内容样本(概念名/命题原文),按内部资料对待,不要外发。")
        else:
            print("口径:--no-samples,只出统计数字,不打印任何概念名/命题/公式原文,"
                  "也不打印笔记本名称。")
        print("=" * 78)

        # 笔记本名是用户起的标题,同样是内容 —— 而且这张表还会带出与本次审计目标
        # 无关的其它笔记本。--no-samples 下只留选库必需的 id/tier/来源数。
        _bar("候选 notebook(按来源数排序)")
        for nb in notebooks[:20]:
            title = f"  {nb['name']}" if show_samples else ""
            print(f"  {nb['id']:<36} {nb['tier']:<9} {nb['n_sources']:>7} 来源{title}")
        if len(notebooks) > 20:
            print(f"  ...(共 {len(notebooks)} 个,只列前 20)")

        target = args.notebook or str(notebooks[0]["id"])
        chosen = next((nb for nb in notebooks if str(nb["id"]) == target), None)
        if chosen is None:
            raise SystemExit(f"找不到 notebook: {target}")
        label = f"({chosen['name']}, tier={chosen['tier']})" if show_samples \
            else f"(tier={chosen['tier']})"
        print(f"\n审计目标: {chosen['id']}  {label}")

        _bar("一、对象类型构成(全量,不抽样)")
        sys.stderr.write("  正在统计类型构成(大库可能要几十秒)...\n")
        sys.stderr.flush()
        comp, n_excluded = type_composition(conn, target)
        grand = sum(n for _t, n in comp)
        if not grand:
            raise SystemExit(
                "该 notebook 没有任何有效 KG 对象"
                f"(另有 {n_excluded} 个已合并/弃用的对象,不进检索)。"
                if n_excluded else "该 notebook 没有任何 KG 对象。"
            )
        print(f"  口径:与产品一致 —— 只算 status ∈ {list(USABLE_STATUSES)} 的对象,")
        print("       连通性只算 review_status != 'rejected' 的边。")
        # 自定义 object_type 是 knowhow 投影用的**用户列名**(专有表头),在
        # --no-samples 下必须脱敏 —— 否则「只出数字」的报告照样泄漏业务词汇。
        # 内置四类是产品固定词汇,不是内容,照常显示。
        custom_seen = 0
        for otype, n in comp:
            builtin = otype in KG_TYPES
            if builtin:
                shown, mark = otype, ""
            elif show_samples:
                shown, mark = otype, "   (自定义类型)"
            else:
                custom_seen += 1
                shown, mark = f"<自定义类型 #{custom_seen}>", "   (名称已脱敏)"
            print(f"  {shown:<20} {n:>12,}  {_pct(n, grand)}{mark}")
        print(f"  {'合计':<16} {grand:>12,}")
        if n_excluded:
            print(f"  另有 {n_excluded:,} 个对象因状态不在有效集内被排除"
                  "(已合并/弃用的历史对象)。")
            print("    它们仍占磁盘,但不进检索 —— 也不进下面的质量指标,"
                  "否则治理做得越多的库看起来质量越差。")
        kg_share = sum(n for t, n in comp if t in ("claim",))
        print(f"\n  → Claim 占 {_pct(kg_share, grand)}。Claim 是句子级命题,本来就"
              "不是专有名词;\n    「节点里很多不是术语」多半首先由这个比例解释,"
              "而不是概念抽取跑偏。")

        source_ids, n_sources_total = sample_source_ids(
            conn, target, args.sources, args.seed)
        full = len(source_ids) >= n_sources_total
        scope = (f"全部 {n_sources_total} 个来源" if full
                 else f"随机 {len(source_ids)}/{n_sources_total} 个来源(seed={args.seed})")
        _bar(f"二、内容分析 —— 口径:{scope}")
        if not full:
            print("  ⚠ 以下所有比例来自抽样,不是全量。DF(文档频次)在抽样下被系统性")
            print("    低估 —— 只在 1 篇出现的名字里,有一部分其实在没抽到的来源中也出现。")
            print(f"    要全量:重跑并加 --sources 0(千万级库会很慢)。")

        # 不挂来源的对象(晋升 / Memory→KG 写路径刻意写 source_id='')按来源永远抽
        # 不到。全量模式必须纳入,抽样模式至少要把它们的存在和数量说出来 —— 否则
        # 一个晋升为主的 base 库会「构成几十万行、内容分析近乎空」而读者无从察觉。
        sys.stderr.write("  正在清点不挂来源的对象...\n")
        sys.stderr.flush()
        n_sourceless = count_sourceless(conn, target)
        n_orphan = count_orphan_source_refs(conn, target)
        if n_sourceless:
            print(f"  不挂来源的对象: {n_sourceless:,} 个 ({_pct(n_sourceless, grand)} 的全库)"
                  " —— 来自晋升 / Memory→KG 写路径")
            if full:
                print("    全量模式:已纳入下面的分析(它们没有来源,故不参与 DF 统计)。")
            else:
                print("    ⚠ 抽样模式按来源抽样,这部分一个都抽不到,未纳入下面的分析。")
                print("      要覆盖它们:重跑并加 --sources 0。")
        if n_orphan:
            print(f"  孤儿来源引用: {n_orphan:,} 个对象的 source_id 在 sources 表里"
                  "已不存在(历史清理残留)")
            if full:
                print("    全量模式:已纳入(全量按 notebook 直查,不经 sources 表)。")
            else:
                print("    ⚠ 抽样模式按 sources 表抽样,这部分抽不到,未纳入下面的分析。")

        if full:
            # 全量按 notebook_id 直查:挂来源的、不挂来源的、孤儿引用的一并覆盖。
            # 绕开 sources 表,「全量」才名副其实(见 load_all_usable_objects)。
            sys.stderr.write("  正在读取该 notebook 的全部有效对象...\n")
            sys.stderr.flush()
            items: List[dict] = []
            capped = load_all_usable_objects(
                conn, target, items, args.max_objects)
            covered = None
        else:
            sys.stderr.write(f"  正在读取 {len(source_ids)} 个来源的对象...\n")
            sys.stderr.flush()
            items, covered, capped = load_objects(
                conn, target, source_ids, args.max_objects)
        print(f"  读到 {len(items):,} 个对象")
        if capped:
            # 绝不静默截断:上限一旦生效,「全量」就不再是全量,必须当场说清楚。
            print(f"  ⚠ 已达 --max-objects={args.max_objects:,} 上限,读取在此停止:")
            if covered is None:
                print(f"    只读到全部 {grand:,} 个有效对象里的前 {len(items):,} 个。")
            else:
                print(f"    完整读完 {covered}/{len(source_ids)} 个来源,"
                      f"第 {covered + 1} 个只读了一半。")
            # 两条路径都要发这句。抽样模式下来源虽是随机抽的,但截断必然落在某个来源
            # **中途**,留下的是那个来源的库内顺序前缀 —— 一个来源自己就超过上限时尤其
            # 明显。上一版只给全量模式发警告、还向抽样模式保证「不会系统性聚集」,那是
            # 一个过强且错误的保证。
            print("    ⚠ 截断留下的是**库内顺序前缀,不是随机样本** —— 数据库可能按"
                  "插入序返回,\n      而插入序与抽取批次、对象类型相关,所以下面的比例"
                  "可能有偏。")
            print("      要更有代表性:调大 --max-objects(注意内存),或用更小的"
                  " --sources K\n      让全部抽中来源都能读完。")
        by_type: Dict[str, List[dict]] = defaultdict(list)
        for it in items:
            by_type[it["object_type"]].append(it)

        whitelist = {
            str(r["term"])
            for r in conn.execute("SELECT term FROM concept_whitelist")
        } if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='concept_whitelist'"
        ).fetchone() else set()
        print(f"  concept_whitelist 词条数: {len(whitelist)}")

        report_concepts(by_type.get("concept", []), whitelist,
                        not args.no_samples, args.seed)
        report_claims(by_type.get("claim", []), not args.no_samples, args.seed)
        report_formulas_procedures(items, not args.no_samples)

        pool = [it["id"] for it in items]
        if args.degree_sample and args.degree_sample < len(pool):
            picked = random.Random(args.seed + 2).sample(pool, args.degree_sample)
        else:
            picked = pool
        sys.stderr.write(f"  正在查 {len(picked)} 个节点的度数...\n")
        sys.stderr.flush()
        deg, basis = degree_and_basis(conn, target, picked)
        report_degree(deg, basis, {it["id"]: it for it in items},
                      len(picked), len(pool), show_samples)

        _bar("怎么读这份报告")
        print("  1) 先看第一节的类型构成 —— 它解释「一千万节点」的量级从哪来。")
        print("  2) concept 的重名率 = 跨文档不去重的代价(设计如此,收敛只发生在检索期的")
        print("     canonical fold,不减少行数)。")
        print("  3) DF 分桶 + 只出现一次的长尾 = 「哪些概念对检索几乎没贡献」的语料统计信号,")
        print("     比正则形状规则可分得多。")
        print("  4) probes 是宽口径疑似信号,会误报;拿随机样本人工过一遍再下结论。")
        print("  5) relink 边占比高 → 度数不能再当质量信号用。「未标注」一档混了 LLM")
        print("     抽取与 knowhow 投影两种出处,不可拆,别整个算到 LLM 头上。")
        print("  6) 不挂来源的对象(晋升 / Memory→KG)按来源抽不到:抽样模式只报数量,")
        print("     --sources 0 才会纳入内容分析。")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
