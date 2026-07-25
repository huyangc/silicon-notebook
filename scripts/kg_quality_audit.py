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
SAMPLE_PRINT = 40
KG_TYPES = ("concept", "claim", "formula", "procedure")

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


def type_composition(conn: sqlite3.Connection, notebook_id: str) -> List[Tuple[str, int]]:
    """全量类型构成。走 idx_knowledge_objects_nb_type_status,不读 payload。"""
    rows = conn.execute(
        "SELECT object_type, COUNT(*) AS n FROM knowledge_objects "
        "WHERE notebook_id = ? GROUP BY object_type ORDER BY n DESC",
        (notebook_id,),
    ).fetchall()
    return [(str(r["object_type"]), int(r["n"])) for r in rows]


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
        "WHERE notebook_id = ? AND (source_id = '' OR source_id IS NULL)",
        (notebook_id,),
    ).fetchone()
    return int(row[0] or 0) if row else 0


def load_sourceless_objects(
    conn: sqlite3.Connection, notebook_id: str
) -> List[dict]:
    """读该 notebook 下不挂来源的对象(晋升 / Memory→KG 产物)。"""
    return [
        _as_item(row)
        for row in conn.execute(
            "SELECT id, object_type, source_id, payload, evidence "
            "FROM knowledge_objects "
            "WHERE notebook_id = ? AND (source_id = '' OR source_id IS NULL)",
            (notebook_id,),
        )
    ]


def load_objects(
    conn: sqlite3.Connection, notebook_id: str, source_ids: Sequence[str]
) -> List[dict]:
    """读抽中来源下的全部 KG 对象。按 source_id 逐个查,走 idx_knowledge_objects_source
    —— 不做一条巨型 IN(百万级 id 会撞 SQLite 变量上限,见部署机冻结那次教训)。

    只覆盖挂了来源的对象;不挂来源的那部分见 load_sourceless_objects。"""
    out: List[dict] = []
    for i, sid in enumerate(source_ids, 1):
        for row in conn.execute(
            "SELECT id, object_type, source_id, payload, evidence FROM knowledge_objects "
            "WHERE source_id = ? AND notebook_id = ?",
            (sid, notebook_id),
        ):
            out.append(_as_item(row))
        if i % 200 == 0:
            sys.stderr.write(f"  ... 已读 {i}/{len(source_ids)} 个来源\n")
            sys.stderr.flush()
    return out


def degree_and_basis(
    conn: sqlite3.Connection, notebook_id: str, object_ids: Sequence[str]
) -> Tuple[Dict[str, int], Counter]:
    """对给定对象逐个查度数(两个方向各一次索引查找),并统计其边的 relink basis。

    刻意逐 id 查而非全表扫 knowledge_relations:千万级边的全扫会把这个诊断本身
    变成一次事故。代价是只覆盖抽样节点,报告里已标注。
    """
    # 两条字面量 SQL 而不是把列名插进 f-string:列名虽来自本地常量元组、没有注入
    # 面,但动态拼 SQL 在这个仓库里是需要理由的形态,写死更省事也更好读。
    outgoing = ("SELECT evidence FROM knowledge_relations "
                "WHERE notebook_id = ? AND source_object_id = ?")
    incoming = ("SELECT evidence FROM knowledge_relations "
                "WHERE notebook_id = ? AND target_object_id = ?")
    deg: Dict[str, int] = {}
    basis = Counter()
    for i, oid in enumerate(object_ids, 1):
        n = 0
        for statement in (outgoing, incoming):
            for row in conn.execute(statement, (notebook_id, oid)):
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
    names = [it["name"] for it in items if it["name"]]
    total = len(names)
    _bar(f"[concept] 抽样 {total} 行")
    if not total:
        print("  (抽样里没有 concept)")
        return

    norm_counts = Counter(_norm(n) for n in names)
    dup_rows = sum(c for c in norm_counts.values() if c > 1)
    print(f"  唯一名 {len(norm_counts)} / 行数 {total}"
          f"  → 重名占用 {dup_rows} 行 ({_pct(dup_rows, total)})")
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
    names = [it["name"] for it in items if it["name"]]
    total = len(names)
    _bar(f"[claim] 抽样 {total} 行")
    if not total:
        print("  (抽样里没有 claim)")
        return
    norm_counts = Counter(_norm(n) for n in names)
    dup_rows = sum(c for c in norm_counts.values() if c > 1)
    print(f"  唯一命题 {len(norm_counts)} / 行数 {total}"
          f"  → 重复占用 {dup_rows} 行 ({_pct(dup_rows, total)})")
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
                  sampled: int, pool: int) -> None:
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
    for otype in sorted(by_type_total):
        print(f"    {otype:<10} {by_type_zero[otype]:6d} / {by_type_total[otype]:<6d}"
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


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="kg_quality_audit.py",
        description="KG 抽取质量离线审计(只读、零 LLM、零写库)。",
    )
    ap.add_argument("--db", default=DEFAULT_DB, help=f"SQLite 库路径(默认 {DEFAULT_DB})")
    ap.add_argument("--notebook", default=None,
                    help="要审计的 notebook id;不给则列出候选并选来源最多的那个")
    ap.add_argument("--sources", type=int, default=DEFAULT_SOURCE_SAMPLE, metavar="K",
                    help=f"随机抽 K 个来源做内容分析(默认 {DEFAULT_SOURCE_SAMPLE};"
                         "0 = 全量,千万级库会很慢)")
    ap.add_argument("--degree-sample", type=int, default=DEFAULT_DEGREE_SAMPLE,
                    metavar="N",
                    help=f"对其中 N 个节点查度数与边来源(默认 {DEFAULT_DEGREE_SAMPLE};0 = 全部抽样节点)")
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
        comp = type_composition(conn, target)
        grand = sum(n for _t, n in comp)
        if not grand:
            raise SystemExit("该 notebook 没有任何 KG 对象。")
        for otype, n in comp:
            mark = "" if otype in KG_TYPES else "   (自定义类型)"
            print(f"  {otype:<16} {n:>12,}  {_pct(n, grand)}{mark}")
        print(f"  {'合计':<16} {grand:>12,}")
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
        if n_sourceless:
            print(f"  不挂来源的对象: {n_sourceless:,} 个 ({_pct(n_sourceless, grand)} 的全库)"
                  " —— 来自晋升 / Memory→KG 写路径")
            if full:
                print("    全量模式:已纳入下面的分析(它们没有来源,故不参与 DF 统计)。")
            else:
                print("    ⚠ 抽样模式按来源抽样,这部分一个都抽不到,未纳入下面的分析。")
                print("      要覆盖它们:重跑并加 --sources 0。")

        sys.stderr.write(f"  正在读取 {len(source_ids)} 个来源的对象...\n")
        sys.stderr.flush()
        items = load_objects(conn, target, source_ids)
        n_sourced = len(items)
        if full and n_sourceless:
            items += load_sourceless_objects(conn, target)
            print(f"  读到 {n_sourced:,} 个挂来源的对象 + "
                  f"{len(items) - n_sourced:,} 个不挂来源的对象 = {len(items):,}")
        else:
            print(f"  读到 {len(items):,} 个对象")
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
                      len(picked), len(pool))

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
