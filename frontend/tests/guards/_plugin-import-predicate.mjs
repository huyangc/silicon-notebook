// 插件包边界的**唯一**判据实现。不是测试入口：文件名不以 `.test.mjs` 结尾，
// `test:node` 的 `find … -name '*.test.mjs'` 因此收不到它，`test-location-guard`
// 的入口扫描（同一条后缀判据）也收不到它。它仍在 `static-source-policy` 的扫描面
// 内——那条策略按「从测试入口可达、且从生产入口不可达」收集模块，本文件正是那种
// 形态，所以它照样受「不读生产源码、不做位置/顺序查询」的约束（本文件两条都不做）。
//
// **为什么单独一个文件**：这份判据有两个消费者——`extension-ui-boundary.test.mjs`
// 的逐插件白名单检查，与 `extension-plugin-package-guard.test.mjs` 的真值表、真包
// 扫描和内建插件那一档。此前它住在前者、由后者 import 过去，于是 node 泳道单跑
// 任一文件都会把**另一份**的全部用例一起加载并再跑一遍（两个文件互为对方的模块
// 依赖）。判据只能有一份拼写——写成两份，一份改了另一份没改，两个守卫就会在互相
// 认同一个陈旧值的同时与真实边界脱节——所以搬家而不是复制。
//
// 放 `test-support/` 反而不对：那里是跨泳道共享的 setup/adapter，这份判据只服务
// 这两个守卫。
import path from "node:path";
import ts from "typescript";

import { callsIn } from "../../test-support/semantic-source.mjs";


/** 仓库外插件包的落点：`features/ext-<包名>/`（同步脚本的 `GENERATED_PACKAGE_DIR`）。 */
const PLUGIN_PACKAGE_DIR = /^features\/ext-[a-z][a-z0-9-]*\//;

const SDK_CONTRACTS = "features/extension-sdk/contracts.ts";
const SDK_UI = "features/extension-sdk/ui.tsx";
const SDK_API = "features/extension-sdk/api.ts";
/** 插件只能用基座已经装好的这两个包（§1.4 白名单的裸说明符那一半）。 */
const BARE_IMPORT_ALLOWLIST = new Set(["lucide-react", "react"]);

// 四条拒绝理由各写一句「为什么」，因为读到失败的人多半是插件作者，他手上没有本仓库的
// 上下文。`api.ts` 被单独点名：它是唯一一个「看起来该给、实际绝不能给」的 SDK 模块。
export const API_PORT_REASON = "api 端口必须由 host 按 contribution.pluginId 注入到 actions.api"
  + "——插件自持 createWorkspaceExtensionApi 工厂，就等于可以构造别的插件的端口，路径限定当场失效";
export const BUILTIN_UI_REASON = "内建插件住在 registry.ts 的 node 泳道闭包里，而 node --test 装不下 .tsx"
  + "：共享弹窗外壳只发给仓库外的 ext-* 包";
export const OUTSIDE_REASON = "插件只能 import 同包兄弟模块、extension-sdk/contracts.ts 与 extension-sdk/ui.tsx";
export const BARE_REASON = "插件只能用基座已有的 react / lucide-react；新依赖走基座 PR"
  + "（插件包不得带 package.json 或 node_modules）";

/**
 * 计时器、两条常驻连接、老式 XHR 与信标发送：兄弟模块和入口一样，不得自己开一条
 * 后台通道。
 *
 * `sendBeacon` 两种拼写都收：它挂在 `navigator` 上，而 `const { sendBeacon } = navigator`
 * 之后的裸调用是同一件事。它值得单列——那是**页面卸载时仍会送达**的一次性外发，
 * 正好是「静默把数据带出去」最方便的形态。
 *
 * `fetch(` 刻意不在这里重复：`api-boundary.test.mjs` 已经对 `appSourceModules()` 的
 * 每个模块（含 `features/ext-<name>/` 的兄弟）普查过它，两处各写一遍只会让将来放宽
 * 其中一处时没人发现另一处还在拦。
 */
const SIDE_CHANNEL_CALLS = new Set([
  "setInterval",
  "setTimeout",
  "window.setInterval",
  "window.setTimeout",
  "globalThis.setInterval",
  "globalThis.setTimeout",
  "navigator.sendBeacon",
  "sendBeacon",
  // 动态 import：绕过静态说明符的白名单，`callsIn` 把它记成目标 `import`。
  "import",
]);
const SIDE_CHANNEL_CONSTRUCTORS = new Set(["EventSource", "WebSocket", "XMLHttpRequest"]);


/**
 * 「这条相对说明符指向同一个插件包内部吗」——插件 import 白名单的第四项。
 *
 * 一个包可以有兄弟模块（§1.5：入口文件之外的任意扁平 `.ts`/`.tsx`），入口 import
 * 它们是正常写法，白名单不认就等于要求每个包把全部代码塞进一个文件。
 *
 * **判据必须在 `path.posix.normalize` 之后做**，不能拿说明符做前缀字符串比较：
 * `./sub/../../app/page.tsx` 拼起来仍以包目录开头，归一化之后才看得出它落在
 * `app/` 里——前缀比较会让任何插件用一串 `..` 直接 import 壳层模块。
 *
 * 判的是「归一化后仍在本包目录之下」而不是「就在同一层」：包是扁平的（同步脚本
 * 拒绝子目录、T6 的 readdir 再查一遍），所以这条差别在真实包上不可达，但它让
 * 归一化成为**承重**的一步——同一层判据下，一个没归一化的实现照样会因为多出的
 * `/` 判否，那条变异就打空了。
 *
 * ⚠ 想变异验证这一条的人注意：**删掉外层那个 `path.posix.normalize` 调用是个空转变异**
 * ——`path.posix.join` 自己就归一化，外层只是把意图写明。真正承重的性质是「解析成路径
 * 再比」而不是「拿原始说明符做前缀字符串比较」，所以有效的变异是把这两行整个换成字符串
 * 拼接（`dirname + "/" + specifier`）。照着「去掉 normalize」做出来的绿色不是守卫失效
 * 的证据。
 */
export function samePluginPackageSpecifier(modulePath, specifier) {
  if (typeof specifier !== "string" || !specifier.startsWith(".")) return false;
  const owner = PLUGIN_PACKAGE_DIR.exec(modulePath)?.[0];
  if (owner === undefined) return false;
  const resolved = path.posix.normalize(
    path.posix.join(path.posix.dirname(modulePath), specifier),
  );
  return resolved.startsWith(owner) && resolved.length > owner.length;
}


/**
 * 「这条 import 说明符越过插件包边界了吗」——`ext-*` 包与内建插件共用的**唯一**判据。
 *
 * 允许集合恰好四项：
 *  · 同包兄弟模块——判据委托给 `samePluginPackageSpecifier`，它在 `path.posix.normalize`
 *    之后判「归一化后仍在本包目录之下」。**归一化是承重的**：`./sub/../../app/page.tsx`
 *    拼起来仍以包目录开头，前缀比较会整条放过它。
 *  · `features/extension-sdk/contracts.ts`——类型合同。
 *  · `features/extension-sdk/ui.tsx`——共享弹窗外壳，**只给仓库外的包**（`builtin` 选项）。
 *  · 裸 `react` / `lucide-react`。
 *
 * 其余一律违规，其中 `features/extension-sdk/api.ts` 带自己的理由：它是唯一一个插件作者
 * 会真心以为该导入的模块。
 *
 * @param {string} packagePath  模块在 `appSourceModules()` 里的路径（`features/…`）
 * @param {readonly string[]} specifiers  该模块的全部 import/export-from 说明符
 * @param {{ builtin?: boolean }} [options]  内建插件档：白名单少一项 `ui.tsx`
 * @returns {{ specifier: unknown, reason: string }[]}
 */
export function pluginPackageImportOffenders(packagePath, specifiers, options = {}) {
  const builtin = options.builtin === true;
  const offenders = [];
  for (const specifier of specifiers) {
    if (typeof specifier !== "string" || specifier.length === 0) {
      offenders.push({ specifier, reason: OUTSIDE_REASON });
      continue;
    }
    if (!specifier.startsWith(".")) {
      if (BARE_IMPORT_ALLOWLIST.has(specifier)) continue;
      offenders.push({ specifier, reason: BARE_REASON });
      continue;
    }
    if (samePluginPackageSpecifier(packagePath, specifier)) continue;
    const resolved = path.posix.normalize(
      path.posix.join(path.posix.dirname(packagePath), specifier),
    );
    if (resolved === SDK_CONTRACTS) continue;
    if (resolved === SDK_UI) {
      if (!builtin) continue;
      offenders.push({ specifier, reason: BUILTIN_UI_REASON });
      continue;
    }
    offenders.push({
      specifier,
      reason: resolved === SDK_API ? API_PORT_REASON : OUTSIDE_REASON,
    });
  }
  return offenders;
}


/**
 * 兄弟模块自开的后台通道。
 *
 * `extension-ui-boundary.test.mjs` 的禁用**文本**扫描现在只覆盖仓库自己的 SDK 模块，
 * 插件包（入口与兄弟）整个交给这条 AST 判据——文本正则连注释都数，插件包不是我们
 * 写的、注释里出现 `setTimeout(` 这种字样完全正常。语义判据认的是真调用，因此既不
 * 误报注释，也覆盖此前只扫入口时漏掉的兄弟模块（T2 评审点名的缺口）。
 */
export function pluginPackageSideChannelOffenders(parsed) {
  const offenders = callsIn(parsed).filter((target) => SIDE_CHANNEL_CALLS.has(target));
  function collect(item) {
    if (ts.isNewExpression(item) && ts.isIdentifier(item.expression)) {
      const constructed = item.expression.text;
      if (SIDE_CHANNEL_CONSTRUCTORS.has(constructed)) offenders.push(`new ${constructed}`);
    }
    ts.forEachChild(item, collect);
  }
  collect(parsed);
  return [...new Set(offenders)].sort();
}
