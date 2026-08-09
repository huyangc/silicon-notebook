import test from "node:test";
import assert from "node:assert/strict";

import {
  assignmentsIn,
  callbackFlowsIn,
  callSitesIn,
  callsIn,
  controlFlowIn,
  findFunction,
  findFunctionIn,
  ifBranchesIn,
  ifConditionsIn,
  importsFrom,
  jsxElements,
  parseText,
  scopedCalls,
  variableInitializersIn,
} from "../../test-support/semantic-source.mjs";


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


test("control-flow queries preserve semantic nesting and statement order", () => {
  const source = parseText(
    [
      "async function upload(items) {",
      "  const gate = blocked();",
      "  if (gate) { fail(gate); return; }",
      "  try {",
      "    for (const item of items) {",
      "      const result = await send(item, controller.signal);",
      "      apply(result);",
      "    }",
      "  } finally {",
      "    active.current = false;",
      "    if (next) leave(next);",
      "  }",
      "}",
    ].join("\n"),
    "a.ts",
  );

  const flow = controlFlowIn(findFunction(source, "upload"));
  assert.deepEqual(flow.map(({ kind }) => kind), ["variables", "if", "try"]);
  assert.deepEqual(flow[0], {
    kind: "variables",
    declarations: [{ name: "gate", initializer: "blocked()" }],
    calls: [{ target: "blocked", arguments: [] }],
  });
  assert.deepEqual(flow[1], {
    kind: "if",
    condition: "gate",
    then: [
      {
        kind: "expression",
        calls: [{ target: "fail", arguments: ["gate"] }],
      },
      { kind: "return" },
    ],
    else: [],
  });
  const loop = flow[2].try[0];
  assert.equal(loop.kind, "for-of");
  assert.equal(loop.initializer, "const item");
  assert.equal(loop.expression, "items");
  assert.deepEqual(loop.body.map(({ kind }) => kind), [
    "variables",
    "expression",
  ]);
  assert.deepEqual(loop.body[0].calls, [
    {
      target: "send",
      arguments: ["item", "controller.signal"],
    },
  ]);
  assert.deepEqual(loop.body[0].awaitedCalls, loop.body[0].calls);
  assert.deepEqual(flow[2].finally, [
    {
      kind: "assignment",
      target: "active.current",
      operator: "=",
      value: "false",
      calls: [],
    },
    {
      kind: "if",
      condition: "next",
      then: [
        {
          kind: "expression",
          calls: [{ target: "leave", arguments: ["next"] }],
        },
      ],
      else: [],
    },
  ]);
});


test("callback-flow queries expose cleanup behavior without source offsets", () => {
  const source = parseText(
    [
      "function Editor() {",
      "  useEffect(() => {",
      "    return () => {",
      "      abort();",
      "      if (dirty) flush();",
      "    };",
      "  }, []);",
      "}",
    ].join("\n"),
    "a.ts",
  );

  assert.deepEqual(
    callbackFlowsIn(findFunction(source, "Editor"), "useEffect"),
    [
      {
        target: "useEffect",
        argumentIndex: 0,
        parameters: [],
        otherArguments: ["[]"],
        flow: [
          {
            kind: "return-callback",
            flow: [
              {
                kind: "expression",
                calls: [{ target: "abort", arguments: [] }],
              },
              {
                kind: "if",
                condition: "dirty",
                then: [
                  {
                    kind: "expression",
                    calls: [{ target: "flush", arguments: [] }],
                  },
                ],
                else: [],
              },
            ],
          },
        ],
      },
    ],
  );
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
    controlFlowIn,
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
