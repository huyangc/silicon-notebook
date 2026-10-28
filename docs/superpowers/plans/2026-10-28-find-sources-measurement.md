# find_sources metadata leg: local PostgreSQL measurement (T3)

Date: 2026-10-28. Spec: `docs/superpowers/specs/2026-10-28-paper-cards-find-sources-design_zh.md` §5.2;
plan task T3 step 2. Code measured: `SourceStore.find_sources_metadata_rows`
(`backend/app/repositories/postgres/source_store.py`, commit 02bdfdce4).

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
