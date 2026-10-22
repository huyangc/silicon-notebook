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
from collections import Counter
from dataclasses import dataclass, field
from typing import (
    Callable, Container, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence,
    Set, Tuple,
)

import numpy as np

from app.domain.kg_merge_seed import (
    _ACRONYM_TOKEN_RE,
    _ALIASES,
    _PAREN_ACRONYM_RE,
    _is_initialism_of,
    _norm,
    _norm_formula,
    _norm_statement,
    _steps_signature,
    _strip_paren_acronym,
    CONCEPT_SEED_VERSION,
    OVERRIDE_SEED_PREFIX,
    check_concept_seed_version,
    concept_seed_of,
    concept_seed_parts,
    concept_seed_v2,
    fold_decision,
    seed_claim,
    seed_concept,
    seed_formula,
    seed_procedure,
    seed_squash_key,
)

_MAX_REPS = 4000  # sanity constant; no longer causes skipping the vector tier
_log = logging.getLogger(__name__)

# _ALIASES / _PAREN_ACRONYM_RE / _ACRONYM_TOKEN_RE / _is_initialism_of /
# _strip_paren_acronym / _norm / _norm_statement / _norm_formula /
# _steps_signature / seed_concept / seed_claim / seed_formula /
# seed_procedure sunk to app.domain.kg_merge_seed in B3 (imported above,
# re-exported unchanged for this module's own callers and for external
# importers such as app.repositories.*.governance_store's seed_fn_for,
# app.services.knowledge_lifecycle, and the kg_merge test suite).


# 聚类算法版本:纳入 _cluster_input_version 的组成部分。凡是改变 seed/聚类语义的
# 代码修改(归一化规则、哨兵策略、护栏)都必须 bump —— 数据版本看不见纯代码变更,
# 不 bump 则已部署库的「刷新图谱」会被版本闸静默跳过、修复永不生效。
# v2: Unicode-safe _norm(NFKC+\w) + seed_or_unique 空seed哨兵(2026-07-08)。
# v3: rejected/deferred 是 canonical component 间的 cannot-link；pending 按
# canonical pair 去重，避免同一簇对通过另一个 seed 邻居反复进入审阅队列。
# v4 (KG-analysis findings PR-4, design 2026-10-19 §五 S1 + §4.4): concepts are
#   seeded in seed space 2 (``concept_seed_v2``: ORIGINAL whitespace / hyphen /
#   underscore dropped before punctuation turns into spaces, so "Q-Former" and
#   "Qformer" share ``qformer`` while "Llama 3.1" / "Llama 31" stay apart); the
#   first v2 full rebuild of a library recomputes its stored v1 merge decisions
#   into v2 seed pairs (``translate_v1_decisions``, collisions with a rejection
#   keep the old seed as ``~v1:<seed>``) and records the published space in
#   ``unified_kg_state.concept_seed_version``; the pending refresh gains the
#   squash-key candidate tier (``squash_pending_candidates``: one star per group
#   of canonicals whose seeds differ only in punctuation-made spaces). With the
#   merge code component (``cluster_code_version``) this bump is what makes an
#   upgraded library read "合并规则已更新，需要更新一次" instead of "无需更新".
#   Claim / formula / procedure seeds are unchanged.
CLUSTER_ALGO_VERSION = 4


def cluster_code_components(settings) -> tuple[int, int, int]:
    """``(embed_dim, runtime_dim, CLUSTER_ALGO_VERSION)``: the three components of
    the clustering input version that come from code and configuration, not
    from the data. ``KnowledgeLifecycleService._cluster_input_version`` folds
    exactly these, read exactly this way, into its hash, and
    ``cluster_code_version`` publishes them; one reader keeps the two from
    drifting. ``CLUSTER_ALGO_VERSION`` is read at call time (a test may patch
    the module attribute)."""
    from app.domain.vector_index import resolve_runtime_dim

    return (
        int(settings.embed_dim),
        resolve_runtime_dim(settings),
        int(CLUSTER_ALGO_VERSION),
    )


def cluster_code_version(settings) -> str:
    """The code component of a merge, as ``unified_kg_state.cluster_code_version``
    stores it (SQLite v97 / PostgreSQL 0077; design 2026-10-19 §五 S4):
    ``"algo=<CLUSTER_ALGO_VERSION>;embed_dim=<n>;runtime_dim=<n>"``.

    ``cluster_input_version`` is a sha1 that folds these in together with data
    counts, so nothing in a report can read the code part back out of it, and
    bumping ``CLUSTER_ALGO_VERSION`` does not mark a library dirty. Stored beside
    it at every merge (the rebuild end-write, and the inputs-unchanged skip path
    under a ``cluster_input_version`` equality guard), the plain text lets a
    reader compare the last merge's code with the running code without
    recomputing the input version's object and vector counts."""
    embed_dim, runtime_dim, algo = cluster_code_components(settings)
    return f"algo={algo};embed_dim={embed_dim};runtime_dim={runtime_dim}"


def seed_or_unique(seed: str, object_id: str) -> str:
    """空/退化 seed(符号-only 名)绝不共簇:回退为按对象唯一的哨兵 seed。
    "~" 会被 _norm 清洗掉,真实名字的 seed 不可能以 "~" 开头 → 无碰撞。
    宁可不并,不可全并(旧行为:全部空 seed 共享 canonical "K-")。

    无碰撞保证仅对 `_norm`-族归一化器成立(它们清洗掉 "~");`_norm_formula`
    保留所有非空白字符,其 seed 理论上可含 "~"(碰撞需某公式全文恰为
    "~"+另一对象的完整 id,实际不可达)。"""
    return seed if seed else f"~{object_id}"


def bridge_canonical_id(
    name: str, object_id: str, *, seed_version: int,
    override: FrozenSet[str] = frozenset(),
) -> str:
    """The canonical id a concept object is bridged under (Tier-2 bridge
    candidates, the ANN twin, and the Memory purge that removes candidates
    naming it), in seed space ``seed_version`` — the notebook's PUBLISHED
    space, the same one its placement uses:

      · v1: ``"K-" + seed_or_unique(_norm(name), object_id)``;
      · v2: ``"K-" + seed_or_unique(concept_seed_v2(name), object_id)``, or
        ``"K-~v1:" + <v1 seed>`` when the object's v1 seed is one of the
        notebook's override keys (``override``: the collision exception of
        the decision recompute, read off the stored v2 decisions).

    Never goes through the acronym alias redirect (``place_new_concepts``
    does), so it can name a cluster no row carries; for the same reason the
    override is matched on the UN-aliased v1 seed — the one the Memory purge
    can recompute from a name alone (``purge_bridge_canonical_ids``). One
    definition for every site."""
    version = check_concept_seed_version(seed_version)
    v1 = seed_or_unique(_norm(name or ""), object_id)
    if version == 1:
        return "K-" + v1
    if v1 in override:
        return "K-" + OVERRIDE_SEED_PREFIX + v1
    return "K-" + seed_or_unique(concept_seed_v2(name or ""), object_id)


def purge_bridge_canonical_ids(objects: Iterable[dict]) -> Dict[int, List[str]]:
    """For objects ``[{object_id, object_type, name}]`` about to be purged:
    every bridge id their concept objects can have been bridged under, per
    the seed space of the candidate rows that may carry it —
    ``{1: [v1 ids], 2: [v2 ids + override ids]}``. A candidate row is matched
    only by the ids of its OWN ``seed_version`` (``bridge_canonical_id``
    above, both forms computed from the name alone: the purge runs whatever
    space the notebook has published, and a library mid-switch holds rows of
    both). They may also be carried by a live shared cluster, so the purge
    acts on a merge candidate naming one only when no cluster row carries it.
    (The ids that can only have been minted from a purged object are
    recognised in SQL by ``memory_sql.cluster_seed_object_id``.)"""
    v1_ids: Set[str] = set()
    v2_ids: Set[str] = set()
    for item in objects:
        if item.get("object_type") != "concept":
            continue
        name, object_id = item.get("name") or "", item["object_id"]
        v1_ids.add(bridge_canonical_id(name, object_id, seed_version=1))
        v2_ids.add(bridge_canonical_id(name, object_id, seed_version=2))
        v1 = seed_or_unique(_norm(name), object_id)
        v2_ids.add(bridge_canonical_id(
            name, object_id, seed_version=2, override=frozenset({v1})))
    return {1: sorted(v1_ids), 2: sorted(v2_ids)}


def build_acronym_alias_maps(
    names: Iterable[str], seed_versions: Sequence[int],
) -> Dict[int, Dict[str, str]]:
    """``build_acronym_alias_map`` for several seed spaces in ONE pass over
    ``names`` (the full rebuild streams them from the database once)."""
    seed_fns = {
        check_concept_seed_version(v): (_norm if v == 1 else concept_seed_v2)
        for v in seed_versions
    }
    maps: Dict[int, Dict[str, str]] = {v: {} for v in seed_fns}
    for name in names:
        m = _PAREN_ACRONYM_RE.match(name or "")
        if not m:
            continue
        head, tok = m.group(1), m.group(2)
        if not _ACRONYM_TOKEN_RE.match(tok) or not _is_initialism_of(tok, head):
            continue
        for version, seed_fn in seed_fns.items():
            acr_seed, exp_seed = seed_fn(tok), seed_fn(head)
            if not acr_seed or not exp_seed or acr_seed == exp_seed:
                continue
            alias = maps[version]
            prev = alias.get(acr_seed)
            if prev is None or exp_seed < prev:
                alias[acr_seed] = exp_seed
    return maps


def build_acronym_alias_map(names: Iterable[str], *, seed_version: int) -> Dict[str, str]:
    """For every "Full (ACR)" name where ACR is the initialism of Full, map the
    acronym's seed to the expansion's seed, both in seed space
    ``seed_version``: ``{seed(ACR): seed(Full)}`` (v1: ``_norm``; v2:
    ``concept_seed_v2`` — the alias is matched on words, the values are squashed
    like every v2 seed). Lets a bare "ACR" elsewhere in the batch be redirected
    onto the expansion's cluster. Deterministic on conflict: if one acronym is
    the initialism of two distinct expansions, the lexicographically smallest
    expansion seed wins (never last-writer-wins → result is independent of
    input order). Claim / formula / procedure batches always pass 1."""
    version = check_concept_seed_version(seed_version)
    return build_acronym_alias_maps(names, (version,))[version]


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


def _seed_with_alias(obj, seed_fn, alias_map: Dict[str, str], *, seed_version: int) -> str:
    """Compute seed via ``seed_fn``, then redirect bare-acronym seeds onto their
    expansion (alias_map, built in the same ``seed_version``). Only redirects
    when the seed equals the plain concept seed of the name in that space —
    i.e. a plain concept seed — so a richer seed (procedure name#steps-signature)
    is never collapsed by a coincidental acronym key. Claim / formula /
    procedure seeds pass ``seed_version=1`` (their alias rule is unchanged)."""
    name = obj.get("name", "") if isinstance(obj, dict) else obj
    base = seed_fn(obj)
    nm = concept_seed_of(name, seed_version)
    if base == nm and nm in alias_map:
        return alias_map[nm]
    return base


def _v1_probe(keys: FrozenSet[str], v1_alias: Mapping[str, str]) -> FrozenSet[str]:
    """The squashed pre-alias texts (``concept_seed_parts``' second value) a
    name must have for its v1 seed to be able to equal one of ``keys``.

    A concept's v1 seed is ``v1_alias.get(n1, n1)`` with ``n1 =
    _ALIASES.get(cleaned, cleaned)`` and ``cleaned`` the v1 form, whose squash
    is the name's squashed pre-alias text. So the v1 seed is a key ``k`` only
    when ``cleaned`` is ``k``, an acronym whose expansion is ``k``, or a static
    alias key whose value is one of those two. A superset test: the caller
    still compares the exact v1 seed."""
    targets = set(keys)
    targets.update(acr for acr, exp in v1_alias.items() if exp in keys)
    probe = {t.replace(" ", "") for t in targets}
    probe.update(k.replace(" ", "") for k, v in _ALIASES.items() if v in targets)
    probe.discard("")
    return frozenset(probe)


class ConceptSeedSpace:
    """The final cluster seed of every concept of one batch in one notebook's
    concept seed space (``CONCEPT_SEED_VERSION`` for a full rebuild, the
    published ``concept_seed_version`` for incremental placement).

    ``seed(obj, object_id)`` = ``seed_or_unique`` of the space's seed after the
    batch's acronym alias redirect, except that in space 2 an object whose v1
    seed (with the v1 alias redirect) is in ``override`` keeps it as
    ``"~v1:" + <v1 seed>`` (the collision exception of
    ``translate_v1_decisions``). ``seed_and_key`` additionally returns that v1
    seed when it is in ``track`` (the keys of stored v1 decisions the first v2
    rebuild recomputes), else ``None``.

    The v1 seed is computed only for names whose squashed pre-alias text can
    lead to a key (``_v1_probe``), so a space with no override and no tracked
    key pays nothing over the plain v2 seed."""

    __slots__ = ("version", "alias", "v1_alias", "override", "track", "_probe")

    def __init__(
        self, version: int, alias: Dict[str, str], *,
        v1_alias: Optional[Dict[str, str]] = None,
        override: Iterable[str] = (), track: Iterable[str] = (),
    ) -> None:
        self.version = check_concept_seed_version(version)
        override = frozenset(override) if self.version != 1 else frozenset()
        track = frozenset(track) if self.version != 1 else frozenset()
        if (override or track) and v1_alias is None:
            raise ValueError("an override or tracked key needs the v1 alias map")
        self.alias = alias
        self.v1_alias = v1_alias or {}
        self.override = override
        self.track = track
        self._probe = _v1_probe(override | track, self.v1_alias) if (override or track) \
            else frozenset()

    @classmethod
    def from_names(
        cls, names: Iterable[str], *, version: int,
        override: Iterable[str] = (), track: Iterable[str] = (),
    ) -> "ConceptSeedSpace":
        version = check_concept_seed_version(version)
        override, track = frozenset(override), frozenset(track)
        need_v1 = version != 1 and bool(override or track)
        maps = build_acronym_alias_maps(names, (version, 1) if need_v1 else (version,))
        return cls(version, maps[version], v1_alias=maps.get(1) if need_v1 else None,
                   override=override, track=track)

    def v1_seed(self, name: str, object_id: str) -> str:
        """The object's seed in space 1 (with the v1 acronym redirect)."""
        n1 = _norm(name)
        return seed_or_unique(self.v1_alias.get(n1, n1), object_id)

    def seed_and_key(self, obj, object_id: str) -> Tuple[str, Optional[str]]:
        name = obj.get("name", "") if isinstance(obj, dict) else obj
        if self.version == 1:
            n1 = _norm(name)
            return seed_or_unique(self.alias.get(n1, n1), object_id), None
        s2, squashed = concept_seed_parts(name)
        seed = seed_or_unique(self.alias.get(s2, s2), object_id)
        if not self._probe or (squashed and squashed not in self._probe):
            return seed, None
        v1 = self.v1_seed(name, object_id)
        if v1 in self.override:
            seed = OVERRIDE_SEED_PREFIX + v1
        return seed, (v1 if v1 in self.track else None)

    def seed(self, obj, object_id: str) -> str:
        return self.seed_and_key(obj, object_id)[0]


_TYPE_SEED_FNS = {"claim": seed_claim, "formula": seed_formula, "procedure": seed_procedure}


class TypeSeedSpace:
    """Claim / formula / procedure seeds — the v1 rule, unchanged (no seed
    version): the type's seed function, the v1 acronym redirect of plain
    concept-shaped seeds, ``seed_or_unique``."""

    __slots__ = ("seed_fn", "alias")

    def __init__(self, object_type: str, names: Iterable[str]) -> None:
        self.seed_fn = _TYPE_SEED_FNS[object_type]
        self.alias = build_acronym_alias_map(names, seed_version=1)

    def seed_and_key(self, obj, object_id: str) -> Tuple[str, Optional[str]]:
        return seed_or_unique(
            _seed_with_alias(obj, self.seed_fn, self.alias, seed_version=1), object_id
        ), None

    def seed(self, obj, object_id: str) -> str:
        return self.seed_and_key(obj, object_id)[0]


def seed_space_for(
    object_type: str, names: Iterable[str], *, concept_seed_version: int,
    override: Iterable[str] = (), track: Iterable[str] = (),
):
    """The seed space of one object type for one batch of names: concepts in
    ``concept_seed_version`` (with ``override`` / ``track``), other types by
    their unchanged v1 rule. ``names`` feeds the acronym alias map(s)."""
    if object_type == "concept":
        return ConceptSeedSpace.from_names(
            names, version=concept_seed_version, override=override, track=track)
    return TypeSeedSpace(object_type, names)


def filter_pending_seeds_by_decisions(
    pending_seeds: List[tuple],
    seeds: List[str],
    confirmed: Set[FrozenSet[str]],
    rejected: Set[FrozenSet[str]],
) -> List[tuple]:
    """Revalidate precomputed pending rows against a live decision snapshot.

    Rebuild computes candidates before its long derivation tail. A manual or
    automated decision may commit before the final pending refresh; filtering
    again inside that refresh transaction prevents the stale candidate snapshot
    from republishing an already-decided component pair.
    """
    uf = _UF(seeds)
    for pair in confirmed:
        if len(pair) != 2:
            continue
        a, b = tuple(pair)
        if a in uf.p and b in uf.p:
            uf.union(a, b)
    rejected_pairs = {frozenset(pair) for pair in rejected}
    rejected_components: Set[FrozenSet[str]] = set()
    for pair in rejected_pairs:
        if len(pair) != 2:
            continue
        a, b = tuple(pair)
        if a not in uf.p or b not in uf.p:
            continue
        ra, rb = uf.find(a), uf.find(b)
        if ra != rb:
            rejected_components.add(frozenset((ra, rb)))
    kept: List[tuple] = []
    for row in pending_seeds:
        a, b = row[0], row[1]
        if a not in uf.p or b not in uf.p:
            continue
        if frozenset((a, b)) in rejected_pairs:
            continue
        ra, rb = uf.find(a), uf.find(b)
        if ra == rb or frozenset((ra, rb)) in rejected_components:
            continue
        kept.append(row)
    return kept


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
    # A human rejects the two *displayed canonical components*, not merely the
    # one ANN seed edge that happened to represent them.  Project every stable
    # rejected seed pair through the confirmed union-find so another member of
    # either component cannot immediately recreate the same visible candidate.
    # A rejected pair already inside one confirmed component is contradictory
    # historical input; confirmed remains authoritative, as before.
    rejected_components: Set[FrozenSet[str]] = set()
    for pair in rej:
        if len(pair) != 2:
            continue
        a, b = tuple(pair)
        if a not in uf.p or b not in uf.p:
            continue
        ra, rb = uf.find(a), uf.find(b)
        if ra != rb:
            rejected_components.add(frozenset((ra, rb)))
    raw = _ann_candidates(seeds, reps, k=top_k, lo=lo, max_reps=rep_ann_max,
                          ann_threads=ann_threads)
    cand = []
    for a, b, sim in raw:
        if rej and frozenset((a, b)) in rej:
            continue
        if rejected_components and frozenset((uf.find(a), uf.find(b))) in rejected_components:
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
    # Several ANN seed edges can connect the same two confirmed components.
    # The UI asks one component-level question, so retain one deterministic,
    # highest-scoring representative rather than enqueueing visually identical
    # rows that reappear after the first one is decided.
    pending_by_component: Dict[FrozenSet[str], tuple] = {}
    for a, b, sim in cand:
        ca, cb = canon_id[a], canon_id[b]
        if sim >= hi or ca == cb:
            continue
        key = frozenset((ca, cb))
        row = (a, b, ca, cb, sim)
        previous = pending_by_component.get(key)
        if previous is None or sim > previous[4] or (
            sim == previous[4] and tuple(sorted((a, b))) < tuple(sorted(previous[:2]))
        ):
            pending_by_component[key] = row
    pending_seeds = sorted(
        pending_by_component.values(),
        key=lambda t: (-t[4], tuple(sorted((t[0], t[1])))),
    )
    pending = [(ca, cb, sim) for _a, _b, ca, cb, sim in pending_seeds]
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
    # Legacy (test-only) helper: the acronym redirect stays in seed space 1,
    # as do the ``_norm`` seeds ``cluster_concepts`` passes. The production
    # full rebuild seeds through ``ConceptSeedSpace`` (rebuild_unified_kg).
    alias_map = build_acronym_alias_map([c.get("name", "") for c in objects],
                                        seed_version=1)
    seed_of = {c["object_id"]: seed_or_unique(_seed_with_alias(c, seed_fn, alias_map,
                                                               seed_version=1),
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
                             existing_cluster_map, rejected, *, seed_version: int,
                             override: FrozenSet[str] = frozenset(),
                             hi=0.94, lo=0.82, top_k=5):
    """新对象 embedding 对已有对象做余弦,命中 ≥lo 且落在不同 canonical 簇、且对未被 rejected →
    桥接候选。返回 [{canonical_a, canonical_b, score}](a<b 去序)。new-vs-existing 分块 numpy,
    不动已有。new_items/existing_items: [{'object_id','name'}];vectors: {object_id: [float]}。
    新对象的桥接 id 按库**已发布**的种子空间算(``seed_version`` / ``override``,
    见 ``bridge_canonical_id``),与本次安置用同一个空间。"""
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
        my_cid = bridge_canonical_id(it.get("name", ""), it["object_id"],
                                     seed_version=seed_version, override=override)
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


def placement_seed_space(
    object_type: str, new_objects: Sequence[dict],
    existing_canon_names: Mapping[str, str], *, concept_seed_version: int,
    override: Iterable[str] = (),
):
    """The seed space ``place_new_concepts`` places one batch in: the acronym
    alias map is built from the new objects' names plus every existing
    canonical name of the type (a bare "ACR" lands in the cluster of an
    existing "Expansion (ACR)"); concepts in the notebook's PUBLISHED
    ``concept_seed_version`` with its ``override`` keys, other types by their
    unchanged rule."""
    return seed_space_for(
        object_type,
        [o.get("name", "") for o in new_objects] + list(existing_canon_names.values()),
        concept_seed_version=concept_seed_version, override=override,
    )


def place_new_concepts(new_objects, existing_canon_names, *, seed_space, id_prefix="K-"):
    """Tier-1 名种子放置:每个新对象按 ``seed_space``(``placement_seed_space`` 建的
    该类型种子空间:已发布的概念种子空间 + 别名表 + 覆盖键)→ canonical_id 追加到
    已有簇或建新簇。不重排任何已有对象(已有 canonical_id 不变 → 无簇分布漂移)。
    existing_canon_names: {canonical_id: canonical_name}(本 object_type 的既有簇)。
    返回 [{canonical_id, member_object_id, canonical_name}]。

    此函数**不再收 cluster_map**(PR-C 有界化)。旧签名多收一份
    ``existing_cluster_map``({member_object_id: canonical_id},全库全类型),
    只用来算 ``existing_cids = set(values())`` 并守 ``... if cid in existing_cids
    else name``。那个守卫在任何满足 ``keys(existing_canon_names) ⊆
    values(existing_cluster_map)`` 的输入上都是**恒等的**,逐位一致证明分三支:

      A. cid ∈ keys(canon_names) ⟹ cid ∈ existing_cids(前提的子集关系),
         旧 = ``canon_names[cid]``,新 = ``canon_names[cid]``。
      B. cid ∉ keys(canon_names) 但 ∈ existing_cids,旧 = ``.get(cid, name)``
         = name(键不在),新 = name。
      C. cid ∉ existing_cids ⟹ 由前提 cid ∉ keys(canon_names),旧 = name(短路),
         新 = ``.get(cid, name)`` = name。

    三支都相等,故 ``.get(cid, name)`` 单独就等价于旧的两段式表达式。生产上前提成立:
    两份数据读自同一张 ``concept_clusters``,cluster_map 不按 object_type 过滤而
    canon_names 只取本类型切片,每个本类型 canonical_id 至少有一行成员 → 必是
    cluster_map 的一个 value。

    两处已知的、能让前提在**单次调用内**不成立的情形,都不改变结论:

      · 同一个 member_object_id 同时挂在两个 object_type 的簇下 —— cluster_map 的
        dict 推导会丢掉先出现的那条 canonical_id。member 是 knowledge_objects 的
        行 id、每行恰有一个 object_type,且融合入口已先扫掉悬空成员行,所以这只
        可能来自数据损坏。
      · 两份数据并非同一个快照 —— master 的 cmap 走**版本缓存** ``cluster_map()``,
        而 canon_names 是当场从库里读的切片,并发写入下两者本就可能错开一个代次。
        也就是说前提在旧实现里同样只是「几乎总成立」。新写法在这种错开下取的是
        **更一致的那一侧**(只信当场读到的 canon_names),旧写法则会因为陈旧 cmap
        里缺一个 cid 而把已有簇名丢掉、改写成新对象自己的 name。

    仍留的全库依赖:``existing_canon_names.values()`` 要整份喂别名表
    (``placement_seed_space``)——见 knowledge_lifecycle ``incremental_fuse_source``
    里的登记说明,那一读**无法**安全收窄。
    """
    rows = []
    for o in new_objects:
        name = o.get("name", "")
        seed = seed_space.seed(o, o["object_id"])
        cid = f"{id_prefix}{seed}"
        rows.append({"canonical_id": cid, "member_object_id": o["object_id"],
                     "canonical_name": existing_canon_names.get(cid, name)})
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


# ===========================================================================
# Stored-decision recompute (PR-4, design 2026-10-19 §五 S1 「实施细化」 steps 1–5)
# ===========================================================================

DECIDED_MERGE_STATUSES = ("confirmed", "rejected", "deferred")
_APART_STATUSES = ("rejected", "deferred")


@dataclass(frozen=True)
class StoredDecision:
    """One stored v1 decision row, as the recompute reads it: ``key_a`` /
    ``key_b`` are its decision keys (``seed_x``, or the canonical id without
    ``K-`` when the seed is empty — ``decided_seed_pairs_from``'s fallback)."""

    row_id: str
    key_a: str
    key_b: str
    status: str


@dataclass
class DecisionTranslation:
    """The recompute of a notebook's v1 decisions into seed space 2.

    ``pairs``: row id → its v2 seed pairs, first pair first (the store updates
    the row in place with it — id, timestamps and reviewer kept — and inserts
    one row per further pair). ``rows``: the plan-time snapshot of each
    translated row; the store translates a row only while its status and keys
    still equal it (a row decided again mid-rebuild stays v1 and is retried).
    ``suspended``: rows a side of which no current object carries — they stay
    v1, the next full rebuild retries them. ``override``: the v1 keys whose
    objects keep their old seed as ``~v1:<key>`` (only grows). ``unresolved``:
    rejections still collapsed after every key that could move was overridden
    — only a confirmed decision can do that, and confirmed stays authoritative
    as everywhere in ``cluster_seeds``. ``seeds_of_key``: the stream's map
    (v1 key → the seeds of the current objects carrying it), kept for
    ``live_pairs``. ``base_override``: the override the stream started
    with (read off the stored v2 decisions); the keys ``override`` adds to
    it move their objects off the v2 seeds the stored v2 decisions name —
    ``move_v2_decisions`` re-keys those decisions: ``moved_rows`` (v2 row
    id → its pairs after the move) and ``moved_keys`` (its keys when the
    plan was made; the store re-keys a row only while they still hold).
    ``collision_ids``: the stored v2 rows (any status, pending included)
    already naming a pair this plan writes — the pending refresh locks them
    with the rows it rewrites, because after writing it keeps one effective
    decision per pair (``drop_superseded_decisions``). Wherever several
    decisions name one pair they fold by ``kg_merge_seed.DECISION_STRENGTH``
    (confirmed first), here and in every reader."""

    pairs: Dict[str, List[Tuple[str, str]]] = field(default_factory=dict)
    rows: Dict[str, StoredDecision] = field(default_factory=dict)
    suspended: List[str] = field(default_factory=list)
    override: FrozenSet[str] = frozenset()
    unresolved: int = 0
    seeds_of_key: Dict[str, Set[str]] = field(default_factory=dict)
    base_override: FrozenSet[str] = frozenset()
    moved_rows: Dict[str, List[Tuple[str, str]]] = field(default_factory=dict)
    moved_keys: Dict[str, Tuple[str, str]] = field(default_factory=dict)
    collision_ids: List[str] = field(default_factory=list)

    @property
    def recomputed_any(self) -> bool:
        """Whether there were v1 decisions to recompute (the plan may exist
        only to map v1 pending keys, see ``live_pairs``)."""
        return bool(self.pairs or self.suspended)

    def live_pairs(self, rows: Iterable[StoredDecision]) -> Dict[FrozenSet[str], str]:
        """``{frozenset(v2 pair): status}`` of v1 decision rows read LIVE in the
        pending refresh: the rows this rebuild did not translate — decided, or
        decided again, after the plan was made (a v1 pending row the user
        settles while the rebuild runs is one) — mapped through the stream's
        v1 → v2 seeds under the final override. The refresh honours a decision
        made while the rebuild ran for v1 rows as it does for v2 rows; the
        next full rebuild translates them for good. A key no current object
        carries maps to nothing."""
        out: Dict[FrozenSet[str], str] = {}
        for r in rows:
            if r.status not in DECIDED_MERGE_STATUSES:
                continue
            side_a = _translated_seeds(r.key_a, self.seeds_of_key, self.override)
            side_b = _translated_seeds(r.key_b, self.seeds_of_key, self.override)
            if side_a and side_b:
                for a, b in _row_pairs(side_a, side_b):
                    fold_decision(out, frozenset((a, b)), r.status)
        return out

    def decided_pairs(self) -> Dict[FrozenSet[str], str]:
        """``{frozenset(v2 pair): status}`` of the translated rows — what the
        clustering of this rebuild uses before the rows are persisted."""
        out: Dict[FrozenSet[str], str] = {}
        for row_id in sorted(self.pairs):
            status = self.rows[row_id].status
            for a, b in self.pairs[row_id]:
                fold_decision(out, frozenset((a, b)), status)
        return out

    def move_v2_decisions(
        self, decided: Mapping[FrozenSet[str], str], rows: Iterable[StoredDecision],
        carried: Container[str],
    ) -> Dict[FrozenSet[str], str]:
        """Re-key the stored v2 decisions onto the seeds their objects carry
        once the keys this rebuild added to the override moved them
        (``~v1:<key>``). A v2 decision names a seed, and a seed decision
        applies to the objects carrying that seed now: when a new override
        key moves some of them to ``~v1:<key>``, the decision follows them —
        one pair per seed the moved and the remaining objects carry (the
        old seed stays only while ``carried``, the final stream's seeds,
        still holds it). Without this the decision would name a seed no
        object carries and silently stop applying (a confirmation lost, a
        rejection lifted).

        ``decided``: the stored v2 decisions the clustering reads, returned
        re-keyed. ``rows``: the same rows with their ids (pending ones
        included, so a row a human settles while the rebuild runs is
        re-keyed too) — recorded in ``moved_rows`` / ``moved_keys`` for the
        pending refresh to persist."""
        moved = _moved_by_seed(self.seeds_of_key, self.base_override, self.override)
        if not moved:
            return dict(decided)
        for r in rows:
            if r.key_a in moved or r.key_b in moved:
                self.moved_rows[r.row_id] = _row_pairs(
                    _moved_side(r.key_a, moved, carried), _moved_side(r.key_b, moved, carried))
                self.moved_keys[r.row_id] = (r.key_a, r.key_b)
        out: Dict[FrozenSet[str], str] = {}
        for pair, status in decided.items():
            a, b = sorted(pair) if len(pair) == 2 else (next(iter(pair)),) * 2
            if a not in moved and b not in moved:
                fold_decision(out, pair, status)
                continue
            for x, y in _row_pairs(_moved_side(a, moved, carried),
                                   _moved_side(b, moved, carried)):
                fold_decision(out, frozenset((x, y)), status)
        return out

    def written_pairs(self) -> Set[FrozenSet[str]]:
        """Every pair the pending refresh writes for this plan: the v1 rows'
        translations and the moved v2 rows' new keys."""
        return {frozenset(pair) for pairs in (*self.pairs.values(), *self.moved_rows.values())
                for pair in pairs}

    def note_collisions(self, rows: Iterable[StoredDecision]) -> None:
        """Record ``collision_ids``: the stored v2 rows (``rows``, the plan-time
        snapshot) that name a pair this plan writes and that it does not
        rewrite itself."""
        written = self.written_pairs()
        self.collision_ids = sorted(
            r.row_id for r in rows
            if r.row_id not in self.moved_rows
            and frozenset((r.key_a, r.key_b)) in written)


class _Everything:
    """``carried`` for the recompute's own check: every seed counts as still
    carried (see ``translate_v1_decisions``)."""

    def __contains__(self, _seed: object) -> bool:
        return True


_EVERYTHING = _Everything()


def _moved_by_seed(
    seeds_of_key: Mapping[str, Set[str]], base: FrozenSet[str], over: FrozenSet[str],
) -> Dict[str, Set[str]]:
    """v2 seed → the ``~v1:<key>`` seeds its objects move to, for the keys
    ``over`` adds to ``base`` (``seeds_of_key`` was recorded under ``base``,
    so a new key's seeds are the plain v2 seeds its objects carried)."""
    out: Dict[str, Set[str]] = {}
    for key in sorted(over - base):
        for seed in seeds_of_key.get(key) or ():
            out.setdefault(seed, set()).add(OVERRIDE_SEED_PREFIX + key)
    return out


def _moved_side(seed: str, moved: Mapping[str, Set[str]], carried: Container[str]) -> Set[str]:
    new = moved.get(seed)
    if not new:
        return {seed}
    return set(new) | ({seed} if seed in carried else set())


def override_keys(decided: Iterable[FrozenSet[str]]) -> FrozenSet[str]:
    """The v1 seeds a notebook keeps as ``~v1:<seed>``: read off its stored
    v2 decision keys (the override lives in the decision rows' seeds, no
    separate table). Every full rebuild and every incremental placement in
    space 2 applies them, or the decision naming ``~v1:<seed>`` would match
    nothing and the rejected pair would collapse into one seed again."""
    width = len(OVERRIDE_SEED_PREFIX)
    return frozenset(
        key[width:] for pair in decided for key in pair
        if key.startswith(OVERRIDE_SEED_PREFIX)
    )


def _translated_seeds(
    key: str, seeds_of_key: Mapping[str, Set[str]], override: FrozenSet[str],
) -> Set[str]:
    found = seeds_of_key.get(key) or set()
    if found and key in override:
        return {OVERRIDE_SEED_PREFIX + key}
    return set(found)


def _row_pairs(side_a: Set[str], side_b: Set[str]) -> List[Tuple[str, str]]:
    """Every pair of the two sides (sorted, deterministic) except self pairs —
    unless every pair is one, then one self pair keeps the decision as a row."""
    pairs = [(x, y) for x in sorted(side_a) for y in sorted(side_b) if x != y]
    if not pairs:
        both = sorted(side_a & side_b)
        pairs = [(both[0], both[0])]
    return pairs


def translate_v1_decisions(
    rows: Sequence[StoredDecision],
    seeds_of_key: Mapping[str, Set[str]],
    *,
    override: Iterable[str] = (),
    v2_confirmed: Iterable[FrozenSet[str]] = (),
) -> DecisionTranslation:
    """Recompute stored v1 decisions into v2 seed pairs (§五 S1 steps 3–4).

    ``seeds_of_key``: v1 key → the seeds (in space 2, under ``override``)
    of the current objects whose v1 seed is that key, as the full rebuild's
    seed stream recorded them (step 2). A v1 key cannot be turned into v2
    seeds from its string — the origin of each space is lost ("Llama 3.1" and
    "Llama-3-1" both gave ``llama 3 1``) — so only the objects' own names can.

    Step 3: a row whose either side has no current object is suspended; a
    confirmed row becomes every pair of its two sides' seeds (an old group S1
    splits is joined back by these pairs); a rejected / deferred row becomes
    every pair as cannot-link. Step 4: the v2 seeds are joined by the
    confirmed pairs (translated, plus ``v2_confirmed`` already stored); a
    rejected / deferred row whose sides fall into ONE component in space 2 but
    were apart in space 1 (v1 components of the v1 confirmed rows) is a
    violation — the same seed means the same group, which no cannot-link can
    undo. Its two keys join the override set (their objects keep
    ``~v1:<key>``); when both already did, every key whose seeds sit in the
    collapsed component joins. Repeat until no violation; the override only
    grows, and with every key overridden the v2 components restricted to the
    keys are the v1 ones, so it terminates.

    ``v2_confirmed`` follows the keys the loop adds to ``override``: a key
    that joins moves its objects off the v2 seeds the stored confirmations
    name, and a confirmation follows them (``DecisionTranslation.
    move_v2_decisions``) — so one that still joins a rejection's two sides
    after the move is reported as ``unresolved`` (confirmed wins) instead of
    vanishing. The check keeps every old seed in the moved pairs (it cannot
    know yet which ones the final stream still carries); an old seed is on
    a rejection's side only while an object the move left alone carries
    it, so the components the check compares are the same."""
    rows = [r for r in rows if r.status in DECIDED_MERGE_STATUSES]
    v2_confirmed = [tuple(p) for p in v2_confirmed if len(p) == 2]
    v1_uf = _UF({k for r in rows for k in (r.key_a, r.key_b)})
    for r in rows:
        if r.status == "confirmed":
            v1_uf.union(r.key_a, r.key_b)
    keys = sorted({k for r in rows for k in (r.key_a, r.key_b) if seeds_of_key.get(k)})
    base = over = frozenset(override)
    unresolved = 0
    while True:
        sides = {k: _translated_seeds(k, seeds_of_key, over) for k in keys}
        live = [r for r in rows if sides.get(r.key_a) and sides.get(r.key_b)]
        pairs = {r.row_id: _row_pairs(sides[r.key_a], sides[r.key_b]) for r in live}
        moved = _moved_by_seed(seeds_of_key, base, over)
        confirmed_v2 = [
            pair for a, b in v2_confirmed
            for pair in _row_pairs(_moved_side(a, moved, _EVERYTHING),
                                   _moved_side(b, moved, _EVERYTHING))
        ] if moved else v2_confirmed
        nodes = {s for ps in pairs.values() for p in ps for s in p}
        nodes.update(s for p in confirmed_v2 for s in p)
        uf = _UF(nodes)
        for r in live:
            if r.status == "confirmed":
                for a, b in pairs[r.row_id]:
                    uf.union(a, b)
        for a, b in confirmed_v2:
            uf.union(a, b)
        add: Set[str] = set()
        unresolved = 0
        for r in live:
            if r.status not in _APART_STATUSES:
                continue
            if v1_uf.find(r.key_a) == v1_uf.find(r.key_b):
                continue        # already contradictory in space 1: confirmed wins
            roots = {uf.find(x) for x in sides[r.key_a]} & {uf.find(y) for y in sides[r.key_b]}
            if not roots:
                continue
            fresh = {k for k in (r.key_a, r.key_b) if k not in over}
            if not fresh:
                fresh = {k for k in keys if k not in over
                         and any(s in uf.p and uf.find(s) in roots for s in sides[k])}
            if fresh:
                add |= fresh
            else:
                unresolved += 1
        if not add:
            break
        over = over | add
    suspended = sorted(r.row_id for r in rows if r.row_id not in pairs)
    return DecisionTranslation(
        pairs=pairs, rows={r.row_id: r for r in rows if r.row_id in pairs},
        suspended=suspended, override=over, unresolved=unresolved,
        seeds_of_key={k: set(v) for k, v in seeds_of_key.items()},
        base_override=base,
    )


# ===========================================================================
# The squash-key candidate tier of the pending refresh (P3, design §4.4)
# ===========================================================================

MERGE_KIND_SPELLING = "spelling_variant"   # 同名不同写法
MERGE_KIND_SIMILAR = "similarity"


def squash_pending_candidates(
    seed_to_canonical: Mapping[str, str],
    members_count: Mapping[str, int],
    reps: Mapping[str, "np.ndarray"],
    *,
    limit: int,
    confirmed: Iterable[FrozenSet[str]] = (),
    rejected: Iterable[FrozenSet[str]] = (),
) -> Tuple[List[tuple], int]:
    """Pending candidates for canonicals whose v2 seeds share a squash key —
    the names differ only in the spaces punctuation made ("Llama 3.1" /
    "Llama 31"), the wide half of F4 that S1 deliberately leaves to a human.

    Seeds starting with ``~`` (the per-object sentinel, the ``~v1:`` override)
    are never grouped. Two distinct seeds can share a squash key only when one
    of them has a space, so the groups are found from the spaced seeds alone
    (a set of their squash keys, then one pass) — no notebook-wide dict of
    squash keys. Per group of ≥ 2 canonicals: the canonical with the most
    members (all its seeds; ties by id) is the anchor and every other one gets
    ONE pair with it (a star, never a chain). Each side is represented by its
    seed in the group with the most members (ties by seed); the score is the
    cosine of those two seeds' representative vectors, 0 when either has none.

    Rows are ``(seed_a, seed_b, canonical_a, canonical_b, score)`` — the
    shape of ``cluster_seeds``' ``pending_seeds`` — heaviest group first (by
    the members of its canonicals), then by squash key.

    The stored decisions (``confirmed`` / ``rejected``, the latter with the
    deferred ones — the snapshot the clustering used, by seed) are applied
    BEFORE the cap, by ``filter_pending_seeds_by_decisions``: a pair a human
    already rejected or deferred would otherwise keep the top places and be
    dropped only by the pending refresh, so with more groups than the cap the
    lighter groups would never reach the queue. The refresh still re-checks
    the decisions live in its own transaction. Returns the first ``limit``
    rows and the total before the cap (the caller reports a cap)."""
    spaced = {
        seed.replace(" ", "") for seed in seed_to_canonical
        if " " in seed and not seed.startswith("~")
    }
    if not spaced:
        return [], 0
    groups: Dict[str, Dict[str, List[str]]] = {}
    for seed, cid in seed_to_canonical.items():
        key = seed_squash_key(seed)
        if key and key in spaced:
            groups.setdefault(key, {}).setdefault(cid, []).append(seed)
    groups = {key: by_cid for key, by_cid in groups.items() if len(by_cid) >= 2}
    if not groups:
        return [], 0
    involved = {cid for by_cid in groups.values() for cid in by_cid}
    total: Dict[str, int] = {cid: 0 for cid in involved}
    for seed, cid in seed_to_canonical.items():
        if cid in total:
            total[cid] += int(members_count.get(seed, 0))

    def rep_seed(seeds: List[str]) -> str:
        return min(seeds, key=lambda s: (-int(members_count.get(s, 0)), s))

    def cosine(a: str, b: str) -> float:
        va, vb = reps.get(a), reps.get(b)
        if va is None or vb is None:
            return 0.0
        na, nb = float(np.linalg.norm(va)), float(np.linalg.norm(vb))
        if na == 0.0 or nb == 0.0:
            return 0.0
        return float(np.dot(va, vb) / (na * nb))

    ordered = sorted(groups.items(),
                     key=lambda item: (-sum(total[c] for c in item[1]), item[0]))
    rows: List[tuple] = []
    for _key, by_cid in ordered:
        cids = sorted(by_cid, key=lambda c: (-total[c], c))
        anchor = cids[0]
        sa = rep_seed(by_cid[anchor])
        for other in cids[1:]:
            sb = rep_seed(by_cid[other])
            rows.append((sa, sb, anchor, other, cosine(sa, sb)))
    confirmed, rejected = set(map(frozenset, confirmed)), set(map(frozenset, rejected))
    if rows and (confirmed or rejected):
        # The decided components are found on the seeds the rows and the
        # decisions name (those the clustering knows): the same components the
        # refresh finds on every seed, at the size of the rows plus decisions.
        named = {s for row in rows for s in row[:2]}
        named.update(s for pair in confirmed | rejected for s in pair if s in seed_to_canonical)
        rows = filter_pending_seeds_by_decisions(rows, sorted(named), confirmed, rejected)
    return rows[:max(0, int(limit))], len(rows)


def merge_pending_tiers(ann_rows: Sequence[tuple], squash_rows: Sequence[tuple]) -> List[tuple]:
    """The ANN pending rows followed by the squash rows, one row per canonical
    pair. A pair both tiers found keeps the ANN row's place and the higher
    score, with the squash tier's seeds — so the row reads as a spelling
    variant (``merge_candidate_kind``) whichever tier found it first."""
    out = list(ann_rows)
    at = {frozenset((row[2], row[3])): index for index, row in enumerate(out)}
    for row in squash_rows:
        key = frozenset((row[2], row[3]))
        index = at.get(key)
        if index is None:
            at[key] = len(out)
            out.append(tuple(row))
        else:
            ann = out[index]
            out[index] = (row[0], row[1], ann[2], ann[3], max(float(ann[4]), float(row[4])))
    return out


def merge_candidate_kind(row: Mapping[str, object]) -> str:
    """``spelling_variant`` (同名不同写法) when the two sides of a merge
    candidate have the same squash key — the names differ only in spaces,
    hyphens, underscores, dots or brackets — else ``similarity``. Read off the
    row's decision keys (``seed_x``, or the canonical id without ``K-``), so
    a bridge candidate and a row of either seed space are judged alike: a v1
    seed squashes to the same key as the v2 seed of the same name."""
    def key(side: str) -> str:
        seed = str(row.get(f"seed_{side}") or "")
        if not seed:
            cid = str(row.get(f"canonical_{side}") or "")
            seed = cid[2:] if cid.startswith("K-") else ""
        return seed_squash_key(seed)

    a, b = key("a"), key("b")
    return MERGE_KIND_SPELLING if a and a == b else MERGE_KIND_SIMILAR
