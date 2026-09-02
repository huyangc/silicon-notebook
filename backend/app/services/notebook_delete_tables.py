"""Batch 3·W1 PR-3 Phase B: the phase-3 (rows) table-cleanup plan.

Design doc: ``docs/superpowers/specs/2026-09-01-batch3-w1-delete-jobization-
design_zh.md``, §1.3 (A/B/closure-external classification), §1.5 (form-one vs
form-two), §T-3 phase 3.

This module is a **pure, backend-neutral declaration** of *what* phase 3
cleans up and *in what order* — never *how* (the PostgreSQL/SQLite stores own
the actual SQL). It exists so the ordering rationale lives in ONE place
instead of being re-derived by whoever reads ``notebook_delete.py``'s phase-3
dispatch loop.

## Ground truth, not the spec's summary arithmetic

§1.3 states "A 类（52 张）" / "B 类（13 张）" and §1.5 states "形一...43 张表 /
形二...22 张表". The per-table classification lists in both sections were
verified line-for-line against a live PostgreSQL schema (every migration
through ``0048`` applied, then ``information_schema``/``pg_index`` introspected
for actual PKs and indexes) and match exactly — **except** that §1.3's own
enumerated A-class list includes ``knowledge_embeddings``, which the very next
paragraph (and §T-3.2 step 5, and this table's own docstring in
``notebook_store.py``) reclassifies as closure-external (it has NO FK to
``notebooks`` — confirmed absent from the full ``pg_constraint`` dump — which
is exactly why ``delete_row_and_orphan_embeddings`` needs an explicit orphan
delete for it and cascade cannot). Treating that appearance as a drafting
slip (the enumerated list is the thing §7's own "存疑与裁决" table already
established as authoritative over summary counts — "谓词计数...合计一致
（40）...守卫由逐行枚举清单驱动，不由计数驱动") resolves the arithmetic
(A-class 52 + B-class 13 = the 65 closure tables; ``knowledge_embeddings``
sits in the closure-external 6, not the A-class 52). This is flagged in the
implementation PR's own report rather than silently "corrected" in the spec
text — see that report's 存疑 section.

## What phase 3 does NOT touch

- The four **archive-input tables** (``ask_jobs``, ``sources``, ``reports``,
  ``source_paper_meta``): their OWN rows are phase 5's job (§T-3.2 step 4) —
  phase 3 only clears their L2 children (``source_elements``/
  ``element_embeddings``/``ask_trace_steps``/``indexing_pipeline_stage_
  sources``) so phase 5's cascade probe through them is free.
- **``answers`` — a fifth, UNDOCUMENTED archive-read dependency (found
  during Phase B implementation, not called out anywhere in the design
  doc's own text).** §1.3 lists ``answers`` as an ordinary A-class table
  with no special casing, but ``NotebookStore._retain_user_activity_
  before_delete``'s ask-projection (both backends) reads it via
  ``LEFT JOIN answers a ON a.id=j.answer_id`` to prefer the answer's own
  (usually longer/more complete) question text over the raw
  ``ask_jobs.question`` — ``COALESCE(NULLIF(a.question,''), j.question)``.
  If phase 3 deletes ``answers`` before phase 5 runs (as an earlier version
  of this module did, treating it as an ordinary A-class direct-delete
  table per §1.3's literal enumeration), that JOIN finds nothing and the
  archived ``retained_user_activity`` row silently degrades to the
  SHORTER ``ask_jobs.question`` — breaking G1/G3's "归档等价性" byte-
  identity requirement with the legacy synchronous path. Caught by an
  EXISTING, unrelated test (``tests/test_admin_questions.py::test_admin_
  questions_combines_ask_and_report_with_filters``) going red, not by a
  Phase-B-authored test. Fix: treat ``answers`` exactly like the four
  archive-input tables — leave its rows alone in phase 3; the final
  ``DELETE FROM notebooks`` in phase 5 cascades it away for free (same
  FK-CASCADE mechanism ``source_paper_meta`` already relies on today,
  since neither table gets an explicit DELETE anywhere in ``delete_row_
  and_orphan_embeddings``). ``feedback`` (FK ``answer_id`` CASCADE) is
  UNAFFECTED by this — it is never read by the archive projections, so it
  stays in phase 3's direct-delete plan (Group A, ahead of everything
  else) exactly as before; by the time phase 5's cascade reaches
  ``answers``, ``feedback`` is already empty, so that cascade leg is a
  free zero-row probe like every other already-cleared table.
- ``notebooks`` itself (phase 5) and ``notebook_delete_jobs``/
  ``notebook_delete_files`` (this job's own bookkeeping, cleaned up inside
  phase 5's transaction — see ``NotebookDeleteJobStore.cleanup_job_on``).
- The two **D-class tables** (§1.3): ``object_schemas``, ``retained_user_
  activity`` — never touched by any phase, by design.

## Why the order is not arbitrary

Every FK in this schema's closure is ``ON DELETE CASCADE`` (verified via
``pg_constraint`` — none is ``RESTRICT``/``NO ACTION`` within the closure), so
no ordering is *required* for correctness: PostgreSQL will happily cascade
regardless of the sequence phase 3 processes tables in. Ordering matters for
**cost**: if table P (a phase-3 direct-delete target) still has live child
rows in table C when P's page is deleted, that DELETE's cascade probe does
real work proportional to however many child rows exist — exactly the
unbounded-transaction problem this whole design exists to avoid. Processing
C to completion before P's own page loop starts keeps every cascade probe a
guaranteed zero-row check (the same "探查本身照付但零命中" accounting §T-3.2
uses for phase 5's final ``DELETE FROM notebooks``).

The dependency edges below are the ones inside phase 3's own scope (i.e.
excluding edges into the four archive-input tables, which stay live until
phase 5 regardless of phase-3 ordering):

  relation_embeddings        -> knowledge_relations
  chunk_embeddings            -> chunks
  chunk_questions              -> chunks
  chunk_elements                -> chunks
  feedback                        -> answers
  knowledge_source_fact_elements -> knowledge_source_facts
  agent_token_notebooks       -> agent_access_tokens
  memory_embeddings/provenance/revisions -> memory_items (nested per page)
  knowhow_cells/knowhow_cell_code -> knowhow_columns/knowhow_rows -> knowhow_tables
  indexing_pipeline_stage_sources -> indexing_pipeline_stages -> kg_build_jobs
  element_embeddings           -> source_elements (element_embeddings is
      direct-by-notebook_id so it is simply scheduled early, well before the
      source_elements chain runs)

``source_elements`` and ``ask_trace_steps`` hang off ``sources``/``ask_jobs``
(§1.3: "source_elements（仅 source_id）" / ask_trace_steps 经 job_id) — both
archive-input tables that stay live until phase 5, so these two cannot be
"a page of the parent, deleted" like the other B-class chains; they are
**read-only parent scans** (page ``sources.id``/``ask_jobs.id`` by
``notebook_id`` without deleting those rows) whose sole purpose is producing
the parent-id set that drives the child delete.

## 每事务行量上界（codex #659 round 10 P1 全链审计）

此前每一轮 codex 评审只点名一条链（round 8：两条只读父链；round 9：
``knowhow_tables``），本轮做一次性终审——对 ``_CHAINS``/``_READ_ONLY_
PARENT_CHAINS`` 里**每一条** Chain 逐条推导「一页父 id（≤500）× 每个父的
子行数上界」，不再逐轮补漏。``DirectTable``（形一/形二）天然不在审计范围
内：两种形态都是单条 DELETE 语句、单个事务、``LIMIT`` 直接命中行数，没有
第二维扇出，上界恒为 500（§1.5）。

| 链 | 子表 | 每父上界推导 | 一页（≤500 父）总上界 | 裁决 |
| --- | --- | --- | --- | --- |
| ``memory_items`` | ``memory_embeddings`` | ``memory_id`` 是该表 **PRIMARY KEY**——每个 memory_id 恰好 0 或 1 行，schema 强制 | ≤500 | 有界，单事务，不用改 |
| ``memory_items`` | ``memory_provenance`` | ``memory_id`` 是该表 **UNIQUE** 列——每个 memory_id 至多 1 行，schema 强制 | ≤500 | 有界，单事务，不用改 |
| ``memory_items`` | ``memory_revisions`` | 每次编辑追加一条修订行，``memory_id`` 上**没有**唯一/计数约束——理论上无界；"~2k/事务"是三张子表合计的实践量级估算，不是 schema 证明 | 实践上 ≪ 一批（前两张子表已经贡献 1000，第三张通常个位数×500），理论上无界 | **裁决：本轮不改**——codex 明确批准"可留"；登记为已知、影响面很小的残余债，不在本轮范围内处理 |
| ``knowhow_rows`` | ``knowhow_cells`` | ``UNIQUE(row_id, column_id)``——每行的 cell 数 = 该表列数；列数在应用层**没有任何上限**（``add_knowhow_column`` 只校验非空/去重，不校验计数） | 500 × 列数，列数无界 | **不有界 → 本轮（round 10 P1）已修**：改用 ``_drain_children_by_parent_ids``（``batch_ok`` 门控多事务） |
| ``knowhow_rows`` | ``knowhow_cell_code`` | 同上，``UNIQUE(row_id, column_id)`` | 500 × 列数，列数无界 | **不有界 → 本轮（round 10 P1）已修**，同上 |
| ``knowhow_tables`` | ``knowhow_columns``/``knowhow_changes``/``knowhow_milestones`` | 单表自身通常是小量级，但一页最多 500 张表**合计**才是真正的维度——三张子表此前全部挤在同一个事务里删完，无上界 | 500 表 × 各表列/变更/里程碑数，合计无界 | **不有界 → round 9 P2 已修**：``_drain_children_by_parent_ids`` 逐子表门控多事务 |
| ``indexing_pipeline_stages`` | ``indexing_pipeline_stage_sources`` | 每个 job 触达的 source 数没有应用层上限（一次索引管线可覆盖任意多来源） | 500 job × 每 job 来源数，合计无界；此前单条 DELETE 语句虽有界（``_CHILD_BATCH_SIZE``），但循环全部挤在同一个事务里，事务总行量无界 | **不有界 → 本轮（round 10 P1）已修**：``_drain_children_by_parent_ids`` 门控多事务 |
| ``source_elements``（只读父链） | ``source_elements`` | 一个 source 的元素数没有应用层上限（长文档可有成千上万个元素） | 500 source × 每 source 元素数，合计无界 | **不有界 → round 8 P1 已修**：``_drain_children_by_parent_ids`` 门控多事务 |
| ``ask_trace_steps``（只读父链） | ``ask_trace_steps`` | 一个 ask job 的 trace 步骤数没有硬上限 | 500 job × 每 job trace 步骤数，合计无界 | **不有界 → round 8 P1 已修**：``_drain_children_by_parent_ids`` 门控多事务 |

结论：全部 6 条 Chain 逐一过审。5 条（``knowhow_rows``/``knowhow_tables``/
``indexing_pipeline_stages``/两条只读父链）已用统一配方（只读父页 SELECT +
``_drain_children_by_parent_ids`` 的 ``batch_ok`` 门控多事务排水 + 独立最终
事务删父行）收口；``memory_items`` 的前两张子表 schema 级有界，第三张
（``memory_revisions``）理论无界但裁决保留（登记为残余债，不在本轮范围）；
全部 ``DirectTable``（形一/形二）本身单语句单事务、天然 ≤500，未纳入逐链
表格。``_CHAINS_WITH_INTERNAL_SUB_BATCHES``（``notebook_delete.py``）是这张
表"已修"裁决的可执行落点——每新增一条需要门控的链，都必须同步进那个
frozenset，否则 ``_run_chain`` 不会给它构造 ``sub_batch_ok``。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Form = Literal["one", "two"]


@dataclass(frozen=True)
class DirectTable:
    """One table phase 3 deletes directly, filtered by a single column that
    is either ``notebook_id`` (every A-class/closure-external table) or, for
    ``agent_access_tokens`` alone, ``default_notebook_id`` (§1.3: "经
    default_notebook_id" — mechanically identical to a direct notebook_id
    filter, just a differently-named column on a single-hop table, so it
    needs no separate parent-chain machinery).

    ``form`` selects the batch-delete primitive (§1.5): ``"one"`` = keyset
    page over ``pk_column`` (single-column PK/unique key) then
    ``DELETE ... WHERE pk = ANY(page) AND filter_column = value``;
    ``"two"`` = the ctid/rowid form for tables with a composite PK or no PK
    at all (``pk_column`` is ``None`` for these).
    """

    table: str
    form: Form
    pk_column: str | None = None
    filter_column: str = "notebook_id"

    def __post_init__(self) -> None:
        if self.form == "one" and not self.pk_column:
            raise ValueError(f"{self.table}: form-one requires pk_column")
        if self.form == "two" and self.pk_column:
            raise ValueError(f"{self.table}: form-two must not set pk_column")


@dataclass(frozen=True)
class Chain:
    """A B-class table group that cannot be cleaned by a single direct
    filter — the child rows are only reachable via a page of PARENT keys.
    ``name`` is the dispatch key the runner's chain functions switch on and
    the job row's ``cursor_table`` value while this chain is in progress."""

    name: str


# ---------------------------------------------------------------------------
# Group A: direct-delete tables that must finish before a later table in this
# same list would otherwise pay an expensive cascade probe against them.
# ---------------------------------------------------------------------------
_GROUP_A_PRECEDES_LATER_PARENTS: tuple[DirectTable, ...] = (
    DirectTable("relation_embeddings", "one", pk_column="relation_id"),
    DirectTable("chunk_embeddings", "one", pk_column="chunk_id"),
    DirectTable("chunk_questions", "one", pk_column="id"),
    DirectTable("chunk_elements", "two"),
    DirectTable("element_embeddings", "one", pk_column="element_id"),
    DirectTable("feedback", "one", pk_column="id"),
    DirectTable("knowledge_source_fact_elements", "two"),
    DirectTable("agent_token_notebooks", "two"),
)

# ---------------------------------------------------------------------------
# The B-class chains (bespoke functions in notebook_delete.py — the
# parent-key traversal each needs is genuinely different per chain, unlike
# the uniform "filter by one column" shape every DirectTable shares).
#
# ``knowhow_rows`` MUST run before ``knowhow_tables`` (code-review P1-D,
# post-implementation): the original single "page 500 knowhow_tables, expand
# every row/column id under them into one DELETE" shape had UNBOUNDED fanout
# — a table with many rows/columns could push one page's cell/cell_code
# DELETE past SQLite's bound-parameter ceiling or PostgreSQL's ~30s
# transaction budget (both verified by code review). Splitting the row/cell
# dimension (which scales with rows × columns — the actual unbounded axis)
# into its OWN chain, paged by ``knowhow_rows.id`` rather than
# ``knowhow_tables.id``, bounds each page's fanout to "N rows' worth of
# cells" (one row has at most as many cells as its table has columns —
# small, not "however many rows this table happens to have"). By the time
# ``knowhow_tables`` runs, every row/cell for this notebook is already gone,
# so that chain's own per-table fanout (columns/changes/milestones) is the
# ordinary "column count per table" scale the original design already
# assumed — no cells to worry about there anymore.
# ---------------------------------------------------------------------------
_CHAINS: tuple[Chain, ...] = (
    Chain("memory_items"),
    Chain("knowhow_rows"),
    Chain("knowhow_tables"),
    Chain("indexing_pipeline_stages"),
)

# ---------------------------------------------------------------------------
# Group C: direct-delete tables that are only safe/cheap to delete AFTER the
# tables above (group A + the chains) have cleared their children.
# ---------------------------------------------------------------------------
_GROUP_C_NOW_SAFE: tuple[DirectTable, ...] = (
    DirectTable("knowledge_relations", "one", pk_column="id"),
    DirectTable("chunks", "one", pk_column="id"),
    DirectTable("knowledge_source_facts", "one", pk_column="id"),
    DirectTable("kg_build_jobs", "one", pk_column="id"),
    DirectTable(
        "agent_access_tokens", "one", pk_column="id",
        filter_column="default_notebook_id",
    ),
)

# ---------------------------------------------------------------------------
# Group D: standalone direct-delete tables with no ordering constraint
# against anything else phase 3 touches (alphabetical — the order carries no
# meaning, but a deterministic order keeps resumption/testing reproducible).
# ---------------------------------------------------------------------------
_GROUP_D_STANDALONE: tuple[DirectTable, ...] = (
    DirectTable("agent_notebook_profile", "two"),
    DirectTable("agent_observations", "one", pk_column="id"),
    DirectTable("agent_profile_jobs", "two"),
    DirectTable("canonical_relations", "two"),
    DirectTable("catalog_candidates", "one", pk_column="id"),
    DirectTable("catalog_jobs", "one", pk_column="id"),
    DirectTable("chunk_element_backfills", "one", pk_column="notebook_id"),
    DirectTable("communities", "one", pk_column="id"),
    DirectTable("community_members", "two"),  # closure-external
    DirectTable("concept_clusters", "one", pk_column="id"),
    DirectTable("concept_comentions", "two"),
    DirectTable("concept_merge_candidates", "one", pk_column="id"),
    DirectTable("conversations", "one", pk_column="id"),  # closure-external
    DirectTable("extraction_runs", "one", pk_column="id"),
    DirectTable("kg_analysis_artifacts", "two"),
    DirectTable("kg_canonical_scratch", "two"),  # closure-external
    DirectTable("kg_cluster_scratch", "two"),  # closure-external
    DirectTable("kg_community_edges", "two"),
    DirectTable("kg_conflict_candidates", "one", pk_column="id"),
    DirectTable("kg_rebuild_checkpoint", "two"),
    DirectTable("kg_relation_completion_state", "two"),
    DirectTable("kg_source_profiles", "two"),
    DirectTable("knowledge_embeddings", "one", pk_column="object_id"),  # closure-external
    DirectTable("knowledge_object_sources", "two"),  # closure-external
    DirectTable("knowledge_objects", "one", pk_column="id"),
    DirectTable("knowledge_source_fact_backfills", "one", pk_column="source_id"),
    DirectTable("mention_edges", "two"),
    DirectTable("merge_review_jobs", "one", pk_column="notebook_id"),
    DirectTable("notebook_assets", "one", pk_column="id"),
    DirectTable("notebook_bases", "two"),
    DirectTable("notebook_grants", "one", pk_column="id"),
    DirectTable("notebook_members", "two"),
    DirectTable("notebook_object_schemas", "two"),
    DirectTable("notebook_share_requests", "one", pk_column="id"),
    DirectTable("promotion_candidates", "one", pk_column="id"),
    DirectTable("source_authors", "one", pk_column="id"),
    DirectTable("source_index_backfills", "one", pk_column="notebook_id"),
    DirectTable("unified_kg_state", "one", pk_column="notebook_id"),
)

# ---------------------------------------------------------------------------
# The two read-only-parent chains: sources/ask_jobs are archive-input tables
# (phase 5 deletes their rows), so these two chains only ever SELECT their
# parent's id page (never delete it) to drive their one child table's delete.
# ---------------------------------------------------------------------------
_READ_ONLY_PARENT_CHAINS: tuple[Chain, ...] = (
    Chain("source_elements"),
    Chain("ask_trace_steps"),
)

# The full, ordered phase-3 plan. ``notebook_delete.py``'s runner walks this
# list; a ``DirectTable`` step is handled by the generic form-one/form-two
# primitives, a ``Chain`` step by a same-named bespoke function.
PHASE_3_PLAN: tuple[DirectTable | Chain, ...] = (
    _GROUP_A_PRECEDES_LATER_PARENTS
    + _CHAINS
    + _GROUP_C_NOW_SAFE
    + _GROUP_D_STANDALONE
    + _READ_ONLY_PARENT_CHAINS
)

# cursor_table values are looked up by name; every entry's identifying string
# must be unique (a DirectTable's is its table name, a Chain's is "chain:" +
# its name, so the two namespaces never collide even where a chain and its
# own owned table share a name, e.g. "knowhow_tables").
CURSOR_KEYS: tuple[str, ...] = tuple(
    f"chain:{step.name}" if isinstance(step, Chain) else step.table
    for step in PHASE_3_PLAN
)

assert len(CURSOR_KEYS) == len(set(CURSOR_KEYS)), "PHASE_3_PLAN cursor keys must be unique"

# 66 = 65 closure tables (52 A-class + 13 B-class) + 6 closure-external
# tables - 4 archive-input tables (ask_jobs/sources/reports/source_paper_meta,
# whose OWN rows stay for phase 5) - 1 undocumented 5th archive dependency
# (`answers` -- see this module's docstring's "What phase 3 does NOT touch"
# section for why) = 66. Sanity-checked against a live PostgreSQL schema
# introspection at implementation time (see this module's docstring) -- kept
# as an executable guard so a future edit that silently drops/duplicates a
# table trips a test, not a code review.
EXPECTED_PHASE_3_TABLE_COUNT = 66


def phase_3_table_names() -> frozenset[str]:
    """Every distinct table name phase 3 ever issues a DELETE against,
    across both direct tables and chain-owned/chain-child tables. Used by
    tests to assert full 65-closure + 6-external coverage minus the four
    archive-input tables."""
    names: set[str] = set()
    for step in PHASE_3_PLAN:
        if isinstance(step, DirectTable):
            names.add(step.table)
    names.update(
        {
            # memory_items chain
            "memory_items", "memory_embeddings", "memory_provenance",
            "memory_revisions",
            # knowhow_rows chain (P1-D split: the row/cell dimension)
            "knowhow_rows", "knowhow_cells", "knowhow_cell_code",
            # knowhow_tables chain (runs after knowhow_rows; the
            # per-table dimension — rows/cells are already gone)
            "knowhow_tables", "knowhow_columns", "knowhow_changes",
            "knowhow_milestones",
            # indexing_pipeline_stages chain
            "indexing_pipeline_stages", "indexing_pipeline_stage_sources",
            # read-only-parent chains (only the CHILD table is deleted; the
            # parent -- sources/ask_jobs -- is read-only here)
            "source_elements", "ask_trace_steps",
        }
    )
    return frozenset(names)
