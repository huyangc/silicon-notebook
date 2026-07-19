# Knowhow 格子 Markdown 规整（Excel 习惯 → CommonMark）

日期：2026-07-18
状态：设计已确认，待写实现计划

一句话：从 Excel 逐字导入的格子内容带着「人写的排版习惯」（Alt+Enter 换行、Tab 缩进、`•` 项目符号、`A.`/`a.` 字母编号），markdown 认不出这些，导致**渲染塌陷 + 步骤解析失效**。方案是给「规整」这一步一个 **LLM 重排 ＋ 零-LLM 内容不变式硬校验 ＋ 规则规整器兜底 ＋ 人工确认终端闸** 的管线，覆盖存量（DeepSeek-V4）与增量（导入/粘贴/编辑）。

---

## 1. 背景与问题

触发场景：DeepSeek-V4 的 `know-how沉淀-转置` 表（5 列 `违例概念/现象识别方法/根因分析动作/修复方法/依赖工具`，20 格，14 格多行），格子在查看态渲染成一大段跑马字，层级全丢。

### 1.1 实测证据（对真实 20 格逐一验证）

- **字符体检**：40 个 Tab（散落 9 格）、9 个 `•`（U+2022，2 格）、大量全角标点（`，：；（）`，正常 CJK，非问题）。Tab 是「从 Excel/表格粘贴」的典型指纹。
- **视觉渲染**（跑真实 `react-markdown + remark-gfm` 管线得到 mdast）：14 格多行里 **9 格整段塌成一长段 paragraph**（结构全丢），5 格顶层 `1.2.3.` 列表在、但子步骤 `a./b.` 被并进上一条，只有 6 个单行短格安全。
- **语义解析**（跑真实 `textops.parse_steps`）：截图那格 `r1c3 修复方法`（`•` + `A./B.`）**抽到 0 条步骤** → 整格在 KG 里退化成一坨 prose；带 `\ta./\tb.` 子步骤的格，子步骤被硬并进父步骤，层级丢失。

### 1.2 双重影响（关键）

`content_md` 同时被两类消费者读：

1. **查看/编辑预览** —— `KnowhowMarkdown`（react-markdown）。单个 `\n` 被吃成空格（无 `remark-breaks`）；`\t1.`/`\ta.` 因带缩进不被当列表。
2. **格子级 KG / Agent API** —— procedure 列的 `content_md` 经 `parse_steps()` 解析成 `steps[]`（"用有序列表写步骤，系统会识别为可执行步骤"就是它）。

所以这不是「不好看」，是**知识本身没被正确结构化**，下游 Agent 拿到残缺/空的步骤。

---

## 2. 现状地基（本设计不改动的部分）

| 环节 | 位置 | 事实 |
|---|---|---|
| 存储 | `knowhow_cells.content_md TEXT`（`migrations.py:1281`） | 单列存 markdown 源；内容类型在**列**上（`knowhow_columns.role`，wire 名 `kind`：`anchor/procedure/entity/attribute`） |
| 导入 | `grid_parser.py:146` / `api.py:201` | xlsx 单元格 `str(cell)` **逐字写入、零转换**；合并单元格把左上值填满整个 range |
| 渲染 | `knowhow-cell-editor.tsx:139/176` | `react-markdown` + `remark-gfm` + `remark-math` + `rehype-katex`；**无 `remark-breaks`、无 raw HTML** |
| 步骤解析 | `textops.py:60`（`parse_steps`） | **扁平**解析；正则 `_ORDERED_ITEM_RE=^\s*\d+[.)]\s+`、`_UNORDERED_ITEM_RE=^\s*[-*+]\s+`——**不认 `•`，不认 `a./A.`**；非 marker 行并入上一步 |
| 现有 LLM | `api.py:692`（`optimize_cell`，「优化表达」） | LLM **改措辞**，suggestion-only，从不写库；per-user rewrite client |

`parse_steps` 用 `^\s*` 容忍前导空白，所以它对 `\t1.` 反而**能**识别（只是把 `\ta.` 子项并进父步骤）；react-markdown 则对缩进**不**容忍。两个消费者对同一份脏文本容忍度不同——一份干净 CommonMark 能同时喂饱两者，这正是本设计的立足点。

---

## 3. 设计目标 / 非目标

**目标**
- 把 Excel 习惯排版规整成干净 CommonMark，**视觉与 `parse_steps` 同时修好**。
- 覆盖**存量**（DeepSeek-V4 的 20 格）+ **增量**（导入 / 粘贴 / 编辑器手动）。
- **保证**：规整只改格式、**不改内容**——靠一道零-LLM 硬校验，而非只靠 prompt。
- 每条路径终点都有**人工确认**（采纳 / 直接编辑 / 放弃）。

**非目标（scope 边界）**
- 不改 `parse_steps` 的扁平模型。规整后嵌套子项会变成**平级步骤**（比现在的 0 步骤 / 一坨跑马字严格更好）；真·层级步骤留作以后单独增强。
- 不翻译、不改措辞（那是 `optimize_cell` 的活，保留其独立角色）。
- 不加 `remark-breaks`（规整后用空行分段即可，不改渲染器全局行为）。
- 不做全库批量。回填脚本可复用，但本次只跑 DeepSeek-V4（`nb-a73f16940c`）。

---

## 4. 核心组件

### 4.1 内容不变式校验 `content_signature` / `content_invariant`（零 LLM，硬闸）

这是「保证不改内容」的机器背书，也是本设计的核心新增。

- `content_signature(md) -> str`：**逐行**剥掉格式字符、只留每行的「有意义字符序列」，再把各非空行**按行拼接**（行间保留分隔）。按行（而非整篇拉平）是必须的——否则内容可在行/条目边界间迁移而签名不变（假通过）。这也意味着校验以**行结构不变**为前提。
  - 剥离：行首 list marker（`- * +`、`• ● ◦ ▪ ‣`、`\d+[.)、]`、**缩进的**单字母 `[a-zA-Z][.)、]`）、`#` 标题符、强调 `** * _`、blockquote `>`、Tab、行首/行尾空白、空行、词间空白，**以及所有标点 `P*` 类**（全角 `，：；（）、“”` / 半角 `,.;:()` / 破折号 等）。
  - 保留：**CJK 表意文字 / ASCII 字母 / 数字，加上所有 Unicode 符号 `S*` 类（数学 `=+<>→`、emoji `✅`、货币 `￥$`、修饰符）**——即「内容」。**只放宽标点 `P*`，符号 `S*` 属内容**（见下「严格度」）。
  - marker 检测与 §4.2 规整器**共用同一套**（保证一致）：凡规整器判为「list marker」的就剥；**顶格 `A.`/`B.`（分节标题）不是 list marker → 其字母/数字部分作内容保留**。
- 图片 `![alt](url)`、代码围栏内容：**单独逐字（byte-identical）校验**，不进 signature。
- `content_invariant(before, after)` ⇔ `signature(before)==signature(after)` 且 图片集合、代码块逐字未变。
  - 相等 → LLM 只动了格式（或标点）→ **通过**；
  - 不等（改一个字/一个数字、调一句顺序、增删一句）→ **不通过** → 回退。
- **严格度（已定：放宽=仅标点 `P*` 类；符号 `S*` 属内容）**：**只有标点 `P*` 不计入内容**——LLM 可整理全角/半角标点及其间距（如 `R：` → `R:`、`（1W1S）` → `(1W1S)`、`、`→`,`）而不触发回退。**数字、字母、CJK，以及所有 Unicode 符号 `S*`（数学 `=+<>→`、emoji `✅`、货币 `￥$`）仍逐字严校**——`1800M`→`1000M`、`V=IR`→`V+IR`、`x<0`→`x>0`、`✅`→`❌`、删字均必拒。注：`*`/`_`/反引号属标点 `Po`，且被强调剥离先行去掉，故不因 `S*` 规则误升为内容（`R*C` 中的 `*` 仍作强调符剥离）。

测试锚点：正例（把 `•`/`A.`/Tab 变干净 markdown，且把 `R：` 整成 `R:`）**必过**；反例（`1800M`→`1800MHz`、删句、两条 bullet 调序）**必拒**；标点归一化（`、`→`,`、全角冒号→半角）**放行**。

### 4.2 规则规整器 `rule_normalize`（零 LLM，兜底 + 安全网）

从「包打天下」降级为**安全网**：LLM 不在 / 不可用 / 没过校验时接手；也用于粘贴即时规整。

**入口 allow-list 门（`is_rich_markdown`，fail CLOSED）**：`rule_normalize` 先过一道保守门——**当且仅当每一行都落在一套封闭文法内**（① 空行；② `classify_line` 判为 bullet/ordered/alpha 的 list-marker 行，含 Excel 的 tab/空格缩进 marker——但其前导空白须过 CommonMark 的 4 空格消歧：含 TAB（Excel 指纹）或纯空格 ≤3（CommonMark 仍算列表项，也是本规整器单层嵌套发出的 3 空格）才放行，**纯空格 ≥4 判为缩进代码块、整格拒绝**（堵住「内容行恰好长得像 marker 的 4 空格缩进代码块被误当列表规整」这个对抗性洞）；③ 顶格 prose 行：无前导 tab/空格、且不以其它 CommonMark 块级构造开头，即不以 ``` / `~~~` / `|` / `>` / `<字母或/` / `#` / `[label]:` 开头，也不是只由 `| : - =` 加空白组成的 GFM 分隔行/setext 下划线/主题分隔线）才允许规整；**任何一行落在文法之外——尤其任何「有前导缩进却不是 list marker」的行（缩进代码块、列表延续行、缩进 prose 都属此类）——整格字节级原样返回，不进逐行重排**。这是把早期的 deny-list（枚举「危险信号」）翻转成 allow-list：markdown 块级结构是开放集合，deny-list 每轮评审都漏一种新形状（围栏、无竖线表格、延续行、4 空格缩进代码块），而 §4.1 的内容不变式对「文字没变、结构被毁」结构性失明兜不住；改成对未知结构默认不动，是唯一稳妥的失败模式（这个功能的目标只是清理 Excel 脏排版，一个已有结构的 cell 不是它的目标）。

门另有两条**上下文感知**规则（Batch F）：**规则 1（懒延续）**——顶格 prose 行**紧跟**（无空行）某 marker 行时，是该列表项的 CommonMark 懒延续（`_normalize` 会注入空行把它拆下来、内容不变式字符盲兜不住），整格拒绝；marker 与 prose 间夹一个空行即按 CommonMark 断开延续、仍放行。**规则 2（主题分隔线）**——在 marker 接受**之前**识别 CommonMark 主题分隔线（去掉所有空格/tab 后由 ≥3 个同一 `* - _` 字符组成，覆盖 `***`/`___` 及空格分隔的 `* * *`/`- - -`/`_ _ _`）并整格拒绝，避免 `* * *` 被 bullet 正则误当列表项规整成 `- * *`（既有 `| : - =` 分隔规则只捕获裸 `---`，本规则推广到空格分隔与 `*`/`_` 形式，两条并存不合并）。

- 逐行处理（仅对通过门的 cell）：
  1. `indent_depth` = 前导 Tab 数 + 前导空格 // 2；对所有 list 行取 `min_depth` 归零做基线（修掉原型里「整体多缩一层」的 bug）。
  2. marker 检测（与 §4.1 共用）：
     | 输入 | 输出 |
     |---|---|
     | `• ● ◦ ▪ ‣` / 已有 `- * +` | `- ` 无序项 |
     | `1.` `1)` `1、` / 已有 `1.` | `1. ` 有序项 |
     | **缩进的** `a.` `b.`（Tab 下子步骤） | 上一级下**嵌套 `- `**（去掉 a/b 字母） |
     | **顶格**的 `A.` `B.`（分节标题） | `**A. …**` 加粗段落（**不是** list） |
     | 无 marker 散行 | 各自成段（空行分隔） |
  3. 发射 CommonMark：list 前补空行、按层级缩进、相邻项成紧凑列表。
- **原样保留**：全角标点、图片 `![](asset://…)`、代码围栏、`$公式$`。
- **幂等**（对已规整内容 = no-op）、**永不抛异常**（任何异常 → 返回原文）。
- 已验证能力：真实 20 格上塌陷从 **9 → 4**（剩 4 为嵌套列表基线细节，实现时收干净）。

### 4.3 LLM 规整 `llm_reformat`（复用 per-user rewrite client）

- 严格 **format-only** prompt（区别于 `optimize_cell`）：
  - 只许把 `•`/`A.`/Tab/换行整理成干净 markdown、把顶格 `A./B.` 加粗成小标题、把缩进子项变嵌套列表；
  - **明令禁止**改词、增删、翻译、改标点、调序；**保持行结构（不拆分/合并行、总行数不变）**——与 §4.1 按行校验对齐，避免请求校验必拒的整行拆分；`![](asset://…)` 原样保留；
  - 只输出重排后的 markdown 正文（JSON `{"reformatted_md": "..."}`）。
- 复用 `optimize_cell` 的 client 解析基建（`chat_json` + schema hint）。

### 4.4 统一编排 `reformat_cell(raw) -> {candidate, source, changed, invariant_passed}`

```
若 LLM 可用:
   cand = llm_reformat(raw)
   若 content_invariant(raw, cand):  source = "llm"
   否则:                             cand = rule_normalize(raw); source = "rule/llm-failed"
否则:                                cand = rule_normalize(raw); source = "rule/no-llm"
changed = (cand != raw)
→ 返回给调用方交【人工确认】：采纳 / 直接编辑 / 放弃      ← 终端闸，贯穿所有落点
```

`source`/`invariant_passed`/`changed` 回传给前端与回填报告，人工据此判断。

---

## 5. 落点（前后端同步）

### 5.1 编辑器「规整格式」按钮（前端 + 后端端点）

- 新按钮挨着「优化表达」；`POST /notebooks/{nb}/knowhow/{table}/rows/{row}/cells/{col}/reformat` → `reformat_cell`（读**已保存**的 `content_md`，与 optimize 一致，有未存改动时禁用）。
- 返回 `{candidate_md, source, changed}`；复用 optimize 的 before/after compare UX：**接受 / 直接编辑 / 放弃**；接受只填回 textarea，仍需手动保存（走既有 `patchKnowhowCell`）。
- 与「优化表达」并列但语义不同：**规整格式**＝只改格式 + 硬校验；**优化表达**＝改措辞（可选润色，叠加在规整之上）。
- **粘贴**：`onPaste` 拦截，前端 `rule_normalize` **即时**规整粘入片段（可 Cmd+Z 撤销）；LLM 精整留给按钮，不在粘贴热路径塞后端往返。

### 5.2 导入（后端 `import_table` / `commit_append`）

- 预览与提交：**inline `rule_normalize`**（即时、免费、零 LLM）——「自动规整、到手不破」。
- 人工确认 = 导入预览这一步（**已存在**，PR#285）。
- **⚠️ 决策点（供评审）**：导入默认走**规则**、不在关键路径对每格调 LLM。理由：批量导入 per-cell LLM 违反效率约束（用户强约束：新增 LLM 调用须先问代价 + 能否 gate）。LLM 精整改为**人工触发**的 batch 动作（§5.3）。若评审希望「导入即 LLM」，可加一个开关，仅对脏格 + 后台 job 执行——但默认关。

### 5.3 批量「一键规整整表 / 整行」（前端 + 后端）

- 复用现有「优化整行」状态机（`knowhow-optimize-logic.ts`）：逐格 `reformat_cell` → 汇总 diff → **人工整体确认**后落库。
- 这是导入后把整表 LLM 精整的入口。

### 5.4 回填脚本（后端，一次性）

- `scripts/backfill_knowhow_md.py --notebook <id> [--apply] [--no-llm]`
- 默认 **dry-run**：产出每格 `before / after / source / invariant_passed / changed` 的 diff 报告。
- **人工过目 → `--apply`** 落库（事务，只改真正变化的格）→ 触发 reprojection（既有 pending 机制）让 KG/步骤按新内容重算。
- 先跑 DeepSeek-V4（`nb-a73f16940c`）；脚本本身通用（按 `--notebook` gate）。
- CLI 用法写进 `README.md` + `README_zh.md`（仓库约定：用户可运行脚本要进两份 README）。

---

## 6. 数据流（以截图格 `r1c3 修复方法` 走一遍）

```
库里(verbatim):  "…增大线延。\nA. 增大 R 和 C 的考量\n\t• 增加RC…\n\t• 增大 R：…\nB. …\n\t• Shielding…"
  ↓ reformat_cell（LLM 重排）
候选:            "…增大线延。\n\n**A. 增大 R 和 C 的考量**\n\n- 增加RC…\n- 增大 R：…\n\n**B. …**\n\n- Shielding…"
  ↓ content_invariant(before, after)   →  剥格式后字符序列相等 → 通过（source=llm）
  ↓ 人工确认（接受）
落库 content_md（规整版）→ row pending → reprojection
  ↓
渲染: intro 段 + A 小标题 + 4 项列表 + B 小标题 + 2 项列表
parse_steps: 0 步骤 → 6 步骤（`•`→`-` 后可识别）
```

---

## 7. 边界与错误处理

- `rule_normalize` **永不抛**：任何异常 → 返回原文（宁可不改，绝不产出更烂的）。
- `llm_reformat` 超时 / 失败 / 未配置 → 回退规则（`source` 标记）。
- `content_invariant` 不通过 → 回退规则（`source=rule/llm-failed`），并在报告/前端标出，供人工留意。
- 空格子 / 单行短格 → `changed=false`，UI 提示「已是规整格式」。
- 回填 `--apply` 事务化，失败回滚；重复跑幂等（已规整内容校验必过、`changed=false`）。
- 图片/代码块：校验保证 LLM 未动；规则器原样保留。

---

## 8. 测试策略

- **`rule_normalize` 单元**：真实 20 格做 golden before/after 夹具（含 `•`、`\ta.`、`A./B.`、多行散行、纯 tool 双行）。
- **`content_signature` / `content_invariant` 单元**：正例（只改格式）过；反例（改词 `1800M→1800MHz`、删句、bullet 调序、`、→,`、动图片 url）拒。
- **`parse_steps` 前后对照**：`r1c3` 0→N；子步骤格从「并入父步骤」→「平级步骤」。
- **parity**：前端 TS `rule_normalize` 与后端 Python 版跑**同一份 golden 夹具**，防漂移。
- **`reformat_cell` 编排**：三分支（llm-pass / llm-fail→rule / no-llm→rule）。
- **集成**：import → 规整 → projection 步骤数。
- **回填**：dry-run 报告快照。

---

## 9. 改动清单（供实现计划）

- **backend**
  - 新 `app/services/knowhow/md_normalize.py`：`rule_normalize` + `content_signature` + `content_invariant`（marker 检测单一真源）。
  - `app/services/knowhow/api.py`：`llm_reformat`（format-only prompt）+ `reformat_cell` 编排。
  - `app/api/routes.py`：`POST …/cells/{col}/reformat` 端点。
  - `app/services/knowhow/api.py`（`import_table`/`commit_append`）：写库前 inline `rule_normalize`。
  - `scripts/backfill_knowhow_md.py`。
- **frontend**
  - `knowhow-cell-editor(-logic).ts`：「规整格式」按钮 + `onPaste` 即时规整 + TS `rule_normalize`（与 Python parity）。
  - `knowhow-model.ts`：`reformatKnowhowCell` wire 调用。
  - `knowhow-optimize-logic.ts`：batch「一键规整整表/整行」复用。
- **docs**：`README.md` / `README_zh.md` 记回填 CLI。
- **⚠️ 守卫注意**：新 facade 成员走 allowlist + 一跳委托；`test_repository_surface_manifest` 行号敏感（新增/移动测试须重生成 EXPECTED_PATCH_DELTAS）；**schema 不变**（无新表，不 bump SCHEMA_VERSION）；架构文档措辞若动须同步 `test_architecture_documentation.py`。

---

## 10. 不做的事（scope 边界）

- `parse_steps` 嵌套层级重构。
- 翻译 / 改措辞（`optimize_cell` 的领域）。
- 全库批量规整（本次只 DeepSeek-V4）。
- 全局启用 `remark-breaks`。
- `test` 表（84 短格）以外的额外清理——回填对短格是幂等 no-op。

---

## 开放问题（留给评审拍板）

1. **导入默认规则 vs LLM**：〔按默认推进〕导入走规则（守效率）、LLM 作人工触发精整。评审可后续改为「脏格 + 后台 job 自动 LLM」。
2. **粘贴规整**：〔按默认推进〕前端 `rule_normalize` 即时；「粘贴后一键 LLM 精整」提示留作后续增强。
3. **校验严格度**：✅ **已定——放宽=仅标点 `P*`**。只有标点不计入内容不变式；数字/字母/CJK **加上所有 Unicode 符号 `S*`（`=+<>→✅￥$`）** 仍严校（详见 §4.1）。
