"use client";

/**
 * knowhow-panel-styles.tsx
 *
 * KnowhowPanel 的整套全局样式（.knowhow-view / .knowhow-* / .kh-* 等），从
 * knowhow-panel.tsx 原样搬来（S4-b）：模板字面量一字未改，注释与缩进都是原文。
 *
 * 只由 KnowhowPanel 渲染一次，位置是面板 <section> 的最后一个子节点（与搬家前
 * 的 <style jsx global> 同一树位置），因此注入时机与级联顺序都不变。字面量里
 * 提到的「本特性唯一保证已挂载的样式容器」仍指 KnowhowPanel——它通过渲染本组件
 * 挂载这份样式。
 *
 * 写法约束：根元素本身就是 `<style jsx global>`（styled-jsx 接受这种写法，
 * `next build` 已实证仍为 global 注入）。不要改成 styled-jsx/css 的
 * `css.global`（未转译时直接 throw，vitest 不转译），也不要拆成独立 .css 文件
 * （注入时机与级联顺序都会变）。改字面量内容会改变 styled-jsx 的样式 id。
 */
export function KnowhowPanelStyles() {
  return (
    <style jsx global>{`
        .knowhow-view {
          position: fixed;
          inset: 0;
          z-index: 50;
          background: var(--panel);
          color: var(--ink);
          display: flex;
          flex-direction: column;
        }

        /* 面板打开时焦点落在面板本身（S7，tabIndex=-1，让读屏先读出「Knowhow 表」）；
           它不是可操作控件，不画焦点框。 */
        .knowhow-view:focus {
          outline: none;
        }

        .knowhow-view-header {
          display: flex;
          justify-content: space-between;
          align-items: flex-start;
          gap: 18px;
          padding: 16px 20px;
          border-bottom: 1px solid var(--line);
        }

        .knowhow-view-header h2 {
          margin: 0;
        }

        .knowhow-view-header p {
          margin: 4px 0 0;
          color: var(--muted);
          font-size: 13px;
          max-width: 82ch;
        }

        .knowhow-view-body {
          flex: 1 1 auto;
          min-height: 0;
          overflow-y: auto;
          padding: 20px;
          display: flex;
          flex-direction: column;
          gap: 16px;
        }

        .knowhow-toolbar {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 12px;
        }

        .knowhow-toolbar-actions {
          display: flex;
          align-items: center;
          gap: 8px;
        }

        .knowhow-import-button {
          display: inline-flex;
          align-items: center;
          gap: 6px;
        }

        .knowhow-loading,
        .knowhow-inline-error {
          padding: 40px 20px;
          text-align: center;
          color: var(--muted);
        }

        .knowhow-inline-error button {
          margin-left: 8px;
        }

        .knowhow-empty {
          min-height: 320px;
          display: grid;
          place-items: center;
          align-content: center;
          gap: 10px;
          text-align: center;
          color: var(--muted);
          padding: 18px;
        }

        .knowhow-empty-icon {
          font-size: 42px;
        }

        .knowhow-empty strong {
          color: var(--ink);
          font-size: 16px;
        }

        .knowhow-empty p {
          margin: 0;
          line-height: 1.5;
        }

        .knowhow-table-cards {
          display: grid;
          grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
          gap: 12px;
        }

        .knowhow-table-card {
          display: grid;
          gap: 6px;
          text-align: left;
          border: 1px solid var(--line);
          border-radius: 12px;
          padding: 16px;
          background: #fff;
          min-width: 0;
        }

        .knowhow-table-card:hover {
          border-color: var(--blue);
          box-shadow: var(--shadow);
        }

        .knowhow-table-card strong {
          font-size: 15px;
          color: var(--ink);
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
        }

        .knowhow-table-card p {
          margin: 0;
          font-size: 13px;
          color: var(--muted);
          display: -webkit-box;
          -webkit-line-clamp: 2;
          -webkit-box-orient: vertical;
          overflow: hidden;
        }

        .knowhow-table-card-meta {
          font-size: 12px;
          color: var(--muted);
        }

        .knowhow-grid-view {
          display: flex;
          flex-direction: column;
          gap: 14px;
          min-height: 0;
          flex: 1 1 auto;
        }

        .knowhow-grid-toolbar {
          display: flex;
          align-items: flex-start;
          gap: 16px;
        }

        .knowhow-grid-title {
          flex: 1 1 auto;
          min-width: 0;
        }

        .knowhow-grid-title h3 {
          margin: 0;
          font-size: 18px;
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
        }

        .knowhow-grid-title p {
          margin: 4px 0 0;
          font-size: 13px;
          color: var(--muted);
        }

        .knowhow-grid-toolbar-actions {
          flex: 0 0 auto;
          display: flex;
          align-items: center;
          gap: 8px;
        }

        .knowhow-reproject-button {
          display: inline-flex;
          align-items: center;
          gap: 6px;
          white-space: nowrap;
        }

        .knowhow-reproject-button:disabled {
          opacity: 0.6;
          cursor: not-allowed;
        }

        .knowhow-confirm {
          display: inline-flex;
          align-items: center;
          gap: 8px;
          padding: 6px 6px 6px 12px;
          border: 1px solid #f0c0c0;
          border-radius: 999px;
          background: #fef2f2;
          font-size: 13px;
          color: #b91c1c;
          white-space: nowrap;
        }

        .knowhow-confirm-yes,
        .knowhow-confirm-no {
          border: none;
          border-radius: 999px;
          padding: 5px 12px;
          font-size: 12px;
          font-weight: 600;
          cursor: pointer;
        }

        .knowhow-confirm-yes {
          background: var(--red);
          color: #fff;
        }

        .knowhow-confirm-yes:disabled {
          opacity: 0.6;
          cursor: not-allowed;
        }

        .knowhow-confirm-no {
          background: #fff;
          color: var(--muted);
          border: 1px solid var(--line);
        }

        .knowhow-action-error {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 12px;
          padding: 8px 14px;
          border: 1px solid #f0c0c0;
          border-radius: 8px;
          background: #fef2f2;
          color: #b91c1c;
          font-size: 13px;
        }

        .knowhow-action-error button {
          border: 0;
          background: transparent;
          color: inherit;
          font-size: 16px;
          line-height: 1;
          cursor: pointer;
        }

        /* Task 12（引用跳转）：陈旧引用提示——信息性而非报错，用中性蓝而非
           .knowhow-action-error 的红，避免"引用的行已不在了"这种平常情况看
           起来像一次操作失败。 */
        .knowhow-jump-notice {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 12px;
          margin: 12px 20px 0;
          padding: 8px 14px;
          border: 1px solid #bfdbfe;
          border-radius: 8px;
          background: #eff6ff;
          color: #1d4ed8;
          font-size: 13px;
        }

        .knowhow-jump-notice button {
          border: 0;
          background: transparent;
          color: inherit;
          font-size: 16px;
          line-height: 1;
          cursor: pointer;
        }

        /* 删表失败、用户已离开那张表时的面板顶部提示（S6）：版式同上面的提示条，
           配色用 .knowhow-action-error 的红——它报的是一次失败，不是信息。 */
        .knowhow-jump-notice--error {
          border-color: #f0c0c0;
          background: #fef2f2;
          color: #b91c1c;
        }

        .knowhow-filter-row {
          display: flex;
          align-items: center;
          gap: 8px;
          position: relative;
        }

        .knowhow-filter-input {
          width: 100%;
          max-width: 360px;
          box-sizing: border-box;
          padding: 9px 12px 9px 32px;
          background: var(--soft);
          border: 1px solid var(--line);
          border-radius: 8px;
          color: var(--ink);
          font-size: 13px;
        }

        .knowhow-filter-icon {
          position: absolute;
          left: 10px;
          color: var(--muted);
          pointer-events: none;
        }

        .knowhow-no-match {
          margin: 18px 0 0;
          color: var(--muted);
          font-size: 13px;
          text-align: center;
        }

        .knowhow-grid-scroll {
          overflow: auto;
          border: 1px solid var(--line);
          border-radius: 10px;
          flex: 1 1 auto;
          min-height: 0;
        }

        .knowhow-grid-table {
          border-collapse: separate;
          border-spacing: 0;
          table-layout: fixed;
          font-size: 13px;
        }

        .knowhow-grid-table th,
        .knowhow-grid-table td {
          border-bottom: 1px solid var(--line);
          border-right: 1px solid var(--line);
          padding: 10px 12px;
          text-align: left;
          vertical-align: top;
        }

        .knowhow-grid-table th:last-child,
        .knowhow-grid-table td:last-child {
          border-right: 0;
        }

        .knowhow-grid-table th {
          background: var(--soft);
          color: var(--muted);
          font-weight: 600;
          position: sticky;
          top: 0;
          z-index: 2;
        }

        .knowhow-grid-table td:first-child,
        .knowhow-grid-table th:first-child {
          position: sticky;
          left: 0;
          background: #fff;
          box-shadow: 2px 0 4px -2px rgba(24, 39, 75, 0.15);
        }

        .knowhow-grid-table th:first-child {
          z-index: 3;
          background: var(--soft);
        }

        .knowhow-grid-table tbody tr:hover td {
          background: #f4f7ff;
        }

        .knowhow-grid-table tbody tr:hover td:first-child {
          background: #eef2ff;
        }

        .knowhow-col-header {
          display: flex;
          align-items: center;
          gap: 6px;
          flex-wrap: wrap;
        }

        .knowhow-col-name {
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
          min-width: 0;
        }

        /* canEdit=true 时的可改名列名：hover 时提示这里可以双击改名（虚线
           下划线 + cursor 变文本），不给太重的边框以免污染只读观感。 */
        .knowhow-col-name--editable {
          cursor: text;
          border-bottom: 1px dashed transparent;
        }
        .knowhow-col-name--editable:hover {
          border-bottom-color: var(--line);
        }

        /* inline 改名输入框：贴合列头字号，去掉浏览器默认外框，只在 focus
           时用蓝色边框强调正在编辑，不改变表格布局。 */
        .knowhow-col-name-input {
          font: inherit;
          color: inherit;
          background: #fff;
          border: 1px solid var(--blue);
          border-radius: 4px;
          padding: 2px 6px;
          min-width: 0;
          width: 100%;
          box-sizing: border-box;
        }
        .knowhow-col-name-input:focus {
          outline: none;
          box-shadow: 0 0 0 2px rgba(31, 94, 255, 0.15);
        }

        .knowhow-cell-open {
          display: -webkit-box;
          -webkit-line-clamp: 2;
          -webkit-box-orient: vertical;
          overflow: hidden;
          text-align: left;
          border: 0;
          background: transparent;
          color: var(--blue);
          font-weight: 600;
          padding: 0;
          cursor: pointer;
          line-height: 1.4;
        }

        .knowhow-cell-open:hover {
          text-decoration: underline;
        }

        .knowhow-cell-text {
          display: -webkit-box;
          -webkit-line-clamp: 2;
          -webkit-box-orient: vertical;
          overflow: hidden;
          line-height: 1.4;
          color: #333a45;
        }

        .knowhow-role-badge {
          display: inline-flex;
          align-items: center;
          font-size: 11px;
          font-weight: 600;
          padding: 2px 8px;
          border-radius: 999px;
          border: 1px solid var(--line);
          background: var(--soft);
          color: var(--muted);
          white-space: nowrap;
        }

        /* 徽章配色随词表收窄为四值 kind（迁移 17 起后端只返回这四值）：
           行标题=蓝、方法步骤=琥珀、工具/事物=绿、普通=中性。 */
        .knowhow-role-badge--anchor {
          color: #1f5eff;
          border-color: #c7d6ff;
          background: #eef2ff;
        }

        .knowhow-role-badge--procedure {
          color: #9a5b00;
          border-color: #f0dab3;
          background: #fdf4e6;
        }

        .knowhow-role-badge--entity {
          color: #177a55;
          border-color: #b7e4cf;
          background: #f0faf5;
        }

        .knowhow-role-badge--attribute {
          color: var(--muted);
          border-color: var(--line);
          background: var(--soft);
        }

        .knowhow-status-badge {
          display: inline-flex;
          align-items: center;
          gap: 6px;
          font-size: 12px;
          padding: 3px 9px;
          border-radius: 999px;
          border: 1px solid var(--line);
          background: var(--soft);
          color: var(--muted);
          white-space: nowrap;
        }

        .knowhow-status-badge--info {
          color: #1f5eff;
          border-color: #c7d6ff;
          background: #eef2ff;
        }

        .knowhow-status-badge--success {
          color: #177a55;
          border-color: #b7e4cf;
          background: #f0faf5;
        }

        .knowhow-status-badge--danger {
          color: #ba2d2d;
          border-color: #f0c0c0;
          background: #fef2f2;
        }

        /* Task 11（代码附件新鲜度"知识已更新"）新增色调——与
           .knowhow-role-badge--procedure / .kh-procedure-hint 同一套琥珀，
           视觉上"需要留意但不是错误"，区别于 --danger 的红。 */
        .knowhow-status-badge--warning {
          color: #9a5b00;
          border-color: #f0dab3;
          background: #fdf4e6;
        }

        .knowhow-status-retry {
          display: inline-flex;
          align-items: center;
          gap: 3px;
          border: 0;
          background: transparent;
          color: inherit;
          font-weight: 700;
          text-decoration: underline;
          padding: 0;
          cursor: pointer;
        }

        .knowhow-status-retry:disabled {
          opacity: 0.6;
          cursor: not-allowed;
        }

        .knowhow-spin {
          animation: knowhow-spin 0.8s linear infinite;
        }

        @keyframes knowhow-spin {
          to {
            transform: rotate(360deg);
          }
        }

        .knowhow-drawer-backdrop {
          position: fixed;
          inset: 0;
          z-index: 55;
          background: rgba(17, 24, 32, 0.36);
          border: 0;
          padding: 0;
        }

        .knowhow-drawer {
          position: fixed;
          top: 0;
          right: 0;
          bottom: 0;
          z-index: 56;
          width: min(640px, 100vw);
          background: #fff;
          box-shadow: var(--shadow);
          display: flex;
          flex-direction: column;
          animation: knowhow-drawer-in 0.18s ease-out;
        }

        @keyframes knowhow-drawer-in {
          from {
            transform: translateX(24px);
            opacity: 0;
          }
          to {
            transform: translateX(0);
            opacity: 1;
          }
        }

        .knowhow-drawer-header {
          display: flex;
          align-items: flex-start;
          justify-content: space-between;
          gap: 12px;
          padding: 18px 20px;
          border-bottom: 1px solid var(--line);
        }

        .knowhow-drawer-header h2 {
          margin: 0;
          font-size: 20px;
          overflow-wrap: anywhere;
        }

        .knowhow-drawer-header-actions {
          display: flex;
          align-items: center;
          gap: 8px;
          flex: 0 0 auto;
        }

        .knowhow-drawer-body {
          flex: 1 1 auto;
          min-height: 0;
          overflow-y: auto;
          padding: 20px;
          display: grid;
          gap: 22px;
          align-content: start;
        }

        .knowhow-drawer-section-head {
          display: flex;
          align-items: center;
          gap: 8px;
          margin-bottom: 8px;
          padding-bottom: 6px;
          border-bottom: 1px solid var(--line);
        }

        .knowhow-drawer-section-head h4 {
          margin: 0;
          font-size: 14px;
          color: var(--ink);
        }

        .knowhow-empty-cell {
          color: var(--muted);
          font-style: italic;
          margin: 0;
        }

        .knowhow-drawer .answer-markdown p,
        .knowhow-drawer .answer-markdown ul,
        .knowhow-drawer .answer-markdown ol {
          font-size: 14px;
        }

        .knowhow-image {
          max-width: 100%;
          border-radius: 8px;
          border: 1px solid var(--line);
        }

        .knowhow-image-loading,
        .knowhow-image-fallback {
          display: inline-block;
          color: var(--muted);
          font-size: 13px;
          padding: 8px 0;
        }

        @media (max-width: 720px) {
          .knowhow-drawer {
            width: 100vw;
          }
        }

        /* 网格空格「+」浅色占位（规格②路A「空格显浅色占位」）：复用
           .knowhow-cell-open 的布局/hover，只覆盖颜色与字重，与已填格子的
           蓝色粗体形成「有内容/待填写」的视觉区分。 */
        .knowhow-cell-open--empty {
          color: var(--muted);
          font-weight: 400;
        }

        /* G2 合并矩阵（规格 §4.2.1）：rowspan 起始格用 --soft 弱底色与四周
           独立格区分，纵向居中避免多行合并后内容贴顶——沿用本文件既有的
           CSS 变量令牌，不额外引入硬编码色值/暗色媒体查询（本应用当前无
           暗色主题，--soft 由全局 :root 统一定义，天然随主题走）。 */
        .knowhow-cell-merged {
          background: var(--soft);
          vertical-align: middle;
        }

        /* 行详情抽屉——分节「编辑」按钮（Task 7：每节打开同一个格子浮窗的
           编辑态）。 */
        .knowhow-drawer-edit-button {
          margin-left: auto;
          display: inline-flex;
          align-items: center;
          gap: 4px;
          border: 1px solid var(--line);
          border-radius: 999px;
          padding: 3px 10px;
          font-size: 12px;
          font-weight: 600;
          color: var(--blue);
          background: #fff;
          cursor: pointer;
          white-space: nowrap;
        }

        .knowhow-drawer-edit-button:hover {
          border-color: var(--blue);
          background: #eef2ff;
        }

        /* Task 15（历史抽屉）新增消费方（「设为里程碑」/「回到这里」/
           「对比」按钮）会在请求进行中传 disabled——原有两个消费方（「优化
           整行」/「一键规整」）从未传过 disabled，这条规则对它们是纯增量，
           不改变既有外观。 */
        .knowhow-drawer-edit-button:disabled {
          opacity: 0.6;
          cursor: not-allowed;
        }

        /* -------------------------------------------------------------------
           kh-* — 格子浮窗（knowhow-cell-editor.tsx 的 KnowhowCellPreview /
           KnowhowCellEditor 共用；命名空间独立于上面的 knowhow-* 前缀，避免
           与既有网格/抽屉样式混淆）。登记在这里而非那个文件自己的 style jsx
           标签里，是因为 styled-jsx 的 global 样式注入绑定「声明该标签的
           组件是否渲染过」——KnowhowPanel 是本特性唯一保证任何时候都已经
           挂载的容器，预览态/编辑态两者谁先渲染都不会缺样式。
           ------------------------------------------------------------------- */

        .kh-modal-overlay {
          position: fixed;
          inset: 0;
          z-index: 65;
          display: grid;
          place-items: center;
          padding: 24px;
          background: rgba(17, 24, 32, 0.36);
        }

        .kh-modal-card {
          width: min(880px, 100%);
          max-height: 80vh;
          background: #fff;
          border-radius: 16px;
          box-shadow: 0 24px 70px rgba(24, 39, 75, 0.24);
          display: flex;
          flex-direction: column;
          overflow: hidden;
          /* 左侧色条是查看/编辑态的第一眼视觉标记（见 --preview / --editor 修饰
             类），通过内边距让 header/body 内容与色条错开。 */
          border-left: 4px solid transparent;
          /* 浮窗 resize 手柄（.kh-modal-resize-handle，见下方）绝对定位需要一个
             定位上下文——overlay 是 position: fixed，不加这条手柄会贴到整个
             视口右下角而不是卡片右下角。 */
          position: relative;
        }

        /* 只读预览态：中性灰蓝左边条 + 白底 header 无强调。 */
        .kh-modal-card--preview {
          border-left-color: #c7d1de;
        }

        /* 编辑态：琥珀色左边条，与 procedure hint / role-badge--procedure 同一套
           琥珀语义（内容处在活跃编辑状态时的一致强调色）。 */
        .kh-modal-card--editor {
          border-left-color: #d99a3b;
        }

        /* knowhow 表版本管理 Task 16：历史态——info 蓝左边条，与
           .kh-mode-tag--history / .knowhow-status-badge--info 同一套蓝色语义
           （呼应"只读翻阅过去"的性质，区别于查看态的中性灰与编辑态的活跃
           琥珀）。 */
        .kh-modal-card--history {
          border-left-color: #1f5eff;
        }

        /* 全屏切换：kh-modal-overlay 的 padding 已经把卡片缩进 24px，全屏时把
           overlay padding 归零由卡片自己撑满，同时把圆角 / 阴影去掉——真实全屏
           质感（不是「更大一点」）。 */
        .kh-modal-overlay:has(> .kh-modal-card--fullscreen) {
          padding: 0;
        }
        .kh-modal-card--fullscreen {
          width: 100vw;
          max-height: 100vh;
          height: 100vh;
          border-radius: 0;
          box-shadow: none;
        }

        /* 浮窗 resize 手柄——右下角小三角，cursor 提示对角缩放。共用
           .kh-modal-card 的这几个消费方：格子浮窗查看/编辑态、代码浮窗、
           矩阵抽屉（.kh-matrix-card 扩展自 .kh-modal-card，同一个定位上下文）、
           行优化弹窗；各自在全屏态（若有）时不渲染这个元素（组件层面用
           fullscreen 布尔条件渲染，不是靠 CSS 隐藏；这条注释本身在 styled-jsx
           的模板字符串里，不能用反引号包代码片段——反引号会提前把整段 CSS
           模板字符串截断，之前踩过一次）。只用既有的两级灰度变量，不新开
           色板；aria-hidden——纯鼠标/触屏手柄，没有键盘等价操作，不需要出现
           在屏幕阅读器的可交互树里。 */
        .kh-modal-resize-handle {
          position: absolute;
          right: 0;
          bottom: 0;
          width: 22px;
          height: 22px;
          cursor: nwse-resize;
          touch-action: none;
          /* 右下角斜纹拖拽角——放大到 22px + 斜纹填充，比原 16px 纯色三角明显
             得多（用户反馈「太小了不知道的人注意不到」）。斜纹方向沿 nwse 对角，
             是各类桌面应用 resize 角的通用记号。只用既有灰度变量，不新开色板。 */
          clip-path: polygon(100% 0%, 100% 100%, 0% 100%);
          background-image: repeating-linear-gradient(
            -45deg,
            var(--muted) 0 1.5px,
            transparent 1.5px 4.5px
          );
        }

        .kh-modal-resize-handle:hover {
          background-color: var(--soft);
          background-image: repeating-linear-gradient(
            -45deg,
            var(--ink) 0 1.5px,
            transparent 1.5px 4.5px
          );
        }

        /* header 里紧跟面包屑的小态标（「查看」/「编辑中」/「历史」）——用同一支
           .kh-mode-tag，靠 --preview / --editor / --history 修饰类换色调，让
           三态在 header 里都有明确文本标注（不只靠左侧色条）。 */
        .kh-mode-tag {
          display: inline-flex;
          align-items: center;
          gap: 4px;
          padding: 2px 8px;
          font-size: 11px;
          font-weight: 600;
          border-radius: 999px;
          border: 1px solid var(--line);
          background: var(--soft);
          color: var(--muted);
          white-space: nowrap;
        }
        .kh-mode-tag--editor {
          border-color: #f0dab3;
          background: #fdf4e6;
          color: #9a5b00;
        }
        /* knowhow 表版本管理 Task 16：历史态徽标——info 蓝，与
           .kh-modal-card--history / .knowhow-status-badge--info 同一套蓝色
           语义（见该规则注释）。 */
        .kh-mode-tag--history {
          border-color: #c7d6ff;
          background: #eef2ff;
          color: #1f5eff;
        }

        /* 合并格编辑提示（anchor 分组 spec §4.4）：紧跟 kh-mode-tag--editor
           出现在面包屑里，告知这一格是「合并共享格」——保存会批量写整组，
           不只是眼前这一行（handleCellSave 的批量写判定与这里的
           affectedBranchCount 同源，见 knowhow-panel.tsx handleCellSave）。
           沿用 kh-mode-tag--editor / kh-procedure-hint 同一套琥珀语义，与
           「编辑中」标签同色系但用一句完整提示文本而非图标短标签的形态，
           避免和状态标签混淆。 */
        .kh-affect-hint {
          display: inline-flex;
          align-items: center;
          padding: 2px 8px;
          font-size: 11px;
          font-weight: 600;
          border-radius: 999px;
          border: 1px solid #f0dab3;
          background: #fdf4e6;
          color: #9a5b00;
          white-space: nowrap;
        }

        /* 全屏切换按钮：复用 icon-button 尺寸/交互，只是有独立 title/图标；不
           需要单独样式，此注释只标记它在 header-actions 里的位置。 */

        .kh-modal-header {
          flex: 0 0 auto;
          padding: 16px 20px;
          border-bottom: 1px solid var(--line);
          display: flex;
          flex-direction: column;
          gap: 8px;
        }

        .kh-modal-header-top {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 12px;
        }

        .kh-modal-breadcrumb {
          display: flex;
          align-items: center;
          flex-wrap: wrap;
          gap: 8px;
          min-width: 0;
          font-size: 15px;
          font-weight: 600;
          color: var(--ink);
        }

        .kh-modal-row-title {
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
          max-width: 320px;
        }

        .kh-modal-sep {
          color: var(--muted);
          font-weight: 400;
        }

        .kh-modal-col-name {
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
        }

        .kh-modal-header-actions {
          display: flex;
          align-items: center;
          gap: 8px;
          flex: 0 0 auto;
        }

        .kh-row-context-toggle {
          align-self: flex-start;
          display: inline-flex;
          align-items: center;
          gap: 4px;
          border: 0;
          background: transparent;
          color: var(--muted);
          font-size: 12px;
          font-weight: 600;
          cursor: pointer;
          padding: 0;
        }

        .kh-row-context-toggle svg {
          transition: transform 0.15s ease;
        }

        .kh-row-context-toggle svg.kh-chevron-open {
          transform: rotate(180deg);
        }

        .kh-row-context-body {
          display: grid;
          gap: 6px;
          padding: 10px 12px;
          border: 1px solid var(--line);
          border-radius: 8px;
          background: var(--soft);
          font-size: 12px;
        }

        .kh-row-context-item {
          display: flex;
          align-items: baseline;
          gap: 8px;
          min-width: 0;
          padding: 4px 8px;
          border-radius: 6px;
          border: 1px solid transparent;
        }

        /* 可点条目（本行其他格子可切换，接了 onSwitchCell 才有 role="button"）
           ——cursor/hover 镜像 knowhow-matrix-drawer.tsx 可点格子的既有处理，
           hover 底色复用同一色值 #f4f7ff，两处视觉语言保持一致；当前格
           （--current，见下）不会同时带这个类，互不冲突。 */
        .kh-row-context-item--clickable {
          cursor: pointer;
        }

        .kh-row-context-item--clickable:hover {
          background: #f4f7ff;
        }

        /* 高亮当前浮层对应的那一行——用户打开浮层后能一眼看到「我在整行的
           哪个位置」，无需在浮层和网格之间来回对照。琥珀色与 editor 左边条
           /kh-procedure-hint 同一色系，保持整套编辑体验的一致强调色。 */
        .kh-row-context-item--current {
          background: #fff7e6;
          border-color: #f0dab3;
        }

        .kh-row-context-current-tag {
          flex: 0 0 auto;
          padding: 1px 6px;
          font-size: 10.5px;
          font-weight: 700;
          letter-spacing: 0.4px;
          border-radius: 999px;
          background: #d99a3b;
          color: #fff;
        }

        .kh-row-context-item strong {
          flex: 0 0 auto;
          color: var(--ink);
        }

        .kh-row-context-text {
          flex: 1 1 auto;
          min-width: 0;
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
          color: var(--muted);
        }

        .kh-row-context-empty {
          margin: 0;
          color: var(--muted);
        }

        .kh-modal-body {
          flex: 1 1 auto;
          min-height: 0;
          overflow-y: auto;
          padding: 16px 20px;
          display: flex;
          flex-direction: column;
          gap: 12px;
        }

        .kh-draft-banner {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 12px;
          padding: 8px 14px;
          border: 1px solid #c7d6ff;
          border-radius: 8px;
          background: #eef2ff;
          color: #1f5eff;
          font-size: 13px;
        }

        .kh-draft-banner-actions {
          display: flex;
          gap: 8px;
          flex: 0 0 auto;
        }

        .kh-draft-banner-actions button {
          border: 1px solid #c7d6ff;
          background: #fff;
          border-radius: 999px;
          padding: 4px 12px;
          font-size: 12px;
          font-weight: 600;
          color: #1f5eff;
          cursor: pointer;
        }

        .kh-procedure-hint {
          margin: 0;
          padding: 8px 12px;
          border-radius: 8px;
          background: #fdf4e6;
          border: 1px solid #f0dab3;
          color: #9a5b00;
          font-size: 12.5px;
        }

        .kh-toolbar {
          display: flex;
          align-items: center;
          gap: 8px;
          flex-wrap: wrap;
        }

        .kh-toolbar-button {
          display: inline-flex;
          align-items: center;
          gap: 6px;
          border: 1px solid var(--line);
          border-radius: 8px;
          background: #fff;
          padding: 5px 10px;
          font-size: 12.5px;
          color: var(--ink);
          cursor: pointer;
        }

        .kh-toolbar-button:hover {
          border-color: var(--blue);
          color: var(--blue);
        }

        .kh-toolbar-button:disabled {
          opacity: 0.6;
          cursor: not-allowed;
        }

        .kh-toolbar-status {
          display: inline-flex;
          align-items: center;
          gap: 6px;
          font-size: 12.5px;
          color: var(--muted);
        }

        .kh-hidden-file-input {
          display: none;
        }

        .kh-inline-error {
          color: #ba2d2d;
          font-size: 12.5px;
          margin: 0 auto 0 0;
        }

        .kh-split {
          flex: 1 1 auto;
          min-height: 260px;
          display: grid;
          gap: 14px;
        }

        /* 三态视图：编辑/预览单栏铺满，并列双栏。 */
        .kh-split--edit,
        .kh-split--preview {
          grid-template-columns: 1fr;
        }
        .kh-split--split {
          grid-template-columns: 1fr 1fr;
        }

        /* 工具栏右侧三选一视图切换（编辑/并列/预览）——分段控件，选中态浮起白底。
           margin-left:auto 把它推到录入按钮的另一端；预览态录入按钮隐藏时它仍靠右。 */
        .kh-view-switch {
          margin-left: auto;
          display: inline-flex;
          align-items: center;
          gap: 2px;
          padding: 2px;
          border: 1px solid var(--line);
          border-radius: 8px;
          background: var(--soft);
        }
        .kh-view-switch-button {
          display: inline-flex;
          align-items: center;
          gap: 5px;
          border: 0;
          border-radius: 6px;
          background: transparent;
          padding: 4px 10px;
          font-size: 12.5px;
          color: var(--muted);
          cursor: pointer;
        }
        .kh-view-switch-button:hover {
          color: var(--ink);
        }
        .kh-view-switch-button--active {
          background: #fff;
          color: var(--ink);
          box-shadow: 0 1px 2px rgba(24, 39, 75, 0.12);
        }

        .kh-editor-pane {
          display: flex;
          min-width: 0;
          border: 1px solid var(--line);
          border-radius: 8px;
          transition: border-color 0.15s ease, background 0.15s ease;
        }

        .kh-editor-pane--drag {
          border-color: var(--blue);
          background: #eef2ff;
        }

        .kh-textarea {
          flex: 1 1 auto;
          width: 100%;
          min-height: 220px;
          border: 0;
          border-radius: 8px;
          padding: 10px 12px;
          font: 13px/1.6 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
          color: var(--ink);
          background: transparent;
          resize: none;
          box-sizing: border-box;
        }

        .kh-textarea:focus {
          outline: none;
        }

        .kh-textarea:disabled {
          opacity: 0.6;
        }

        .kh-preview-pane {
          min-width: 0;
          overflow-y: auto;
          border: 1px solid var(--line);
          border-radius: 8px;
          padding: 10px 12px;
        }

        .kh-preview-body {
          min-height: 120px;
        }

        .kh-preview-edit-button {
          display: inline-flex;
          align-items: center;
          gap: 6px;
          border: 1px solid var(--line);
          border-radius: 999px;
          padding: 5px 12px;
          font-size: 12.5px;
          font-weight: 600;
          color: var(--blue);
          background: #fff;
          cursor: pointer;
        }

        .kh-preview-edit-button:hover {
          border-color: var(--blue);
          background: #eef2ff;
        }

        /* Task 9：「优化表达」工具栏按钮——与列表/代码/图片同款按钮外壳，配一个
           淡紫色调把它跟纯排版操作区分开(LLM 触发的动作，不是格式化)。 */
        .kh-toolbar-button--optimize {
          border-color: #d9c7ff;
          color: #6d3fd1;
        }

        .kh-toolbar-button--optimize:hover:not(:disabled) {
          border-color: #6d3fd1;
          background: #f5f0ff;
        }

        /* Task 8（knowhow-md-normalize）：「规整格式」工具栏按钮——刻意不用
           上面 --optimize 的紫色（那支紫色的既定含义是"LLM 触发的动作，不是
           格式化"，见上面注释），规整格式恰恰是"只改格式"，配一支绿色调
           （与 .kh-optimize-queue-status--accepted 同一个 --green），呼应
           "确定性规则重排、无需担心语义被悄悄改写"的直觉。 */
        .kh-toolbar-button--reformat {
          border-color: #a9dfc7;
          color: var(--green);
        }

        .kh-toolbar-button--reformat:hover:not(:disabled) {
          border-color: var(--green);
          background: #eaf7f0;
        }

        /* Task 9：单格「优化表达」原文/建议对照（规格③）与「优化整行」队列
           里当前格的对照复用同一套 .kh-optimize-* 样式。 */
        .kh-optimize-compare {
          flex: 1 1 auto;
          min-height: 220px;
          display: grid;
          grid-template-columns: 1fr 1fr;
          gap: 14px;
        }

        .kh-optimize-pane {
          display: flex;
          flex-direction: column;
          min-width: 0;
          border: 1px solid var(--line);
          border-radius: 8px;
          overflow: hidden;
        }

        .kh-optimize-pane h5 {
          margin: 0;
          padding: 8px 12px;
          font-size: 12px;
          font-weight: 700;
          color: var(--muted);
          background: var(--soft);
          border-bottom: 1px solid var(--line);
        }

        .kh-optimize-pane--suggestion h5 {
          color: #6d3fd1;
          background: #f5f0ff;
        }

        /* Task 8：「规整格式」候选面板标题——绿色调，呼应上面
           .kh-toolbar-button--reformat 的用色，与紫色的"优化建议"区分开。 */
        .kh-optimize-pane--reformat-suggestion h5 {
          color: var(--green);
          background: #eaf7f0;
        }

        .kh-optimize-pane-body {
          flex: 1 1 auto;
          min-height: 0;
          overflow-y: auto;
          padding: 10px 12px;
        }

        /* Task 8：changed=false 时的友好提示——替代"原文/建议完全相同"的空
           对照面板；source 小标签复用同一个 .kh-reformat-source-tag，与
           「规整建议」面板标题旁的用法保持视觉一致。 */
        .kh-reformat-unchanged {
          flex: 1 1 auto;
          min-height: 220px;
          display: flex;
          flex-direction: column;
          align-items: center;
          justify-content: center;
          gap: 8px;
          border: 1px dashed var(--line);
          border-radius: 8px;
          padding: 24px;
          text-align: center;
        }

        .kh-reformat-unchanged p {
          margin: 0;
          font-size: 14px;
          font-weight: 600;
          color: var(--ink);
        }

        .kh-reformat-source-tag {
          display: inline-flex;
          align-items: center;
          padding: 2px 8px;
          border-radius: 999px;
          background: var(--soft);
          color: var(--muted);
          font-size: 11.5px;
          font-weight: 600;
        }

        .kh-optimize-queue-list {
          margin: 0;
          padding: 0;
          list-style: none;
          display: flex;
          flex-direction: column;
          gap: 4px;
        }

        .kh-optimize-queue-item {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 10px;
          padding: 6px 10px;
          border-radius: 8px;
          font-size: 12.5px;
          color: var(--muted);
        }

        .kh-optimize-queue-item--current {
          background: var(--soft);
          color: var(--ink);
          font-weight: 600;
        }

        .kh-optimize-queue-col {
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
          min-width: 0;
        }

        .kh-optimize-queue-status {
          flex: 0 0 auto;
          font-size: 11.5px;
          font-weight: 600;
        }

        .kh-reformat-queue-actions,
        .kh-reformat-item-toolbar {
          display: flex;
          align-items: center;
          gap: 8px;
        }

        .kh-reformat-link-button {
          border: 0;
          padding: 2px 0;
          color: var(--blue);
          background: transparent;
          font-size: 11.5px;
          font-weight: 600;
          cursor: pointer;
        }

        .kh-reformat-link-button:hover {
          text-decoration: underline;
        }

        .kh-reformat-item-detail {
          min-height: 0;
          display: flex;
          flex: 1 1 auto;
          flex-direction: column;
          gap: 12px;
        }

        .kh-reformat-item-toolbar .kh-view-switch {
          margin-left: auto;
        }

        .kh-md-diff {
          min-height: 220px;
          overflow: auto;
          border: 1px solid var(--line);
          border-radius: 8px;
          background: #fff;
        }

        .kh-md-diff-notice {
          margin: 0;
          padding: 8px 12px;
          border-bottom: 1px solid #f0dab3;
          background: #fdf4e6;
          color: #865000;
          font-size: 12px;
        }

        .kh-md-diff-lines {
          min-width: max-content;
          padding: 6px 0;
          font: 12.5px/1.6 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
        }

        .kh-md-diff-line {
          display: grid;
          grid-template-columns: 28px minmax(0, 1fr);
          min-height: 20px;
          white-space: pre-wrap;
          overflow-wrap: anywhere;
        }

        .kh-md-diff-line--delete {
          background: #fff0f0;
          color: #842323;
        }

        .kh-md-diff-line--add {
          background: #eaf7f0;
          color: #176340;
        }

        .kh-md-diff-marker {
          user-select: none;
          padding: 0 8px;
          color: var(--muted);
          text-align: right;
        }

        .kh-md-diff-line code {
          padding-right: 12px;
          font: inherit;
        }

        .kh-md-diff-token--changed {
          border-radius: 2px;
          background: rgba(194, 42, 42, 0.2);
          font-weight: 700;
        }

        .kh-md-diff-line--add .kh-md-diff-token--changed {
          background: rgba(23, 122, 85, 0.22);
        }

        .kh-md-diff-token--whitespace {
          text-decoration: underline dotted currentColor;
          text-underline-offset: 3px;
        }

        .kh-md-diff-token--tab::before {
          content: "⇥";
          user-select: none;
          opacity: 0.65;
        }

        .kh-optimize-queue-status--ready,
        .kh-optimize-queue-status--in_progress {
          color: #1f5eff;
        }

        .kh-optimize-queue-status--accepted {
          color: #177a55;
        }

        .kh-optimize-queue-status--error {
          color: #ba2d2d;
        }

        .kh-optimize-queue-status--skipped,
        .kh-optimize-queue-status--waiting {
          color: var(--muted);
        }

        /* 行级智能补全：逐列审阅卡片。置信度只表达建议可靠程度，不以颜色
           暗示会自动执行；所有写入仍需用户逐项点击接受。 */
        .kh-completion-card {
          width: min(820px, 100%);
        }

        .kh-completion-body {
          display: grid;
          gap: 14px;
        }

        .kh-completion-item {
          border: 1px solid var(--line);
          border-radius: 12px;
          padding: 14px;
          background: #fff;
        }

        .kh-completion-retrieval {
          display: grid;
          gap: 9px;
          padding: 12px;
          border: 1px solid var(--line);
          border-radius: 12px;
          background: color-mix(in srgb, var(--soft) 72%, #fff);
        }

        .kh-completion-retrieval-meta,
        .kh-completion-evidence-head,
        .kh-completion-evidence-meta {
          display: flex;
          align-items: center;
          gap: 7px;
          flex-wrap: wrap;
          min-width: 0;
        }

        .kh-completion-retrieval-meta span,
        .kh-completion-evidence-head span,
        .kh-completion-evidence-meta span {
          border: 1px solid var(--line);
          border-radius: 999px;
          padding: 2px 8px;
          color: var(--muted);
          background: #fff;
          font-size: 11px;
          overflow-wrap: anywhere;
        }

        .kh-completion-item-head,
        .kh-completion-references,
        .kh-completion-item-actions,
        .kh-completion-load-error {
          display: flex;
          align-items: center;
          gap: 8px;
          flex-wrap: wrap;
        }

        .kh-completion-item-head h4 {
          margin: 0;
          font-size: 14px;
        }

        .kh-completion-confidence,
        .kh-completion-reference {
          border-radius: 999px;
          padding: 2px 8px;
          font-size: 11px;
          border: 1px solid var(--line);
          background: var(--soft);
          color: var(--muted);
        }

        .kh-completion-confidence--high {
          color: var(--green);
          border-color: color-mix(in srgb, var(--green) 35%, var(--line));
        }

        .kh-completion-confidence--medium {
          color: #8a5a14;
          border-color: #e7c987;
        }

        .kh-completion-confidence--low {
          color: var(--muted);
        }

        .kh-completion-suggestion,
        .kh-completion-abstain {
          margin-top: 12px;
          padding: 12px;
          border-radius: 9px;
          background: var(--soft);
        }

        .kh-completion-abstain {
          color: var(--muted);
          margin-bottom: 0;
        }

        .kh-completion-basis {
          margin: 10px 0 0;
          color: var(--muted);
          font-size: 12px;
        }

        .kh-completion-references {
          margin-top: 10px;
          font-size: 12px;
          color: var(--muted);
        }

        .kh-completion-evidence {
          display: grid;
          gap: 8px;
          margin-top: 12px;
        }

        .kh-completion-evidence h5 {
          margin: 0;
          color: var(--text);
          font-size: 12px;
        }

        .kh-completion-evidence-card {
          min-width: 0;
          padding: 10px;
          border: 1px solid var(--line);
          border-radius: 9px;
          background: color-mix(in srgb, var(--soft) 55%, #fff);
        }

        .kh-completion-evidence-head {
          justify-content: space-between;
        }

        .kh-completion-evidence-head strong,
        .kh-completion-evidence-excerpt,
        .kh-completion-no-evidence {
          min-width: 0;
          overflow-wrap: anywhere;
          word-break: break-word;
        }

        .kh-completion-evidence-meta {
          margin-top: 7px;
        }

        .kh-completion-evidence-excerpt {
          margin-top: 8px;
          color: var(--text);
          font-size: 12px;
        }

        .kh-completion-evidence-excerpt > :first-child {
          margin-top: 0;
        }

        .kh-completion-evidence-excerpt > :last-child {
          margin-bottom: 0;
        }

        .kh-completion-no-evidence {
          margin: 0;
          color: var(--muted);
          font-size: 12px;
        }

        .kh-completion-item-actions {
          justify-content: flex-end;
          margin-top: 12px;
        }

        /* knowhow-md-normalize Task 9：「一键规整」批量弹窗的各态复用同一套
           .kh-optimize-queue-status 外壳，只是状态词表不同（见
           knowhow-optimize-logic.ts ReformatBatchCellStatus），配色沿用上面
           同一套语义——蓝=正在进行中/等待确认保存，绿=已成功落地，红=失败，
           灰=中性(待处理/无需改动)，琥珀=已中止/已跳过(需注意但非失败)。 */
        .kh-optimize-queue-status--pending,
        .kh-optimize-queue-status--unchanged {
          color: var(--muted);
        }

        .kh-optimize-queue-status--running,
        .kh-optimize-queue-status--changed,
        .kh-optimize-queue-status--saving {
          color: #1f5eff;
        }

        .kh-optimize-queue-status--saved {
          color: #177a55;
        }

        .kh-optimize-queue-status--reformat_error,
        .kh-optimize-queue-status--save_error {
          color: #ba2d2d;
        }

        /* 「已中止」——用户主动停下的在飞格子。刻意用琥珀而非红色：它不是失败，
           但也不是中性的"待处理/无需改动"，值得与它们区分开、让用户一眼看出
           哪些格子被中止截断了（P2-g）。「已跳过（内容已变）」（F2）同理用琥珀
           ——保存前发现被他人改动而主动放弃这次写入，不是保存失败（红），也不是
           中性状态，需要用户注意（重新运行规整）。 */
        .kh-optimize-queue-status--aborted,
        .kh-optimize-queue-status--stale_skipped {
          color: #9a6a00;
        }

        .kh-modal-footer {
          flex: 0 0 auto;
          padding: 14px 20px;
          border-top: 1px solid var(--line);
          display: flex;
          align-items: center;
          justify-content: flex-end;
          gap: 12px;
        }

        .kh-footer-actions {
          display: flex;
          align-items: center;
          gap: 10px;
        }

        .kh-footer-actions button {
          display: inline-flex;
          align-items: center;
          gap: 6px;
          border: 1px solid var(--line);
          border-radius: 8px;
          padding: 8px 14px;
          font-size: 13px;
          font-weight: 600;
          background: #fff;
          color: var(--ink);
          cursor: pointer;
        }

        .kh-footer-actions button:disabled {
          opacity: 0.6;
          cursor: not-allowed;
        }

        /* 复合选择器而非 !important：.kh-footer-actions button 的 (0,1,1)
           特异度本会盖过裸的 .kh-primary-button (0,1,0)；两个类叠加成
           (0,2,0) 天然胜出，不需要 !important 这把大锤（也不会连累其它
           容器里若出现同名类时的可覆盖性）。本类目前只在 .kh-footer-actions
           内使用（KnowhowCellEditor 的「保存并下一格」/优化对照的「接受」、
           KnowhowRowOptimizeModal 的「接受」）。 */
        .kh-footer-actions .kh-primary-button {
          background: var(--blue);
          border-color: var(--blue);
          color: #fff;
        }

        .kh-close-guard {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 12px;
          width: 100%;
          font-size: 13px;
          color: #ba2d2d;
        }

        .kh-close-guard-actions {
          display: flex;
          gap: 8px;
          flex: 0 0 auto;
        }

        .kh-close-guard-actions button {
          border: 1px solid var(--line);
          border-radius: 8px;
          padding: 6px 12px;
          font-size: 12.5px;
          font-weight: 600;
          background: #fff;
          cursor: pointer;
        }

        /* 同上，复合选择器而非 !important：本类目前只在
           .kh-close-guard-actions 内使用（「放弃并关闭」）。 */
        .kh-close-guard-actions .kh-danger-button {
          border-color: #f0c0c0;
          background: #fef2f2;
          color: #b91c1c;
        }

        /* -------------------------------------------------------------------
           Task 11（代码附件 UI，knowhow-code.tsx 的 KnowhowCodeChip /
           KnowhowCodeModal 共用）——登记在这里而非 knowhow-code.tsx 自己的
           <style jsx> 里，理由同上方 kh-modal-* 一段注释：这是本特性唯一
           保证任何时候都已挂载的样式容器。
           ------------------------------------------------------------------- */

        /* chip 本身是 <button>，这里只重置 UA 默认按钮观感（字体/指针），
           配色完全交给叠加的 .knowhow-status-badge--{tone} 类。 */
        .kh-code-chip {
          font: inherit;
          cursor: pointer;
        }

        /* "添加代码"安静入口（none 态 + canEdit）：虚线 + 弱化配色，与三态
           里 implemented/stale 的实心徽章形成"待完成 vs 已完成"的视觉区分，
           不与其它徽章抢注意力。 */
        .kh-code-chip--add {
          display: inline-flex;
          align-items: center;
          gap: 4px;
          border: 1px dashed var(--line);
          border-radius: 999px;
          padding: 2px 8px;
          font-size: 11px;
          color: var(--muted);
          background: transparent;
        }

        .kh-code-chip--add:hover {
          border-color: var(--blue);
          color: var(--blue);
        }

        .kh-code-explain {
          margin: 0;
          color: var(--muted);
          font-size: 13px;
        }

        .kh-code-lang-row {
          display: flex;
          align-items: center;
          gap: 10px;
          flex-wrap: wrap;
        }

        .kh-code-lang-tag {
          display: inline-flex;
          align-items: center;
          font-size: 11px;
          font-weight: 600;
          padding: 2px 8px;
          border-radius: 999px;
          border: 1px solid var(--line);
          background: var(--soft);
          color: var(--muted);
          white-space: nowrap;
        }

        .kh-code-updated {
          font-size: 12px;
          color: var(--muted);
        }

        .kh-code-lang-input {
          width: 100%;
          max-width: 320px;
          box-sizing: border-box;
          padding: 6px 10px;
          border: 1px solid var(--line);
          border-radius: 8px;
          font-size: 13px;
          color: var(--ink);
          background: #fff;
        }

        .kh-code-lang-input:disabled {
          opacity: 0.6;
        }

        /* 等宽代码块（规格⑥-4"点击浮层查看（等宽...）"）：只读展示，不经
           KnowhowMarkdown——代码是原样文本，不是 markdown。 */
        .kh-code-block {
          flex: 1 1 auto;
          min-height: 160px;
          max-height: 50vh;
          overflow: auto;
          margin: 0;
          padding: 12px 14px;
          border: 1px solid var(--line);
          border-radius: 8px;
          background: var(--soft);
          font: 13px/1.6 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
          color: var(--ink);
          white-space: pre-wrap;
          word-break: break-word;
        }

        .kh-code-textarea {
          flex: 1 1 auto;
          min-height: 240px;
        }

        /* 行详情抽屉：本行代码状态加载失败的轻量内联提示（不阻塞整行其余内容
           渲染——只影响代码 chip 这一小块辅助信息）。 */
        .kh-code-status-error {
          display: flex;
          align-items: center;
          gap: 8px;
          margin: 0 0 4px;
          font-size: 12.5px;
          color: var(--muted);
        }

        .kh-code-status-error button {
          border: 0;
          background: transparent;
          color: var(--blue);
          font-size: 12.5px;
          font-weight: 600;
          text-decoration: underline;
          cursor: pointer;
          padding: 0;
        }

        /* -------------------------------------------------------------------
           Task 7（C 概念矩阵抽屉，knowhow-matrix-drawer.tsx 的
           KnowhowMatrixDrawer 专用）——登记在这里而非该组件自己的
           <style jsx>，理由同上方 kh-modal-* / kh-code-* 两段注释：这是本
           特性唯一保证任何时候都已挂载的样式容器。
           矩阵语义（spec §4.3）：属性为行、分支为列，是主网格 G2（属性为列、
           概念为行，见上方 .knowhow-cell-merged 一段）的转置视图。
           ------------------------------------------------------------------- */

        /* 矩阵比普通格子浮窗（880px）宽——属性名列 + 多个分支列并排，880px
           在 3 个以上分支时会太挤。放在 .kh-modal-card 之后、mobile 断点
           媒体查询之前，窄屏时仍会被下方 @media (max-width: 720px) 里的
           .kh-modal-card 规则（源码序在后，同优先级取后者）压回 100vw。 */
        .kh-matrix-card {
          width: min(1040px, 96vw);
        }

        .kh-matrix {
          width: 100%;
          border-collapse: collapse;
          font-size: 13px;
        }

        .kh-matrix th,
        .kh-matrix td {
          border: 1px solid var(--line);
          padding: 8px 12px;
          text-align: left;
          vertical-align: top;
        }

        /* 可点格子（knowhow-matrix-drawer.tsx clickableCellProps 挂了
           role="button" 的共享格/分支格，isClickable 为真才有）：cursor
           提示两种格子共用；hover 底色只覆盖分支格，用 :not(.kh-matrix-
           shared) 与下方共享格的琥珀 hover 互斥，两条规则各管一半格子、
           不必比拼选择器优先级。不可点的格子没有 role="button"，不受影响。
           分支格 hover 呼应 G2 网格 .knowhow-grid-table tbody tr:hover td
           的浅蓝底语义，这里限定单格而非整行——矩阵同一行内每个分支格是
           各自独立的点击目标。 */
        .kh-matrix [role="button"] {
          cursor: pointer;
        }

        .kh-matrix td[role="button"]:not(.kh-matrix-shared):hover {
          background: #f4f7ff;
        }

        .kh-matrix thead th {
          background: var(--soft);
          color: var(--muted);
          font-weight: 600;
          white-space: nowrap;
          min-width: 140px;
        }

        /* Follow-up B（删分支，spec §4.4）：表头「分支 N」文本 + 删除图标
           一行两端对齐——图标常驻宽度不大，justify-content: space-between
           让它贴右边，不挤在文本正后面。二次确认态（.knowhow-confirm 整体
           替换本 span）不受这条规则影响，两者互斥渲染。 */
        .kh-matrix-branch-head {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 8px;
        }

        /* 表头删除图标：比既有 .icon-button（42px 圆形，为 header 顶栏设计）
           小得多的定位样式——塞进表头格会显得极不协调，这里单开一个紧凑的
           无边框图标按钮，hover 时变红提示危险操作，同 .knowhow-confirm 的
           #fef2f2 底色/var(--red)，不新开色值。 */
        .kh-matrix-branch-delete {
          display: inline-flex;
          align-items: center;
          justify-content: center;
          flex: 0 0 auto;
          border: none;
          border-radius: 4px;
          padding: 2px;
          background: transparent;
          color: var(--muted);
          cursor: pointer;
        }

        .kh-matrix-branch-delete:hover {
          color: var(--red);
          background: #fef2f2;
        }

        .kh-matrix-branch-complete {
          display: inline-flex;
          align-items: center;
          gap: 3px;
          border: 1px solid var(--line);
          border-radius: 999px;
          padding: 2px 6px;
          color: var(--blue);
          background: #fff;
          font-size: 11px;
          cursor: pointer;
          white-space: nowrap;
        }

        .kh-matrix-branch-complete:hover {
          border-color: var(--blue);
          background: #eef2ff;
        }

        /* 属性名列（行头）：灰底与表头呼应，纵向居中避免长文本内容把它挤到
           顶部——同一令牌 var(--soft)，与 G2 的 .knowhow-cell-merged / 表头
           视觉同源，强化"这一列是结构性标签，不是内容"的区分。 */
        .kh-matrix-rowhead {
          background: var(--soft);
          color: var(--ink);
          font-weight: 600;
          white-space: nowrap;
          vertical-align: middle;
        }

        /* 全分支同值的共享属性格（MatrixAttrRow.sharedSpan）：琥珀底，与本
           文件既有的 procedure 徽章 / kh-modal-card--editor 左边条 /
           status-badge--warning 同一套琥珀语义（"这一格代表多个分支共享的
           同一个值"，不是不同分支各自的内容）。 */
        .kh-matrix-shared {
          background: #fdf4e6;
          vertical-align: middle;
        }

        /* 共享格 hover：可点时保持琥珀色系而非上面分支格的浅蓝——复用既有
           琥珀边框色 #f0dab3（同 .knowhow-status-badge--warning 一套）当加
           深一档的 hover 底色，不新开色值，选择器与上方分支格 hover 互斥
           不会互相盖掉。 */
        .kh-matrix-shared[role="button"]:hover {
          background: #f0dab3;
        }

        /* 命中分支高亮（spec §4.5，ask 引用跳转 Task 11 接线）：表头文字变蓝
           + 内嵌蓝框标记整列/该格。用 inset box-shadow 而非 border——
           border-collapse: collapse 会让相邻格子的边框互相吃掉，box-shadow
           不参与折叠、能在合并边框表格里稳定画出完整的一圈高亮。 */
        .kh-matrix-branch--hi {
          color: var(--blue);
          box-shadow: inset 0 0 0 2px var(--blue);
        }

        .kh-matrix-cell--hi {
          box-shadow: inset 0 0 0 2px var(--blue);
        }

        /* -------------------------------------------------------------------
           Task 15（历史抽屉，knowhow-history-drawer.tsx 的
           KnowhowHistoryDrawer 专用）——登记在这里而非该组件自己的
           <style jsx>，理由同上方 kh-modal-*/kh-matrix-* 两段注释：这是本
           特性唯一保证任何时候都已挂载的样式容器。
           ------------------------------------------------------------------- */

        /* 比普通格子浮窗（880px）宽一些——两版对比/单条 diff 的"此前/此后"
           并排两栏在窄卡片里会太挤；比矩阵抽屉（1040px）窄一点，折中。 */
        .kh-history-card {
          width: min(960px, 96vw);
        }

        .kh-history-toolbar {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 12px;
          flex-wrap: wrap;
          margin-bottom: 14px;
        }

        /* 复用既有 .kh-view-switch（knowhow-cell-editor.tsx 编辑/并列/预览
           三态切换同一套控件，见该组件工具栏）——但那里的 margin-left: auto
           是为了在"录入按钮 + 切换"这种单侧布局里把自己推到最右；这里切换
           是本工具栏最左侧的第一个元素，"只看里程碑"复选框在最右侧，需要
           justify-content: space-between 撑开中间，故清空这个继承来的
           margin-left——只在本工具栏这个特定祖先选择器下覆盖，不影响格子
           编辑器那处既有用法（选择器特异度靠祖先类而非 !important）。 */
        .kh-history-toolbar .kh-view-switch {
          margin-left: 0;
        }

        .kh-history-filter-toggle {
          display: inline-flex;
          align-items: center;
          gap: 6px;
          font-size: 12.5px;
          color: var(--muted);
          cursor: pointer;
          white-space: nowrap;
        }

        .kh-history-days {
          display: flex;
          flex-direction: column;
          gap: 18px;
        }

        .kh-history-day {
          font-size: 12.5px;
          font-weight: 600;
          color: var(--muted);
          margin-bottom: 8px;
        }

        .kh-history-list {
          list-style: none;
          margin: 0;
          padding: 0;
          display: flex;
          flex-direction: column;
          gap: 8px;
        }

        .kh-history-item {
          border: 1px solid var(--line);
          border-radius: 10px;
          padding: 10px 12px;
        }

        .kh-history-item-summary {
          display: flex;
          align-items: center;
          gap: 8px;
          flex-wrap: wrap;
        }

        .kh-history-item-toggle {
          flex: 1 1 auto;
          min-width: 0;
          display: flex;
          align-items: center;
          gap: 8px;
          border: 0;
          background: transparent;
          padding: 0;
          font: inherit;
          color: inherit;
          text-align: left;
          cursor: pointer;
        }

        .kh-history-item-time {
          color: var(--muted);
          font-variant-numeric: tabular-nums;
          white-space: nowrap;
        }

        .kh-history-item-actions {
          display: flex;
          align-items: center;
          gap: 8px;
          margin-top: 8px;
          flex-wrap: wrap;
        }

        /* 「设为里程碑」/「回到这里」两个按钮复用既有 .knowhow-drawer-edit-
           button（行详情抽屉分节「编辑」按钮同一支 pill 样式）——但那里的
           margin-left: auto 是为了把自己推到分节标题行的最右端；这里两个
           按钮就是本行唯一内容，希望紧跟在摘要文字下方左对齐，同上面
           .kh-view-switch 的覆盖手法，只在本容器下清空继承来的 margin。 */
        .kh-history-item-actions .knowhow-drawer-edit-button {
          margin-left: 0;
        }

        .kh-history-milestone-form {
          display: flex;
          align-items: center;
          gap: 8px;
          margin-top: 8px;
        }

        .kh-history-milestone-form input {
          flex: 1 1 auto;
          min-width: 0;
          max-width: 260px;
          padding: 6px 10px;
          border: 1px solid var(--line);
          border-radius: 8px;
          font-size: 13px;
          color: var(--ink);
        }

        .kh-history-milestone-form input:disabled {
          opacity: 0.6;
        }

        .kh-history-milestone-form button {
          border: 1px solid var(--line);
          border-radius: 8px;
          padding: 6px 12px;
          font-size: 12.5px;
          font-weight: 600;
          background: #fff;
          color: var(--ink);
          cursor: pointer;
          white-space: nowrap;
        }

        .kh-history-milestone-form button:disabled {
          opacity: 0.6;
          cursor: not-allowed;
        }

        .kh-history-detail {
          margin-top: 10px;
          padding-top: 10px;
          border-top: 1px solid var(--line);
        }

        .kh-history-diff-section + .kh-history-diff-section {
          margin-top: 12px;
        }

        .kh-history-diff-cell + .kh-history-diff-cell {
          margin-top: 12px;
        }

        .kh-history-diff-cell-head {
          font-size: 12.5px;
          font-weight: 600;
          color: var(--muted);
          margin-bottom: 6px;
        }

        .kh-history-diff-pair {
          display: grid;
          grid-template-columns: 1fr 1fr;
          gap: 10px;
        }

        .kh-history-diff-block {
          border-radius: 8px;
          padding: 8px 10px;
          font-size: 13px;
          min-width: 0;
        }

        .kh-history-diff-block--before {
          background: #fef2f2;
          border: 1px solid #f0c0c0;
        }

        .kh-history-diff-block--after {
          background: #f0faf5;
          border: 1px solid #b7e4cf;
        }

        .kh-history-diff-label {
          font-size: 11px;
          font-weight: 600;
          color: var(--muted);
          margin-bottom: 4px;
        }

        .kh-history-diff-empty {
          margin: 0;
          color: var(--muted);
          font-size: 12.5px;
        }

        .kh-history-row-card {
          border-radius: 8px;
          padding: 10px 12px;
          border-left: 3px solid transparent;
          background: var(--soft);
        }

        .kh-history-row-card + .kh-history-row-card {
          margin-top: 10px;
        }

        /* 新增/删除行卡片左边条——绿/红呼应 .kh-history-diff-block--after/
           --before 同一套色值，色彩语义在"单格 diff"与"整行增删"之间保持
           一致（新增=绿，删除=红）。 */
        .kh-history-row-card--added {
          border-left-color: #177a55;
        }

        .kh-history-row-card--removed {
          border-left-color: #ba2d2d;
        }

        .kh-history-row-card-cell {
          margin-top: 6px;
          font-size: 13px;
        }

        .kh-history-column-diff-list {
          list-style: none;
          margin: 0;
          padding: 0;
          display: flex;
          flex-direction: column;
          gap: 6px;
          font-size: 13px;
          color: var(--ink);
        }

        .kh-history-stale-milestones {
          margin-top: 16px;
          padding-top: 12px;
          border-top: 1px dashed var(--line);
        }

        .kh-history-empty {
          padding: 30px 0;
          text-align: center;
          color: var(--muted);
        }

        .kh-history-compare-bar {
          display: flex;
          align-items: flex-end;
          gap: 16px;
          flex-wrap: wrap;
          margin-bottom: 16px;
        }

        .kh-history-compare-bar label {
          display: flex;
          flex-direction: column;
          gap: 4px;
          font-size: 12px;
          color: var(--muted);
        }

        .kh-history-compare-bar select {
          min-width: 240px;
          max-width: 360px;
          padding: 6px 10px;
          border: 1px solid var(--line);
          border-radius: 8px;
          font-size: 13px;
          color: var(--ink);
          background: #fff;
        }

        .kh-history-compare-result > * + * {
          margin-top: 14px;
        }

        @media (max-width: 720px) {
          /* 窄屏走整屏浮窗：card 是 100vw，overlay 若还留着 24px padding，卡片就被
             推出视口 24px——右侧的关闭按钮、三态切换、「保存并下一格」会被裁掉
             （600×900 实测 left=24/width=600/right=624）。与全屏态
             (.kh-modal-overlay:has(> .kh-modal-card--fullscreen)) 同样的处理：
             整屏就把 overlay 的内边距归零，由卡片自己撑满。 */
          .kh-modal-overlay {
            padding: 0;
          }

          .kh-modal-card {
            /* 用 100%（overlay 是 position:fixed;inset:0，已排除滚动条）而非 100vw
               ——100vw 含经典滚动条宽度，窄化桌面窗口时同样会横向溢出。 */
            width: 100%;
            max-height: 100vh;
            border-radius: 0;
          }

          /* 窄屏浮窗几何已停用（见 use-floating-window 的 isFloatingDisabledWidth），
             手柄按下去不会有任何反应——留着只是给出错误的可交互暗示。 */
          .kh-modal-resize-handle {
            display: none;
          }

          .kh-split,
          .kh-split--split {
            grid-template-columns: 1fr;
          }

          .kh-optimize-compare {
            grid-template-columns: 1fr;
          }

          /* Task 15：窄屏下"此前/此后"并排两栏挤不下，同上面几处并列视图
             一样退回单栏纵向堆叠。 */
          .kh-history-diff-pair {
            grid-template-columns: 1fr;
          }
        }
      `}</style>
  );
}
