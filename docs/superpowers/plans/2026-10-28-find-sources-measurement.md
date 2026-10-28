# find_sources metadata leg: local PostgreSQL measurement (T3)

Date: 2026-10-28. Spec: `docs/superpowers/specs/2026-10-28-paper-cards-find-sources-design_zh.md` §5.2;
plan task T3 step 2. Code measured: `SourceStore.find_sources_metadata_rows`
(`backend/app/repositories/postgres/source_store.py`, commit 9ca95f83f).

## Corpus and method

- Local PostgreSQL (the developer machine, Darwin), one schema built through `PostgresMigrator`
  (every shipped index present, including 0048's three trigram GINs), then bulk-loaded with COPY and
  `VACUUM ANALYZE` (the GIN pending-list artifact 0048 documents is removed before measuring).
- 69,000 sources in four notebooks: **nb-big 49,000**, **nb-mid 10,000**, two 5,000 neighbours.
  1 in 500 sources is a hidden Memory/Knowhow projection. Titles 6-12 words, summaries 35-60 words,
  file names derived from titles; 80% of visible sources carry paper meta (70% `is_paper=1`,
  10% `is_paper=0`) with venue, year 2010-2025 and five keywords; 4 author rows per meta row
  (220,180 author rows; production's ratio is ~4.3). Term frequencies: `model` in ~30% of titles and
  summaries, `gan` ~5%, `diffusion` ~2%, a CJK pair in ~15%, `zqxjtitle` in one title.
- Each case calls the store once through a statement-capturing connection, then runs
  `EXPLAIN (ANALYZE, BUFFERS)` on the scope statement, on each term leg separately and on the whole
  `UNION ALL`; best of three runs, planning + execution, in ms. "wall" is the store call itself
  (both statements, row transfer and the Python re-check/assembly).
- Run twice: with the 0048 indexes, and after `DROP INDEX` of all three.
- Script: kept outside the repository (scratchpad `bench_find_sources.py`); it is the corpus
  generator above plus the capture/EXPLAIN loop.

## Results: nb-big (49k sources)

| case | 0048 | scope | title | summary | author | paper_title | venue | keywords | UNION ALL | wall |
|---|---|---|---|---|---|---|---|---|---|---|
| `zqxjtitle` papers | with | 19.7 | 0.07 | 34.1 | 0.04 | 0.04 | 2.9 | 14.0 | 22.9 | 151 |
| `zqxjtitle` papers | without | 16.9 | 17.9 | 29.7 | 12.5 | 6.3 | 2.8 | 14.4 | 34.6 | 146 |
| `diffusion` papers | with | 19.0 | 0.83 | 33.0 | 0.02 | 0.43 | 2.9 | 15.5 | 23.0 | 138 |
| `diffusion` papers | without | 16.1 | 19.1 | 30.6 | 12.2 | 7.3 | 3.1 | 17.1 | 36.0 | 148 |
| `gan` papers | with | 19.6 | 1.4 | 31.7 | 0.28 | 0.76 | 2.8 | 16.5 | 24.6 | 189 |
| `gan` papers | without | 16.4 | 19.1 | 29.1 | 12.6 | 6.4 | 2.8 | 16.3 | 37.4 | 186 |
| `wu` (2 chars) papers | with | 17.2 | 17.2 | 27.5 | 13.1 | 6.3 | 2.8 | 14.4 | 83.0 | 272 |
| `wu` (2 chars) papers | without | 16.1 | 19.8 | 27.8 | 13.0 | 6.4 | 2.8 | 14.7 | 86.3 | 250 |
| `图像` papers | with | 16.3 | 16.0 | 25.3 | 12.1 | 5.8 | 2.8 | 15.8 | 34.9 | 183 |
| `图像` papers | without | 16.4 | 19.2 | 26.2 | 12.3 | 5.8 | 2.8 | 15.3 | 32.6 | 162 |
| `diffusion,gan,model` all | with | 19.1 | 12.2 | **73.0** | 0.32 | 7.4 | 5.9 | 43.1 | 144.2 | 529 |
| `diffusion,gan,model` all | without | 19.7 | 34.5 | **74.5** | 26.4 | 16.4 | 6.1 | 41.6 | 79.7 | 423 |
| filter venue=cvpr, year>=2020 | with / without | 3.1 / 3.0 | | | | | | | | 30 / 22 |
| filter author=wang | with / without | 17.7 / 28.4 | | | | | | | | 36 / 51 |
| `diffusion`, allow list = all 48,902 visible ids (MCP) | with | 22.8 | 0.85 | 29.6 | 0.02 | 0.42 | 2.9 | 15.8 | 22.3 | 142 |
| `diffusion`, allow list = all visible ids | without | 22.3 | 19.9 | 29.5 | 12.3 | 6.4 | 2.8 | 15.6 | 34.7 | 157 |
| `diffusion`, allow list = 49 ids (narrowed run) | with | 0.16 | 0.9 | 28.9 | 0.02 | 0.41 | 2.8 | 15.9 | 21.9 | 53 |
| `diffusion`, allow list = 49 ids | without | 0.14 | 17.6 | 28.6 | 12.2 | 6.3 | 2.8 | 15.5 | 34.3 | 53 |

## Results: nb-mid (10k sources)

Every leg is under 15 ms in every case, with or without the 0048 indexes (largest: summary 14.6 ms
on the three-term case; scope 3.0-5.1 ms; UNION ALL 8-18 ms; wall 3-102 ms).

## Readings

1. **The 0048 indexes are used by the new legs as designed** (title/file_name, author, paper_title
   drop from 6-20 ms to well under 1 ms on selective terms). Without them every one of those legs
   is still a notebook-bounded scan under 35 ms on the 49k notebook; the queries stay correct.
2. **Over 50 ms: only the summary leg**, and only with several terms one of which is common
   (73-75 ms, with or without 0048 -- 0048 indexes no summary). The full UNION ALL of that case is
   144 ms with 0048 (the planner trusts the title GIN on a 30% term) and 80 ms without.
   Per the plan's rule this adds one hotpath index, **batch 8 `idx_sources_nb_summary_trgm`**
   (`(notebook_id public.text_ops, lower(summary) public.gin_trgm_ops) WHERE source_type NOT IN
   ('memory','knowhow')`), measured on the same corpus:

   | summary leg on nb-big | without batch 8 | with batch 8 |
   |---|---|---|
   | `diffusion` | 30 ms (seq) | 1.0-1.5 ms (Bitmap Index Scan) |
   | `diffusion OR gan OR model` | 73 ms | 30-32 ms (BitmapOr of three scans) |
   | `zqxjtitle` (rare) | 30 ms | 0.04 ms |

   Write amplification: 82 MB of index over a 47 MB `sources` heap on this corpus (~1.7x; summaries
   carry many more trigrams than titles). It is operator-built only (`scripts/build_hotpath_indexes.py
   --apply`), no migration, like any optional hotpath index; the leg works without it.
3. **venue and keywords stay unindexed**: venue 3-6 ms, keywords 14-17 ms on one term and 43 ms on
   the three-term case -- under the threshold.
4. **The scope statement** (filtered scope, every visible source of the library) is 16-23 ms on
   49k, 3 ms on 10k, and 0.15 ms with a 49-id allow list (`member_of` custom plan); binding the full
   48,902-id MCP allow list costs ~3-6 ms over the unbound read.
5. **Wall time is dominated by row volume, not by SQL**: on the 49k notebook a single-term call
   returns ~44k scope rows and costs 140-190 ms end to end (SQL ~40-60 ms); the pathological
   three-common-term call returns 43k matched rows and costs 420-530 ms, mostly row transfer and the
   Python re-check. This is the price of the design's "metadata first, then restrict the semantic
   leg to the filtered scope" on a whole-library scope; a narrowed run pays ~50 ms. Not addressed
   here (no index helps it); flagged for review together with T5's per-run `find_sources` budget.

## Re-measurement after the review fixes (end to end)

Code compared: **before** = `cc41c2298` (the store returning full display rows for the whole filtered
scope, keywords matched on the jsonb text); **after** = the review-fix commit (the scope statement
returns `id, pub_year, pending` only, a counts statement reports `scope_total` / `visible_total` /
`meta_pending_excluded`, keywords matched per array element, term legs bound to the scope's ids only
when the filters narrowed it to at most 10% of the library, display rows hydrated for one page only).
Same corpus, regenerated (same seed). Per case, best of three: **store** = one
`find_sources_metadata_rows` call; **UNION ALL** = `EXPLAIN (ANALYZE)` of the whole term-leg statement
(planning + execution); **finder** = `SourceFinder.find` page 1 (20 items) end to end -- source set,
metadata leg, ranking, page hydration (listing rows, paper meta, cards, card bases); no `query`, so no
semantic leg. "list" cases pass an explicit allow list (MCP: every visible id; narrowed: 49/50 ids);
the others read without a list and are cut to the run plan. Result sets (found / scope) are identical
before and after in every case. Times in ms.

Indexes: **0048+b8** = the three 0048 trigram GINs plus batch 8 `idx_sources_nb_summary_trgm` (the
missing "all optional hotpath indexes together" measurement); **none** = no trigram index.

| indexes | notebook | case | store before | store after | UNION ALL before | UNION ALL after | finder before | finder after | found / scope |
|---|---|---|---|---|---|---|---|---|---|
| 0048+b8 | nb-big | `diffusion` papers | 130.5 | 95.7 | 21.2 | 22.5 | 211.5 | 149.5 | 4886 / 44051 |
| 0048+b8 | nb-big | `gan` papers | 153.6 | 127.3 | 23.3 | 35.8 | 241.9 | 192.2 | 11957 / 44051 |
| 0048+b8 | nb-big | `wu` papers | 257.4 | 212.5 | 90.3 | 94.3 | 322.2 | 289.5 | 21094 / 44051 |
| 0048+b8 | nb-big | `diffusion,gan,model` all | 447.2 | 385.5 | 105.0 | 103.5 | 551.2 | 498.1 | 43059 / 48902 |
| 0048+b8 | nb-big | `diffusion` papers, year>=2020 | 65.8 | 62.9 | 21.5 | 22.2 | 85.2 | 91.5 | 1661 / 12825 |
| 0048+b8 | nb-big | filter venue=cvpr, year>=2020 | 4.6 | 15.9 | -- | -- | 17.3 | 31.3 | 790 / 790 |
| 0048+b8 | nb-big | `diffusion` papers, MCP list | 134.2 | 138.4 | 21.4 | 22.3 | 172.4 | 179.1 | 4886 / 44051 |
| 0048+b8 | nb-big | `diffusion` all, 49-id list | 24.7 | 16.0 | 20.9 | 1.8 | 24.7 | 17.2 | 6 / 49 |
| 0048+b8 | nb-mid | `diffusion` papers | 24.4 | 19.6 | 4.8 | 6.8 | 33.7 | 29.5 | 1040 / 8999 |
| 0048+b8 | nb-mid | `diffusion,gan,model` all | 80.5 | 66.6 | 16.9 | 16.5 | 115.9 | 79.3 | 8773 / 9980 |
| 0048+b8 | nb-mid | `wu` papers | 456.9 | 451.3 | 17.7 | 20.5 | 477.6 | 469.5 | 4265 / 8999 |
| 0048+b8 | nb-mid | `diffusion` all, 50-id list | 6.9 | 4.2 | 4.9 | 0.7 | 8.1 | 5.3 | 4 / 50 |
| none | nb-big | `diffusion` papers | 145.3 | 120.7 | 36.7 | 50.0 | 213.2 | 176.0 | 4886 / 44051 |
| none | nb-big | `diffusion,gan,model` all | 424.7 | 363.6 | 82.7 | 96.3 | 520.9 | 477.6 | 43059 / 48902 |
| none | nb-big | `diffusion` papers, year>=2020 | 80.6 | 88.7 | 36.3 | 47.4 | 100.5 | 125.9 | 1661 / 12825 |
| none | nb-big | filter venue=cvpr, year>=2020 | 4.8 | 16.0 | -- | -- | 17.5 | 32.7 | 790 / 790 |
| none | nb-big | `diffusion` papers, MCP list | 147.4 | 167.9 | 35.0 | 47.1 | 180.7 | 204.1 | 4886 / 44051 |
| none | nb-big | `diffusion` all, 49-id list | 38.6 | 14.0 | 35.7 | 0.4 | 41.4 | 15.1 | 6 / 49 |
| none | nb-mid | `diffusion` papers | 29.0 | 24.6 | 8.8 | 10.9 | 37.6 | 32.8 | 1040 / 8999 |

(0048 alone sits between the two; the full per-state dump is in the scratch `e2e.json` / `e2e2.json`.)

Readings:

1. **Whole-library term searches get 10-30% faster end to end** (nb-big `diffusion` 212 -> 150 ms with
   all indexes, three common terms 551 -> 498 ms): the scope statement no longer carries titles and meta
   for 44k rows, and the finder no longer copies them; the page alone is hydrated (5-10 ms).
2. **A narrowed scope binds the term legs**: 49 ids -> the UNION ALL drops from 21-36 ms to 0.4-1.8 ms.
   Binding a large share does not pay: with an earlier 50% cut, the 12.8k-id `year>=2020` scope (26%)
   made the UNION ALL 21 -> 52 ms, so the cut is 10% (`find_sources_rows.TERM_LEG_BIND_MAX_SHARE`).
3. **The counts statement costs ~11 ms on 49k** (visible ∧ allow-list ∧ pending counts over the
   visible sources). It is what makes `meta_pending_excluded` and the "filters narrowed the list"
   attestation possible; it is skipped for `kind=all` without filters or list. The pure-filter case
   pays it in full (4.6 -> 16 ms store, 17 -> 31 ms end to end) and the MCP whole-library list pays it
   plus the 44k-id `FILTER` (+7-23 ms end to end). Both stay far under the 50 ms index threshold per statement.
4. **Keywords per element** (`jsonb_array_elements`, five keywords per meta row) adds 1-13 ms to the
   UNION ALL on 49k (it was 14-17 ms on the jsonb text); still unindexed, still under 50 ms.
5. **All optional indexes together (0048 + batch 8)**: the three-common-term UNION ALL is 104 ms -- the
   planner still trusts the title GIN on a 30% term (the 144 ms of the first measurement; 82-96 ms
   without any trigram index). Single selective terms are 21-36 ms. No new index is warranted.
6. **Open, pre-existing**: nb-mid `wu` (a 2-character term) with the 0048 indexes costs ~450 ms in the
   store call before and after, while EXPLAIN of each statement is ~20 ms. It reproduces only through
   the driver's repeated executions (best of three on one connection), which points at psycopg's
   auto-prepare switching the unbound term statement to a generic plan for a term too short for
   trigrams. Not introduced here; flagged for the T10/hotpath follow-up.
