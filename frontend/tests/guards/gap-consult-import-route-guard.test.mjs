// 站外来源建议「导入」（ask.gap_consult，X9 PR-A T3）走的是**核心** URL 来源端点，
// 绝不能悄悄改道插件路由。
//
// 背景：`GapConsultHostPort` 是给插件的建议入口——插件只负责说"这个 URL 值得看"，
// 导入这个动作本身与插件无关，是一次普通的核心「添加来源」写入
// （`POST /notebooks/{id}/sources/url`，同粘贴链接框走的 `importUrlSources`）。
// `frontend/features/extension-sdk` 那份既有守卫（extension-plugin-package-guard /
// extension-ui-boundary）只扫描 `features/ext-*` 与 `features/agent-profile` 这两类
// 插件包，本组件与它的调用点都在 `frontend/app/` 下——不在那两份守卫的扫描面里，
// 一次把导入悄悄换成 `/api/extensions/*` 不会被它们抓到。这是 CLAUDE.md T3 变异
// 清单第 4 条钉的那个"不红则补一条"缺口。
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
  const calls = callsIn(findFunction(page, "importGapSuggestion"));

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
});

test("answer-gap-suggestions.tsx 不含任何插件路由字符串，也不 import 插件端口", async () => {
  const module = await parseModule("answer-gap-suggestions.tsx");

  const offendingLiteral = stringLiterals(module).find((value) => value.includes("/api/extensions"));
  assert.equal(
    offendingLiteral,
    undefined,
    `组件本身不该拼接插件路由，命中了字符串字面量：${offendingLiteral}`,
  );

  const importSpecifiers = [];
  function visit(node) {
    if (
      ts.isImportDeclaration(node)
      && node.moduleSpecifier
      && ts.isStringLiteral(node.moduleSpecifier)
    ) {
      importSpecifiers.push(node.moduleSpecifier.text);
    }
    ts.forEachChild(node, visit);
  }
  visit(module);

  // 组件只应该从外部拿到 onImport 这一个回调，绝不该自己 import 任何 API 客户端
  // 或插件端口——真正打网络请求的地方是调用方（page.tsx::importGapSuggestion），
  // 不是这个纯展示组件自己。
  const apiImport = importSpecifiers.find((specifier) => /api-client|extension-sdk\/api/.test(specifier));
  assert.equal(
    apiImport,
    undefined,
    `组件不该自己 import API 客户端/插件端口，命中了：${apiImport}`,
  );
});
