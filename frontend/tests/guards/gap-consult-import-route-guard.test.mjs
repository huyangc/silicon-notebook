// 「把一个库外链接导入成本笔记本的一条来源」走的是**核心** URL 来源端点，
// 绝不能悄悄改道插件路由。
//
// 背景：`GapConsultHostPort` 是给插件的建议入口——插件只负责说"这个 URL 值得看"，
// 导入这个动作本身与插件无关，是一次普通的核心「添加来源」写入
// （`POST /notebooks/{id}/sources/url`，同粘贴链接框走的 `importUrlSources`）。
// `frontend/features/extension-sdk` 那份既有守卫（extension-plugin-package-guard /
// extension-ui-boundary）只扫描 `features/ext-*` 与 `features/agent-profile` 这两类
// 插件包，本组件与它的调用点都在 `frontend/app/` 下——不在那两份守卫的扫描面里，
// 一次把导入悄悄换成 `/api/extensions/*` 不会被它们抓到。这是按
// `docs/development.md` 通用变异验证规则在评审中发现并补上的缺口。
//
// 覆盖边界（如实说明，不声称全覆盖）：本守卫按 AST 认**调用名**与**字符串字面量**
// 两种形态——`fetch("/api/extensions/...")`、`someExtensionApi()` 这类都能抓到。
// 抓不到的：① 拼接出来的路径（`"/api/" + "extensions/" + id`，没有一段字面量整段
// 含 `/api/extensions`）；② 经一层间接函数转发（`importGapSuggestion` 调用一个自己
// 起的 helper，helper 内部再打插件路由——`callsIn` 只看直接调用名，不会跟进 helper
// 函数体）；③ 运行时反射/动态 import。这些形态的兜底是代码评审，不是这份测试。
import test from "node:test";
import assert from "node:assert/strict";
import ts from "typescript";

import {
  callsIn,
  findFunction,
  parseModule,
  stringLiterals,
} from "../../test-support/semantic-source.mjs";

test("page.tsx 的 importGapSuggestion 调用核心 importUrlSources，不触达插件路由", async () => {
  const page = await parseModule("page.tsx");
  const fn = findFunction(page, "importGapSuggestion");
  const calls = callsIn(fn);

  assert.ok(
    calls.includes("importUrlSources"),
    "importGapSuggestion 必须调用核心 source-api.ts::importUrlSources —— "
      + `实际调用集合：${JSON.stringify(calls)}`,
  );
  const extensionRouteCall = calls.find((call) => /extension/i.test(call));
  assert.equal(
    extensionRouteCall,
    undefined,
    `importGapSuggestion 不得调用任何插件路由相关函数，命中了：${extensionRouteCall}`,
  );

  // 调用名扫描只认得出「插件路由长得像一个函数调用」的形态——真打插件路由更常见
  // 的写法是裸 `fetch("/api/extensions/...")`，路径是字符串字面量而不是调用名。
  // 镜像组件半（下面第二条用例）已有的字符串字面量扫描，把它对准这个函数体。
  const literals = stringLiterals(fn);
  // 空转保护：先证明这份扫描面本身不是空的（importUrlSources 调用带了 URL 数组
  // 参数、notebookId 字段等，字面量列表理应非空）——否则下面「没命中」的断言测的
  // 是「什么都没扫到」而不是「扫到了、确认干净」，findFunction 解析失败时会静默
  // 全绿（同 long-task-button-guard 对「入口被改名/删除」的空转保护同一个判据）。
  assert.ok(
    literals.length > 0,
    "importGapSuggestion 函数体内没有扫到任何字符串字面量——函数可能被改名/清空，"
      + "导致下面的路由字符串检查是一次空转",
  );
  const offendingLiteral = literals.find((value) => value.includes("/api/extensions"));
  assert.equal(
    offendingLiteral,
    undefined,
    `importGapSuggestion 不得拼接任何插件路由字符串，命中了字面量：${offendingLiteral}`,
  );
});

// 组件侧的扫描面。**必须是一张显式清单而不是一个写死的文件名**：这条守卫最初只盯
// `answer-gap-suggestions.tsx`，而后来「点击 → 回调」的接线被抽进
// `import-row-state.tsx`（站外来源建议与外部证据引用卡共用同一份逐行状态机），
// 第二个消费方是 `answer-panel.tsx` —— 两个新文件都不在原扫描面里，把导入悄悄改道
// `/api/extensions/*` 不会被抓到（评审 P1）。
//
// `scanImports`：只有**纯展示/纯状态**的那两个文件才断言「不 import 任何 API 客户端」。
// `answer-panel.tsx` 是整块答案视图，本就合法地 import 了 `./api-config`（附图资产 URL
// 要 `API_BASE`），把它纳入 import 扫描等于给这条规则开一个必然要放行的例外；它只进
// 字面量那半——真打插件路由的写法是 `fetch("/api/extensions/…")`，字面量扫描抓得到。
const COMPONENT_SCAN = [
  {
    file: "answer-gap-suggestions.tsx",
    scanImports: true,
    why: "站外来源建议清单：只从外部拿 onImport 一个回调",
  },
  {
    file: "import-row-state.tsx",
    scanImports: true,
    why: "共享的逐行导入状态机：点击 → 回调的接线住在这里，它才是真正的改道落点",
  },
  {
    file: "answer-panel.tsx",
    scanImports: false,
    why: "持有 useImportRowController 的答案视图（合法 import api-config，只扫字面量）",
  },
  {
    // 「导入为来源」那颗按钮随引用小卡片一起抽进了 citation-card.tsx（全局问答与
    // 笔记本内问答共用同一张卡）。它才是现在的 ImportRowButton 调用点——不跟着搬，
    // 这条守卫的扫描面就又漏掉了真正的改道落点（与上面那条 P1 同一个坑）。
    file: "citation-card.tsx",
    scanImports: false,
    why: "外部证据引用卡的「导入为来源」调用点（合法 import api-config，只扫字面量）",
  },
];

function importSpecifiersOf(module) {
  const specifiers = [];
  function visit(node) {
    if (
      ts.isImportDeclaration(node)
      && node.moduleSpecifier
      && ts.isStringLiteral(node.moduleSpecifier)
    ) {
      specifiers.push(node.moduleSpecifier.text);
    }
    ts.forEachChild(node, visit);
  }
  visit(module);
  return specifiers;
}

for (const entry of COMPONENT_SCAN) {
  test(`${entry.file} 不含任何插件路由字符串${entry.scanImports ? "，也不 import 插件端口" : ""}`, async () => {
    const module = await parseModule(entry.file);

    const literals = stringLiterals(module);
    // 空转保护同上一条：这些文件本身就有不少字符串字面量（className、按钮文案、
    // aria 属性…），先确认扫描面非空，再断言其中没有一条命中插件路由。
    assert.ok(
      literals.length > 0,
      `${entry.file} 没有扫到任何字符串字面量——parseModule 可能解析失败，`
        + "导致下面的路由字符串检查是一次空转",
    );
    const offendingLiteral = literals.find((value) => value.includes("/api/extensions"));
    assert.equal(
      offendingLiteral,
      undefined,
      `${entry.file}（${entry.why}）不该拼接插件路由，命中了字符串字面量：${offendingLiteral}`,
    );

    if (!entry.scanImports) return;

    const importSpecifiers = importSpecifiersOf(module);
    // 空转保护同上：这些文件至少 import 了 react/lucide-react/workspace-model，
    // 先确认扫描面非空，再断言其中没有一条命中 API 客户端/插件端口。
    assert.ok(
      importSpecifiers.length > 0,
      `${entry.file} 没有扫到任何 import 声明——parseModule 可能解析失败，`
        + "导致下面的插件端口检查是一次空转",
    );

    // 这两个文件只应该从外部拿到 onImport 这一个回调，绝不该自己 import 任何 API
    // 客户端或插件端口——真正打网络请求的地方是调用方
    // （page.tsx::importGapSuggestion），不是纯展示组件/纯状态机自己。
    const apiImport = importSpecifiers.find((specifier) => /api-client|extension-sdk\/api/.test(specifier));
    assert.equal(
      apiImport,
      undefined,
      `${entry.file}（${entry.why}）不该自己 import API 客户端/插件端口，命中了：${apiImport}`,
    );
  });
}
