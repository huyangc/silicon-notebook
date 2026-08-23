/**
 * 面向消费方的 workspace UI registry：内建目录 + 构建期装载的仓库外插件。
 *
 * 拆成两个模块不是洁癖，是 node 泳道的硬约束——`./registry.ts` 被 `node --test`
 * 直接 import，而 `./registry.local.ts` 会 import 插件包的 `.tsx` 入口（Node 报
 * `Unknown file extension`）。所以合并只能发生在这里：只有浏览器、vitest 与
 * `next build` 这些处理得了 `.tsx` 的消费方才 import 本模块。
 *
 * `./registry.local.ts` 是 `frontend/scripts/sync-ui-plugins.mjs` 的生成物，不入库，
 * 由 `postinstall` 与五个 `pre*` 钩子保证它总在（零插件时是一个空数组存根）。
 *
 * 合并结果再过一次 `defineWorkspaceUiRegistry`：本地条目与内建条目走同一套元数据
 * 校验、同一条重复 id 拒绝、同一份稳定排序与冻结，仓库外的包拿不到任何豁免。
 *
 * 两条 spread 的先后**不承重**：`defineWorkspaceUiRegistry` 按 id 排序后冻结，换个
 * 顺序产出逐字相同的数组（重复 id 由它直接拒绝，不存在「谁覆盖谁」）。写死这个形状
 * 只是让合并点只有一种读法，也让守卫能钉住「恰好这两个来源、没有第三份、没有就地
 * 拼装的条目」。
 */
import type { WorkspaceUiContribution } from "./contracts.ts";
import { BUILTIN_WORKSPACE_UI_CONTRIBUTIONS, defineWorkspaceUiRegistry } from "./registry.ts";
import { LOCAL_WORKSPACE_UI_CONTRIBUTIONS } from "./registry.local.ts";


export const WORKSPACE_UI_CONTRIBUTIONS: readonly WorkspaceUiContribution[] =
  defineWorkspaceUiRegistry([
    ...BUILTIN_WORKSPACE_UI_CONTRIBUTIONS,
    ...LOCAL_WORKSPACE_UI_CONTRIBUTIONS,
  ]);
