# KG Visualization View (Frontend) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Add a full-screen, inspect-the-unified-KG view to the silicon-notebook frontend — concept-level force graph + filters/search + a "pending merges" review list + a per-concept evidence drill-down — reading the new `/unified-kg` backend endpoints.

**Architecture:** All UI lives in the single `frontend/app/page.tsx` (existing pattern). A new `kgViewOpen` overlay (full-screen, like the existing `utility-modal` idiom) renders 3 zones: left filter/search/pending rail, center `react-force-graph-2d` canvas (dynamically imported, no SSR), right concept-detail panel. The existing `关系图` button is repurposed to open this view (replacing the old list-style `/graph` modal). Styling goes in `app/globals.css`. There is no frontend test framework — the gate is `npm run lint` (`tsc --noEmit`) plus a manual browser check.

**Tech Stack:** Next.js 15.3 (App Router, single-page), React, `react-force-graph-2d` (canvas/WebGL graph), global CSS.

**Spec:** `docs/superpowers/specs/2026-06-02-kg-unified-and-viz-design.md` §3. **Backend endpoints (live on master):**
- `POST /api/notebooks/{id}/unified-kg/rebuild` → `{clusters:number}`
- `GET /api/notebooks/{id}/unified-kg?level=concept` → `{nodes:[{id,object_type,payload:{name,...}}], edges:[{source_object_id,target_object_id,edge_type}]}`
- `GET /api/notebooks/{id}/concepts/{canonical_id}/detail` → `{canonical_id,canonical_name,members:[{id,object_type,payload,evidence}],attached:[{id,object_type,payload,evidence,edge_type}],evidence:[{source_id,source_title,element_id,element_type,location_label,quoted_span,confidence}]}`
- `GET /api/notebooks/{id}/unified-kg/pending-merges` → `[{id,canonical_a,canonical_b,score,status}]`
- `POST /api/notebooks/{id}/unified-kg/merges/{candidate_id}/confirm` → `{ok:true}`
- `POST /api/notebooks/{id}/unified-kg/merges/{candidate_id}/reject` → `{ok:true}`

**Existing anchors in `page.tsx`:** `const API_BASE` (line 8); `async function api<T>(path, options)` (line 286); the `关系图` button (line ~1289, `onClick={() => openGraph().catch(reportError)}`); `openGraph()`/`graphOpen` state (lines 399/1016); the old graph `utility-modal` block (line ~1827); `reportError` helper; `currentNotebookId` state holding the selected notebook id.

> **Note for all tasks:** the frontend has NO automated tests. For each task: implement, run `cd frontend && npm run lint` (must be **0 errors**), and report. Manual visual verification happens at the end (Task 6) in the browser. "Write the failing test" steps are replaced by "define the type/signature first, then implement against it."

---

## Task 1: Add `react-force-graph-2d` + no-SSR wrapper

**Files:**
- Modify: `frontend/package.json` (via `npm install`)
- Modify: `frontend/app/page.tsx` (add the dynamic import near the top, after existing imports)

- [ ] **Step 1: Install the dependency**

Run (MAIN session — subagents have no network; if you are a subagent and `npm install` fails with a network error, STOP and report NEEDS_CONTEXT so the controller installs it):
```bash
cd frontend && npm install react-force-graph-2d
```
Expected: it's added to `dependencies` in `package.json`; `node_modules/react-force-graph-2d` exists.

- [ ] **Step 2: Add a no-SSR dynamic import in `page.tsx`**

`react-force-graph-2d` touches `window`/`canvas`, so it must not server-render. At the TOP of `page.tsx`, after the existing `import` lines, add:
```tsx
import dynamic from "next/dynamic";
// react-force-graph-2d uses canvas/window; load client-side only.
const ForceGraph2D = dynamic(() => import("react-force-graph-2d"), { ssr: false });
```
(If `"use client"` is at the very top of the file, keep it first; `import dynamic` and the others go below it.)

- [ ] **Step 3: Typecheck**

Run: `cd frontend && npm run lint`
Expected: 0 errors. (If `react-force-graph-2d` ships no types and tsc complains about an implicit `any` module, add `// @ts-expect-error - react-force-graph-2d has no bundled types` directly above the dynamic import, OR create `frontend/app/react-force-graph-2d.d.ts` with `declare module "react-force-graph-2d";`. Prefer the `.d.ts` declaration.)

- [ ] **Step 4: Commit**

```bash
git add frontend/package.json frontend/package-lock.json frontend/app/page.tsx frontend/app/react-force-graph-2d.d.ts
git commit -m "feat(kg-viz): add react-force-graph-2d (no-SSR dynamic import)"
```

---

## Task 2: Types + API functions for the unified KG

**Files:**
- Modify: `frontend/app/page.tsx` (add types near the other `type ...` declarations ~line 145–222; add fetch helpers near other `async function` API wrappers)

- [ ] **Step 1: Add the TypeScript types** (place with the other `type` declarations)

```tsx
type UnifiedConceptNode = { id: string; object_type: string; payload: { name?: string; [k: string]: unknown } };
type UnifiedEdge = { source_object_id: string; target_object_id: string; edge_type: string };
type UnifiedGraphResp = { nodes: UnifiedConceptNode[]; edges: UnifiedEdge[] };
type EvidenceItem = { source_id: string; source_title: string; element_id: string; element_type: string; location_label: string; quoted_span: string; confidence: number };
type KgObject = { id: string; object_type: string; payload: { name?: string; section_path?: string; [k: string]: unknown }; evidence: EvidenceItem[]; edge_type?: string };
type ConceptDetailResp = { canonical_id: string; canonical_name: string; members: KgObject[]; attached: KgObject[]; evidence: EvidenceItem[] };
type PendingMerge = { id: string; canonical_a: string; canonical_b: string; score: number; status: string };
// graph-ready node/link shapes for react-force-graph-2d
type FgNode = { id: string; name: string; type: string; val: number };
type FgLink = { source: string; target: string; label: string };
```

- [ ] **Step 2: Add the API helper functions** (near the other `api<...>(...)` call sites)

```tsx
const rebuildUnifiedKg = (nb: string) => api<{ clusters: number }>(`/notebooks/${nb}/unified-kg/rebuild`, { method: "POST" });
const fetchUnifiedGraph = (nb: string) => api<UnifiedGraphResp>(`/notebooks/${nb}/unified-kg?level=concept`);
const fetchConceptDetail = (nb: string, cid: string) => api<ConceptDetailResp>(`/notebooks/${nb}/concepts/${encodeURIComponent(cid)}/detail`);
const fetchPendingMerges = (nb: string) => api<PendingMerge[]>(`/notebooks/${nb}/unified-kg/pending-merges`);
const confirmMergeApi = (nb: string, cid: string) => api<{ ok: boolean }>(`/notebooks/${nb}/unified-kg/merges/${encodeURIComponent(cid)}/confirm`, { method: "POST" });
const rejectMergeApi = (nb: string, cid: string) => api<{ ok: boolean }>(`/notebooks/${nb}/unified-kg/merges/${encodeURIComponent(cid)}/reject`, { method: "POST" });
```

- [ ] **Step 3: Typecheck + commit**

Run: `cd frontend && npm run lint` (expect 0 errors).
```bash
git add frontend/app/page.tsx && git commit -m "feat(kg-viz): unified-KG types + API helpers"
```

---

## Task 3: The KG view — state + data loading + 3-zone shell

**Files:**
- Modify: `frontend/app/page.tsx` (component state near the other `useState`s ~line 398; an `openKgView` loader near `openGraph`; the overlay JSX near the existing graph `utility-modal` ~line 1827)

- [ ] **Step 1: Add state + a derived graph transform** (with the other `useState` hooks in the main component)

```tsx
const [kgViewOpen, setKgViewOpen] = useState(false);
const [uGraph, setUGraph] = useState<UnifiedGraphResp | null>(null);
const [pendingMerges, setPendingMerges] = useState<PendingMerge[]>([]);
const [selectedConcept, setSelectedConcept] = useState<string | null>(null);
const [conceptDetail, setConceptDetail] = useState<ConceptDetailResp | null>(null);
const [kgSearch, setKgSearch] = useState("");
```

- [ ] **Step 2: Add the loader** (near `openGraph`, using `currentNotebookId` and `reportError`)

```tsx
async function openKgView() {
  if (!currentNotebookId) return;
  setKgViewOpen(true);
  setSelectedConcept(null); setConceptDetail(null);
  try {
    await rebuildUnifiedKg(currentNotebookId);           // ensure clusters are current
    const [g, pend] = await Promise.all([
      fetchUnifiedGraph(currentNotebookId),
      fetchPendingMerges(currentNotebookId),
    ]);
    setUGraph(g); setPendingMerges(pend);
  } catch (err) { reportError(err); }
}

async function selectConcept(canonicalId: string) {
  if (!currentNotebookId) return;
  setSelectedConcept(canonicalId);
  try { setConceptDetail(await fetchConceptDetail(currentNotebookId, canonicalId)); }
  catch (err) { reportError(err); }
}
```

- [ ] **Step 3: Add a derived graph-data builder** (a `useMemo` in the component body; degree-sized nodes, type-colored)

```tsx
const fgData = useMemo(() => {
  if (!uGraph) return { nodes: [] as FgNode[], links: [] as FgLink[] };
  const deg: Record<string, number> = {};
  uGraph.edges.forEach((e) => { deg[e.source_object_id] = (deg[e.source_object_id] ?? 0) + 1; deg[e.target_object_id] = (deg[e.target_object_id] ?? 0) + 1; });
  const q = kgSearch.trim().toLowerCase();
  const nodes: FgNode[] = uGraph.nodes
    .filter((n) => !q || (n.payload.name ?? "").toLowerCase().includes(q))
    .map((n) => ({ id: n.id, name: n.payload.name ?? n.id, type: n.object_type, val: 1 + (deg[n.id] ?? 0) }));
  const keep = new Set(nodes.map((n) => n.id));
  const links: FgLink[] = uGraph.edges
    .filter((e) => keep.has(e.source_object_id) && keep.has(e.target_object_id))
    .map((e) => ({ source: e.source_object_id, target: e.target_object_id, label: e.edge_type }));
  return { nodes, links };
}, [uGraph, kgSearch]);
```

- [ ] **Step 4: Render the 3-zone overlay** (add near the existing `{graphOpen && (...)}` block). Keep it full-screen (new CSS classes added in Task 6):

```tsx
{kgViewOpen && (
  <section className="kg-view" role="dialog" aria-modal="true">
    <div className="kg-view-header">
      <div><h2>知识图谱</h2><p>跨文档统一概念图（canonical 概念 + 概念间关系）。点击概念查看证据与挂载的断言/公式。</p></div>
      <button className="icon-button" onClick={() => setKgViewOpen(false)} title="Close">×</button>
    </div>
    <div className="kg-view-body">
      <aside className="kg-rail">
        <input className="kg-search" placeholder="搜索概念…" value={kgSearch} onChange={(e) => setKgSearch(e.target.value)} />
        <div className="kg-rail-section">
          <h3>待确认合并 ({pendingMerges.length})</h3>
          {pendingMerges.length === 0 ? <p className="tool-hint">无</p> : pendingMerges.map((m) => (
            <div className="kg-merge-row" key={m.id}>
              <span>{m.canonical_a.replace(/^K-/, "")} ↔ {m.canonical_b.replace(/^K-/, "")} <em>({m.score.toFixed(2)})</em></span>
              <span className="kg-merge-actions">
                <button onClick={() => decideMerge(m.id, true)}>合并</button>
                <button onClick={() => decideMerge(m.id, false)}>拒绝</button>
              </span>
            </div>
          ))}
        </div>
      </aside>
      <div className="kg-canvas">
        {uGraph === null ? <p className="tool-hint">加载中…</p> : (
          <ForceGraph2D
            graphData={fgData}
            nodeLabel={(n: any) => `${n.name} (${n.type})`}
            nodeVal={(n: any) => n.val}
            nodeAutoColorBy="type"
            linkDirectionalArrowLength={3}
            onNodeClick={(n: any) => selectConcept(n.id)}
          />
        )}
      </div>
      <aside className="kg-detail">
        {!conceptDetail ? <p className="tool-hint">点击左侧/图中概念查看详情</p> : (
          <div className="stack">
            <h3>{conceptDetail.canonical_name}</h3>
            <div className="tag-row"><span className="tag">成员 {conceptDetail.members.length}</span><span className="tag">挂载 {conceptDetail.attached.length}</span></div>
            <h4>挂载的断言 / 公式 / 过程</h4>
            {conceptDetail.attached.length === 0 ? <p className="tool-hint">无</p> : conceptDetail.attached.map((a) => (
              <div className="checklist-row" key={a.id}><span className="tag">{a.object_type}</span> {a.payload.name as string} <em>({a.edge_type})</em></div>
            ))}
            <h4>证据</h4>
            {conceptDetail.evidence.slice(0, 20).map((ev, i) => (
              <div className="kg-evidence" key={i}><span className="tag">{ev.source_title || ev.source_id}</span> <span>{ev.quoted_span}</span></div>
            ))}
          </div>
        )}
      </aside>
    </div>
  </section>
)}
```

> `decideMerge` is defined in Task 4. `useMemo` must be imported from React — add it to the existing `import { ... } from "react"` line if not already present.

- [ ] **Step 5: Typecheck**

Run: `cd frontend && npm run lint`. Expected: it will error that `decideMerge` is undefined — that's fine, it's added in Task 4. If OTHER errors appear (types, missing `useMemo` import), fix them. Do not commit yet (next task completes the wiring).

---

## Task 4: Merge review action + entry wiring

**Files:**
- Modify: `frontend/app/page.tsx`

- [ ] **Step 1: Add the `decideMerge` handler** (near `selectConcept`)

```tsx
async function decideMerge(candidateId: string, confirm: boolean) {
  if (!currentNotebookId) return;
  try {
    if (confirm) await confirmMergeApi(currentNotebookId, candidateId);
    else await rejectMergeApi(currentNotebookId, candidateId);
    await rebuildUnifiedKg(currentNotebookId);             // apply the decision
    const [g, pend] = await Promise.all([fetchUnifiedGraph(currentNotebookId), fetchPendingMerges(currentNotebookId)]);
    setUGraph(g); setPendingMerges(pend);
    if (selectedConcept) setConceptDetail(await fetchConceptDetail(currentNotebookId, selectedConcept).catch(() => null));
  } catch (err) { reportError(err); }
}
```

- [ ] **Step 2: Repoint the `关系图` entry button to the new view**

Find the button at ~line 1289:
```tsx
<button className="sort-button" onClick={() => openGraph().catch(reportError)}>关系图</button>
```
Replace its label + handler:
```tsx
<button className="sort-button" onClick={() => openKgView()}>知识图谱</button>
```
(Leave the old `openGraph`/`graphOpen`/old-modal code in place for now — it's now unreferenced from the UI but removing it is a separate cleanup; OR delete the old `{graphOpen && (...)}` modal block + `openGraph` + `graphOpen` state if you confirm via grep nothing else references them. Prefer deleting if clean.)

- [ ] **Step 3: Add Esc-to-close** (optional but match the app's modal feel). If the app already has a global keydown handler for modals, add `kgViewOpen` to it; otherwise skip (× button suffices).

- [ ] **Step 4: Typecheck + commit**

Run: `cd frontend && npm run lint`. Expected: **0 errors** now (decideMerge defined).
```bash
git add frontend/app/page.tsx && git commit -m "feat(kg-viz): unified-KG full-screen view (graph + filters + merge review + detail)"
```

---

## Task 5: Styling

**Files:**
- Modify: `frontend/app/globals.css`

- [ ] **Step 1: Add the view styles** (append to `globals.css`; reuse existing color variables where the file defines them — grep for `--` custom properties; if none, use the literal colors below)

```css
.kg-view { position: fixed; inset: 0; z-index: 50; background: #0b0e14; color: #e6e6e6; display: flex; flex-direction: column; }
.kg-view-header { display: flex; justify-content: space-between; align-items: flex-start; padding: 16px 20px; border-bottom: 1px solid #222; }
.kg-view-header h2 { margin: 0; }
.kg-view-header p { margin: 4px 0 0; color: #9aa4b2; font-size: 13px; max-width: 70ch; }
.kg-view-body { flex: 1; display: grid; grid-template-columns: 280px 1fr 320px; min-height: 0; }
.kg-rail, .kg-detail { overflow-y: auto; padding: 12px; border-right: 1px solid #222; }
.kg-detail { border-right: none; border-left: 1px solid #222; }
.kg-canvas { position: relative; min-width: 0; min-height: 0; background: #0b0e14; }
.kg-search { width: 100%; padding: 8px; margin-bottom: 12px; background: #11151c; border: 1px solid #2a3140; border-radius: 6px; color: #e6e6e6; }
.kg-rail-section h3 { font-size: 13px; color: #9aa4b2; margin: 8px 0; }
.kg-merge-row { display: flex; flex-direction: column; gap: 4px; padding: 8px; border: 1px solid #222; border-radius: 6px; margin-bottom: 8px; font-size: 13px; }
.kg-merge-actions { display: flex; gap: 6px; }
.kg-merge-actions button { flex: 1; padding: 4px; cursor: pointer; }
.kg-evidence { padding: 6px 0; border-bottom: 1px solid #1a1f29; font-size: 13px; display: flex; gap: 8px; }
```

- [ ] **Step 2: Verify the canvas fills its zone**

`react-force-graph-2d` sizes to its parent only if the parent has explicit dimensions. The `.kg-canvas` grid cell has a size from the grid; the ForceGraph2D component reads its container. If it renders 0-height, wrap the `<ForceGraph2D>` so it gets the cell size (the lib auto-sizes to the nearest sized ancestor). Confirm visually in Task 6; if it's collapsed, pass explicit `width`/`height` via a ref-measured container (note it for the controller).

- [ ] **Step 3: Typecheck + commit**

Run: `cd frontend && npm run lint` (CSS isn't typechecked, but confirm the build is still clean).
```bash
git add frontend/app/globals.css && git commit -m "feat(kg-viz): styles for the unified-KG view"
```

---

## Task 6: Manual verification (MAIN session)

**Files:** none.

- [ ] **Step 1: Ensure servers are up** — backend (no `--reload`) on :8000, frontend on :3000.

- [ ] **Step 2: Build check** — `cd frontend && npm run build` completes without errors (catches SSR/dynamic-import issues `tsc` misses).

- [ ] **Step 3: Browser walkthrough** — open http://localhost:3000, select a notebook with ≥1 extracted source, click **知识图谱**:
  - the force graph renders concept nodes (colored by type, sized by degree);
  - typing in the search box filters nodes live;
  - clicking a node loads the right-panel detail (attached claims/formulas + evidence);
  - the "待确认合并" list shows pending merges (will be empty if `EMBED_PROVIDER` is unset → name-only merges produce no gray-zone candidates — that's expected; note it);
  - 合并/拒绝 buttons update the graph.

- [ ] **Step 4: Record result** — append a one-line status to `fangan_todo.md` under the unified-KG entry (viz view shipped) and note whether the canvas sizing needed the explicit width/height fallback.

- [ ] **Step 5: Commit** (if any fixups) — `git add -A && git commit -m "chore(kg-viz): manual verification + notes"`.

---

## v1 scope (deliberate simplifications vs spec §3)
- **Filters = live name search only.** Spec mentioned source/section/type toggles, but the concept-level `/unified-kg` view is single-type (all nodes are `concept`) and a canonical concept spans multiple sources, so per-source/section filtering doesn't map cleanly to the concept overview. Name search is the practical v1 filter; source/section filtering is a follow-up if needed.
- **Pending merges shown as a rail LIST** (with 合并/拒绝), not highlighted node-pairs on the canvas. List review is simpler and sufficient for inspection; canvas highlighting of candidate pairs is a follow-up.
- **Node size = computed edge-degree** (the backend concept-level nodes carry `{id, object_type, payload}`, not a `mentions_count`), colored by type via `nodeAutoColorBy`.

## Self-Review notes (for the implementer)
- **No tests** in the frontend — `npm run lint` (tsc) + `npm run build` + the manual walkthrough are the gates.
- **`useMemo`/`useState`** must be imported from React (check the existing import line).
- **Canvas sizing** is the one real risk (Task 5 Step 2); have the manual check confirm it, with the ref-measured width/height as the documented fallback.
- **Pending-merges will be empty without a real embedder** — that's correct behavior, not a bug; the merge-review UI is still exercised once `EMBED_PROVIDER` is configured.
