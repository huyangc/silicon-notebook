# 深度报告全局引用溯源 Implementation Plan(折入 PR #181)

> REQUIRED SUB-SKILL: superpowers:subagent-driven-development / TDD。

**Goal:** 报告的 `[k]` 引用从「节内局部编号、纯文本」升级为「全局按来源去重编号 + 结构化 references + 前端可点击 chip(复用 ask 的 remarkCitations)」。

**背景:** 现状每节 `ReasoningRetriever` 深挖后 `_draft_section` 用 `_chunk_answer_context`/`_answer_context` 产出节内 `id_map`(key `k1..kn` → ctx),节 markdown 含节内 `[k_i]`;`_assemble` 只把来源标题去重列进「参考文献」,`[k_i]` 从不重编号/解析,跨节 `[k1]` 指向不同来源。

**关键事实(已侦察):**
- id_map 条目形状(chunk 版,`_chunk_answer_context` sqlite_repository.py:10485):`{object_id, object_type, name, definition, snippet, source_title, location_label, tier}`。KG 版(`_answer_context`)同构(实现者按真实字段兜底取值)。
- 前端复用:`answer-citations.ts` 的 `remarkCitations(refsByKey)` 把 `[k\d+]` → `cite:KEY` 链接(仅当 key ∈ refsByKey);`AnswerMarkdown` 的 `<a href="cite:">` → cite-chip 按钮 → `onReferenceClick(reference)`。`AnswerReference = {id, displayLabel, anchor?, citation?}`(answer-formatting.ts:22);`referenceByAnchorKey(refs)` 按 `anchor.key` 建 refsByKey。
- `reports` 表是本 PR 新建 → 加列无迁移兼容负担。

**验证命令:**
```bash
PY=/opt/homebrew/Caskroom/miniconda/base/bin/python
$PY -m pytest backend/tests/test_report_engine.py backend/tests/test_report_api.py -q
$PY -m pytest backend/tests -q
cd frontend && npx tsc --noEmit && npm run lint && npm run test
```

---

### Task 1: 后端——全局重编号 + 结构化 references

**Files:** `backend/app/services/report_engine.py`、`backend/app/services/sqlite_repository.py`、`backend/app/models/schemas.py`、`backend/tests/test_report_engine.py`、`backend/tests/test_report_api.py`

- [ ] **Step 1: 失败测试(test_report_engine.py 追加)**

```python
def test_assemble_global_citation_renumber_and_references(repo, monkeypatch):
    """跨节 [k] 全局按来源去重重编号:同一来源在不同节共享同一全局 [k];未知
    marker 被剥除;references 结构化有序;content_md 内联与参考文献段一致。"""
    nb = _mk_nb(repo)
    eng = _mk_engine(repo, _OutlineLLM())
    outline = [{"title": "A", "scope": "sa", "sub_queries": ["qa"]},
               {"title": "B", "scope": "sb", "sub_queries": ["qb"]}]
    # A 引用 Razavi(k1)+Gray(k2)且有个幻觉 k9;B 再次引用 Razavi(节内 k1)
    razavi = {"object_id": "c1", "object_type": "chunk", "name": "BGR",
              "source_title": "Razavi Analog CMOS", "location_label": "§11", "tier": "base"}
    gray = {"object_id": "c2", "object_type": "chunk", "name": "PN",
            "source_title": "Gray & Meyer", "location_label": "§1", "tier": "base"}
    razavi_b = {"object_id": "c9", "object_type": "chunk", "name": "curv",
                "source_title": "Razavi Analog CMOS", "location_label": "§11.4", "tier": "base"}
    sections = [
        {"title": "A", "scope": "sa", "grounded": True,
         "markdown": "## A\nCTAT+PTAT 抵消 [k1]。指数式 [k2]。幻觉 [k9]。",
         "id_map": {"k1": razavi, "k2": gray},
         "attempted": [], "top_concepts": []},
        {"title": "B", "scope": "sb", "grounded": True,
         "markdown": "## B\n曲率补偿 [k1]。",
         "id_map": {"k1": razavi_b},          # 同 Razavi 来源,节内也叫 k1
         "attempted": [], "top_concepts": []},
    ]
    monkeypatch.setattr(eng.repo, "_retrieve_neighbors", lambda *a, **k: [])
    rid = repo.create_report(nb.id, "q")
    md, gaps, references = eng._assemble(nb.id, rid, "q", outline, sections)

    # 全局去重:Razavi=k1(A、B 共享)、Gray=k2 → 2 条 references
    assert [r["key"] for r in references] == ["k1", "k2"]
    assert references[0]["label"] == "Razavi Analog CMOS"
    assert references[1]["label"] == "Gray & Meyer"
    # A 段:k1/k2 保留、幻觉 k9 被剥除
    assert "[k1]" in md and "[k2]" in md and "[k9]" not in md and "幻觉 。" in md
    # B 段:节内 k1(Razavi)→ 全局仍 k1(与 A 的 Razavi 同号)
    b_seg = md.split("## B")[1]
    assert "[k1]" in b_seg and "[k2]" not in b_seg
    # 参考文献段列出 [k1]/[k2] + 标题
    assert "## 参考文献" in md
    assert "[k1]" in md.split("## 参考文献")[1] and "Razavi Analog CMOS" in md.split("## 参考文献")[1]


def test_assemble_no_citations_omits_references(repo):
    nb = _mk_nb(repo)
    eng = _mk_engine(repo, _OutlineLLM())
    outline = [{"title": "A", "scope": "s", "sub_queries": ["q"]}]
    sections = [{"title": "A", "scope": "s", "grounded": False,
                 "markdown": "## A\n全是【通识】x。", "id_map": {},
                 "attempted": [], "top_concepts": []}]
    md, gaps, references = eng._assemble(nb.id, rid := repo.create_report(nb.id, "q"),
                                         "q", outline, sections)
    assert references == [] and "## 参考文献" not in md
```

同时改 test_report_api.py 的生命周期测试补一行:`assert "references" in detail`(ReportDetail 带该字段,默认 `[]`)。

- [ ] **Step 2: 跑失败** `$PY -m pytest backend/tests/test_report_engine.py -q -k "citation or references"` → `_assemble` 返回 2 元组解包失败 / 无 references。

- [ ] **Step 3: 实现**

**3a. `report_engine.py` 顶部**加 `import re` 与 `_MARKER = re.compile(r"\[k(\d+)\]")`(放模块常量区)。

**3b. `_draft_section`**(约 :89-97)返回 dict:把 `"id_map_sources": self._sources_of(id_map)` 一行替换为 `"id_map": id_map,`(保留 attempted/top_concepts/grounded/markdown/title/scope 不变);`_sources_of` 静态方法可删(不再被引用)。

**3c. `_assemble`** 整体替换为(签名与调用不变,返回值变 3 元组):

```python
    def _assemble(self, notebook_id, rid, question, outline, sections):
        from app.services.prompts import report_summary_prompt
        summary = ""
        try:
            sections_block = "\n\n".join(
                s["markdown"][:2000] for s in sections if s.get("markdown"))
            raw = self.repo.reasoning_llm_client.chat_json(
                [{"role": "user", "content": report_summary_prompt(question, sections_block)}],
                '{"summary":""}', cancel_event=self.cancel_event)
            summary = str(json.loads(raw).get("summary", "")).strip()
        except AskCancelled:
            raise
        except Exception:
            pass

        # --- 全局引用重编号(按来源去重):节内 [k_i] → 全局 [k{N}] ---
        references: List[dict] = []
        ref_pos: Dict[str, int] = {}       # dedup key -> 全局 1-based

        def _dk(ctx):                       # 去重键:source_id > source_title > object_id
            return str(ctx.get("source_id") or ctx.get("source_title")
                       or ctx.get("object_id") or "")

        def _label(ctx):
            return (str(ctx.get("source_title") or ctx.get("name")
                        or ctx.get("object_id") or "").strip() or "(unnamed)")

        remapped: Dict[int, str] = {}
        for si, s in enumerate(sections):
            id_map = s.get("id_map") or {}

            def _sub(m, _id_map=id_map):
                ctx = _id_map.get(f"k{m.group(1)}")
                if not ctx:
                    return ""               # 剥除幻觉/未知 marker
                dk = _dk(ctx)
                if dk not in ref_pos:
                    ref_pos[dk] = len(references) + 1
                    references.append({
                        "key": f"k{ref_pos[dk]}",
                        "object_id": str(ctx.get("object_id") or ""),
                        "object_type": str(ctx.get("object_type") or ""),
                        "label": _label(ctx),
                        "name": str(ctx.get("name") or ""),
                        "source_title": str(ctx.get("source_title") or ""),
                        "location_label": str(ctx.get("location_label") or ""),
                        "tier": str(ctx.get("tier") or "personal"),
                    })
                return f"[k{ref_pos[dk]}]"

            remapped[si] = _MARKER.sub(_sub, s.get("markdown") or "")

        # --- 知识缺口(逻辑不变,concept 连通性仍用 top_concepts) ---
        gaps: List[str] = []
        for s in sections:
            for a in s.get("attempted", []):
                if a.get("new") == 0:
                    gaps.append(f"「{s['title']}」节:子查询 “{a['query']}” 在库内未检得新证据")
        pairs_checked = 0
        concepts = [(s["title"], c) for s in sections for c in s.get("top_concepts", [])]
        for i in range(len(concepts)):
            for j in range(i + 1, len(concepts)):
                if concepts[i][0] == concepts[j][0]:
                    continue
                if pairs_checked >= _GAP_PAIR_CAP:
                    break
                pairs_checked += 1
                a, b = concepts[i][1], concepts[j][1]
                try:
                    neigh = self.repo._retrieve_neighbors(notebook_id, a["object_id"], None, "both")
                except Exception:
                    continue
                if not any(h.object_id == b["object_id"] for h in neigh):
                    gaps.append(f"图谱缺口:「{a['name']}」与「{b['name']}」尚无关联边")
        for s in sections:
            if s.get("markdown") and not s.get("grounded"):
                gaps.append(f"「{s['title']}」节无库内引用支撑(全部为推断/通识,建议补充语料)")
        gaps = list(dict.fromkeys(gaps))[:30]

        # --- 组装 content_md(用重编号后的节 markdown) ---
        plan_lines = [f"- {s['title']}: " + "; ".join(o.get("sub_queries", []))
                      for s, o in zip(sections, outline)]
        parts = [f"# 深度报告:{question}", ""]
        if summary:
            parts += ["## 执行摘要", "", summary, ""]
        for si, s in enumerate(sections):
            if s.get("failed"):
                parts += [f"## {s['title']}", "", f"（本节生成失败:{s.get('error','')}）", ""]
            elif remapped.get(si):
                parts += [remapped[si], ""]
        if gaps:
            parts += ["## 知识缺口", ""] + [f"- {g}" for g in gaps] + [""]
        if references:
            parts += ["## 参考文献", ""] + [
                f"- [{r['key']}] {r['label']}"
                + (f" · {r['location_label']}" if r["location_label"] else "")
                for r in references] + [""]
        parts += ["## 分析计划", ""] + plan_lines
        return "\n".join(parts), gaps, references
```

**3d. `run()`**(约 :138-153):把 `content_md, gaps = self._assemble(...)` 改为 `content_md, gaps, references = self._assemble(...)`;persist 前剥除 id_map、并存 references:

```python
            content_md, gaps, references = self._assemble(notebook_id, rid, question, outline, sections)
            for s in sections:
                s.pop("id_map", None)                 # 账目仅供 assemble,不入库
            self.repo.update_report(notebook_id, rid, sections=sections,
                                    content_md=content_md, gaps=gaps,
                                    references=references, status="done", progress="完成")
```

(删掉原先 `update_report(sections=sections, progress="汇总中")` 那一次中间写,或保留但注意此刻 sections 仍含 id_map——建议保留中间写但改成只写 progress:`self.repo.update_report(notebook_id, rid, progress="汇总中")`。)

**3e. `sqlite_repository.py`**:
- `reports` CREATE TABLE 加列 `references_json TEXT NOT NULL DEFAULT '[]'`(在 gaps_json 后)。
- `update_report` 的循环元组加 `("references_json", references, True)`,并给函数签名加 `references=None` kwarg。
- `_report_row_to_dict` 的 `full=True` 分支加 `references=json.loads(row["references_json"] or "[]")`。

**3f. `schemas.py`** `ReportDetail` 加 `references: List[dict] = Field(default_factory=list)`。

- [ ] **Step 4: 跑过** 新测试 + `$PY -m pytest backend/tests/test_report_engine.py backend/tests/test_report_api.py -q`

- [ ] **Step 5: Commit** `feat(report): 全局引用重编号(按来源去重)+ 结构化 references + 参考文献段`

---

### Task 2: 前端——可点击引用 chip(复用 remarkCitations)

**Files:** `frontend/app/report-view.tsx`(改 `ReportMarkdown` + `ReportDetailT` 类型 + 详情视图)

- [ ] **Step 1: 类型**——`ReportDetailT` 加 `references: { key: string; label: string; name?: string; source_title?: string; location_label?: string; object_id?: string; object_type?: string; tier?: string }[]`。

- [ ] **Step 2: `ReportMarkdown` 改为复用 ask 引用基建**——import:

```tsx
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import { remarkCitations } from "./answer-citations";
import { referenceByAnchorKey, type AnswerReference } from "./answer-formatting";
```

组件签名改 `{ markdown, references }`;由 references 构造 `AnswerReference[]`(anchor.key=ref.key、displayLabel=`[序号]`、anchor 填来源字段)→ `referenceByAnchorKey` → refsByKey;`<a href="cite:">` 渲染成 chip(样式复用 `cite-chip`),点击 `onReferenceClick`=设 `selectedRefKey` 高亮 + 滚动到 `## 参考文献`(用 `document.getElementById("report-references")?.scrollIntoView`,给参考文献标题包一层带该 id 的元素——用 `h2` 组件覆盖:文本为「参考文献」时挂 id)。urlTransform 放行 `cite:`(照 answer-markdown.tsx:114)。保留现有 remarkGfm/remarkMath/rehypeKatex 与 pre/table 覆盖。

```tsx
function ReportMarkdown({ markdown, references = [] }: { markdown: string; references?: ReportDetailT["references"] }) {
  const [selectedRefKey, setSelectedRefKey] = useState<string | null>(null);
  const refObjs: AnswerReference[] = references.map((r, i) => ({
    id: `report:${r.key}`,
    displayLabel: `[${i + 1}]`,
    anchor: {
      key: r.key, object_id: r.object_id || "", object_type: r.object_type || "",
      label: r.label, name: r.name, source_title: r.source_title,
      location_label: r.location_label, tier: r.tier,
    },
  }));
  const refsByKey = referenceByAnchorKey(refObjs);
  const components = {
    a({ href, children }: { href?: string; children?: React.ReactNode }) {
      if (href?.startsWith("cite:")) {
        const key = href.slice(5);
        if (refsByKey[key]) {
          return (
            <button type="button"
              className={`cite-chip${selectedRefKey === key ? " active" : ""}`}
              onClick={() => {
                setSelectedRefKey(key);
                document.getElementById("report-references")?.scrollIntoView({ behavior: "smooth", block: "start" });
              }}>
              {children}
            </button>
          );
        }
        return <span>{children}</span>;
      }
      return <a href={href} target="_blank" rel="noreferrer">{children}</a>;
    },
    h2({ children }: { children?: React.ReactNode }) {
      const text = Array.isArray(children) ? children.join("") : String(children ?? "");
      return <h2 id={text.includes("参考文献") ? "report-references" : undefined}>{children}</h2>;
    },
    pre({ children }: { children?: React.ReactNode }) { return <pre className="answer-code">{children}</pre>; },
    table({ children }: { children?: React.ReactNode }) {
      return <div className="answer-table-wrap"><table className="answer-table">{children}</table></div>;
    },
  } as Parameters<typeof ReactMarkdown>[0]["components"];
  return (
    <div className="report-markdown answer-markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath, [remarkCitations, refsByKey] as [typeof remarkCitations, Record<string, AnswerReference>]]}
        rehypePlugins={[rehypeKatex]}
        urlTransform={(url) => (url.startsWith("cite:") ? url : defaultUrlTransform(url))}
        components={components}>
        {markdown}
      </ReactMarkdown>
    </div>
  );
}
```

- [ ] **Step 3: 传参**——详情视图里 `<ReportMarkdown markdown={detail.content_md} references={detail.references} />`。

- [ ] **Step 4: 校验** `cd frontend && npx tsc --noEmit && npm run lint && npm run test`;`git diff | grep -c '^-.*[“”]'` = 0(勿动 page.tsx 弯引号——本任务只碰 report-view.tsx)。

- [ ] **Step 5: Commit** `feat(fe): 报告 [k] 引用可点击 chip(复用 remarkCitations)+ 滚动到参考文献`

---

### Task 3: 全量验证(控制器)
`$PY -m pytest backend/tests -q` 全绿 + `bash scripts/check.sh` EXIT=0 → rebase → push(更新 #181)。
