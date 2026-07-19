import test from "node:test";
import assert from "node:assert/strict";

import {
  assignmentsIn,
  callSitesIn,
  callsIn,
  findFunction,
  findFunctionIn,
  ifBranchesIn,
  ifConditionsIn,
  importsFrom,
  jsxElements,
  parseText,
  scopedCalls,
  variableInitializersIn,
} from "./semantic-source.mjs";


test("semantic TypeScript queries ignore line movement", () => {
  const first = parseText("function save() { api('/save'); }", "a.tsx");
  const moved = parseText(
    "\n\nfunction save() { api('/save'); }",
    "a.tsx",
  );

  assert.deepEqual(
    callsIn(findFunction(first, "save")),
    callsIn(findFunction(moved, "save")),
  );
});


test("semantic TypeScript queries retain qualified scope", () => {
  const first = parseText("function save() { api('/save'); }", "a.tsx");
  const moved = parseText("function other() { api('/save'); }", "a.tsx");

  assert.notDeepEqual(scopedCalls(first), scopedCalls(moved));
});


test("qualified function lookup distinguishes repeated nested handler names", () => {
  const source = parseText(
    [
      "function First() { function save() { first(); } }",
      "function Second() { function save() { second(); } }",
    ].join("\n"),
    "a.ts",
  );

  assert.deepEqual(callsIn(findFunctionIn(source, "First", "save")), ["first"]);
  assert.deepEqual(callsIn(findFunctionIn(source, "Second", "save")), ["second"]);
});


test("imports and JSX elements have position-independent identities", () => {
  const source = parseText(
    "import { Send as Icon } from 'lucide-react';\n"
      + "export function Composer() { return <button aria-label='发送'><Icon /></button>; }\n",
    "a.tsx",
  );

  assert.deepEqual(importsFrom(source, "lucide-react"), [
    { imported: "Send", local: "Icon" },
  ]);
  assert.deepEqual(jsxElements(source, "button"), [
    {
      scope: "<module>.Composer",
      attributes: { "aria-label": "发送" },
    },
  ]);
});


test("semantic wiring queries expose calls, guards, bindings, and initializers", () => {
  const source = parseText(
    [
      "function Editor({ busy }) {",
      "  const disabled = reason !== null || busy;",
      "  function save() {",
      "    if (disabled) return;",
      "    api.save(value, normalize(value));",
      "  }",
      "  return <button disabled={disabled} onClick={save}>Save</button>;",
      "}",
    ].join("\n"),
    "a.tsx",
  );

  assert.deepEqual(callSitesIn(findFunction(source, "save")), [
    { target: "api.save", arguments: ["value", "normalize(value)"] },
    { target: "normalize", arguments: ["value"] },
  ]);
  assert.deepEqual(ifConditionsIn(findFunction(source, "save")), ["disabled"]);
  assert.deepEqual(ifBranchesIn(findFunction(source, "save")), [
    {
      condition: "disabled",
      thenCalls: [],
      elseCalls: [],
      thenReturns: true,
      elseReturns: false,
    },
  ]);
  assert.deepEqual(variableInitializersIn(findFunction(source, "Editor")), [
    {
      name: "disabled",
      initializer: "reason !== null || busy",
    },
  ]);
  assert.deepEqual(assignmentsIn(findFunction(source, "Editor")), []);
  assert.deepEqual(jsxElements(source, "button"), [
    {
      scope: "<module>.Editor",
      attributes: {},
      bindings: {
        disabled: "disabled",
        onClick: "save",
      },
    },
  ]);
});


test("semantic assignment queries report target, operator, and value", () => {
  const source = parseText(
    "function refresh() { seq.current += 1; active = false; }",
    "a.ts",
  );

  assert.deepEqual(assignmentsIn(findFunction(source, "refresh")), [
    { target: "active", operator: "=", value: "false" },
    { target: "seq.current", operator: "+=", value: "1" },
  ]);
});


test("semantic wiring queries ignore source formatting and line movement", () => {
  const first = parseText(
    "function save(){if(busy)return;api.save(value,normalize(value))}",
    "a.ts",
  );
  const moved = parseText(
    "\n\nfunction save() {\n  if (busy) return;\n  api.save(value, normalize(value));\n}\n",
    "a.ts",
  );

  for (const query of [
    callSitesIn,
    assignmentsIn,
    ifBranchesIn,
    ifConditionsIn,
    variableInitializersIn,
  ]) {
    assert.deepEqual(
      query(findFunction(first, "save")),
      query(findFunction(moved, "save")),
    );
  }
});
