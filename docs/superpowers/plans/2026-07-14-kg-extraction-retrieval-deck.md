# KG Extraction and Retrieval Deck Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and verify a 14-slide Chinese PowerPoint that presents silicon-notebook's current KG extraction, retrieval, reasoning, and governance flow in a premium technology-product style.

**Architecture:** A plain JavaScript ES module uses `@oai/artifact-tool` to compose a fully editable 16:9 deck. Repository documentation and implementation files provide factual content; the deck is exported to `outputs/`, rendered to PNG, inspected slide-by-slide, and validated for canvas overflow.

**Tech Stack:** JavaScript ES modules, `@oai/artifact-tool`, bundled presentation rendering and QA scripts, LibreOffice/Poppler through the bundled runtime.

## Global Constraints

- The deck contains 14 slides and uses Chinese audience-facing copy.
- The final output is `/Users/hzf/workspace/silicon_notebook/outputs/silicon-notebook-KG抽取与检索方法流程.pptx`.
- Use a 16:9 dark navy visual system with ice-blue/electric-cyan accents and sparse warm-gold emphasis.
- Deck titles are at least 50pt, slide titles at least 35pt, mid-level text at least 24pt, and body text at least 16pt.
- Use only current, verified capabilities documented in `README_zh.md`, `fangan_done.md`, and the corresponding backend implementation.
- Do not claim heuristic KG extraction when no LLM is configured; the extraction run records `no-llm` and creates no synthetic KG.
- Do not present retired Scenario, Case, Checklist, or Studio features as current capabilities.
- Use native PowerPoint shapes for simple flows; create connectors before nodes and fix every unintended overlap or overflow.

---

### Task 1: Prepare content ledger and artifact workspace

**Files:**
- Create: external scratch `tmp/source-notes.txt`
- Create: external scratch `tmp/deck-plan.txt`
- Create: external scratch `tmp/deck.mjs`
- Create: `/Users/hzf/workspace/silicon_notebook/outputs/`

**Interfaces:**
- Consumes: `README_zh.md`, `fangan_done.md`, `backend/app/services/kg_ingest.py`, `backend/app/services/graph_retrieval.py`, `backend/app/services/kg/graph_reason.py`, `backend/app/services/kg/follow_chain.py`.
- Produces: a factual source ledger and a slide-by-slide content map used by the deck module.

- [ ] **Step 1: Resolve the host scratch directory and initialize the workspace**

Run `node -p "require('node:os').tmpdir()"`, create the presentation workspace below it, and run `setup_artifact_tool_workspace.mjs --workspace <tmp-dir>`.

- [ ] **Step 2: Write the content ledger**

Record the exact source files and the verified statements used for parsing, windowing, object types, evidence binding, retrieval modes, PPR, bounded graph traversal, `follow_chain`, tiers, governance, and scale indexing.

- [ ] **Step 3: Write the slide content map**

Give each of the 14 slides one takeaway title, one visual job, and no more than four supporting points.

### Task 2: Compose and export the first deck

**Files:**
- Modify: external scratch `tmp/deck.mjs`
- Create: `/Users/hzf/workspace/silicon_notebook/outputs/silicon-notebook-KG抽取与检索方法流程.pptx`

**Interfaces:**
- Consumes: the source ledger and slide map from Task 1.
- Produces: an editable PowerPoint with consistent typography, footer, page markers, diagrams, and product copy.

- [ ] **Step 1: Implement reusable slide primitives**

Create helpers for backgrounds, headings, footers, section labels, evidence bands, node marks, connectors, and controlled glow effects. Keep all helpers inside the single deck module so the final PPTX has no runtime dependency.

- [ ] **Step 2: Implement slides 1–8**

Build the product opening, end-to-end overview, parsing, adaptive window extraction, four object types, evidence grounding, and extraction quality controls.

- [ ] **Step 3: Implement slides 9–14**

Build unified KG/tier federation, retrieval modes, evidence-network recall, bounded reasoning, concrete semiconductor query journey, and the governance/scale conclusion.

- [ ] **Step 4: Export the PPTX**

Run `node <tmp-dir>/deck.mjs` and verify that the expected final file exists and is non-empty.

### Task 3: Render and inspect every slide

**Files:**
- Create: external scratch `tmp/preview/slide-*.png`
- Create: external scratch `tmp/qa/montage.png`
- Create: external scratch `tmp/qa/qa-ledger.txt`

**Interfaces:**
- Consumes: the first exported PPTX.
- Produces: a page-level QA ledger and a list of required deck revisions.

- [ ] **Step 1: Render all slides**

Run `render_slides.py <final-pptx>` and confirm 14 PNG files are produced.

- [ ] **Step 2: Create and inspect the montage**

Run `create_montage.py --input_dir <preview-dir> --output_file <qa-dir>/montage.png`; inspect the montage for narrative rhythm and consistency.

- [ ] **Step 3: Inspect every slide at full size**

Open each `slide-*.png` and record title wrapping, clipped text, malformed glyphs, low contrast, connector crossings, density, and visual imbalance in `qa-ledger.txt`.

- [ ] **Step 4: Run structural overflow checks**

Run `slides_test.py <final-pptx>` and require zero slide-canvas overflow errors.

### Task 4: Revise, re-render, and deliver

**Files:**
- Modify: external scratch `tmp/deck.mjs`
- Replace: `/Users/hzf/workspace/silicon_notebook/outputs/silicon-notebook-KG抽取与检索方法流程.pptx`
- Update: external scratch `tmp/qa/qa-ledger.txt`

**Interfaces:**
- Consumes: Task 3's QA findings.
- Produces: the final verified PPTX.

- [ ] **Step 1: Fix every QA finding**

Shorten copy or adjust layout before reducing font sizes; correct all unintended overlap, clipping, weak contrast, and connector placement.

- [ ] **Step 2: Export and render the revised deck**

Run the deck module again, render all 14 slides again, and inspect the changed slides at full size.

- [ ] **Step 3: Re-run structural validation**

Run `slides_test.py <final-pptx>` and confirm a successful exit with no overflow errors.

- [ ] **Step 4: Verify final artifact**

Confirm the final PPTX exists, opens through the renderer, contains 14 slides, and retains editable text and native diagram shapes.
