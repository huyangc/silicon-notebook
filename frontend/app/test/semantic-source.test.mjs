import test from "node:test";
import assert from "node:assert/strict";

import {
  callsIn,
  findFunction,
  importsFrom,
  jsxElements,
  parseText,
  scopedCalls,
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
