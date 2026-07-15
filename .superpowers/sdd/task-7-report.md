# Task 7 Report: 前端（类型 + 详情论文块 + 搜索占位 + 补抽按钮）

## Summary

Implemented all 4 JSX/CSS/type changes from the brief, verbatim except for the
backfill button's `className`, which the brief explicitly left for me to
choose by inspecting the codebase's existing secondary-button conventions.

## Files changed

- `frontend/app/workspace-model.ts` — added `PaperAuthor`/`PaperMeta` types
  before `SourceSummary`; added `authors?/pub_year?/venue?/paper_meta?` to
  `SourceSummary`. Field names cross-checked byte-for-byte against
  `backend/app/models/schemas.py` (`PaperAuthor`, `PaperMeta`, `SourceSummary`,
  `SourceDetail`) — exact match, no invented names.
- `frontend/app/page.tsx`:
  - Search placeholder (was `:3575`) → `"搜索来源（标题/作者/文件名）"`.
  - New backfill button inserted as its own `{!isReader && (...)}` block
    right after the existing `添加来源` button block (was `:3487-3491`),
    before the KG-build block — matches the file's existing pattern of one
    top-level gated block per action (not a shared fragment).
  - Paper metadata block inserted into the detail modal right after
    `source-detail-meta` (was `:4412-4417`), before `extraction_warning`.
    Verbatim from the brief.
- `frontend/app/globals.css` — appended the brief's `.source-detail-paper`
  block immediately after `.source-detail-meta` (was `:4293-4297`), verbatim.
  Note: the brief's CSS block contains **zero hardcoded color values** (only
  `currentColor`/`opacity`/layout), so there was nothing to adapt to file
  variables — appended as-is.

## Backfill button class decision

The brief said: "class 复用该文件现有次级按钮样式，先 grep 附近按钮的
className 选现成的". I grepped every button className in page.tsx and
found:
- `add-source-button` (used 3x in this exact panel already) — bold solid
  teal 44px CTA. The orchestrating task explicitly warned against reusing
  this ("do NOT invent a new visual language" was paired with "reuse an
  existing **secondary/quiet**" style, implying add-source-button itself —
  the strong CTA — is not the target).
- `ghost-button` — used in the KG rail (LLM 预审 buttons) but has **zero**
  CSS definition anywhere in the repo (confirmed via grep across all `.css`
  files) — an orphaned/unstyled class. Would render as a bare native button,
  inconsistent with the polish bar.
- `.button` / `.button.secondary` — real, existing, already-defined CSS
  (`globals.css:3303-3318`): `.button` is a blue primary CTA already used in
  `report-view.tsx` (two call sites); `.button.secondary` is a proper
  bordered/white/quiet variant (`border-color: var(--line); color:
  var(--ink); background: #fff`) that is currently unused in any `.tsx` but
  fully defined — i.e., reusing it adds zero new CSS.

Chose `className="button secondary"`: it is a genuine existing "secondary"
design token (not invented), visually quieter than `add-source-button`, and
— because `.sources-body` is a flex column (default `align-items: stretch`)
— it still stretches to the same width as the buttons above it, so it stays
aligned with the rest of the action stack (UI polish bar: 对齐).

Also added `type="button"` (not in the brief's snippet) for consistency with
every other button in this panel, which all declare it explicitly.

## Verification

- `cd frontend && npm ci --no-audit --no-fund` — succeeded (459 packages, 5s;
  network was available in this sandbox).
- `npx tsc --noEmit` (== `npm run lint`) — **0 errors**, exit code 0.
- `npm test` (existing suite, `node --test` over all `*.test.mjs` under
  `app/`) — **281/281 pass**, 0 fail. No regressions in
  `workspace-layout.test.mjs` or any other suite from the new button/JSX
  block.
- Curly-quote guard: `git diff frontend/app/page.tsx | grep -c '^-.*[""]'`
  as literally copy-pasted from the brief gave a **false positive** (matched
  1 line: the placeholder line, which only contains straight ASCII `"`
  around the JSX attribute — no actual curly quote character). Re-verified
  with explicit Unicode codepoints: `grep -cP '^-.*[\x{201C}\x{201D}]'` →
  **0**. No real curly quotes (U+201C / U+201D) were touched anywhere in the
  diff. This looks like a shell/terminal quote-normalization artifact in how
  the brief's exact command string gets typed/pasted, not a real signal —
  worth flagging so the check isn't trusted blindly at face value next time.

## Self-review checklist

- [x] Types match backend field names exactly (`pub_year`, `paper_meta.year`,
      etc.) — cross-checked against `backend/app/models/schemas.py:247-260,
      272-296,330-333`.
- [x] Paper block renders only when `paper_meta?.is_paper`; every sub-field
      (`title`/`authors`/`venue`+`year`/`doi`/`keywords`) individually
      conditional; DOI link is `https://doi.org/${doi}` with `target="_blank"
      rel="noreferrer"`.
- [x] Backfill button: gated on `!isReader` (same gate the file already uses
      for `添加来源`/KG-build buttons); toast text is exactly
      `已提交 ${queued} 篇论文的信息补全` / `论文信息已是最新，无需补全`;
      error path is bare `reportError(err)`, same as every other inline
      async action in this file.
- [x] No existing curly quotes touched (see false-positive note above; real
      check via explicit codepoints = 0).
- [x] CSS uses the file's existing variables — no new colors were introduced
      at all (brief's block had none to begin with).
- [x] `api()` path has no leading `/api` (`API_BASE` already ends in `/api`,
      confirmed against `backend/app/main.py:238-240` — `router` is mounted
      with `prefix="/api"`, and the existing `buildKg`/`rebuildKg` helpers at
      `page.tsx:502-503` follow the same no-prefix convention).

## Commit

`feat(frontend): paper metadata display, author search hint + backfill entry`
