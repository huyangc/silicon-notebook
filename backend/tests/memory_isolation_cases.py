"""A pre-isolation ("contaminated") knowledge-graph world shared by the SQLite
and PostgreSQL tests of the ruling-M1 migration (SQLite ``_migration_87`` /
PostgreSQL ``0067_memory_kg_isolation.sql``) and of the post-readiness
rebuild. Both backends seed the SAME rows and assert the SAME expectations,
so neither side can drift into agreeing with its own implementation (same
reason as ``memory_sql_cases.py``). Not a test module (no ``test_`` prefix).

World:

* Notebook F holds a document source and two Memory sources of member bob;
  notebook C holds only a document source (the control: nothing in C may
  change).
* In F, built before the write-side guards existed:
  - ``K-bandgap``: a shared cluster whose members are a document object AND a
    Memory object (the published generation 0) -- plus a generation-1
    (building) row of the same canonical id without the Memory member, which
    must go too: the WHOLE cluster, every generation;
  - ``K-secret project``: a canonical id minted from the Memory object's own
    seed, whose cluster name therefore IS the Memory's text;
  - ``KL-bob claim``: a building-generation claim cluster whose only member is
    a Memory claim;
  - ``K-ldo``: a clean shared cluster (must survive untouched);
  - ``ko-pll``: a shared object carrying Memory evidence (a manual merge folded
    it in) with the matching reverse-index row;
  - ``ko-private``: a Memory object that received shared evidence (kept
    private, unchanged);
  - canonical relations, mention edges, co-mention pairs (one bridged only by
    a Memory claim), merge and conflict candidates (applied ones included),
    promotion proposals of every path and status, communities, the analysis
    precompute, rebuild checkpoints, clustering scratch left by an interrupted
    run, and a chunk under a Memory source with its embedding, question,
    element index (and, on SQLite, FTS) rows;
  - ``ko-allmem``: a shared object whose evidence is ENTIRELY Memory and
    which has no reverse-index row at all (only the evidence scan finds it).
* Public libraries (tier ``base``) in F, each with a shared object carrying
  Memory evidence: PUBF (reverse index not attested: ``source_index_backfilled``
  0, and the Memory item has no reverse-index row), PUBR (attested flag, but a
  reverse-index backfill is still running; no Memory reverse-index row) and
  PUBA (attested and complete; the Memory reverse-index row is there). PUBF
  and PUBR take the evidence scan, PUBA the reverse index only.
* Outside F, every notebook with clusters is marked 2 for the post-readiness
  check (G), public libraries and copies included: C (clean; plus a
  BUILDING-generation cluster whose name no member carries, which the check
  must not read), G (a cluster named after a since-deleted Memory object),
  H (a ``K-~ko-...`` cluster whose seed object is gone), PUB (a public
  library holding an approved promotion copy of a Memory object in F, whose
  evidence and reverse-index row name F's Memory source: never touched), COPY
  (a notebook copy: clusters but no state row), GX (the review's case: a
  dirty notebook whose cluster name is a surviving member's own, but whose
  cluster description and community text were written while a since-deleted
  Memory was a member, with that Memory claim's mention row and merge
  candidates left behind), GD (dirty only), GM (a mention row of a claim
  object that no longer exists) and GC (a community member naming nothing
  that exists).
"""
from __future__ import annotations

import json

NOW = "2026-09-29T00:00:00+00:00"

NB_F = "nb-mki-f"
NB_C = "nb-mki-clean"
#: holds no Memory any more, but a cluster named after a since-deleted
#: Memory object survives on a shared member (dangling seed: name arm).
NB_G = "nb-mki-ghost"
#: holds no Memory; a degenerate-name cluster ``K-~ko-...`` whose seed object
#: is gone (dangling seed: encoded-object-id arm), its name still carried.
NB_H = "nb-mki-lost-seed"
#: public libraries (tier base)
NB_PUB = "nb-mki-pub"
NB_PUBF = "nb-mki-pubf"
NB_PUBR = "nb-mki-pubr"
NB_PUBA = "nb-mki-puba"
#: a notebook copy: clusters, no unified_kg_state row (by design)
NB_COPY = "nb-mki-copy"
#: an orphan Memory source (its memory item is gone: owner unknown) in a
#: notebook without a state row (only possible after an offline merge)
NB_O = "nb-mki-orphan"
#: the other G notebooks, one per post-readiness signal (see SIGNALS)
NB_GX = "nb-mki-gx"
NB_GD = "nb-mki-gd"
NB_GM = "nb-mki-gm"
NB_GC = "nb-mki-gc"
#: dirty, with a curator's decided merge pairs whose losing canonical ids no
#: cluster row carries (the normal state: decisions key on seeds)
NB_GK = "nb-mki-gk"
OWNER = "u-mki-owner"
BOB = "u-mki-bob"

#: notebook -> tier (``personal`` otherwise)
TIERS = {NB_PUB: "base", NB_PUBF: "base", NB_PUBR: "base", NB_PUBA: "base"}
#: the notebooks holding a Memory source: cleaned by the migration, marker 0
F_NOTEBOOKS = (NB_F, NB_O, NB_PUBA, NB_PUBF, NB_PUBR)
#: F's notebooks whose evidence itself is scanned (step 9, mki_scan)
EVIDENCE_SCANNED = frozenset({NB_F, NB_O, NB_PUBF, NB_PUBR})
#: the post-readiness seed check's answer for every notebook the migration
#: marks 2 (G): the first signal that fires, None = clean
SIGNALS = {
    NB_C: None, NB_COPY: None, NB_PUB: None,
    NB_G: "seed", NB_H: "seed",
    NB_GX: "dirty", NB_GD: "dirty",
    NB_GM: "stale_reference", NB_GC: "stale_reference",
    NB_GK: "dirty",
}
#: the notebooks the migration marks 2 (outside F, clusters), in id order
SEED_CHECKED = tuple(sorted(SIGNALS))
#: the notebooks the first pass rebuilds, in work order (id order): F, and
#: the G notebooks a signal queued
QUEUED = tuple(sorted(
    set(F_NOTEBOOKS) | {nb for nb, signal in SIGNALS.items() if signal}))

#: (memory item id, created_by, notebook)
MEMORY_ITEMS = (
    ("mem-mki-1", BOB, NB_F), ("mem-mki-2", BOB, NB_F), ("mem-mki-3", OWNER, NB_F),
    ("mem-mki-pubf", BOB, NB_PUBF), ("mem-mki-pubr", BOB, NB_PUBR),
    ("mem-mki-puba", BOB, NB_PUBA),
)

#: (notebook, source id, source_type, memory_id)
SOURCES = (
    (NB_F, "src-doc", "upload", None),
    (NB_F, "src-mem", "memory", "mem-mki-1"),
    (NB_F, "src-mem2", "memory", "mem-mki-2"),
    (NB_F, "src-mem3", "memory", "mem-mki-3"),
    (NB_C, "src-c-doc", "upload", None),
    (NB_G, "src-g-doc", "upload", None),
    (NB_H, "src-h-doc", "upload", None),
    (NB_PUB, "src-pub-doc", "upload", None),
    (NB_PUBF, "src-pubf-doc", "upload", None),
    (NB_PUBF, "src-pubf-mem", "memory", "mem-mki-pubf"),
    (NB_PUBR, "src-pubr-doc", "upload", None),
    (NB_PUBR, "src-pubr-mem", "memory", "mem-mki-pubr"),
    (NB_PUBA, "src-puba-doc", "upload", None),
    (NB_PUBA, "src-puba-mem", "memory", "mem-mki-puba"),
    (NB_COPY, "src-copy-doc", "upload", None),
    (NB_GX, "src-gx-doc", "upload", None),
    (NB_GD, "src-gd-doc", "upload", None),
    (NB_GM, "src-gm-doc", "upload", None),
    (NB_GC, "src-gc-doc", "upload", None),
    (NB_GK, "src-gk-doc", "upload", None),
    (NB_O, "src-o-mem", "memory", "mem-gone"),
    (NB_O, "src-o-doc", "upload", None),
)
MEMORY_SOURCES = frozenset({"src-mem", "src-mem2", "src-mem3"})

#: an evidence entry ``"text:<s>"`` is a legacy array item that is a bare JSON
#: string, not an object (it must be kept, and must not break the migration).
#: (notebook, object id, source_id, object_type, name, evidence source ids)
OBJECTS = (
    (NB_F, "ko-bandgap-doc", "src-doc", "concept", "Bandgap", ("src-doc",)),
    (NB_F, "ko-bandgap-mem", "src-mem", "concept", "bandgap", ("src-mem",)),
    # bob's Memory object carrying bob's other Memory (kept) and the owner's
    # Memory (another member's: stripped) from pre-upgrade manual merges
    (NB_F, "ko-secret-mem", "src-mem", "concept", "Secret Project",
     ("src-mem", "src-mem2", "src-mem3")),
    (NB_F, "ko-secret-doc", "src-doc", "concept", "secret project", ("src-doc",)),
    (NB_F, "ko-ldo-1", "src-doc", "concept", "LDO", ("src-doc",)),
    (NB_F, "ko-ldo-2", "src-doc", "concept", "ldo", ("src-doc",)),
    (NB_F, "ko-pll", "src-doc", "concept", "PLL",
     ("src-doc", "text:legacy note", "src-mem")),
    (NB_F, "ko-x", "src-doc", "concept", "Xtal", ("src-doc",)),
    (NB_F, "ko-claim-mem", "src-mem", "claim", "bob claim", ("src-mem",)),
    (NB_F, "ko-claim-doc", "src-doc", "claim", "doc claim", ("src-doc",)),
    (NB_F, "ko-private", "src-mem2", "concept", "Private",
     ("src-mem2", "text:legacy note", "src-doc")),
    (NB_C, "ko-c-1", "src-c-doc", "concept", "Clean", ("src-c-doc",)),
    (NB_C, "ko-c-2", "src-c-doc", "concept", "clean", ("src-c-doc",)),
    (NB_G, "ko-g-1", "src-g-doc", "concept", "Alpha", ("src-g-doc",)),
    (NB_G, "ko-g-2", "src-g-doc", "concept", "alpha", ("src-g-doc",)),
    (NB_G, "ko-g-3", "src-g-doc", "concept", "Beta", ("src-g-doc",)),
    (NB_H, "ko-h-1", "src-h-doc", "concept", "??", ("src-h-doc",)),
    # a shared object whose evidence is entirely Memory, with NO reverse-index
    # row (NO_REVERSE_INDEX): only the evidence scan can find it
    (NB_F, "ko-allmem", "src-doc", "concept", "AllMem", ("src-mem", "src-mem2")),
    # a public library's approved promotion copy of F's Memory object: its
    # evidence (and reverse-index row) name F's Memory source; outside F,
    # never touched
    (NB_PUB, "ko-pub", "", "concept", "Secret Project", ("src-mem",)),
    (NB_PUB, "ko-pub-doc", "src-pub-doc", "concept", "Pub", ("src-pub-doc",)),
    (NB_PUBF, "ko-pubf", "src-pubf-doc", "concept", "PubF",
     ("src-pubf-doc", "src-pubf-mem")),
    (NB_PUBR, "ko-pubr", "src-pubr-doc", "concept", "PubR",
     ("src-pubr-doc", "src-pubr-mem")),
    (NB_PUBA, "ko-puba", "src-puba-doc", "concept", "PubA",
     ("src-puba-doc", "src-puba-mem")),
    (NB_COPY, "ko-copy-1", "src-copy-doc", "concept", "Copy", ("src-copy-doc",)),
    (NB_GX, "ko-gx-1", "src-gx-doc", "concept", "Alpha", ("src-gx-doc",)),
    (NB_GX, "ko-gx-2", "src-gx-doc", "concept", "Omega", ("src-gx-doc",)),
    (NB_GD, "ko-gd-1", "src-gd-doc", "concept", "Delta", ("src-gd-doc",)),
    (NB_GM, "ko-gm-1", "src-gm-doc", "concept", "Mu", ("src-gm-doc",)),
    (NB_GC, "ko-gc-1", "src-gc-doc", "concept", "Gamma", ("src-gc-doc",)),
    (NB_GK, "ko-gk-1", "src-gk-doc", "concept", "Alpha", ("src-gk-doc",)),
    (NB_GK, "ko-gk-2", "src-gk-doc", "concept", "Alpha Beta", ("src-gk-doc",)),
    (NB_GK, "ko-gk-3", "src-gk-doc", "concept", "Gamma", ("src-gk-doc",)),
    (NB_GK, "ko-gk-4", "src-gk-doc", "concept", "Gamma Ray", ("src-gk-doc",)),
    (NB_O, "ko-o-mem", "src-o-mem", "concept", "orph", ("src-o-mem",)),
    (NB_O, "ko-o-doc", "src-o-doc", "concept", "orph", ("src-o-doc",)),
)
#: (object, evidence source) pairs seeded WITHOUT their reverse-index row
NO_REVERSE_INDEX = frozenset({
    ("ko-allmem", "src-mem"), ("ko-allmem", "src-mem2"),
    ("ko-pubf", "src-pubf-mem"), ("ko-pubr", "src-pubr-mem"),
})
#: expected evidence after the migration, where it changes
EVIDENCE_AFTER = {
    "ko-pll": ("src-doc", "text:legacy note"),
    "ko-secret-mem": ("src-mem", "src-mem2"),
    "ko-allmem": (),
    "ko-pubf": ("src-pubf-doc",),
    "ko-pubr": ("src-pubr-doc",),
    "ko-puba": ("src-puba-doc",),
}
#: Memory objects that lost another member's Memory evidence
CROSS_OWNER_STRIPPED = 1
MEMORY_OBJECTS = frozenset(
    {"ko-bandgap-mem", "ko-secret-mem", "ko-claim-mem", "ko-private"}
)

#: (notebook, relation id, source_id, source object, target object)
RELATIONS = (
    (NB_F, "kr-mem", "src-mem", "ko-secret-mem", "ko-bandgap-mem"),
    (NB_F, "kr-doc", "src-doc", "ko-ldo-1", "ko-ldo-2"),
)

#: (notebook, canonical id, canonical name, object type, generation, members)
CLUSTERS = (
    (NB_F, "K-bandgap", "Bandgap", "concept", 0, ("ko-bandgap-doc", "ko-bandgap-mem")),
    (NB_F, "K-bandgap", "Bandgap", "concept", 1, ("ko-bandgap-doc",)),
    (NB_F, "K-secret project", "Secret Project", "concept", 0,
     ("ko-secret-mem", "ko-secret-doc")),
    (NB_F, "KL-bob claim", "bob claim", "claim", 1, ("ko-claim-mem",)),
    (NB_F, "K-ldo", "LDO", "concept", 0, ("ko-ldo-1", "ko-ldo-2")),
    (NB_C, "K-clean", "Clean", "concept", 0, ("ko-c-1", "ko-c-2")),
    (NB_G, "K-alpha", "Alpha", "concept", 0, ("ko-g-1", "ko-g-2")),
    (NB_G, "K-ghost memory", "Ghost Memory", "concept", 0, ("ko-g-3",)),
    (NB_H, "K-~ko-h-gone", "??", "concept", 0, ("ko-h-1",)),
    # C's BUILDING generation (1; C publishes 0): a name no member carries.
    # The seed check reads the published generation only. Its canonical id
    # sorts BEFORE C's published "K-clean", so it falls inside the first
    # page's key range: only the generation filter keeps it out.
    (NB_C, "K-building stale", "Stale Building", "concept", 1, ("ko-c-1",)),
    (NB_PUB, "K-pub", "Pub", "concept", 0, ("ko-pub-doc",)),
    (NB_COPY, "K-copy", "Copy", "concept", 0, ("ko-copy-1",)),
    (NB_GX, "K-alpha", "Alpha", "concept", 0, ("ko-gx-1",)),
    (NB_GD, "K-delta", "Delta", "concept", 0, ("ko-gd-1",)),
    (NB_GM, "K-mu", "Mu", "concept", 0, ("ko-gm-1",)),
    (NB_GC, "K-gamma", "Gamma", "concept", 0, ("ko-gc-1",)),
    # GK: 'Alpha' and 'Alpha Beta' merged by the curator's confirmed decision
    # (the cluster took the min seed, so K-alpha beta is carried by no row)
    (NB_GK, "K-alpha", "Alpha", "concept", 0, ("ko-gk-1", "ko-gk-2")),
    (NB_GK, "K-gamma", "Gamma", "concept", 0, ("ko-gk-3",)),
    (NB_GK, "K-gamma ray", "Gamma Ray", "concept", 0, ("ko-gk-4",)),
    # the orphan Memory's cluster (goes whole; the notebook gets a state row)
    (NB_O, "K-orph", "orph", "concept", 0, ("ko-o-mem", "ko-o-doc")),
)
#: (notebook, canonical id) -> canonical_description (default "about <name>"):
#: GX's was written while a since-deleted Memory was a member.
DESCRIPTIONS = {(NB_GX, "K-alpha"): "Alpha: per BOB MEMORY SECRET, the tapeout slips"}
#: text of the deleted Memory that GX still carries before its rebuild
GX_SECRET = "BOB MEMORY SECRET"
#: canonical ids that must be gone everywhere after the migration.
TAINTED_CANONICALS = frozenset({"K-bandgap", "K-secret project", "KL-bob claim"})
#: Memory objects outside NB_F (in F notebooks without Memory clusters but one)
OTHER_MEMORY_OBJECTS = frozenset({"ko-o-mem"})

#: (notebook, src, edge, tgt)
CANONICAL_RELATIONS = (
    (NB_F, "K-secret project", "related_to", "K-bandgap"),
    (NB_F, "K-ldo", "related_to", "ko-x"),
    (NB_C, "K-clean", "related_to", "ko-c-2"),
)
#: (notebook, claim object, concept canonical)
MENTION_EDGES = (
    (NB_F, "ko-claim-mem", "K-ldo"),
    (NB_F, "ko-claim-mem", "ko-pll"),
    (NB_F, "ko-claim-doc", "K-bandgap"),
    (NB_F, "ko-claim-doc", "K-ldo"),
    (NB_F, "ko-claim-doc", "ko-x"),
    # the deleted Memory claim's mention row (GX), and GM's only stale one
    # after a live one (page order)
    (NB_GX, "ko-gx-memclaim-gone", "K-alpha"),
    (NB_GM, "ko-gm-1", "K-mu"),
    (NB_GM, "ko-gm-gone", "K-mu"),
)
#: (notebook, canonical_a, canonical_b)
COMENTIONS = (
    (NB_F, "K-bandgap", "K-ldo"),
    (NB_F, "K-ldo", "ko-pll"),   # bridged only by the Memory claim
    (NB_F, "K-ldo", "ko-x"),     # bridged only by the document claim
)
#: (id, notebook, canonical_a, canonical_b, status)
MERGE_CANDIDATES = (
    ("mc-1", NB_F, "K-secret project", "K-ldo", "pending"),
    ("mc-2", NB_F, "K-ldo", "ko-x", "confirmed"),
    ("mc-3", NB_F, "ko-private", "K-ldo", "rejected"),  # a Memory object itself
    # a unique-seed sentinel minted from a Memory object: removed by the SQL
    ("mc-sentinel", NB_F, "K-~ko-secret-mem", "K-ldo", "rejected"),
    # the bridge id of the Memory concept "Private" (K- + its normalised
    # name): no cluster carries it; only kg_merge's normaliser can derive it,
    # so the migration keeps it and the rebuild worker purges it
    ("mc-bridge", NB_F, "K-private", "K-ldo", "confirmed"),
    # GX: a sentinel minted from the deleted Memory object (provably dead:
    # purged when GX is queued), a bridge id of its name (kept: it acts only
    # if an object mints that seed again) and a control naming a live
    # cluster and a live unclustered object
    ("mc-gx-sentinel", NB_GX, "K-~ko-gx-mem-gone", "K-alpha", "rejected"),
    ("mc-gx-bridge", NB_GX, "K-bob secret", "K-alpha", "confirmed"),
    ("mc-gx-keep", NB_GX, "K-alpha", "ko-gx-2", "confirmed"),
)
#: curator decisions keyed on seeds whose canonical ids drifted: (id,
#: notebook, canonical_a, canonical_b, status, seed_a, seed_b). They must
#: survive the queueing and the rebuild, and the confirmed merge must hold.
DECIDED = (
    ("mc-gk-confirmed", NB_GK, "K-alpha", "K-alpha beta", "confirmed",
     "alpha", "alpha beta"),
    ("mc-gk-rejected", NB_GK, "K-gamma ray", "K-alpha", "rejected",
     "gamma ray", "alpha"),
)
#: merge candidates only the rebuild worker can recognise as Memory-named
MERGE_CANDIDATES_BRIDGE = frozenset({"mc-bridge"})
#: merge candidates of a queued G notebook with a sentinel side whose object
#: is gone: purged when the seed check queues the notebook
MERGE_CANDIDATES_STALE = frozenset({"mc-gx-sentinel"})
#: (id, notebook, kind, left_ref, right_ref, status, resolution, winner_ref)
#: -- any status goes when a side is Memory-derived (applied rows quote it in
#: rationale/payload). cf-5 (modify, shared winner) and cf-6 (discard, Memory
#: winner, shared loser) changed a shared object for a Memory one: the summary
#: line counts them (conflicts_applied_on_shared=2); cf-7 (discard, shared
#: winner) did not change the shared side and is not counted.
CONFLICTS = (
    ("cf-1", NB_F, "node", "ko-secret-mem", "ko-ldo-1", "pending", None, None),
    ("cf-2", NB_F, "edge", "kr-mem", "kr-doc", "pending", None, None),
    ("cf-3", NB_F, "node", "ko-ldo-1", "ko-ldo-2", "pending", None, None),
    ("cf-4", NB_F, "node", "ko-x", "ko-private", "rejected", None, None),
    ("cf-5", NB_F, "node", "ko-secret-mem", "ko-ldo-2", "applied", "modify", "ko-ldo-2"),
    ("cf-6", NB_F, "node", "ko-bandgap-mem", "ko-x", "applied", "discard",
     "ko-bandgap-mem"),
    ("cf-7", NB_F, "node", "ko-ldo-1", "ko-claim-mem", "applied", "discard", "ko-ldo-1"),
    # applied edge discard won by the Memory relation: the shared relation
    # kr-doc was set to rejected -- counted on the edge arm
    ("cf-8", NB_F, "edge", "kr-mem", "kr-doc", "applied", "discard", "kr-mem"),
)
CONFLICTS_APPLIED_ON_SHARED = 2
CONFLICTS_APPLIED_ON_SHARED_EDGES = 1
#: (id, notebook, members, title)
COMMUNITIES = (
    ("cm-1", NB_F, ("K-bandgap", "K-ldo"), "Bandgap and LDO"),
    ("cm-2", NB_F, ("ko-x",), "Xtal"),
    ("cm-c", NB_C, ("K-clean",), "Clean"),
    # written while the deleted Memory was a member of K-alpha
    ("cm-gx", NB_GX, ("K-alpha",), GX_SECRET),
    # a member naming nothing that exists (after a live one, page order)
    ("cm-gc", NB_GC, ("K-gamma", "ko-gc-gone"), "Gamma"),
    ("cm-copy", NB_COPY, ("K-copy", "ko-copy-1"), "Copy"),
)
#: (id, notebook, object_id, object_type, status, reason, reviewed_by) --
#: promotion proposals. OPEN generic-path proposals of Memory-derived objects
#: are rejected (reason memory_derived_object, reviewed_by empty, updated_at
#: moved), never deleted; already-closed ones, approved ones, the creator-only
#: Memory path (object_type 'memory') and shared objects' stay untouched.
PROMOTIONS = (
    ("pr-generic-mem", NB_F, "ko-secret-mem", "concept", "proposed", "", ""),
    ("pr-generic-mem-review", NB_F, "ko-private", "concept", "under_review", "",
     "u-curator"),
    ("pr-generic-mem-rejected", NB_F, "ko-bandgap-mem", "concept", "rejected",
     "duplicate", "u-curator"),
    ("pr-generic-mem-approved", NB_F, "ko-claim-mem", "claim", "approved", "",
     "u-curator"),
    ("pr-memory-path", NB_F, "mem-mki-1", "memory", "proposed", "", ""),
    ("pr-shared", NB_F, "ko-ldo-1", "concept", "proposed", "", ""),
)
#: the open generic proposals of Memory-derived objects the migration rejects.
PROMOTIONS_REJECTED = frozenset({"pr-generic-mem", "pr-generic-mem-review"})
#: ``app.domain.memory_kg_isolation.MEMORY_PROMOTION_REJECTED_REASON``.
PROMOTION_REJECTED_REASON = "memory_derived_object"
#: (notebook, run id, object id, seed / canonical name) -- clustering scratch
#: an interrupted run left behind (canonical names derived from Memory).
SCRATCH = (
    (NB_F, "run-old", "ko-secret-mem", "secret project"),
    (NB_C, "run-old", "ko-c-1", "clean"),
)
#: (notebook, chunk id, source id)
CHUNKS = (
    (NB_F, "ch-mem", "src-mem"),
    (NB_F, "ch-doc", "src-doc"),
    (NB_C, "ch-c", "src-c-doc"),
)
#: unified_kg_state seeds: notebook -> (kg_mutation_seq, cluster_mutation_seq,
#: dirty, source_index_backfilled). NB_COPY has no state row.
STATE = {
    NB_F: (7, 4, 0, 0), NB_C: (3, 2, 0, 0), NB_G: (5, 3, 0, 0), NB_H: (2, 2, 0, 0),
    NB_PUB: (1, 1, 0, 1), NB_PUBF: (1, 1, 0, 0), NB_PUBR: (1, 1, 0, 1),
    NB_PUBA: (1, 1, 0, 1),
    NB_GX: (9, 4, 1, 0), NB_GD: (4, 2, 1, 0), NB_GM: (2, 1, 0, 0), NB_GC: (2, 1, 0, 0),
    NB_GK: (4, 2, 1, 0),
}
#: source_index_backfills rows: notebook -> status
BACKFILLS = {NB_PUBR: "running", NB_PUBA: "complete"}
NOTEBOOKS = (NB_F, NB_C, NB_G, NB_H, NB_PUB, NB_PUBF, NB_PUBR, NB_PUBA, NB_COPY,
             NB_GX, NB_GD, NB_GM, NB_GC, NB_O, NB_GK)


def _evidence(source_ids) -> str:
    return json.dumps(
        [sid[len("text:"):] if sid.startswith("text:") else
         {"source_id": sid, "element_id": f"el-{sid}", "quoted_span": f"span {sid}"}
         for sid in source_ids],
        ensure_ascii=False,
    )


def seed(db, *, postgres: bool) -> None:
    """Insert the whole world on ``db`` (inside the caller's write block).
    ``postgres`` picks the placeholder style and the jsonb/bytea spellings."""
    p = "%s" if postgres else "?"
    js = "%s::jsonb" if postgres else "?"
    vec = b"\x00\x01" if postgres else "[0.0]"

    def ins(table: str, columns: str, values: tuple, json_columns=()) -> None:
        names = [c.strip() for c in columns.split(",")]
        marks = ",".join(js if n in json_columns else p for n in names)
        db.execute(f"INSERT INTO {table}({columns}) VALUES ({marks})", values)

    for index, uid in enumerate((OWNER, BOB)):
        if postgres:
            ins("users", "id,email,display_name,role,status,created_at,updated_at,"
                "username,password_hash,password_salt,password_iterations",
                (uid, f"{uid}@example.test", uid, "user", "active", NOW, NOW,
                 f"k{index:08d}", "", "", 0))
        else:
            ins("users", "id,email,display_name,role,status,created_at,updated_at",
                (uid, f"{uid}@example.test", uid, "user", "active", NOW, NOW))
    for notebook in NOTEBOOKS:
        ins("notebooks", "id,name,purpose,primary_domain,status,created_by,"
            "created_at,updated_at,tier",
            (notebook, "NB", "", "", "ready", OWNER, NOW, NOW,
             TIERS.get(notebook, "personal")))
        if notebook not in STATE:
            continue
        ks, cs, dirty, backfilled = STATE[notebook]
        ins("unified_kg_state", "notebook_id,dirty,kg_mutation_seq,cluster_mutation_seq,"
            "community_seq,canonical_rel_seq,mention_seq,cluster_input_version,"
            "source_index_backfilled,updated_at",
            (notebook, dirty, ks, cs, ks, ks, ks, "pre-isolation", backfilled, NOW))
    for notebook, status in BACKFILLS.items():
        ins("source_index_backfills", "notebook_id,status,created_at,updated_at",
            (notebook, status, NOW, NOW))
    for memory_id, created_by, notebook in MEMORY_ITEMS:
        ins("memory_items", "id,notebook_id,created_by,origin,status,title,content_md,"
            "created_at,updated_at",
            (memory_id, notebook, created_by, "ask_answer", "confirmed", memory_id, "x",
             NOW, NOW))
    for notebook, source_id, source_type, memory_id in SOURCES:
        ins("sources", "id,notebook_id,title,source_type,memory_id,created_at,updated_at",
            (source_id, notebook, source_id, source_type, memory_id, NOW, NOW))
    for notebook, object_id, source_id, object_type, name, ev in OBJECTS:
        ins("knowledge_objects", "id,notebook_id,object_type,status,source_id,payload,"
            "evidence,created_at,updated_at",
            (object_id, notebook, object_type, "approved", source_id,
             json.dumps({"name": name}), _evidence(ev), NOW, NOW),
            json_columns=("payload", "evidence"))
        for sid in ev:
            if sid.startswith("text:") or (object_id, sid) in NO_REVERSE_INDEX:
                continue
            ins("knowledge_object_sources", "object_id,source_id,notebook_id",
                (object_id, sid, notebook))
    for notebook, relation_id, source_id, src, tgt in RELATIONS:
        ins("knowledge_relations", "id,notebook_id,source_id,source_object_id,"
            "target_object_id,edge_type,evidence,created_at",
            (relation_id, notebook, source_id, src, tgt, "related_to",
             _evidence((source_id,)), NOW), json_columns=("evidence",))
    for notebook, canonical, name, object_type, generation, members in CLUSTERS:
        for member in members:
            ins("concept_clusters", "id,notebook_id,canonical_id,member_object_id,"
                "canonical_name,object_type,canonical_description,created_at,generation",
                (f"cc-{canonical}-{generation}-{member}", notebook, canonical, member,
                 name, object_type,
                 DESCRIPTIONS.get((notebook, canonical), f"about {name}"), NOW,
                 generation))
    for notebook, src, edge, tgt in CANONICAL_RELATIONS:
        ins("canonical_relations", "notebook_id,canonical_src,edge_type,canonical_tgt,"
            "sample_relation_ids,updated_at",
            (notebook, src, edge, tgt, json.dumps(["kr-x"]), NOW),
            json_columns=("sample_relation_ids",))
    for notebook, claim, concept in MENTION_EDGES:
        ins("mention_edges", "notebook_id,claim_object_id,concept_canonical_id",
            (notebook, claim, concept))
    for notebook, a, b in COMENTIONS:
        ins("concept_comentions", "notebook_id,canonical_a,canonical_b",
            (notebook, a, b))
    for cid, notebook, a, b, status in MERGE_CANDIDATES:
        ins("concept_merge_candidates", "id,notebook_id,canonical_a,canonical_b,score,"
            "status,rationale,created_at,updated_at",
            (cid, notebook, a, b, 0.9, status, f"{a} vs {b}", NOW, NOW))
    for cid, notebook, a, b, status, seed_a, seed_b in DECIDED:
        ins("concept_merge_candidates", "id,notebook_id,canonical_a,canonical_b,score,"
            "status,rationale,seed_a,seed_b,created_at,updated_at",
            (cid, notebook, a, b, 0.8, status, f"{a} vs {b}", seed_a, seed_b, NOW, NOW))
    for cid, notebook, kind, left, right, status, resolution, winner in CONFLICTS:
        ins("kg_conflict_candidates", "id,notebook_id,kind,left_ref,right_ref,rationale,"
            "resolved_payload,status,resolution,winner_ref,created_at,updated_at",
            (cid, notebook, kind, left, right, f"{left} vs {right}",
             json.dumps({"name": f"merged {left}"}), status, resolution, winner,
             NOW, NOW),
            json_columns=("resolved_payload",))
    for pid, notebook, object_id, object_type, status, reason, reviewer in PROMOTIONS:
        ins("promotion_candidates", "id,notebook_id,object_id,object_type,status,"
            "reason,reviewed_by,created_at,updated_at",
            (pid, notebook, object_id, object_type, status, reason, reviewer, NOW, NOW))
    for notebook, run_id, object_id, name in SCRATCH:
        ins("kg_cluster_scratch", "notebook_id,run_id,object_id,seed",
            (notebook, run_id, object_id, name))
        ins("kg_canonical_scratch", "notebook_id,run_id,seed,canonical_id,"
            "canonical_name,canonical_description",
            (notebook, run_id, name, f"K-{name}", name, f"about {name}"))
    for cid, notebook, members, title in COMMUNITIES:
        ins("communities", "id,notebook_id,level,member_ids,size,title,summary,created_at",
            (cid, notebook, 0, json.dumps(list(members)), len(members), title,
             f"summary of {title}", NOW), json_columns=("member_ids",))
        for member in members:
            ins("community_members", "canonical_id,notebook_id,level,community_id,"
                "canonical_name", (member, notebook, 0, cid, member))
    for notebook, source_id in ((NB_F, "src-mem"), (NB_C, "src-c-doc")):
        ins("kg_analysis_artifacts", "notebook_id,kind,payload,created_at",
            (notebook, "boards", json.dumps({"n": 1}), NOW), json_columns=("payload",))
        ins("kg_source_profiles", "notebook_id,source_id", (notebook, source_id))
        ins("kg_rebuild_checkpoint", "notebook_id,input_version,stage,item_key,payload,"
            "created_at", (notebook, "v", "merge_review", "k",
                           json.dumps({"canonical_name": "x"}), NOW),
            json_columns=("payload",))
    ins("kg_community_edges", "notebook_id,src_community_id,dst_community_id",
        (NB_F, "cm-1", "cm-2"))
    ins("kg_community_edges", "notebook_id,src_community_id,dst_community_id",
        (NB_C, "cm-c", "cm-c"))
    for notebook, chunk_id, source_id in CHUNKS:
        ins("chunks", "id,notebook_id,source_id,text,created_at",
            (chunk_id, notebook, source_id, f"text of {chunk_id}", NOW))
        ins("chunk_embeddings", "chunk_id,notebook_id,vector,created_at",
            (chunk_id, notebook, vec, NOW))
        ins("chunk_questions", "id,chunk_id,notebook_id,source_id,question,vector,"
            "created_at", (f"q-{chunk_id}", chunk_id, notebook, source_id,
                           f"question {chunk_id}", vec, NOW))
        ins("chunk_elements", "notebook_id,element_id,chunk_id",
            (notebook, f"el-{chunk_id}", chunk_id))
        if not postgres:
            ins("chunks_fts", "chunk_id,notebook_id,text",
                (chunk_id, notebook, f"text of {chunk_id}"))


#: table -> the columns that identify a row (for set snapshots).
SNAPSHOT_TABLES = {
    "concept_clusters": "notebook_id,canonical_id,member_object_id,generation,canonical_name",
    "canonical_relations": "notebook_id,canonical_src,edge_type,canonical_tgt",
    "mention_edges": "notebook_id,claim_object_id,concept_canonical_id",
    "concept_comentions": "notebook_id,canonical_a,canonical_b",
    "concept_merge_candidates": "id,notebook_id,canonical_a,canonical_b,status",
    "kg_conflict_candidates": "id,notebook_id,kind,left_ref,right_ref",
    "communities": "id,notebook_id,title,summary",
    "promotion_candidates": "id,notebook_id,object_id,object_type,status,reason,"
                            "reviewed_by,updated_at",
    "kg_cluster_scratch": "notebook_id,run_id,object_id,seed",
    "kg_canonical_scratch": "notebook_id,run_id,canonical_id,canonical_name",
    "community_members": "community_id,notebook_id,canonical_id",
    "kg_analysis_artifacts": "notebook_id,kind",
    "kg_source_profiles": "notebook_id,source_id",
    "kg_community_edges": "notebook_id,src_community_id,dst_community_id",
    "kg_rebuild_checkpoint": "notebook_id,stage,item_key",
    "knowledge_object_sources": "notebook_id,object_id,source_id",
    "knowledge_objects": "notebook_id,id,source_id,status",
    "knowledge_relations": "notebook_id,id,source_id",
    "chunks": "notebook_id,id,source_id",
    "chunk_embeddings": "notebook_id,chunk_id",
    "chunk_questions": "notebook_id,chunk_id,source_id",
    "chunk_elements": "notebook_id,chunk_id,element_id",
    "unified_kg_state": "notebook_id,dirty,kg_mutation_seq,cluster_mutation_seq,"
                        "community_seq,canonical_rel_seq,mention_seq,"
                        "cluster_input_version",
}


def snapshot(db, *, include_fts: bool = False) -> dict:
    """Every snapshot table as a set of tuples, plus each object's evidence
    source ids (in order) -- enough to state the whole post-migration world."""
    out = {}
    for table, columns in SNAPSHOT_TABLES.items():
        out[table] = {
            tuple(row[c.strip()] for c in columns.split(","))
            for row in db.execute(f"SELECT {columns} FROM {table}").fetchall()
        }
    evidence = {}
    for row in db.execute("SELECT id, evidence FROM knowledge_objects").fetchall():
        items = row["evidence"]
        if isinstance(items, str):
            items = json.loads(items)
        evidence[row["id"]] = tuple(
            item.get("source_id") if isinstance(item, dict) else f"text:{item}"
            for item in items
        )
    out["evidence"] = evidence
    if include_fts:
        out["chunks_fts"] = {
            (row["notebook_id"], row["chunk_id"])
            for row in db.execute("SELECT notebook_id, chunk_id FROM chunks_fts")
        }
    return out


def for_notebook(snap: dict, notebook_id: str) -> dict:
    """The part of a snapshot that belongs to one notebook (column 0 of every
    tuple is notebook_id, except community_members/merge/conflict/communities
    whose notebook column is 1)."""
    scoped = {}
    for table, rows in snap.items():
        if table == "evidence":
            continue
        index = 1 if table in {
            "community_members", "concept_merge_candidates",
            "kg_conflict_candidates", "communities", "promotion_candidates",
        } else 0
        scoped[table] = {row for row in rows if row[index] == notebook_id}
    return scoped


def assert_isolated(before: dict, after: dict) -> None:
    """The acceptance statements of plan §3.1 / E4-5 on the seeded world."""
    f_after = for_notebook(after, NB_F)
    # 1. The shared cluster with a Memory member is gone WHOLE (both
    #    generations, the document member's rows too); the cluster minted from
    #    the Memory seed and the Memory-only claim cluster are gone; the clean
    #    cluster is untouched.
    assert {r[1] for r in f_after["concept_clusters"]} == {"K-ldo"}
    assert f_after["concept_clusters"] == {
        (NB_F, "K-ldo", "ko-ldo-1", 0, "LDO"), (NB_F, "K-ldo", "ko-ldo-2", 0, "LDO"),
    }
    # 2. No derived layer names a tainted canonical id or a Memory object.
    tainted = TAINTED_CANONICALS | MEMORY_OBJECTS
    for table, rows in f_after.items():
        if table in {"knowledge_objects", "knowledge_relations",
                     "knowledge_object_sources", "unified_kg_state",
                     "promotion_candidates"}:
            continue
        for row in rows:
            assert not (set(row) & tainted), (table, row)
    assert f_after["canonical_relations"] == {(NB_F, "K-ldo", "related_to", "ko-x")}
    assert f_after["mention_edges"] == {
        (NB_F, "ko-claim-doc", "K-ldo"), (NB_F, "ko-claim-doc", "ko-x"),
    }
    # the pair bridged only by the Memory claim goes; the document-bridged stays
    assert f_after["concept_comentions"] == {(NB_F, "K-ldo", "ko-x")}
    assert {r[0] for r in f_after["concept_merge_candidates"]} == (
        {"mc-2"} | MERGE_CANDIDATES_BRIDGE)
    assert {r[0] for r in f_after["kg_conflict_candidates"]} == {"cf-3"}
    # Promotion proposals: rejected, never deleted (the audit trail stays).
    promo_before = {r[0]: r for r in for_notebook(before, NB_F)["promotion_candidates"]}
    promo_after = {r[0]: r for r in f_after["promotion_candidates"]}
    assert set(promo_after) == set(promo_before) == {p[0] for p in PROMOTIONS}
    for pid, row in promo_after.items():
        if pid in PROMOTIONS_REJECTED:
            # (id, nb, object, type, status, reason, reviewed_by, updated_at)
            assert row[:4] == promo_before[pid][:4], pid
            assert row[4:7] == ("rejected", PROMOTION_REJECTED_REASON, ""), pid
            assert row[7] != promo_before[pid][7], pid
        else:
            assert row == promo_before[pid], pid
    assert {r[0] for r in f_after["communities"]} == {"cm-2"}
    assert f_after["community_members"] == {("cm-2", NB_F, "ko-x")}
    for table in ("kg_analysis_artifacts", "kg_source_profiles",
                  "kg_community_edges", "kg_rebuild_checkpoint",
                  "kg_cluster_scratch", "kg_canonical_scratch"):
        assert f_after[table] == set(), table
    # 3. The shared object loses its Memory evidence and reverse-index row;
    #    the Memory object that received shared evidence keeps it (private).
    #    A legacy string item is kept on both kinds of object. A Memory object
    #    loses another member's Memory evidence, keeps its owner's other one.
    for object_id, expected in EVIDENCE_AFTER.items():
        assert after["evidence"][object_id] == expected, object_id
    assert after["evidence"]["ko-private"] == before["evidence"]["ko-private"] == (
        "src-mem2", "text:legacy note", "src-doc")
    kos_after = {r[1:] for r in f_after["knowledge_object_sources"]}
    assert ("ko-pll", "src-mem") not in kos_after
    assert ("ko-secret-mem", "src-mem3") not in kos_after
    assert kos_after >= {
        ("ko-pll", "src-doc"), ("ko-private", "src-mem2"), ("ko-private", "src-doc"),
        ("ko-secret-mem", "src-mem"), ("ko-secret-mem", "src-mem2"),
    }
    # the shared object whose evidence was all Memory had no reverse-index
    # row: found by the evidence scan all the same (evidence [] above)
    assert not {k for k in kos_after if k[0] == "ko-allmem"}
    # public libraries in F: the unattested (PUBF) and still-backfilling
    # (PUBR) ones lose Memory evidence that has no reverse-index row (evidence
    # scan); the attested one (PUBA) loses it through its reverse-index row,
    # which goes too
    kos_all = {r[1:] for r in after["knowledge_object_sources"]}
    assert ("ko-puba", "src-puba-mem") in {
        r[1:] for r in before["knowledge_object_sources"]}
    assert ("ko-puba", "src-puba-mem") not in kos_all
    assert {("ko-pubf", "src-pubf-doc"), ("ko-pubr", "src-pubr-doc"),
            ("ko-puba", "src-puba-doc")} <= kos_all
    # the public library OUTSIDE F: its approved promotion copy keeps the
    # evidence and the reverse-index row naming F's Memory source
    assert after["evidence"]["ko-pub"] == before["evidence"]["ko-pub"] == ("src-mem",)
    assert (NB_PUB, "ko-pub", "src-mem") in after["knowledge_object_sources"]
    # objects and relations themselves are never deleted by the migration
    assert after["knowledge_objects"] == before["knowledge_objects"]
    assert after["knowledge_relations"] == before["knowledge_relations"]
    # 4. The chunk tables hold zero rows for Memory sources; others intact.
    assert {r[1] for r in f_after["chunks"]} == {"ch-doc"}
    for table in ("chunk_embeddings", "chunk_questions", "chunk_elements"):
        assert {r[1] for r in f_after[table]} == {"ch-doc"}, table
    if "chunks_fts" in after:
        assert {r[1] for r in after["chunks_fts"] if r[0] == NB_F} == {"ch-doc"}
    # the orphan Memory source's notebook: its cluster went whole
    assert for_notebook(after, NB_O)["concept_clusters"] == set()
    # 5. F's derived-layer gates are reset once; C is byte-for-byte untouched.
    ((_, *o_state),) = for_notebook(after, NB_O)["unified_kg_state"]
    assert for_notebook(before, NB_O)["unified_kg_state"] == set()
    assert o_state[0] == 1  # dirty; inserted row, marker 0 asserted by callers
    for notebook in F_NOTEBOOKS:
        if notebook == NB_O:
            continue
        ((_, dirty, ks, cs, comm, crel, ment, civ),) = for_notebook(
            after, notebook)["unified_kg_state"]
        assert (dirty, ks, cs, comm, crel, ment) == (
            1, STATE[notebook][0] + 1, STATE[notebook][1] + 1, -1, -1, -1), notebook
        assert civ == "pre-isolation"
    before_c = for_notebook(before, NB_C)
    after_c = for_notebook(after, NB_C)
    assert after_c == before_c
    for object_id in ("ko-c-1", "ko-c-2"):
        assert after["evidence"][object_id] == before["evidence"][object_id]
    # 6. Every G notebook is only MARKED for the worker's post-readiness check
    #    (marker 2, asserted by the callers): every row of theirs, state row
    #    included, unchanged -- except that the copy, which had no state row,
    #    now has one of table defaults (what a reader took the missing row for).
    for notebook in SEED_CHECKED:
        b, a = for_notebook(before, notebook), for_notebook(after, notebook)
        if notebook == NB_COPY:
            assert b.pop("unified_kg_state") == set()
            assert a.pop("unified_kg_state") == {
                (NB_COPY, 0, 0, 0, -1, -1, -1, "")}
        assert a == b, notebook
