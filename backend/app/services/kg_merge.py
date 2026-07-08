"""Pure cross-document Concept clustering. No DB/IO. Vectorized cosine over
one representative vector per name-seed cluster (well under O(N^2) of members).
confirmed pairs force-union; rejected pairs block.

Note — transitive-reject limitation (v1):
  ``rejected`` suppresses the DIRECT vector edge between two seeds, but does
  NOT guarantee they never co-cluster.  A transitive chain (A–C and B–C both
  ≥ hi) can still merge a rejected A–B pair.  This is an accepted v1
  limitation.
"""
from __future__ import annotations
import logging
import re
import unicodedata
from collections import Counter
from typing import Dict, Iterable, List, Set, FrozenSet

import numpy as np

_MAX_REPS = 4000  # sanity constant; no longer causes skipping the vector tier
_log = logging.getLogger(__name__)

_ALIASES = {
    "vco": "voltage controlled oscillator",
    "pll": "phase locked loop",
    "lna": "low noise amplifier",
    "mos": "mos transistor",
    "mosfet": "mos transistor",
    "bjt": "bipolar junction transistor",
    "opamp": "op amp",
    "op amp": "op amp",
}


# "Full (ACR)" shape: capture the part before the paren and the paren token.
_PAREN_ACRONYM_RE = re.compile(r"^(.*\S)\s*\(([^)]+)\)\s*$")
# Cheap sanity guard: paren token is a single run of letters/digits (no spaces).
_ACRONYM_TOKEN_RE = re.compile(r"^[A-Za-z0-9]+$")


def _is_initialism_of(acr: str, full: str) -> bool:
    """True iff ``acr`` is the initialism of ``full`` — the concatenated first
    characters of full's words (split on space/-/_) equal acr's letters/digits.
    This is what separates a genuine abbreviation ("CSA" of "Compressed Sparse
    Attention", "MoE" of "Mixture-of-Experts") from a qualifier/discriminator
    ("LP" of "Filter", "n" of "Channel", "v3" of "Type") that merely happens to
    be short or upper-case. Only initialisms are safe to strip/alias."""
    initials = "".join(w[0] for w in re.split(r"[\s\-_]+", full or "") if w).lower()
    acr_clean = re.sub(r"[^a-z0-9]+", "", (acr or "").lower())
    return bool(initials) and initials == acr_clean


def _strip_paren_acronym(name: str) -> str:
    """If ``name`` is "Full (ACR)" where ACR is the initialism of Full, return
    "Full" (the part before the paren). Otherwise return ``name`` unchanged.
    Qualifier parens (LP/HP/n/p/v3) are NOT initialisms of the head → left
    intact, so e.g. "Filter (LP)" and "Filter (HP)" stay distinct."""
    m = _PAREN_ACRONYM_RE.match(name or "")
    if not m:
        return name
    head, tok = m.group(1), m.group(2)
    if _ACRONYM_TOKEN_RE.match(tok) and _is_initialism_of(tok, head):
        return head
    return name


def _norm(name: str) -> str:
    # NFKC 先行:中文语料常见全角拉丁/数字/括号(（）ＡＢＣ１２３)折到 ASCII,
    # 让 acronym 剥离与 _ALIASES 能看见;纯 ASCII 输入是恒等变换。
    folded = unicodedata.normalize("NFKC", name or "")
    stripped = _strip_paren_acronym(folded)
    # Unicode \w 保留 CJK/希腊/带音标字母 —— 旧 [^a-z0-9+/ ] 把纯中文名清成空
    # seed,全库此类实体确定性塌缩进同一个 "K-" 簇(实测中文库 54% concept)。
    # 纯 ASCII 名输出与旧类逐字节相同(下划线两版都归并为空格)。
    cleaned = re.sub(r"[^\w+/ ]+", " ", stripped.strip().lower())
    cleaned = re.sub(r"[\s\-_]+", " ", cleaned).strip()
    return _ALIASES.get(cleaned, cleaned)


# 聚类算法版本:纳入 _cluster_input_version 的组成部分。凡是改变 seed/聚类语义的
# 代码修改(归一化规则、哨兵策略、护栏)都必须 bump —— 数据版本看不见纯代码变更,
# 不 bump 则已部署库的「刷新图谱」会被版本闸静默跳过、修复永不生效。
# v2: Unicode-safe _norm(NFKC+\w) + seed_or_unique 空seed哨兵(2026-07-08)。
CLUSTER_ALGO_VERSION = 2


def seed_or_unique(seed: str, object_id: str) -> str:
    """空/退化 seed(符号-only 名)绝不共簇:回退为按对象唯一的哨兵 seed。
    "~" 会被 _norm 清洗掉,真实名字的 seed 不可能以 "~" 开头 → 无碰撞。
    宁可不并,不可全并(旧行为:全部空 seed 共享 canonical "K-")。

    无碰撞保证仅对 `_norm`-族归一化器成立(它们清洗掉 "~");`_norm_formula`
    保留所有非空白字符,其 seed 理论上可含 "~"(碰撞需某公式全文恰为
    "~"+另一对象的完整 id,实际不可达)。"""
    return seed if seed else f"~{object_id}"


def build_acronym_alias_map(names: Iterable[str]) -> Dict[str, str]:
    """For every "Full (ACR)" name where ACR is the initialism of Full, map the
    acronym's seed to the expansion's seed: ``{_norm(ACR): _norm(Full)}``. Lets
    a bare "ACR" elsewhere in the batch be redirected onto the expansion's
    cluster. Deterministic on conflict: if one acronym is the initialism of two
    distinct expansions, the lexicographically smallest expansion seed wins
    (never last-writer-wins → result is independent of input order)."""
    alias: Dict[str, str] = {}
    for name in names:
        m = _PAREN_ACRONYM_RE.match(name or "")
        if not m:
            continue
        head, tok = m.group(1), m.group(2)
        if not _ACRONYM_TOKEN_RE.match(tok) or not _is_initialism_of(tok, head):
            continue
        acr_seed, exp_seed = _norm(tok), _norm(head)
        if not acr_seed or not exp_seed or acr_seed == exp_seed:
            continue
        prev = alias.get(acr_seed)
        if prev is None or exp_seed < prev:
            alias[acr_seed] = exp_seed
    return alias


def _norm_statement(s: str) -> str:
    """Claim/step text normalizer: lowercase, drop punctuation, collapse whitespace."""
    cleaned = re.sub(r"[^\w\s]+", " ", (s or "").lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def _norm_formula(s: str) -> str:
    """Formula normalizer: drop ALL whitespace, lowercase (expression identity)."""
    return re.sub(r"\s+", "", (s or "").lower())


def _steps_signature(payload: dict) -> str:
    steps = (payload or {}).get("steps")
    if not isinstance(steps, list):
        return ""
    names = sorted(_norm_statement(st.get("name", ""))
                   for st in steps if isinstance(st, dict) and st.get("name"))
    return "|".join(n for n in names if n)


def seed_concept(obj) -> str:
    """Seed function for concepts. obj may be a dict (production) or str (test shorthand)."""
    if isinstance(obj, str):
        return _norm(obj)
    return _norm(obj.get("name", ""))


def seed_claim(obj) -> str:
    """Seed function for claims. obj may be a dict (production) or str (test shorthand)."""
    if isinstance(obj, str):
        return _norm_statement(obj)
    return _norm_statement(obj.get("name", ""))


def seed_formula(obj) -> str:
    """Seed function for formulas. obj may be a dict (production) or str (test shorthand)."""
    if isinstance(obj, str):
        return _norm_formula(obj)
    return _norm_formula(obj.get("name", ""))


def seed_procedure(obj) -> str:
    """Seed function for procedures. obj may be a dict (production) or str (test shorthand)."""
    if isinstance(obj, str):
        return _norm_statement(obj)
    nm = _norm_statement(obj.get("name", ""))
    sig = _steps_signature(obj.get("payload") or {})
    return f"{nm}#{sig}" if sig else nm


_CONTRAST_GROUPS = [
    {"single", "double"}, {"low", "high"}, {"n", "p"}, {"nmos", "pmos"},
    {"series", "shunt"}, {"voltage", "current"}, {"positive", "negative"},
    {"input", "output"}, {"forward", "reverse"},
    {"drain", "source", "gate", "bulk", "body"},
    {"first", "second", "third", "fourth"}, {"upper", "lower"},
    {"even", "odd"}, {"internal", "external"}, {"inverting", "noninverting"},
]


def _discriminative_conflict(name_a: str, name_b: str) -> bool:
    """两个规范名仅各差一个 token 且该对差异 token 属同一对立组 → 视为不同变体, 禁止合并。"""
    ta, tb = _norm(name_a).split(), _norm(name_b).split()
    only_a = list((Counter(ta) - Counter(tb)).elements())
    only_b = list((Counter(tb) - Counter(ta)).elements())
    if len(only_a) == 1 and len(only_b) == 1 and only_a[0] != only_b[0]:
        for g in _CONTRAST_GROUPS:
            if only_a[0] in g and only_b[0] in g:
                return True
    return False


# Sharding floor: max_reps below this is treated as "too small" and ignored — a
# single global hnsw is built instead (hnswlib scales to millions). Sharding is
# an extreme-OOM fallback only; it drops cross-shard synonym pairs, so it must
# never engage at ordinary sizes. Callers pass kg_cluster_rep_ann_max (default
# 2M) which is far above this floor; only a deliberately tiny cap disables it.
# Set well above the old _MAX_REPS (4000) so no real caller silently shards.
_MIN_SHARD_CAP = 20_000


def _ann_candidates(seeds: List[str], reps: Dict[str, "np.ndarray"],
                    k: int = 5, lo: float = 0.82,
                    max_reps: int | None = None,
                    ann_threads: int = 1) -> List[tuple]:
    """hnswlib 余弦 top-k 近邻候选(sim≥lo), 去重无序对。O(N log N)。
    reps: seed -> 代表向量(未归一化亦可, cosine 空间内部归一)。
    默认单个全局 hnsw 索引(不分片), 消除跨片丢对。
    max_reps: 仅作极端 OOM 兜底 —— 唯有 max_reps ≥ _MIN_SHARD_CAP 且 n 超过它时
    才分片建索引(每片自洽, 跨片同义对会丢失, 有 WARNING); 过小的 max_reps 被忽略。
    """
    import math
    import hnswlib
    idx_seeds = [s for s in seeds if s in reps]
    n = len(idx_seeds)
    if n < 2:
        return []

    def _run_shard(shard_seeds: List[str]) -> List[tuple]:
        """Build one hnswlib index over shard_seeds and return deduped (a,b,sim) pairs."""
        sn = len(shard_seeds)
        if sn < 2:
            return []
        Mshard = np.asarray([reps[s] for s in shard_seeds], dtype=np.float32)
        dim = int(Mshard.shape[1])
        index = hnswlib.Index(space="cosine", dim=dim)
        index.init_index(max_elements=sn, ef_construction=200, M=16, random_seed=42)
        index.set_num_threads(max(1, ann_threads))
        index.add_items(Mshard, np.arange(sn))
        index.set_ef(max(64, k + 32))
        kk = min(k + 1, sn)
        labels, distances = index.knn_query(Mshard, k=kk)
        local_out: List[tuple] = []
        local_seen: set = set()
        for i in range(sn):
            for lab, dist in zip(labels[i], distances[i]):
                j = int(lab)
                if j == i:
                    continue
                sim = 1.0 - float(dist)
                if sim < lo:
                    continue
                a, b = (i, j) if i < j else (j, i)
                if (a, b) in local_seen:
                    continue
                local_seen.add((a, b))
                local_out.append((shard_seeds[a], shard_seeds[b], sim))
        return local_out

    # Default path: single global index over all seeds — no cross-shard loss.
    # Sharding only kicks in as an OOM fallback with a realistically large cap.
    shard_enabled = max_reps is not None and max_reps >= _MIN_SHARD_CAP
    if not shard_enabled or n <= max_reps:
        return _run_shard(idx_seeds)

    # OOM fallback: n exceeds a large cap — build one index per shard of size <= max_reps.
    n_shards = math.ceil(n / max_reps)
    _log.warning(
        "rep-ANN sharding: %d seeds exceeds cap %d; building in %d shards "
        "(cross-shard synonym pairs may be missed)",
        n, max_reps, n_shards,
    )
    out: List[tuple] = []
    seen: set = set()
    for shard_idx in range(n_shards):
        start = shard_idx * max_reps
        end = min(start + max_reps, n)
        shard_seeds = idx_seeds[start:end]
        for a, b, sim in _run_shard(shard_seeds):
            key = (a, b) if a < b else (b, a)
            if key not in seen:
                seen.add(key)
                out.append((a, b, sim))
    return out


def _star_groups(seeds: List[str], members_count: Dict[str, int],
                 edges: List[tuple], hi: float) -> Dict[str, str]:
    """贪心星型: 按成员数降序, 未分配 seed 作锚点, 认领其 ≥hi 直接邻居中未分配者。
    只允许"锚点—直接邻居", 不允许锚点间再链 → 簇直径有界, 无链式大簇。
    返回 seed -> anchor。O(N log N + N·k)。"""
    adj: Dict[str, List[tuple]] = {}
    for a, b, sim in edges:
        if sim >= hi:
            adj.setdefault(a, []).append((b, sim))
            adj.setdefault(b, []).append((a, sim))
    order = sorted(seeds, key=lambda s: (-members_count.get(s, 0), s))
    assigned: Dict[str, str] = {}
    for s in order:
        if s in assigned:
            continue
        assigned[s] = s
        for nb, _sim in adj.get(s, []):
            if nb not in assigned:
                assigned[nb] = s
    return assigned


class _UF:
    def __init__(self, items): self.p = {x: x for x in items}
    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]; x = self.p[x]
        return x
    def union(self, a, b): self.p[self.find(a)] = self.find(b)


def _seed_with_alias(obj, seed_fn, alias_map: Dict[str, str]) -> str:
    """Compute seed via ``seed_fn``, then redirect bare-acronym seeds onto their
    expansion (alias_map). Only redirects when the seed equals ``_norm(name)`` —
    i.e. a plain concept seed — so a richer seed (procedure name#steps-signature)
    is never collapsed by a coincidental acronym key."""
    name = obj.get("name", "") if isinstance(obj, dict) else obj
    base = seed_fn(obj)
    nm = _norm(name)
    if base == nm and nm in alias_map:
        return alias_map[nm]
    return base


def cluster_seeds(
    seeds: List[str],
    reps: Dict[str, np.ndarray],
    members_count: Dict[str, int],
    seed_first_name: Dict[str, str],
    confirmed: Set[FrozenSet[str]],
    rejected: Set[FrozenSet[str]],
    *,
    conflict_fn=None,
    id_prefix: str = "K-",
    hi: float = 0.94,
    lo: float = 0.82,
    top_k: int = 5,
    max_pending: int = 1000,
    rep_ann_max: int | None = None,
    ann_threads: int = 1,
) -> dict:
    """seed 级聚类核心(随 #seeds 有界)。confirmed/rejected 为 seed 对(frozenset)。
    rep_ann_max: 传给 _ann_candidates 的分片上限(None=不分片)。
    返回 {seed_to_canonical, canonical_names, auto_candidates, pending, pending_seeds, capped}。"""
    uf = _UF(seeds)
    for pair in confirmed:
        if len(pair) != 2:
            continue
        a, b = tuple(pair)
        if a in uf.p and b in uf.p:
            uf.union(a, b)
    rej = {frozenset(p) for p in rejected}
    raw = _ann_candidates(seeds, reps, k=top_k, lo=lo, max_reps=rep_ann_max,
                          ann_threads=ann_threads)
    cand = []
    for a, b, sim in raw:
        if rej and frozenset((a, b)) in rej:
            continue
        if conflict_fn and conflict_fn(seed_first_name.get(a, a), seed_first_name.get(b, b)):
            continue
        cand.append((a, b, sim))
    star = _star_groups(seeds, members_count, cand, hi)
    auto_set = {frozenset((nb, anc)) for nb, anc in star.items() if nb != anc}
    groups: Dict[str, List[str]] = {}
    for s in seeds:
        groups.setdefault(uf.find(s), []).append(s)
    canon_id, canon_name = {}, {}
    for root, grp in groups.items():
        best = max(grp, key=lambda s: members_count.get(s, 0))
        cid = id_prefix + min(grp)
        for s in grp:
            canon_id[s] = cid
        canon_name[cid] = seed_first_name[best]
    auto_candidates = [(canon_id[a], canon_id[b], sim) for a, b, sim in cand
                       if sim >= hi and frozenset((a, b)) in auto_set and canon_id[a] != canon_id[b]]
    pending = [(canon_id[a], canon_id[b], sim) for a, b, sim in cand
               if sim < hi and canon_id[a] != canon_id[b]]
    pending.sort(key=lambda t: t[2], reverse=True)
    pending_seeds = [(a, b, canon_id[a], canon_id[b], sim) for a, b, sim in cand
                     if sim < hi and canon_id[a] != canon_id[b]]
    pending_seeds.sort(key=lambda t: t[4], reverse=True)
    was_capped = len(pending) > max_pending
    return {"seed_to_canonical": canon_id, "canonical_names": canon_name,
            "auto_candidates": auto_candidates, "pending": pending[:max_pending],
            "pending_seeds": pending_seeds[:max_pending], "capped": was_capped}


def cluster_objects(
    objects: List[dict],
    vectors: Dict[str, List[float]],
    confirmed: Set[FrozenSet[str]],
    rejected: Set[FrozenSet[str]],
    *,
    seed_fn,
    conflict_fn=None,
    id_prefix: str = "K-",
    hi: float = 0.94,
    lo: float = 0.82,
    top_k: int = 5,
    max_pending: int = 1000,
) -> dict:
    """Generic cross-document clustering (concept/claim/formula/procedure).
    seed_fn(obj)->str 决定精确合并 key;conflict_fn(name_a,name_b)->bool 为护栏(None=不拦);
    id_prefix 决定 canonical_id 前缀(各类型隔离)。其余算法(ANN→护栏→星型→三档分流→
    confirmed/rejected force-union/block)与原 cluster_concepts 完全一致。"""
    # Acronym aliasing: redirect a bare-acronym seed onto its expansion's seed
    # when an "Expansion (ACR)" name also appears in this batch. seed_fn already
    # strips trailing parenthetical acronyms (via _norm), so "Full (ACR)" and
    # "Full" coincide; this only catches the standalone "ACR" form. We only
    # redirect when the object's seed is a pure _norm(name) (i.e. concepts) so a
    # richer seed (e.g. procedure name#steps-signature) is never clobbered.
    alias_map = build_acronym_alias_map([c.get("name", "") for c in objects])
    seed_of = {c["object_id"]: seed_or_unique(_seed_with_alias(c, seed_fn, alias_map),
                                              c["object_id"]) for c in objects}
    seeds = sorted(set(seed_of.values()))

    seed_first_name: Dict[str, str] = {}
    for c in objects:
        s = seed_of[c["object_id"]]
        if s not in seed_first_name:
            seed_first_name[s] = c.get("name", "")

    members: Dict[str, List[str]] = {}
    for c in objects:
        members.setdefault(seed_of[c["object_id"]], []).append(c["object_id"])

    reps: Dict[str, np.ndarray] = {}
    for s in seeds:
        vs = [vectors[o] for o in members[s] if o in vectors]
        if vs:
            reps[s] = np.mean(np.asarray(vs, dtype=np.float32), axis=0)

    members_count = {s: len(lst) for s, lst in members.items()}
    # ann_threads 故意不传:此 cluster_seeds 调用属 legacy(cluster_objects/cluster_concepts
    # 仅测试在用,rebuild_unified_kg 直接调 cluster_seeds 并已传 kg_cluster_ann_threads)。
    # 若将来把生产重建改指到这里,记得把按核解析的线程数一并透传,否则会静默退回单核。
    sd = cluster_seeds(seeds, reps, members_count, seed_first_name, confirmed, rejected,
                       conflict_fn=conflict_fn, id_prefix=id_prefix, hi=hi, lo=lo,
                       top_k=top_k, max_pending=max_pending)
    canon_id = sd["seed_to_canonical"]
    cluster_map = {c["object_id"]: canon_id[seed_of[c["object_id"]]] for c in objects}
    names = {c["object_id"]: sd["canonical_names"][cluster_map[c["object_id"]]] for c in objects}
    return {"cluster_map": cluster_map, "canonical_names": names,
            "auto_candidates": sd["auto_candidates"], "pending": sd["pending"],
            "pending_seeds": sd["pending_seeds"], "capped": sd["capped"]}


def cluster_concepts(
    concepts: List[dict],
    vectors: Dict[str, List[float]],
    confirmed: Set[FrozenSet[str]],
    rejected: Set[FrozenSet[str]],
    hi: float = 0.94,
    lo: float = 0.82,
    top_k: int = 5,
    max_pending: int = 1000,
) -> dict:
    """精确同名 + 已确认对 force-union; 向量候选经 ANN→护栏→星型, 但**不自动 union**:
    ≥hi 进 auto_candidates(LLM 兜底), [lo,hi) 进 pending(人工)。全程 sub-quadratic。
    薄包装: 委托 cluster_objects,使用 _norm seed + _discriminative_conflict 护栏 + K- 前缀。
    confirmed/rejected 中的名称先用 _norm 标准化,与 cluster_objects 期待的 seed 格式对齐。"""
    norm_confirmed = {frozenset(_norm(n) for n in p) for p in confirmed}
    norm_rejected = {frozenset(_norm(n) for n in p) for p in rejected}
    return cluster_objects(
        concepts, vectors, norm_confirmed, norm_rejected,
        seed_fn=lambda c: _norm(c.get("name", "")),
        conflict_fn=_discriminative_conflict,
        id_prefix="K-",
        hi=hi, lo=lo, top_k=top_k, max_pending=max_pending,
    )


def detect_bridge_candidates(new_items, new_vectors, existing_items, existing_vectors,
                             existing_cluster_map, rejected, *, hi=0.94, lo=0.82, top_k=5):
    """新对象 embedding 对已有对象做余弦,命中 ≥lo 且落在不同 canonical 簇、且对未被 rejected →
    桥接候选。返回 [{canonical_a, canonical_b, score}](a<b 去序)。new-vs-existing 分块 numpy,
    不动已有。new_items/existing_items: [{'object_id','name'}];vectors: {object_id: [float]}。"""
    import numpy as np
    if not new_vectors or not existing_vectors:
        return []
    ex_ids = [i["object_id"] for i in existing_items if i["object_id"] in existing_vectors]
    if not ex_ids:
        return []
    EX = np.asarray([existing_vectors[i] for i in ex_ids], dtype="float32")
    EX /= (np.linalg.norm(EX, axis=1, keepdims=True) + 1e-9)
    ex_cid = {i["object_id"]: existing_cluster_map.get(i["object_id"]) for i in existing_items}
    out, seen = [], set()
    for it in new_items:
        v = new_vectors.get(it["object_id"])
        if v is None:
            continue
        q = np.asarray(v, dtype="float32"); q /= (np.linalg.norm(q) + 1e-9)
        sims = EX @ q
        idx = np.argsort(-sims)[:top_k]
        my_cid = "K-" + seed_or_unique(_norm(it.get("name", "")), it["object_id"])
        for j in idx:
            s = float(sims[j])
            if s < lo:
                break
            other_cid = ex_cid.get(ex_ids[j])
            if not other_cid or other_cid == my_cid:
                continue
            a, b = sorted((my_cid, other_cid))
            if frozenset((a, b)) in rejected or (a, b) in seen:
                continue
            seen.add((a, b))
            out.append({"canonical_a": a, "canonical_b": b, "score": s})
    return out


def place_new_concepts(new_objects, existing_cluster_map, existing_canon_names,
                       *, seed_fn, id_prefix="K-"):
    """Tier-1 名种子放置:每个新对象按 seed_fn → canonical_id 追加到已有簇或建新簇。
    不重排任何已有对象(已有 canonical_id 不变 → 无簇分布漂移)。
    existing_cluster_map: {existing_object_id: canonical_id};existing_canon_names: {canonical_id: name}。
    返回 [{canonical_id, member_object_id, canonical_name}]。"""
    existing_cids = set(existing_cluster_map.values())
    # Acronym aliasing: redirect bare-acronym seeds onto an expansion's seed when
    # an "Expansion (ACR)" name is present among the new objects or existing
    # canonical names. Keeps a standalone "ACR" landing in the expansion cluster.
    alias_map = build_acronym_alias_map(
        [o.get("name", "") for o in new_objects] + list(existing_canon_names.values()))
    rows = []
    for o in new_objects:
        name = o.get("name", "")
        seed = seed_or_unique(_seed_with_alias(o, seed_fn, alias_map), o["object_id"])
        cid = f"{id_prefix}{seed}"
        canon_name = existing_canon_names.get(cid, name) if cid in existing_cids else name
        rows.append({"canonical_id": cid, "member_object_id": o["object_id"],
                     "canonical_name": canon_name})
    return rows


def derive_unified_graph(nodes: List[dict], edges: List[dict], cluster_map: Dict[str, str]) -> dict:
    """Rewire member-Concept endpoints to canonical ids; dedup edges. O(V+E).

    Only CONCEPT members are folded to canonical ids. cluster_map may also carry
    non-concept (claim/formula/procedure) entries (used for answer-context dedup),
    but the unified graph view folds concepts only — folding non-concept node ids
    on edges while keeping their nodes raw would create dangling edges."""
    concept_ids = {n["id"] for n in nodes if n["object_type"] == "concept"}
    def canon(oid): return cluster_map.get(oid, oid) if oid in concept_ids else oid
    seen_concept, out_nodes = set(), []
    for n in nodes:
        if n["object_type"] == "concept":
            cid = canon(n["id"])
            if cid in seen_concept:
                continue
            seen_concept.add(cid)
            out_nodes.append({**n, "id": cid})
        else:
            out_nodes.append(n)
    seen_edge, out_edges = set(), []
    for e in edges:
        s, t = canon(e["source_object_id"]), canon(e["target_object_id"])
        if s == t:
            continue
        key = (s, t, e["edge_type"])
        if key in seen_edge:
            continue
        seen_edge.add(key)
        out_edges.append({"source_object_id": s, "target_object_id": t, "edge_type": e["edge_type"]})
    return {"nodes": out_nodes, "edges": out_edges}


def limit_graph_by_degree(graph: dict, limit: int) -> dict:
    """Return the `limit` most-connected nodes (by degree) plus only the edges
    whose BOTH endpoints survive — the "core" subgraph shown before the user
    widens the range. Isolated (degree-0) nodes rank last, so they appear only
    when the full graph is requested. No-op when limit covers every node."""
    nodes = graph["nodes"]
    edges = graph["edges"]
    if limit is None or limit >= len(nodes):
        return {"nodes": nodes, "edges": edges}
    deg: Dict[str, int] = {}
    for e in edges:
        deg[e["source_object_id"]] = deg.get(e["source_object_id"], 0) + 1
        deg[e["target_object_id"]] = deg.get(e["target_object_id"], 0) + 1
    # stable sort: degree desc, preserving original order among equal degrees
    ranked = sorted(nodes, key=lambda n: deg.get(n["id"], 0), reverse=True)
    keep = {n["id"] for n in ranked[:limit]}
    kept_nodes = [n for n in nodes if n["id"] in keep]
    kept_edges = [e for e in edges
                  if e["source_object_id"] in keep and e["target_object_id"] in keep]
    return {"nodes": kept_nodes, "edges": kept_edges}
