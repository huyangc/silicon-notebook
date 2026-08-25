import assert from "node:assert/strict";
import test from "node:test";

import ts from "typescript";

import {
  callSitesIn,
  parseModule,
} from "../../test-support/semantic-source.mjs";

const page = await parseModule("page.tsx");

function onClickHandlers() {
  const handlers = [];
  function visit(node) {
    if (
      ts.isJsxAttribute(node)
      && node.name.getText(page) === "onClick"
      && node.initializer
      && ts.isJsxExpression(node.initializer)
      && node.initializer.expression
    ) {
      handlers.push(callSitesIn(node.initializer.expression));
    }
    ts.forEachChild(node, visit);
  }
  visit(page);
  return handlers;
}

const handlers = onClickHandlers();

for (const command of [
  "retryIndexingPipelineRebuild",
  "revertIndexingPipelineToBuiltin",
]) {
  test(`${command} keeps the explicit full-rebuild confirmation`, () => {
    const matching = handlers.filter((calls) => calls.some(
      (call) => call.target === `notebookCollection.${command}`,
    ));
    assert.equal(
      matching.length,
      1,
      `${command} must have exactly one notebook-settings handler`,
    );
    assert.ok(
      matching[0].some(
        (call) => call.target === "window.confirm"
          && call.arguments.length === 1
          && call.arguments[0].startsWith("indexingPipelineConfirmMessage("),
      ),
      `${command} must confirm the full rebuild in the same handler`,
    );
  });
}
