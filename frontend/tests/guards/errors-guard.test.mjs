import test from "node:test";
import assert from "node:assert/strict";

import ts from "typescript";

import {
  appSourceModules,
  callsIn,
  findFunction,
  importsIn,
  parseModule,
  stringLiterals,
} from "../../test-support/semantic-source.mjs";


const APPROVED_BARE_STATUS_ERRORS = Object.freeze({
  "pending-center.tsx|<module>.usePendingActions.connect|bare-status-error|Error": {
    count: 1,
    reason: "stream status is internal reconnect control flow",
  },
});
const APPROVED_MESSAGE_READS = Object.freeze({
  "admin/usage/page.tsx|<module>.AdminUsagePage|property|message": {
    count: 5,
    reason: "one access is a forbidden sentinel; view, role-notice, limit-notice and reset-notice states contain fixed or humanized copy",
  },
  "admin/usage/page.tsx|<module>.AdminUsagePage.submitRoleChange|property|message": {
    count: 1,
    reason: "the role-update catch reads only the forbidden control-flow sentinel",
  },
  "admin/usage/page.tsx|<module>.AdminUsagePage.saveDefault|property|message": {
    count: 1,
    reason: "the default-limit catch reads only the forbidden control-flow sentinel",
  },
  "admin/usage/page.tsx|<module>.AdminUsagePage.submitLimitChange|property|message": {
    count: 1,
    reason: "the per-user limit catch reads only the forbidden control-flow sentinel",
  },
  "admin/usage/page.tsx|<module>.AdminUsagePage.submitResetPassword|property|message": {
    count: 1,
    reason: "the password-reset catch reads only the forbidden control-flow sentinel",
  },
  "answer-gap-suggestions.tsx|<module>.GapSuggestionsPanel|property|message": {
    count: 1,
    reason: "renders the per-item ImportState.message the panel itself constructed: a fixed "
      + "Chinese fallback the panel writes on its own catch, or the ImportOutcome.message an "
      + "onImport caller resolved with — which is either a caught exception already run "
      + "through toUserMessage, or the backend's own rejected[0].reason string shown verbatim "
      + "(same posture as the existing URL-import rejected-list box at page.tsx's urlRejected "
      + "rendering) — never a raw caught exception read directly by this component",
  },
  "answer-gap-suggestions.tsx|<module>.GapSuggestionsPanel.handleImport|property|message": {
    count: 1,
    reason: "reads the typed ImportOutcome.message an onImport callback resolved with — "
      + "page.tsx's importGapSuggestion returns either a caught exception already run through "
      + "toUserMessage, or the backend's own rejected[0].reason string shown verbatim (same "
      + "posture as the existing URL-import rejected-list box at page.tsx's urlRejected "
      + "rendering), never a raw caught exception itself",
  },
  "dev/logs/activity/ActivityView.tsx|<module>.isForbidden|property|message": {
    count: 1,
    reason: "the activity view's shared predicate reads only the forbidden control-flow sentinel",
  },
  "knowhow-cell-editor.tsx|<module>.KnowhowCellEditor|property|message": {
    count: 2,
    reason: "optimizer and reformatter error states are written through the humanization boundary",
  },
  "knowhow-history-drawer.tsx|<module>.KnowhowHistoryDrawer.handleRevert|property|message": {
    count: 1,
    reason: "revert failure is narrowed to KnowhowRevertError before its server-validated guidance is copied out",
  },
  "knowhow-model.ts|<module>.parseKnowhowRevertFailure|property|message": {
    count: 3,
    reason: "revert failure payload is schema-checked (code+status match) before its guidance is returned",
  },
  "knowhow-model.ts|<module>.revertKnowhowTable|property|message": {
    count: 1,
    reason: "validated revert-failure guidance is copied into its typed error",
  },
  "knowhow-panel.tsx|<module>.KnowhowPanel|property|message": {
    count: 1,
    reason: "typed source-cleanup error carries server-validated 409 guidance",
  },
  "knowhow-transfer.ts|<module>.transferKnowhowTable|property|message": {
    count: 1,
    reason: "validated source-cleanup guidance is copied into its typed error",
  },
  "page.tsx|<module>.Home|property|message": {
    count: 1,
    reason: "application-owned information modal state is not exception text",
  },
  "transfer-model.ts|<module>.parseCleanupFailure|property|message": {
    count: 3,
    reason: "409 cleanup payload is schema-checked before its guidance is returned",
  },
});
const APPROVED_DIAGNOSTIC_READS = Object.freeze({
  "bundle-intake.ts|<module>.processMarkdownCandidate|diagnostic|error": {
    count: 3,
    reason: "InlineResult.error is md-bundle.ts's typed too-large payload ({bytes,limit,images}), "
      + "not a caught exception; .bytes/.limit feed inlineTooLargeMessage's fixed copy and "
      + ".images is a per-image {src,path,encodedBytes} list rendered through "
      + "inlineTooLargeImageLines' fixed copy — none of the three is diagnostic text",
  },
  "dev/logs/activity/ActivityDetail.tsx|<module>.AskDetailPane|diagnostic|error": {
    count: 1,
    reason: "owner-only developer activity detail intentionally renders the raw ask failure",
  },
  // 来源解析诊断原文**不再**经过活动视图：那串异常可能带服务端绝对路径，而管理员
  // 看的是别人的活动流（与 ScopedSourceDetail 同一条披露边界）。契约改成
  // `parse_failed` 布尔之后，SourceDetailPane 与 toActivitySource 两条 error_message
  // 读取一并消失，对应的豁免条目也随之删除——这份清单是精确计数的普查，留着会报红。
  "dev/logs/components/LogDetail.tsx|<module>.LogDetail|diagnostic|error": {
    count: 2,
    reason: "owner-only developer log detail intentionally renders raw records",
  },
  "dev/logs/components/StatsBar.tsx|<module>.StatsBar|diagnostic|error": {
    count: 1,
    reason: "status aggregate is a numeric count rather than diagnostic text",
  },
  "memory-panel.tsx|<module>.MemoryPanel.submitTransfer|diagnostic|error": {
    count: 1,
    reason: "per-item transfer diagnostics are selected only for bounded logging",
  },
  "page.tsx|<module>.Home.ingestZipFile|diagnostic|error": {
    count: 1,
    reason: "ZipBundleResult.error is md-bundle.ts's typed parse-failure payload "
      + "({code,path,actual,limit,method}), not a caught exception; it is only ever "
      + "passed through bundleErrorMessage's fixed Chinese copy, never shown raw",
  },
  "use-source-library.ts|<module>.useSourceLibrary.tick|diagnostic|error_message": {
    count: 2,
    reason: "background source failures pass through toUserMessage",
  },
  "use-kg-graph.ts|<module>.useKgGraph|diagnostic|error": {
    count: 2,
    reason: "KG review/build job diagnostics pass through toUserMessage or fixed terminal copy",
  },
  "page.tsx|<module>.Home|diagnostic|error_message": {
    count: 1,
    reason: "source detail uses the field only to select fixed user copy",
  },
  "system-api.ts|<module>.probeReady|diagnostic|error": {
    count: 3,
    reason: "startup diagnostics are captured in a snapshot and bounded logging",
  },
  "ask-api.ts|<module>.runAskStream.consumeLine|diagnostic|error": {
    count: 1,
    reason: "stream diagnostics are logged before a branded scenario error",
  },
  "use-ask-session.ts|<module>.useAskSession.tick|diagnostic|error": {
    count: 2,
    reason: "reconnected Ask job diagnostics pass through toUserMessage before fixed user copy",
  },
  "use-report-workspace.ts|<module>.useReportWorkspace|diagnostic|error": {
    count: 3,
    reason: "report failure detail is condition-only and bounded-log-only",
  },
});


function declaredScopeName(node, sourceFile) {
  if (
    ts.isFunctionDeclaration(node)
    || ts.isMethodDeclaration(node)
    || ts.isClassDeclaration(node)
  ) {
    return node.name?.getText(sourceFile);
  }
  if (
    ts.isVariableDeclaration(node)
    && node.initializer
    && (
      ts.isArrowFunction(node.initializer)
      || ts.isFunctionExpression(node.initializer)
    )
  ) {
    return node.name.getText(sourceFile);
  }
  return undefined;
}


function containsProperty(node, propertyName) {
  let found = false;
  function visit(child) {
    if (
      ts.isPropertyAccessExpression(child)
      && child.name.text === propertyName
    ) {
      found = true;
    }
    ts.forEachChild(child, visit);
  }
  visit(node);
  return found;
}


function callName(node) {
  if (ts.isIdentifier(node)) return node.text;
  if (ts.isPropertyAccessExpression(node)) return node.name.text;
  if (ts.isCallExpression(node)) return callName(node.expression);
  return "";
}


function semanticErrorSites(sourceFile, path) {
  const scopes = ["<module>"];
  const grouped = new Map();
  const unsafeCopy = [];

  function record(kind, target, node) {
    const key = `${path}|${scopes.join(".")}|${kind}|${target}`;
    const current = grouped.get(key) ?? {
      key,
      count: 0,
      diagnosticSnippets: [],
    };
    current.count += 1;
    current.diagnosticSnippets.push(
      node.getText(sourceFile).replace(/\s+/g, " "),
    );
    grouped.set(key, current);
  }

  function visit(node) {
    const scopeName = declaredScopeName(node, sourceFile);
    if (scopeName) scopes.push(scopeName);

    if (ts.isPropertyAccessExpression(node)) {
      if (node.name.text === "message" && path !== "errors.ts") {
        record("property", "message", node);
      }
      if (
        (node.name.text === "error" || node.name.text === "error_message")
        && path !== "errors.ts"
        && node.expression.getText(sourceFile) !== "console"
      ) {
        record("diagnostic", node.name.text, node);
      }
    }

    if (
      ts.isNewExpression(node)
      && callName(node.expression) === "Error"
      && node.arguments?.length
    ) {
      const argument = node.arguments[0];
      if (containsProperty(argument, "status")) {
        record("bare-status-error", "Error", node);
      }
      if (
        ts.isCallExpression(argument)
        && /^(?:toUserMessage|humanize|extractErrorMessage)$/.test(
          callName(argument.expression),
        )
      ) {
        unsafeCopy.push({
          path,
          scope: scopes.join("."),
          kind: "rewrapped-humanized-error",
          diagnosticSnippet: node.getText(sourceFile).replace(/\s+/g, " "),
        });
      }
      if (
        (
          ts.isStringLiteral(argument)
          || ts.isNoSubstitutionTemplateLiteral(argument)
        )
        && /[一-鿿]/.test(argument.text)
      ) {
        unsafeCopy.push({
          path,
          scope: scopes.join("."),
          kind: "unbranded-user-copy",
          diagnosticSnippet: node.getText(sourceFile).replace(/\s+/g, " "),
        });
      }
    }

    ts.forEachChild(node, visit);
    if (scopeName) scopes.pop();
  }

  visit(sourceFile);
  return {
    grouped: [...grouped.values()].sort(
      (left, right) => left.key.localeCompare(right.key),
    ),
    unsafeCopy,
  };
}


async function repositoryErrorSites() {
  const result = {
    bareStatus: [],
    message: [],
    diagnostic: [],
    unsafeCopy: [],
  };
  for (const { path, module } of await appSourceModules()) {
    const findings = semanticErrorSites(module, path);
    result.unsafeCopy.push(...findings.unsafeCopy);
    for (const finding of findings.grouped) {
      if (finding.key.includes("|bare-status-error|")) {
        result.bareStatus.push(finding);
      } else if (finding.key.includes("|diagnostic|")) {
        result.diagnostic.push(finding);
      } else {
        result.message.push(finding);
      }
    }
  }
  return result;
}


function comparable(findings) {
  return Object.fromEntries(findings.map(({ key, count }) => [key, count]));
}

function reviewedCounts(manifest) {
  return Object.fromEntries(
    Object.entries(manifest).map(([key, review]) => {
      assert.ok(review.reason, key);
      return [key, review.count];
    }),
  );
}


test("reviewed raw-error exceptions use semantic identity", async () => {
  const sites = await repositoryErrorSites();
  assert.deepEqual(
    comparable(sites.bareStatus),
    reviewedCounts(APPROVED_BARE_STATUS_ERRORS),
  );
  assert.deepEqual(
    comparable(sites.message),
    reviewedCounts(APPROVED_MESSAGE_READS),
  );
  assert.deepEqual(
    comparable(sites.diagnostic),
    reviewedCounts(APPROVED_DIAGNOSTIC_READS),
  );
});


test("user copy is not rewrapped or thrown without the humanized brand", async () => {
  const sites = await repositoryErrorSites();
  assert.deepEqual(sites.unsafeCopy, []);
});


test("critical catch boundaries call the shared humanization layer", async () => {
  const page = await parseModule("page.tsx");
  const ask = await parseModule("ask-api.ts");
  const answerPanel = await parseModule("answer-panel.tsx");
  const knowhow = await parseModule("knowhow-import-logic.ts");

  assert.ok(callsIn(findFunction(page, "reportError")).includes("toUserMessage"));
  assert.ok(callsIn(findFunction(ask, "runAskStream")).includes("humanizedError"));
  assert.ok(callsIn(findFunction(ask, "runAskStream")).includes("logDiagnostic"));
  assert.equal(
    callsIn(findFunction(page, "runSystemModelTest")).includes("logDiagnostic"),
    false,
  );
  assert.equal(
    callsIn(findFunction(answerPanel, "AnswerView")).includes("logDiagnostic"),
    false,
  );
  assert.deepEqual(
    callsIn(findFunction(knowhow, "extractErrorMessage")),
    ["toUserMessage"],
  );
});


test("migrated clients use the shared transport boundary", async () => {
  const modules = new Map(
    (await appSourceModules()).map((item) => [item.path, item.module]),
  );
  const clients = [
    "notebook-share.ts",
    "notebook-tier.ts",
    "promotion-queue.ts",
    "edge-review-queue.ts",
    "knowhow-model.ts",
    "knowhow-panel.tsx",
    "model-services.ts",
    "memory-panel.tsx",
    "transfer-picker.tsx",
    "pending-center.tsx",
    "knowhow-cell-editor.tsx",
    "admin/usage/api.ts",
    "admin/usage/notebooks.ts",
    "dev/logs/api.ts",
  ];

  for (const path of clients) {
    const module = modules.get(path);
    assert.ok(module, path);
    assert.ok(importsIn(module).some(({ module: imported }) => imported.includes("api-client")),
      `${path}: missing shared api client import`);
    const calls = callsIn(module);
    assert.ok(
      calls.includes("requestJson")
      || calls.includes("requestVoid")
      || calls.includes("requestBlob")
      || calls.includes("performApiRequest"),
      `${path}: missing shared transport call`,
    );
  }
});


test("model and report clients retain bounded diagnostics and scenario copy", async () => {
  const modelServices = await parseModule("model-services.ts");
  const report = await parseModule("use-report-workspace.ts");
  const guardedModelClients = [
    "fetchModelServiceStatus",
    "testSystemModelService",
    "testAllSystemModelServices",
  ];
  for (const client of guardedModelClients) {
    assert.deepEqual(
      callsIn(findFunction(modelServices, client))
        .filter((target) => target === "requestJson"),
      ["requestJson"],
      `${client}: every model request must use the shared bounded transport`,
    );
  }
  assert.equal(
    callsIn(modelServices)
      .filter((target) => target === "requestJson")
      .length,
    guardedModelClients.length,
    "model-services.ts has an unreviewed or missing HTTP error boundary",
  );
  assert.equal(callsIn(report).includes("console.error"), false);
  assert.equal(callsIn(report).includes("logDiagnostic"), true);
  assert.equal(callsIn(report).includes("toUserMessage"), true);
  assert.equal(
    stringLiterals(report).includes("报告操作没成功，请稍后重试"),
    true,
  );
  assert.equal(
    stringLiterals(report).includes("报告没能生成完，可以重试"),
    true,
  );
});
