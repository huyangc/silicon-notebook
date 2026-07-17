# PR A · 枚举映射层 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立 `frontend/app/vocabulary.ts`（跨模块枚举映射 + 严格查表器 `label()`），并把「后端枚举直出给用户」与「兜底即原值」收编掉。

> 计数勘误：初稿写「4 处兜底即原值」，是我自查用的 grep 写窄了（只认 `?? stage|status|s|step.step_type` 这几个变量名）。
> 勘误的勘误：第一版「宽模式」`\[[a-zA-Z.]+\]` 的字符类**漏了下划线**，于是 `[latest.step_type]`、`[edge.edge_type]` 这类带 `_` 的键全被跳过，
> 当时报的「约 10 处」仍然偏少。正确模式是 `\[[a-zA-Z0-9_.]+\]\s*\?\?\s*[a-zA-Z]`，全量 14 处命中，其中 **9 处**是真的「label 表兜底回原值」。
> 本 PR 收 5 处（Task 3 的 4 处 + Task 7 的 `reasoning-trace.ts:86`）；另 8 处（`RELATION_LABELS`×6、`FIELD_LABELS`×2）**刻意不在本 PR**，理由见 Task 8 Step 2 的表——
> 尤其 `FIELD_LABELS[key] ?? key` 未必是 bug：自定义类型的字段名是用户自己起的，兜底显示原 key 就是显示用户自己的词。

**Architecture:** 共享核心不是词典，是**枚举映射 + 严格查表器**。`label(map, value, fallback)` 的签名**强制**调用方传兜底词，使「兜底即原值」在类型层面写不出来。散文词不进这个模块（抽不成常量），由 PR B 的文档 + lint 管。

**Tech Stack:** TypeScript / React (Next.js app router)、`node --test` + `*.test.mjs`

设计依据：[2026-07-17-user-facing-vocabulary-design.md](../specs/2026-07-17-user-facing-vocabulary-design.md)

## Global Constraints

- 词汇表定稿（spec §1）逐字照用：`base` → **公共知识库**，`personal` → **个人知识库**。不得写成「基准库」「个人层」「我的资料」。
- **不得**在本 PR 引入 `OBJECT_TYPE` 映射。object_type 的真源在后端（spec §2.1），归 PR A2。
- **不得**碰 `frontend/app/kg-type-mark.tsx`。它的 TitleCase 兜底归 PR A2。
- **不得**碰 `page.tsx:5207/5208`（edge_type / review_status）。edge_type 是开放关系词表且 admin-only（P2），不在本 PR。
- **不得**碰 `ask-modes.ts` 的 `label` / `desc` 措辞。它是独立的 per-feature 真源，有 `scripts/check_ask_modes_contract.py` 跨栈守卫钉着；本 PR 只**引用**它，不改它。
- 只动 `frontend/`。本 PR 零后端改动、零迁移。
- **A/B 边界**：归 PR A 的是**机器值到达屏幕的地方**。与被改枚举**同处一个元素**的相邻散文（例：tier 徽章第 140 行的 tooltip）作为不可避免的连带一并改——留着会让同一元素内部自相矛盾（徽章说「公共知识库」而它自己的 tooltip 说「基准库」）。除此之外的散文一律归 PR B，不要顺手改。
- **不改命名，只改机制**。除词汇表定稿的 tier 两词外，各枚举的中文措辞以「贴合现状 / 直白可懂」为准，不借本 PR 推行新叫法。
- 每个 task 结束时 `cd frontend && npm run test && npx tsc --noEmit` 必须绿。
- **测试卫生（Task 2 评审揪出来的，别再犯）**：枚举取值的断言归 `vocabulary.test.mjs`——Task 1 已覆盖 TIER / PARSE_STATUS / EVIDENCE_LEVEL / MODEL_STAGE / PROMOTION_STATUS 四态 / 原型链 / 空串。**接线类 task（2/4/5/6）不要再抄一遍同样的 `label(MAP, value, fallback)` 断言**：那是在测上游 Task 1 的代码，净新增覆盖为零，而测试名（「XX 徽章显示中文」）会承诺它根本没验证的东西——它既不 import 也不渲染那个组件，把接线改回裸值它也不会红。只在带来**真新覆盖**时才加测试。接线本身的回归钉子是 Task 8 Step 2 的静态断言（`grep` 断言裸值形状已消失）。
- 本 worktree 无 `frontend/node_modules`。**Task 0 先装。**

---

### Task 0: 装依赖

**Files:** 无（仅环境）

- [ ] **Step 1: 安装前端依赖**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/merge-duplicate-dbs-d5e135/frontend
npm install
```

Expected: 装完，`node_modules/` 存在。

- [ ] **Step 2: 确认基线是绿的（改动前）**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/merge-duplicate-dbs-d5e135/frontend
npm run test && npx tsc --noEmit
```

Expected: 全绿。若此时就红，先停下报告——那是既有问题，不是本 PR 引入的。

---

### Task 1: `vocabulary.ts` 骨架与 `label()`

**Files:**
- Create: `frontend/app/vocabulary.ts`
- Test: `frontend/app/vocabulary.test.mjs`

**Interfaces:**
- Produces: `label(map: Record<string, string>, value: string, fallback: string): string`；常量 `TIER`、`PARSE_STATUS`、`ELEMENT_TYPE`、`KNOWLEDGE_STATUS`、`EVIDENCE_LEVEL`、`MODEL_STAGE`、`PROMOTION_STATUS`

- [ ] **Step 1: 写失败的测试**

创建 `frontend/app/vocabulary.test.mjs`：

```js
import test from "node:test";
import assert from "node:assert/strict";
import { label, TIER, PARSE_STATUS, EVIDENCE_LEVEL } from "./vocabulary.ts";

test("label 命中时返回映射值", () => {
  assert.equal(label(TIER, "base", "未知"), "公共知识库");
  assert.equal(label(TIER, "personal", "未知"), "个人知识库");
});

test("label 未命中时返回 fallback，绝不返回原值", () => {
  assert.equal(label(TIER, "shadow_tier", "未知来源"), "未知来源");
  assert.notEqual(label(TIER, "shadow_tier", "未知来源"), "shadow_tier");
});

test("label 不被原型链上的键名骗到", () => {
  // map[value] + 真值判断会让这些键命中 Object.prototype 并返回函数/对象。
  // TS 推成 string 但运行时不是,渲染进 JSX 就是 React 白屏。
  for (const key of ["constructor", "toString", "__proto__", "hasOwnProperty", "valueOf"]) {
    const out = label(TIER, key, "未知来源");
    assert.equal(out, "未知来源", `${key} 命中了原型链`);
    assert.equal(typeof out, "string", `${key} 返回了非字符串`);
  }
});

test("label 不把合法的空串翻译误判为未命中", () => {
  assert.equal(label({ silent: "" }, "silent", "兜底"), "");
});

test("label 对空字符串与未定义值同样不泄漏原值", () => {
  assert.equal(label(PARSE_STATUS, "", "处理中"), "处理中");
  assert.equal(label(PARSE_STATUS, "totally_new_status", "处理中"), "处理中");
});

test("证据等级三个取值都有中文", () => {
  assert.equal(label(EVIDENCE_LEVEL, "grounded", "—"), "有据");
  assert.equal(label(EVIDENCE_LEVEL, "inferred", "—"), "推断");
  assert.equal(label(EVIDENCE_LEVEL, "overview", "—"), "概述");
});
```

- [ ] **Step 2: 跑测试确认它失败**

```bash
cd frontend && npm run test 2>&1 | grep -A3 vocabulary
```

Expected: FAIL — `Cannot find module './vocabulary.ts'`

- [ ] **Step 3: 写最小实现**

创建 `frontend/app/vocabulary.ts`：

```ts
// 跨模块枚举 → 用户可见中文的单一真源。
// 只装「跨模块」的枚举；功能自己的枚举留在各自模块里，但同样必须走 label()。
// 散文词不进这里（抽成常量只会让代码更难读）——由 AGENTS.md 词汇表 +
// scripts/check_ui_vocabulary.py 管。
//
// object_type 刻意不在此处：后端 extraction_profiles.OBJECT_TYPE_LABELS 才是它的
// 真源，且已通过 API 下发（KnowledgeTypeCount.label），自定义类型也走同一条路。
// 见 docs/superpowers/specs/2026-07-17-user-facing-vocabulary-design.md §2.1。

export const TIER: Record<string, string> = {
  base: "公共知识库",
  personal: "个人知识库",
};

export const PARSE_STATUS: Record<string, string> = {
  uploaded: "已上传",
  queued: "排队中",
  parsed: "已解析",
  extracting: "分析中",
  extracted: "已就绪",
  failed: "解析失败",
  "metadata-only": "仅元数据",   // source_ingestion.py:274 真实会写入
};

export const ELEMENT_TYPE: Record<string, string> = {
  heading: "标题",
  paragraph: "正文",
  table: "表格",
  formula: "公式",
  code_block: "代码",
  text: "正文",
  knowhow_cell: "经验表单元格",
};

export const KNOWLEDGE_STATUS: Record<string, string> = {
  reviewed: "已审阅",
  approved: "已批准",
  deprecated: "已弃用",
  conflict: "有冲突",
  project_specific: "项目专用",
};

export const EVIDENCE_LEVEL: Record<string, string> = {
  grounded: "有据",
  inferred: "推断",
  overview: "概述",
};

// 措辞刻意保持与现状一字不差(answer-panel.tsx:354-358 原有的四个名字)。
// 本 PR 只修「兜底即原值」这个机制,不碰命名——模型角色命名与设置页对齐
// (报错说「向量模型」但设置页没这一项)属于 PR C 错误层的范围。这里改名会
// 给同一批东西发明第三套叫法,PR C 还得再改一遍。
export const MODEL_STAGE: Record<string, string> = {
  embed: "向量模型",
  rerank: "重排模型",
  answer: "答案模型",
  rewrite: "改写模型",
};

// 取值真源:migrations.py:413 的建表注释 `proposed | under_review | approved | rejected`,
// 且 page.tsx:5156 线上代码正按 proposed / under_review 分支。没有 "pending" 这个值。
export const PROMOTION_STATUS: Record<string, string> = {
  proposed: "待审核",
  under_review: "审核中",
  approved: "已收录",
  rejected: "未采纳",
};

/**
 * 严格查表：命中返回映射值，未命中返回 `fallback`——永远不会是 `value` 本身。
 *
 * 签名强制传 fallback，是为了让「兜底即原值」这个 bug 写不出来。后端每加一个
 * 枚举值，旧写法（`MAP[v] ?? v`）都会自动把英文 id 泄漏给用户；这里则会退到一个
 * 中性词，并在开发期把未映射的值喊出来。
 */
export function label(map: Record<string, string>, value: string, fallback: string): string {
  // 必须用 hasOwn 而不是 `map[value]` + 真值判断:后者会走原型链,
  // map["constructor"] / map["toString"] / map["__proto__"] 都会「命中」并返回
  // 一个函数或对象。TS 因 Record<string,string> 的索引签名把它推成 string,
  // tsc 抓不到;渲染进 JSX 就是 "Objects are not valid as a React child" 白屏——
  // 比本 PR 要修的「泄漏英文 id」更严重。hasOwn 同时顺带堵住另一个坑:
  // 真值判断会把合法翻译成空串的 key 误判为未命中。
  if (Object.hasOwn(map, value)) return map[value];
  if (process.env.NODE_ENV !== "production") {
    console.error(`[vocabulary] 未映射的枚举值：${JSON.stringify(value)}`);
  }
  return fallback;
}
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd frontend && npm run test 2>&1 | grep -A3 vocabulary && npx tsc --noEmit
```

Expected: 4 个 vocabulary 测试全 PASS，tsc 无错。

- [ ] **Step 5: 提交**

```bash
git add frontend/app/vocabulary.ts frontend/app/vocabulary.test.mjs
git commit -m "feat(fe): 枚举映射层 vocabulary.ts + 严格查表器 label()

label(map, value, fallback) 的签名强制传兜底词,使「兜底即原值」在类型层面
写不出来——后端每加一个枚举值,旧写法 MAP[v] ?? v 都会自动把英文 id 泄漏给用户。

object_type 刻意不收:后端 OBJECT_TYPE_LABELS 才是真源且已经 API 下发,归 PR A2。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: tier 徽章——`base` / `personal` 不再直出

**Files:**
- Modify: `frontend/app/answer-panel.tsx:136-145`
- Test: `frontend/app/answer-citations.test.mjs`（既有文件，追加用例）

**Interfaces:**
- Consumes: `label`、`TIER`（Task 1）

**背景：** 这是全站最讽刺的一处。第 140 行的 tooltip 已经写好了中文，可用户**看得见**的徽章文字（142 行）却是裸枚举 `base` / `personal`。译名存在，只是没用在能看见的地方。

- [ ] **Step 1: 先读现状，确认锚点**

```bash
sed -n '136,145p' frontend/app/answer-panel.tsx
```

Expected: 能看到 `title={tier === "base" ? "来自基准库（权威参考层）" : "来自个人层"}` 与 `{tier === "base" ? "base" : "personal"}`

- [ ] **Step 2: 写失败的测试**

在 `frontend/app/answer-citations.test.mjs` 末尾追加（**追加到文件尾，不要插在中间**——插入会移动行号，打破按行号钉住的守卫）：

```js
import { label, TIER } from "./vocabulary.ts";

test("tier 徽章显示中文而非裸枚举", () => {
  assert.equal(label(TIER, "base", "未知来源"), "公共知识库");
  assert.equal(label(TIER, "personal", "未知来源"), "个人知识库");
  // 回归钉子:徽章上永远不该出现这两个英文原值
  assert.notEqual(label(TIER, "base", "未知来源"), "base");
  assert.notEqual(label(TIER, "personal", "未知来源"), "personal");
});
```

- [ ] **Step 3: 跑测试确认它失败**

```bash
cd frontend && npm run test 2>&1 | grep -A3 "tier 徽章"
```

Expected: FAIL — `Cannot find module './vocabulary.ts'` 或断言失败（取决于 Task 1 是否已在同分支）

- [ ] **Step 4: 改实现**

在 `frontend/app/answer-panel.tsx` 顶部 import 区加：

```ts
import { label, TIER } from "./vocabulary";
```

把 140 行的 title 与 142 行的徽章文字改为：

```tsx
            title={tier === "base" ? "来自公共知识库" : "来自个人知识库"}
          >
            {label(TIER, tier, "未知来源")}
```

- [ ] **Step 5: 跑测试确认通过**

```bash
cd frontend && npm run test && npx tsc --noEmit
```

Expected: 全绿。

- [ ] **Step 6: 提交**

```bash
git add frontend/app/answer-panel.tsx frontend/app/answer-citations.test.mjs
git commit -m "fix(fe): 引用徽章显示「公共知识库/个人知识库」,不再直出 base/personal

同一元素的 tooltip 早就写好了中文,可用户看得见的徽章文字却是裸枚举。
tooltip 里用「权威参考层」解释「基准库」也一并去掉——那是用一个内部词
解释另一个内部词。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: 四处「兜底即原值」

**Files:**
- Modify: `frontend/app/answer-panel.tsx:282`（`?? step.step_type`）
- Modify: `frontend/app/answer-panel.tsx:353-359`（`?? stage`）
- Modify: `frontend/app/report-view.tsx:250`（`?? status`）
- Modify: `frontend/app/admin/usage/notebooks.ts:33`（`?? s`）
- Test: `frontend/app/vocabulary.test.mjs`（追加）

**Interfaces:**
- Consumes: `label`、`MODEL_STAGE`（Task 1）

**背景：** 这四处的共同病根是 `?? 原值`——后端每加一个枚举值，UI 就自动把英文 id 泄漏给用户。注意 `admin/usage/notebooks.ts` 的 `STATUS_CN` 常被当成「正面样板」，但它自己就是这个病的实例。

- [ ] **Step 1: 确认四处现状**

```bash
sed -n '282p' frontend/app/answer-panel.tsx
sed -n '353,359p' frontend/app/answer-panel.tsx
sed -n '250p' frontend/app/report-view.tsx
sed -n '33p' frontend/app/admin/usage/notebooks.ts
```

Expected: 分别看到 `?? step.step_type`、`?? stage`、`?? status`、`?? s`

- [ ] **Step 2: 写失败的测试**

追加到 `frontend/app/vocabulary.test.mjs` 末尾：

```js
import { MODEL_STAGE } from "./vocabulary.ts";

test("模型阶段名走 label，未知 stage 不泄漏英文 id", () => {
  // 措辞与现状一致(本 PR 不改命名,只改兜底机制)
  assert.equal(label(MODEL_STAGE, "embed", "某个模型"), "向量模型");
  assert.equal(label(MODEL_STAGE, "brand_new_stage", "某个模型"), "某个模型");
  assert.notEqual(label(MODEL_STAGE, "brand_new_stage", "某个模型"), "brand_new_stage");
});
```

- [ ] **Step 3: 跑测试确认它失败**

```bash
cd frontend && npm run test 2>&1 | grep -A3 "模型阶段名"
```

Expected: FAIL

- [ ] **Step 4: 改四处实现**

`frontend/app/answer-panel.tsx:282` —— 用既有的 `TRACE_STEP_LABELS`，只换查表方式：

```tsx
                <span>{label(TRACE_STEP_LABELS, step.step_type, "处理中")}</span>
```

`frontend/app/answer-panel.tsx:353-359` —— 删掉就地的 `as Record<string, string>` 表，改用 `MODEL_STAGE`：

```tsx
        const labelOf = (stage: string) => label(MODEL_STAGE, stage, "某个模型");
```

`frontend/app/report-view.tsx:250`：

```tsx
      <span className="report-status-label">{label(STATUS_LABELS, status, "处理中")}</span>
```

（`report-view.tsx` 顶部 import 加 `import { label } from "./vocabulary";`）

`frontend/app/admin/usage/notebooks.ts:33`：

```ts
export function notebookStatusLabel(s: string): string {
  return label(STATUS_CN, s, "未知状态");
}
```

（`notebooks.ts` 顶部 import 加 `import { label } from "../../vocabulary";` —— 注意它在 `app/admin/usage/` 下，相对路径要退两级）

- [ ] **Step 5: 跑测试确认通过**

```bash
cd frontend && npm run test && npx tsc --noEmit
```

Expected: 全绿。

- [ ] **Step 6: 静态确认这四处再无原值兜底**

```bash
cd frontend && grep -rnE "\?\?\s*(stage|status|s|step\.step_type)\b" app/answer-panel.tsx app/report-view.tsx app/admin/usage/notebooks.ts | grep -iE "label|_CN|LABELS"
```

Expected: **无输出**。有输出说明还有漏网。

- [ ] **Step 7: 提交**

```bash
git add frontend/app/answer-panel.tsx frontend/app/report-view.tsx frontend/app/admin/usage/notebooks.ts frontend/app/vocabulary.test.mjs
git commit -m "fix(fe): 四处「兜底即原值」改走 label(),后端加枚举不再自动泄漏英文 id

MAP[v] ?? v 意味着后端每加一个枚举值,UI 就把英文 id 显示给用户。
notebooks.ts 的 STATUS_CN 常被当成正面样板,但它自己就是这个病的实例。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: 来源解析状态与内容块类型

**Files:**
- Modify: `frontend/app/page.tsx:4455`（`{sourceDetail.parse_status || sourceDetail.status}`）
- Modify: `frontend/app/page.tsx:4503`（`{element.element_type}`）
- Modify: `frontend/app/page.tsx:4550`（`source_status_counts` 的裸 key）
- Test: `frontend/app/vocabulary.test.mjs`（追加）

**Interfaces:**
- Consumes: `label`、`PARSE_STATUS`、`ELEMENT_TYPE`（Task 1）

- [ ] **Step 1: 确认现状**

```bash
sed -n '4455p;4503p;4550p' frontend/app/page.tsx
```

Expected: 看到三处裸值渲染。

- [ ] **Step 2: 写失败的测试**

追加到 `frontend/app/vocabulary.test.mjs`：

```js
import { PARSE_STATUS, ELEMENT_TYPE } from "./vocabulary.ts";

// 注意:不要写成 `for (const v of ["uploaded","queued",...])` 去循环断言「都有中文」——
// 那个字面量数组就是从 PARSE_STATUS 自己的 key 抄来的,等于在断言「表里有表里已有的
// 东西」,恒真,验证不了真实完整性。前端没有 parse_status / element_type 的独立锚点
// (对比:KNOWLEDGE_STATUS 有 workspace-model.ts 的 KNOWLEDGE_STATUS_OPTIONS 可锚),
// 所以这里只断言「行为安全」:已知值译对、未知值退到中性兜底而非泄漏原值。
// 真正的「映射表覆盖后端全部取值」需要一个跨栈守卫(照 check_ask_modes_contract.py),
// 归 PR B。

// PARSE_STATUS 已由 vocabulary.test.mjs(Task 1)覆盖——这里不要再抄一遍。
// ELEMENT_TYPE 是 Task 1 没测过的，属真新覆盖，加这一条：
test("内容块类型:已知值译对，未知值退中性兜底", () => {
  assert.equal(label(ELEMENT_TYPE, "table", "内容"), "表格");
  assert.equal(label(ELEMENT_TYPE, "some_future_block", "内容"), "内容");
});
```

- [ ] **Step 3: 跑测试确认它失败**

```bash
cd frontend && npm run test 2>&1 | grep -A3 "解析状态六个"
```

Expected: FAIL

- [ ] **Step 4: 改实现**

`page.tsx` 顶部 import 区加：

```ts
import { label, PARSE_STATUS, ELEMENT_TYPE, KNOWLEDGE_STATUS, PROMOTION_STATUS } from "./vocabulary";
```

4455 行：

```tsx
                <span className="tag">{label(PARSE_STATUS, sourceDetail.parse_status || sourceDetail.status, "处理中")}</span>
```

4503 行：

```tsx
                <span className="tag element-type-tag">{label(ELEMENT_TYPE, element.element_type, "内容")}</span>
```

4550 行——把裸 key 换成中文（保留 `key={k}` 作为 React key，只改显示文字）：

```tsx
                {Object.entries(analytics.source_status_counts).map(([k, v]) => <span className="tag" key={k}>{label(PARSE_STATUS, k, "其他")} {v}</span>)}
```

- [ ] **Step 5: 跑测试确认通过**

```bash
cd frontend && npm run test && npx tsc --noEmit
```

Expected: 全绿。

- [ ] **Step 6: 提交**

```bash
git add frontend/app/page.tsx frontend/app/vocabulary.test.mjs
git commit -m "fix(fe): 来源解析状态/内容块类型显示中文,不再直出后端枚举

用户点任一资料就会看到「parse_status」原值;失败时既看不懂也没有下一步动作。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: 知识条目状态（列表 / 下拉 / 筛选三处）

**Files:**
- Modify: `frontend/app/page.tsx:5830`（`{item.status}`）
- Modify: `frontend/app/page.tsx:5837`（下拉 `{value}`）
- Modify: `frontend/app/page.tsx:5782`（筛选 `{value}`）
- Modify: `frontend/app/page.tsx:5110`（`{cand.status}`，晋升队列）
- Modify: `frontend/app/page.tsx:5460` 与 `:5486`（`evidence.element_type` / `occurrence.element_type`——Task 4 评审发现的同源泄漏，间接写法：压进 `meta` 数组后 `{meta.map(item => <span>{item}</span>)}` 渲染，Task 4 的窄 grep 没抓到）
- Test: `frontend/app/vocabulary.test.mjs`（追加）

**Interfaces:**
- Consumes: `label`、`KNOWLEDGE_STATUS`、`PROMOTION_STATUS`（Task 1）

**背景：** 三处渲染的是同一个 `KNOWLEDGE_STATUS_OPTIONS`（`workspace-model.ts:283-289`：`reviewed / approved / deprecated / conflict / project_specific`），却都直出英文。

- [ ] **Step 1: 确认现状**

```bash
sed -n '5110p;5782p;5830p;5837p' frontend/app/page.tsx
sed -n '283,289p' frontend/app/workspace-model.ts
```

- [ ] **Step 2: 写失败的测试**

追加到 `frontend/app/vocabulary.test.mjs`：

```js
import { KNOWLEDGE_STATUS, PROMOTION_STATUS } from "./vocabulary.ts";
import { KNOWLEDGE_STATUS_OPTIONS } from "./workspace-model.ts";

test("KNOWLEDGE_STATUS 覆盖 workspace-model 里的每一个取值", () => {
  for (const v of KNOWLEDGE_STATUS_OPTIONS) {
    assert.notEqual(label(KNOWLEDGE_STATUS, v, "其他"), v, `${v} 未映射,会直出英文`);
  }
});

// PROMOTION_STATUS 四态已由 vocabulary.test.mjs(Task 1)覆盖，不要再抄。
// 上面那条 KNOWLEDGE_STATUS 测试是真新覆盖——它从 workspace-model.ts 的
// KNOWLEDGE_STATUS_OPTIONS 这个独立锚点取值，而不是照抄映射表自己的 key，
// 所以它真能发现「映射表漏了后端某个取值」。这是本 PR 唯一一处有独立锚点的枚举。
```

- [ ] **Step 3: 跑测试确认它失败**

```bash
cd frontend && npm run test 2>&1 | grep -A3 "KNOWLEDGE_STATUS 覆盖"
```

Expected: FAIL

- [ ] **Step 4: 改实现**

5830 行：

```tsx
                    <span className="tag">{label(KNOWLEDGE_STATUS, item.status, "其他")}</span>
```

5837 行（下拉——`value` 属性保持原值，只改显示文字）：

```tsx
                        {KNOWLEDGE_STATUS_OPTIONS.map((value) => <option key={value} value={value}>{label(KNOWLEDGE_STATUS, value, "其他")}</option>)}
```

5782 行（筛选——`all` 是前端自造的哨兵值，不是后端枚举，保留原有分支）：

```tsx
          {statuses.map((value) => <option key={value} value={value}>{value === "all" ? "全部状态" : label(KNOWLEDGE_STATUS, value, "其他")}</option>)}
```

5110 行：

```tsx
                        <span className="tag">{label(PROMOTION_STATUS, cand.status, "处理中")}</span>
```

- [ ] **Step 5: 跑测试确认通过**

```bash
cd frontend && npm run test && npx tsc --noEmit
```

Expected: 全绿。

- [ ] **Step 6: 提交**

```bash
git add frontend/app/page.tsx frontend/app/vocabulary.test.mjs
git commit -m "fix(fe): 知识条目状态三处 + 晋升候选状态显示中文

列表徽章/下拉/筛选渲染的是同一个 KNOWLEDGE_STATUS_OPTIONS,却都直出英文。
下拉与筛选的 value 属性保持原值,只改显示文字。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: 记忆侧的 wire 值（证据等级 / 提问方式 / Agent 证据）

**Files:**
- Modify: `frontend/app/memory-model.ts:228-229`
- Modify: `frontend/app/memory-panel.tsx:1104`
- Modify: `frontend/app/memory-panel.tsx:821-823`
- Test: `frontend/app/memory-model.test.mjs`（既有文件，**追加到末尾**）

**Interfaces:**
- Consumes: `label`、`EVIDENCE_LEVEL`（Task 1）；`ASK_MODES`（`ask-modes.ts`，只读不改）

**背景：** `memory-model.ts:228` 渲染 `provenance.mode` 原值 → 用户看到「问答模式: chunk」，而 `ask-modes.ts:18-23` 早有现成的用户可见 label。`memory-panel.tsx:821-823` 整行都是 wire 字段：用户看到「1. unsupported」+ 一串 UUID +「invalid · unverified」。

- [ ] **Step 1: 确认现状**

```bash
sed -n '228,229p' frontend/app/memory-model.ts
sed -n '1104p;820,824p' frontend/app/memory-panel.tsx
sed -n '17,24p' frontend/app/ask-modes.ts
```

- [ ] **Step 2: 写失败的测试**

追加到 `frontend/app/memory-model.test.mjs` 末尾：

```js
import { label, EVIDENCE_LEVEL } from "./vocabulary.ts";
import { ASK_MODES } from "./ask-modes.ts";

test("提问方式复用 ask-modes 的 label,不直出 chunk", () => {
  const modeLabels = Object.fromEntries(ASK_MODES.map((m) => [m.id, m.label]));
  assert.equal(label(modeLabels, "chunk", "—"), "通用问答");
  assert.notEqual(label(modeLabels, "chunk", "—"), "chunk");
});

test("证据等级不直出 grounded", () => {
  assert.equal(label(EVIDENCE_LEVEL, "grounded", "—"), "有据");
  assert.notEqual(label(EVIDENCE_LEVEL, "grounded", "—"), "grounded");
});
```

- [ ] **Step 3: 跑测试确认它失败**

```bash
cd frontend && npm run test 2>&1 | grep -A3 "提问方式复用"
```

Expected: FAIL

- [ ] **Step 4: 改实现**

`memory-model.ts` 顶部 import 加：

```ts
import { label, EVIDENCE_LEVEL } from "./vocabulary";
import { ASK_MODES } from "./ask-modes";

const ASK_MODE_LABELS: Record<string, string> = Object.fromEntries(
  ASK_MODES.map((m) => [m.id, m.label]),
);
```

228-229 行：

```ts
    ["提问方式", label(ASK_MODE_LABELS, String(provenance.mode ?? ""), "—")],
    ["依据", label(EVIDENCE_LEVEL, String(provenance.evidence_level ?? ""), "—")],
```

`memory-panel.tsx:1104`：

```tsx
              <span>依据：{label(EVIDENCE_LEVEL, String(provenance.evidence_level ?? ""), "未知")}</span>
```

`memory-panel.tsx:821-823` —— 三个 wire 值全部查表，UUID 收进 title：

在 `memory-panel.tsx` 顶部加映射：

```ts
const EVIDENCE_TYPE: Record<string, string> = {
  source: "原文出处",
  element: "原文片段",
  knowledge: "知识条目",
  unsupported: "无法识别",
};
const EVIDENCE_STATUS: Record<string, string> = {
  valid: "已核对",
  invalid: "未能核对",
};
```

821-823 行改为：

```tsx
                    <strong>{index + 1}. {label(EVIDENCE_TYPE, evidence.type, "未知来源")}</strong>
                    <code title={evidence.identity}>{evidence.identity.slice(0, 12)}…</code>
                    <span>{label(EVIDENCE_STATUS, evidence.status, "未能核对")}</span>
```

（`evidence.reason` 是自由文本不是枚举，且对用户无信息量——从可见文字里去掉；需要排查时它仍在 wire 上）

- [ ] **Step 5: 跑测试确认通过**

```bash
cd frontend && npm run test && npx tsc --noEmit
```

Expected: 全绿。

- [ ] **Step 6: 提交**

```bash
git add frontend/app/memory-model.ts frontend/app/memory-panel.tsx frontend/app/memory-model.test.mjs
git commit -m "fix(fe): 记忆侧 wire 值不再直出(证据等级/提问方式/Agent 证据)

「问答模式: chunk」——chunk 是最典型的内部数据结构名,而 ask-modes.ts 早有
现成的用户可见 label,这里直接复用它而不是再抄一份。
Agent 证据行原本整行都是 wire 字段:「1. unsupported」+ UUID +「invalid · unverified」。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: 推理轨迹里的 next_action 状态机泄漏 + 第 5 处原值兜底

**Files:**
- Modify: `frontend/app/reasoning-trace.ts:61`（`next_action` 直出）
- Modify: `frontend/app/reasoning-trace.ts:86`（`TRACE_STEP_LABELS[latest.step_type] ?? latest.step_type` —— Task 3 执行时发现的第 5 处原值兜底，与 Task 3 修的 `answer-panel.tsx:283` 是同一张表、同一个病，只是喂的是折叠态摘要 chip）
- Test: `frontend/app/reasoning-trace.test.mjs`（既有文件，**追加到末尾**）

**Interfaces:**
- Consumes: `label`（Task 1）

**背景：** `next_action` 的取值来自 `backend/app/services/prompts.py:251`（`answer|expand_graph|add_subquery|…`），直接显示英文动作名。更严重的是 `backend/app/services/reasoning_retrieval.py:525` 有 `summary=decision.reason or decision.next_action`——`step.summary` 整条都可能是 `expand_graph`。

**本 task 只收前端这处直出。** 后端那条 summary 兜底属于 PR C 的范畴（后端侧），不在此。

- [ ] **Step 1: 确认现状**

```bash
sed -n '59,63p' frontend/app/reasoning-trace.ts
```

Expected: 看到 `if (typeof detail.next_action === "string") return detail.next_action;`

- [ ] **Step 2: 写失败的测试**

追加到 `frontend/app/reasoning-trace.test.mjs` 末尾：

```js
import { getTraceStepDetail } from "./reasoning-trace.ts";

test("next_action 不把状态机动作名直出给用户", () => {
  const out = getTraceStepDetail({ step_type: "reflect", detail: { next_action: "expand_graph" } });
  assert.notEqual(out, "expand_graph");
});

test("未知 next_action 不显示,而不是显示原值", () => {
  const out = getTraceStepDetail({ step_type: "reflect", detail: { next_action: "brand_new_action" } });
  assert.notEqual(out, "brand_new_action");
});
```

（导出名已核实：`reasoning-trace.ts:44` 的 `getTraceStepDetail`。同文件另有 `TRACE_STEP_LABELS:3`、`formatDuration:28`、`getReasoningTraceSummary:68`）

- [ ] **Step 3: 跑测试确认它失败**

```bash
cd frontend && npm run test 2>&1 | grep -A3 "next_action 不把"
```

Expected: FAIL — 返回了 `expand_graph`

- [ ] **Step 4: 改实现**

在 `frontend/app/reasoning-trace.ts` 顶部加：

```ts
import { label } from "./vocabulary";

const NEXT_ACTION: Record<string, string> = {
  answer: "开始作答",
  expand_graph: "顺着相关内容继续找",
  add_subquery: "换个角度再查一遍",
};
```

61 行改为——**未映射时返回空串（不显示），而不是原值**：

```ts
  if (typeof detail.next_action === "string") return label(NEXT_ACTION, detail.next_action, "");
```

- [ ] **Step 5: 跑测试确认通过**

```bash
cd frontend && npm run test && npx tsc --noEmit
```

Expected: 全绿。

- [ ] **Step 6: 提交**

```bash
git add frontend/app/reasoning-trace.ts frontend/app/reasoning-trace.test.mjs
git commit -m "fix(fe): 推理轨迹不再把 next_action 状态机动作名直出

next_action 取值来自 prompts.py(answer|expand_graph|add_subquery),用户看到
的是英文动作名。未映射时返回空串不显示,而不是退回原值。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 8: 收口验证

**Files:** 无改动，仅验证

- [ ] **Step 1: 全量 check**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/merge-duplicate-dbs-d5e135
PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh
```

Expected: 全绿（含 `npm run test` / `npm run lint` / `npm run build`）。

- [ ] **Step 2: 静态确认本 PR 范围内再无枚举直出**

```bash
cd frontend
# 语义探针:任何「<...>{ 某对象.枚举字段 }<...>」形态的裸值直出,不再锚定具体变量名
# ——本 PR 过程中这条 grep 因只认特定变量名(item.status/element.element_type/...)反复漏报,
# 补了 3 次补丁(member.status 误报、evidence.element_type 间接写法、edge.edge_type 下划线)。
# 改成按「字段名」锚定,任何前缀变量都覆盖:
grep -rnE ">\{[a-zA-Z_]+\.(status|parse_status|element_type|object_type|review_status|edge_type|tier|mode|evidence_level)\}<" app/page.tsx | grep -vE "label\("
# 间接写法(压进数组再 map 渲染,如 evidence/occurrence.element_type):
grep -rnE "^\s*[a-z]+\.(status|element_type|parse_status)," app/page.tsx | grep -vE "label\("
# 语义探针跑完后应**只剩**下面这 6 处(全部有据可查、不在本 PR),多一处就是新泄漏:
#   5112 {cand.object_type}   → A2 (object_type 真源在后端)
#   5587 {schema.object_type} → A2
#   5696 {schema.object_type} → A2
#   5208 {edge.edge_type}     → 后续 (开放关系词表, 与 edge_type 同族)
#   5209 {edge.review_status} → 后续 (同上, admin 关系审核)
#   5589 {schema.status}      → 后续 (active|revoked, schema 启用态, 独立小枚举, 非 KNOWLEDGE_STATUS)
# 注:member.status(5798) 曾在此列,已由 Task 5 补丁 5f678760 修掉(走 label(KNOWLEDGE_STATUS))。
grep -rn '"base" : "personal"' app/answer-panel.tsx
# 宽模式:任意「查表后兜底回原值」的形状,不再只认特定变量名
# (最初计划写的窄 grep 只认 ?? stage|status|s|step.step_type,漏掉了
#  reasoning-trace.ts:86 的 ?? latest.step_type —— Task 3 执行时才发现)
# 注意:不能写成 `| grep -v "^\s*\*"` 去滤 JSDoc —— -rn 会给每行加 file:line: 前缀,
# `^` 永远锚不上,那个过滤器是死的(Task 3 评审实测复现)。改成匹配前缀之后的内容:
grep -rnE "\[[a-zA-Z0-9_.]+\]\s*\?\?\s*[a-zA-Z]" app/answer-panel.tsx app/report-view.tsx \
  app/admin/usage/notebooks.ts app/reasoning-trace.ts app/vocabulary.ts | grep -vE ":\s*\*"
```

Expected: **三条命令全部无输出**（`vocabulary.ts` 里 JSDoc 注释提到 `MAP[v] ?? v` 属文档，已由 `grep -v` 排除；若仍有命中，逐条判断是不是真泄漏）。

**不在本 PR 范围、但已知同形状的 5 处**（留给 PR B / 后续，**不要**在本 PR 顺手改）：

| 位置 | 形状 | 为什么不在本 PR |
|---|---|---|
| `page.tsx:757` / `:4746` / `:4952` / `:5019` / `:5023` / `:5053` | `RELATION_LABELS[x] ?? x` | 关系词表是**开放**词表（LLM 抽取产出任意关系名），不是封闭枚举；与被本 PR 排除的 `edge_type` 同族 |
| `page.tsx:5009` / `:5547` | `FIELD_LABELS[key] ?? key` | **兜底回原值在这里可能是对的**——自定义类型的字段名是用户自己起的，显示原 key 就是显示用户自己的词。需先判定「内置字段 vs 用户自定义字段」再决定，不能一刀切 |

**不是这个病、别顺手改的 5 处**（形状像但语义合法）：`knowhow-code-logic.ts:79`（对象兜底 `?? NONE_CODE_VIEW`）、`answer-formatting.ts:284`（正则捕获 `match[1] ?? match[2]`）、`knowhow-import-logic.ts:62`、`knowhow-optimize-logic.ts:159`、`page.tsx:3868`（`?? null`）。判据是**兜底值是不是 `value` 自己**。

- [ ] **Step 3: 确认没有越界改到 PR A2 / B 的地盘**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/merge-duplicate-dbs-d5e135
git diff --stat master...HEAD -- backend/ frontend/app/kg-type-mark.tsx frontend/app/ask-modes.ts
```

Expected: **无输出**——本 PR 零后端改动，未碰 `kg-type-mark.tsx`（归 A2）、未碰 `ask-modes.ts`。

- [ ] **Step 4: 真机验证**

前后端已在跑（3000 / 8000，用户自己启的，**不要重启**）。前端有 HMR，改动会自动生效。

用浏览器工具确认：任问一个问题 → 答案引用角标旁的徽章显示「公共知识库」或「个人知识库」，而不是 `base` / `personal`。

- [ ] **Step 5: 开 PR**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/merge-duplicate-dbs-d5e135
git rebase origin/master   # 本仓库 PR 合并方式是 Rebase and merge,分支必须线性
git push -u origin HEAD
gh pr create --base master --title "feat(fe): 枚举映射层——后端枚举不再直出给用户" --body "$(cat <<'EOF'
## 做了什么

建立 `frontend/app/vocabulary.ts`(跨模块枚举映射 + 严格查表器 `label()`),收编 12 处
「后端枚举直出」与 4 处「兜底即原值」。

`label(map, value, fallback)` 的签名**强制**传兜底词,使 `MAP[v] ?? v` 这个 bug 在类型
层面写不出来——旧写法意味着后端每加一个枚举值,UI 就自动把英文 id 泄漏给用户。

最讽刺的一处是引用徽章:tooltip 里中文译名早就写好了,可用户看得见的徽章却是裸的
`base` / `personal`。

## 不在本 PR

- `object_type`:真源在后端(`extraction_profiles.OBJECT_TYPE_LABELS`,已 API 下发),
  前端那份是重复且更差的英文表 → 归 PR A2(带 schema 迁移,单独评审)
- `edge_type`:开放关系词表且 admin-only(P2)
- 散文文案 → 归 PR B

设计:`docs/superpowers/specs/2026-07-17-user-facing-vocabulary-design.md`

## 验证

`scripts/check.sh` 全绿;真机确认引用徽章显示中文。

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review

**Spec 覆盖对照（spec §2 共享核心 → 本计划）：**

| spec 要求 | 本计划的 task |
|---|---|
| `vocabulary.ts` 只装跨模块枚举 | Task 1 |
| `label()` 签名强制兜底词 | Task 1（Step 3 实现 + Step 1 测试钉住「绝不返回原值」）|
| 收编 tier 直出 | Task 2 |
| 收编 4 处 `??` 原值兜底 | Task 3 |
| 收编 parse_status / element_type | Task 4 |
| 收编 knowledge status / promotion status | Task 5 |
| 收编 evidence_level / mode / Agent 证据 | Task 6 |
| 收编 next_action | Task 7 |
| `ask-modes.ts` 保持独立、只引用不改 | Task 6（复用它的 label 而非抄一份）+ Task 8 Step 3 钉住未改 |
| object_type 不在本 PR | Global Constraints + Task 8 Step 3 钉住未碰 `kg-type-mark.tsx` |

**刻意超出 spec 的一处：** spec 未提 `edge_type`。本计划显式把它排除（开放关系词表 + admin-only P2），并写进 Global Constraints，避免执行者顺手改了。

**行号敏感性：** Task 2 / 6 / 7 都要求测试**追加到文件末尾**——插在中间会移动行号，打破 `test_repository_surface_manifest.py` 那类按行号钉住的守卫。

**自查的两处失效（执行中被发现，已回写）：**

1. 函数名：原写成 `traceStepDetail`，实际是 `reasoning-trace.ts:44` 的 `getTraceStepDetail`。已改正。
2. **「4 处兜底即原值」是错的**，真实约 10 处——我自查用的 grep 只认特定变量名，漏掉了 `?? latest.step_type` 这种。
   Task 3 执行时由实现者发现。已把 `reasoning-trace.ts:86` 折进 Task 7，把 Task 8 的 grep 换成宽模式，
   并把刻意排除的 5 处连同理由列进 Task 8 Step 2 —— 一个不写明「漏了什么」的守卫，读起来就像「全覆盖了」。

**类型一致性：** `label(map, value, fallback)` 的三参签名在 Task 1 定义后，Task 2–7 的每一处调用都是三参，无漂移。
