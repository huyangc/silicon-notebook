# 实现计划：扩展 UI 组件层（Extension UI Kit）+ arXiv 样板重塑

状态：已实现并完成专项验证（2026-08-25）。用户已批准方案（三选项讨论后选定「SDK 补一层结果化组件」，
并同批把 arXiv 样板改用新组件）。本文是实现的规格来源；文件路径均已核实，**行号与函数
内部形状以当前树为准**，实现前先自行复核。

## 背景与问题

`ExtensionModal`（`frontend/features/extension-sdk/ui.tsx`）的**外壳**已经复用核心类
（`utility-modal` / `source-modal-header` / `source-detail-body` / `FloatingModalCard`），
但弹窗**内容**没有任何供给：插件红线禁止写 CSS、禁止内联颜色（守卫
`frontend/tests/guards/extension-ui-layout-guard.test.mjs` 两侧钉死），于是插件作者
"合规的做法就是裸 HTML"——arXiv 样板面板
（`examples/extensions/arxiv-search/ui/arxiv-search/workspace-plugin.tsx`）全程只用了
`.button`，列表/输入框/提示全是浏览器默认样式。样板是内网插件作者会照抄的模板，
它裸奔，之后每个内网插件都裸奔。

## 主 agent 裁决（优先于下文任何相反表述）

1. **组件一律加进 `frontend/features/extension-sdk/ui.tsx`，不新开 SDK 模块。**
   插件 import 白名单是精确四项（同包兄弟、`contracts.ts`、`ui.tsx`、裸 react/lucide-react），
   判据唯一实现在 `frontend/tests/guards/_plugin-import-predicate.mjs`（`SDK_UI` 常量精确
   钉 `features/extension-sdk/ui.tsx`）。新开 `ui-kit.tsx` 意味着改判据 + 两个消费守卫 +
   SOP 文档，为文件组织付合同变更的代价，不值。`ui.tsx` 变长是可接受的。
2. **样式全部落在基座 `frontend/app/globals.css` 的新 `.extension-*` 类**，只用既有
   `:root` token（`--ink`/`--muted`/`--line`/`--soft`/`--danger*`/`--warning*` 等），
   **零颜色字面量、零内联 style**——layout guard 现有用例「ui.tsx 的内联 style 例外只放行
   zIndex 这一个键」必须保持绿：新组件一个内联 style 都不许有。
3. **组件集合按 arXiv 面板的真实需要定形，七个，不做预备性扩张**（YAGNI；将来新形状走
   基座 PR，这与「插件新依赖走基座 PR」是同一条哲学）：
   - `ExtensionFormRow` — 表单行容器（flex 横排、间距、输入框自动伸展）。渲染 `<form>`，
     透传 `onSubmit`。
   - `ExtensionTextInput` — 文本输入框（token 描边、圆角、focus 态与系统一致；透传
     value/onChange/placeholder/aria-label/disabled）。
   - `ExtensionResultList` / `ExtensionResultItem` — 结果清单。item 结构化 props：
     `checkbox?: { checked, onChange, ariaLabel }`（可选，给成勾选行）、`title`（必填，
     ReactNode）、`meta?`（次行，muted 小字：作者/日期这类）、`summary?`（摘要，muted、
     CSS 多行 clamp，默认 4 行；**纯 CSS clamp（`-webkit-line-clamp`），不做 JS 展开
     交互**——v1 先把视觉做对，展开是后续增强）、`children?`（附加内容）。列表去掉
     浏览器默认 marker，行间用 `--line` 分隔或留白节奏，复选框与标题对齐。
   - `ExtensionActions` — 底部动作行（按钮横排、间距、与内容留白），装「导入所选」
     「加载更多」这类 CTA。
   - `ExtensionAlert` — 提示条，`tone: "error" | "warning" | "status"` 三态：error 用
     `--danger*` 三件套 + `role="alert"`，warning 用 `--warning*`，status 中性
     （`--soft`/`--muted`）+ `role="status"`。与 `AnomalyBadge` 的三档语义对齐但**不**
     复用其类（那是来源异常小字专用，语境不同）。
   - `ExtensionEmptyState` — 空态一句话（muted、居中或留白版式），装「没有找到相关文献」。
4. **组件不带默认中文文案**——所有 copy 由插件传入。理由：文案守卫（界面词汇硬门）覆盖
   `app` 与 `features`，组件零自带文案就零守卫面；插件包不在词汇守卫扫描面内（部署方
   自己的责任），样板包的文案照旧受 G2 `check_sample_plugin.sh` 同步树扫描约束。
5. **新增守卫：`ui.tsx` 里出现的每个 className token 必须真的存在于 `globals.css`**
   （照抄 `group-page-style-guard.test.mjs` 的做法：走 AST 收 className 静态 token，逐个
   对账类选择器存在性）。这是这次改动最值钱的一道门——className 字符串没有类型检查，
   「挂了样式表里不存在的类」不报错、只是长错了，正是群组页改版踩过的坑。样板包
   （及将来 ext-* 包）里的 className 由既有 G2 同步树守卫兜底，本守卫至少覆盖 SDK 自身。
6. **样板重塑不改任何行为**：状态机、请求形状、防御性 re-check、receipt 语义、
   `alreadyImported` 会话级记忆全部逐字保留，只换渲染层。`arxiv-sample-ui-package.test.mjs`
   钉住的 manifest 字段一个都不动。
7. **文档同步**（实现 PR 内完成，不留尾巴）：`docs/deployment-extensions-sop.md` + `_zh.md`
   的 UI 编写节补「面板内容用 SDK 组件层」一段（组件清单 + 一句"推荐而非强制"——裸 HTML
   不违规，只是丑）；样板 `examples/extensions/arxiv-search/README.md` + `_zh.md` 同步。
   CLAUDE.md「Workspace UI registry」条目末尾加一句组件层存在性提示（连同 AGENTS.md
   对应节，四份同步规则按现行口径执行）。

## 任务拆分（串行，每任务收尾跑 G1 相关泳道）

### T1 组件层 + CSS + 守卫

- `frontend/features/extension-sdk/ui.tsx`：新增裁决 3 的七个组件与类型导出；模块头注释
  补一段「内容组件层」的存在理由（把「视觉红线做成结构性的」那段论证从 ExtensionModal
  推广到内容层）。
- `frontend/app/globals.css`：新增 `.extension-form-row` / `.extension-input` /
  `.extension-result-list`（含 item/title/meta/summary 子类）/ `.extension-actions` /
  `.extension-alert`（三 tone 修饰类）/ `.extension-empty` 一组类，只用 token。视觉基准
  向既有弹窗内容（来源详情、model-service-panel）看齐，不发明新的间距/字号体系。
- 新守卫 `frontend/tests/guards/extension-ui-kit-style-guard.test.mjs`：裁决 5 的
  className↔globals.css 对账；外加一条钉「kit 组件零内联 style」（复用 layout guard 的
  现有判据形状，或直接把 kit 纳入其现有用例的扫描面——二选一，取改动小的）。
- 组件测试 `frontend/tests/component/extension-ui-kit.component.test.tsx`：三 tone 映射到
  对应类与 role、checkbox 行受控切换、summary clamp 类挂上、空 props 分支不炸。
- **变异验证**：把某个组件的 className 改成不存在的类名，确认新守卫真的红；改回。
  先 `git commit` 打检查点再做（变异纪律照 CLAUDE.md）。

### T2 arXiv 样板重塑

- `examples/extensions/arxiv-search/ui/arxiv-search/workspace-plugin.tsx`：裁决 6 口径下
  换用 kit 组件——检索行 → `ExtensionFormRow`+`ExtensionTextInput`；错误/无结果/超限提示 →
  `ExtensionAlert`/`ExtensionEmptyState`；结果清单 → `ExtensionResultList`（checkbox +
  title + authors/date meta + summary clamp）；「加载更多」「导入所选」→ `ExtensionActions`；
  导入回执 → `ExtensionResultList`（每行按 created/repeat/rejected 配 status/warning/error
  的 `ExtensionAlert` 行内形态或 meta 标注，实现取视觉更清楚的一种）。
- 跑 `frontend/tests/unit/arxiv-sample-ui-package.test.mjs` 与既有样板测试；本地跑一遍
  `scripts/check_sample_plugin.sh`（G2 泳道，验证同步树 + 词汇扫描仍绿）。
- import 白名单自检：重塑后 workspace-plugin.tsx 的 import 仍只有 contracts/ui/react/
  lucide-react/同包兄弟（`extension-ui-boundary` / `extension-plugin-package-guard` 绿）。

### T3 文档同步 + 全量门禁

- 裁决 7 的四处文档；`bash scripts/check.sh` 整绿；提 PR 走 codex 评审闭环。

## 验收

- arXiv 弹窗视觉与核心弹窗同语言（输入框有描边、清单无裸 marker、摘要收敛、提示分级
  着色），插件侧仍零 CSS/零内联样式/零颜色字面量。
- 新守卫经变异验证证明有效。
- G1 全绿；`check_sample_plugin.sh` 绿。
