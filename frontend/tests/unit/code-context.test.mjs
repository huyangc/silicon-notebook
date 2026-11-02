import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);
const navigation = require("../../../scripts/code_context_ts.cjs");

test("TypeScript and TSX navigation keeps complete selected implementations", () => {
  const fixture = "import { label, unusedLabel } from './labels';\nimport unused from './unused';\n"
    + "export function Panel() { function detail() { return label; } return <div>{detail()}</div>; }\n"
    + "export const Button = () => <button>last marker</button>;\n";
  const result = navigation.indexText(fixture, "/project/panel.tsx");
  assert.deepEqual(result.symbols.map(s => s.name), ["Panel", "Panel.detail", "Button"]);
  const panel = result.symbols.find(s => s.name === "Panel");
  assert.deepEqual(panel.imports, ["import { label } from './labels';"]);
  assert.ok(panel.source.endsWith("return <div>{detail()}</div>; }"));
  assert.ok(result.symbols.find(s => s.name === "Button").source.includes("last marker"));
});

test("TypeScript references distinguish imported aliases from shadowed and dynamic names", () => {
  const fixture = "import { work as run } from './owner';\nimport * as owner from './owner';\n"
    + "function yes() { return run() + owner.work(); }\n"
    + "function no(run, owner) { return run() + owner.work(); }\n"
    + "function dynamic(obj) { return obj.work(); }\n";
  const refs = navigation.references(fixture, "/project/caller.ts", "/project/owner.ts", "work");
  assert.ok(refs.some(r => r.kind === "import"));
  assert.deepEqual([...new Set(refs.filter(r => r.kind === "reference").map(r => r.scope))], ["yes"]);
});

test("default TSX imports and class methods retain their symbol identities", () => {
  const component = navigation.indexText("export default function Panel() { return <div />; }", "/project/panel.tsx");
  assert.equal(component.symbols[0].name, "Panel");
  const refs = navigation.references("import Page from './panel'; function render() { return Page(); }",
    "/project/caller.ts", "/project/panel.tsx", "Panel", true);
  assert.ok(refs.some(r => r.kind === "reference" && r.scope === "render"));
  const methods = navigation.indexText("class A { read() {} } class B { read() {} }", "/project/x.ts");
  assert.deepEqual(methods.symbols.filter(s => s.kind === "method").map(s => s.name), ["A.read", "B.read"]);
});

test("TypeScript parser rejects malformed input instead of navigating a recovered tree", () => {
  assert.throws(() => navigation.indexText("function broken( {", "/project/broken.ts"), /syntax error/);
});

test("nested symbol dependencies stay local and source is parsed without execution", () => {
  const fixture = "throw new Error('do not execute');\n"
    + "function Home() { function load() { return 1; } function upload() {"
    + " function contained() { return 2; } return load() + contained(); } return upload(); }";
  const result = navigation.indexText(fixture, "/project/page.tsx");
  assert.deepEqual(result.symbols.find(s => s.name === "Home.upload").dependencies, ["Home.load"]);
  assert.deepEqual(result.symbols.find(s => s.name === "Home").dependencies, []);
});

test("property names, JSX attributes, declarations and labels are not lexical references", () => {
  const fixture = "import { work } from './owner';\n"
    + "const props = { work: 0 };\n"
    + "class Field { work = 0; }\n"
    + "class Method { work() { return 0; } }\n"
    + "interface Shape { work: number; }\n"
    + "function attributes() { return <div work={0} />; }\n"
    + "function intrinsic() { return <work />; }\n"
    + "function labels() { work: for (;;) { break work; } }\n";
  const refs = navigation.references(fixture, "/project/caller.tsx", "/project/owner.ts", "work");
  assert.ok(refs.some(r => r.kind === "import"));
  assert.deepEqual(refs.filter(r => r.kind === "reference"), []);
});

test("shorthand values, computed keys and calls remain lexical references", () => {
  const fixture = "import { work } from './owner';\n"
    + "function shorthand() { return { work }; }\n"
    + "function computed() { return { [work]: 0 }; }\n"
    + "function calls() { return work(); }\n"
    + "function attributeValue() { return <div value={work} />; }\n";
  const refs = navigation.references(fixture, "/project/caller.tsx", "/project/owner.ts", "work");
  assert.deepEqual([...new Set(refs.filter(r => r.kind === "reference").map(r => r.scope))].sort(),
    ["attributeValue", "calls", "computed", "shorthand"]);
});

test("JSX component bindings and namespace members differ from intrinsic tag spellings", () => {
  const fixture = "import { button, Button } from './owner';\nimport * as UI from './owner';\n"
    + "function intrinsic() { return <button />; }\n"
    + "function component() { return <Button />; }\n"
    + "function namespace() { return <UI.Button />; }\n";
  const button = navigation.references(fixture, "/project/caller.tsx", "/project/owner.tsx", "button");
  assert.deepEqual(button.filter(r => r.kind === "reference"), []);
  const component = navigation.references(fixture, "/project/caller.tsx", "/project/owner.tsx", "Button");
  assert.deepEqual([...new Set(component.filter(r => r.kind === "reference").map(r => r.scope))].sort(),
    ["component", "namespace"]);
});
