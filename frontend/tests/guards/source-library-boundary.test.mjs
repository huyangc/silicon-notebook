import test from "node:test";
import assert from "node:assert/strict";

import { importsIn, parseModule } from "../../test-support/semantic-source.mjs";


test("page composes one source-library owner and does not retain source CRUD/detail state", async () => {
  const page = await parseModule("page.tsx");
  const text = page.getText(page);

  assert.equal((text.match(/useSourceLibrary\(/g) ?? []).length, 1);
  for (const legacyOwner of [
    "const [sources, setSources]",
    "const [sourceDetail, setSourceDetail]",
    "const [deletingSourceIds, setDeletingSourceIds]",
    "const pollCountRef",
    "async function openSourceById",
    "async function deleteSource(source",
  ]) {
    assert.equal(text.includes(legacyOwner), false, `page still owns ${legacyOwner}`);
  }

  const sourceImports = importsIn(page)
    .filter((item) => item.module === "./source-api")
    .map((item) => item.imported);
  for (const hookOwned of [
    "getSource",
    "getNotebookSource",
    "getNotebookSourceElementsPage",
    "parseSource",
    "deleteSource",
  ]) {
    assert.equal(sourceImports.includes(hookOwned), false, `page imports hook-owned ${hookOwned}`);
  }
});


test("source-library hook is narrow and does not depend on other workspace domains", async () => {
  const hook = await parseModule("use-source-library.ts");
  const modules = importsIn(hook).map((item) => item.module);
  for (const forbidden of [
    "./ask-api.ts",
    "./report-api.ts",
    "./knowledge-api.ts",
    "./notebook-api.ts",
  ]) {
    assert.equal(modules.includes(forbidden), false, `source hook imports ${forbidden}`);
  }
  assert.doesNotMatch(hook.getText(hook), /\b(setCurrentNotebook|setKnowledge|setCheckup)\b/);
});
