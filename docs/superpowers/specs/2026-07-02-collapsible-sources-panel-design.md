# 设计:Source Stack 侧栏可收起(对话区铺满)

- 日期：2026-07-02
- 范围：notebook 工作区(問答/知識庫)左侧「Source Stack」栏支持收起/展开;收起后右侧对话区铺满整页。Gemini 式边缘 hover 把手交互 + 状态持久化。
- 不在本 spec：KG 视图布局、移动端堆叠布局(不启用收起)、其它面板。

## 背景与动机

用户希望能收起左侧来源栏,让对话区占满整个宽度(参考 Gemini 侧栏收起)。当前布局 `.workspace-grid` 是两列 CSS grid `minmax(270px, 25%) minmax(0, 1fr)`([globals.css:701](../../../frontend/app/globals.css))——左 `aside.workspace-panel.sources-panel`、右 `section.workspace-panel.chat-panel`([page.tsx:3000-3158](../../../frontend/app/page.tsx))。收起即把左列压到 0、右列铺满。

## 架构

纯前端、纯展示状态。一个持久化布尔 `sourcesCollapsed` 驱动 `.workspace-grid` 的一个修饰类,CSS 切换网格列宽 + 面板可见性;两个 hover 触发的把手按钮(收起/展开)。无后端、无数据流变化。

## 组件

### 1. 状态 `sourcesCollapsed`(持久化)
- `page.tsx` 新增 `const [sourcesCollapsed, setSourcesCollapsed] = useState(false)`。
- 初始化 + 持久化到 `localStorage` key `sn.sourcesCollapsed`(读:惰性初始化或 mount effect 避免 SSR 不匹配——Next.js 客户端组件,用 mount 后 `useEffect` 读取 localStorage 再 `setSourcesCollapsed`,写:`useEffect([sourcesCollapsed])` 存回)。默认 `false`(展开)。
- toggle:`const toggleSources = () => setSourcesCollapsed(v => !v)`。

### 2. 布局切换(CSS)
- `.workspace-grid` 容器加条件类:`className={\`workspace-grid ${sourcesCollapsed ? "sources-collapsed" : ""}\`}`。
- `globals.css`:
  - `.workspace-grid { transition: grid-template-columns 200ms ease; }`(平滑)。
  - `.workspace-grid.sources-collapsed { grid-template-columns: 0 minmax(0, 1fr); }`。
  - `.workspace-grid.sources-collapsed .sources-panel { width: 0; min-width: 0; padding: 0; border: 0; box-shadow: none; overflow: hidden; opacity: 0; pointer-events: none; }`(内容不撑开、不可交互;transition opacity/width 与网格同步)。
  - chat-panel 无需改(`minmax(0,1fr)` 自动铺满)。

### 3. 收起把手(展开态)
- 位置:`sources-panel` 右边缘的一个 hover 触发区 —— 一个 `absolute` 定位的小圆钮(直径 ~26px,`«` / lucide `ChevronLeft` 或 `PanelLeftClose`),垂直居中贴右边缘。
- 默认低可见(opacity 0 或半透明),`.sources-panel:hover` 时浮出(opacity 1);始终可点(便于键盘/触屏可达)。
- `aria-label="收起来源栏"`,`title="收起"`。点击 `toggleSources`。

### 4. 展开把手(收起态)
- 收起态时,在 `workspace-grid` 内渲染一条**极窄常驻 rail**(左侧,宽 ~12px,`.sources-reveal-rail`),hover 浮出小圆钮(`»` / `ChevronRight` 或 `PanelLeftOpen`),`aria-label="展开来源栏"`。点击 `toggleSources`。
- rail 仅在 `sourcesCollapsed` 时渲染(条件渲染),`position: absolute` 贴 grid 左缘,`z-index` 高于 chat-panel 内容但不挡工具行。

## 数据流 / 边界

- 单向:`sourcesCollapsed` → class → CSS。toggle 只翻转状态。
- **持久化**:localStorage 读/写;读失败(隐私模式等)静默降级为默认展开。
- **移动端**:现有 media query(`globals.css:3121/3184`)把 `.workspace-grid` 改为竖向堆叠。收起类只覆盖桌面网格列——在堆叠断点下用 media query 让 `.sources-collapsed` 的列覆盖失效 / 把手隐藏(`@media (max-width: <断点>) { .sources-reveal-rail, 收起把手 { display: none } .workspace-grid.sources-collapsed { grid-template-columns: (堆叠原值) } }`),即移动端不启用收起,行为同现状。
- **过渡期**:chat 内容(力导图/列表)已 `minmax(0,1fr)` 容器自适应,收起放大时自然回流,无溢出。

## 测试

- **tsc** 干净 + 现有前端测试绿。
- **视觉验证**(preview):展开 → hover 右边缘出「«」→ 点击收起、对话区铺满 → 收起态左侧细 rail、hover 出「»」→ 点击展开复原;刷新后保持收起态;移动端断点下把手隐藏、布局同现状。
- 把手对齐/圆钮精致,符合项目 UI 对齐标准。

## 风险

- **SSR/hydration 不匹配**:localStorage 只能客户端读——用 mount 后 effect 读取(首帧默认展开,读后可能瞬时切收起)。可接受;若闪烁明显,后续可加 `suppressHydrationWarning` 或 CSS 预判,不在 v1。
- **把手可发现性**:纯边缘 hover 盲区难发现——已用「收起态常驻细 rail」缓解;展开态的收起钮半透明常驻(非全隐)提升可发现性。
