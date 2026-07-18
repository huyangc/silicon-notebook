// 守卫:新建 notebook 的默认名是**持久化契约**,不是界面文案。
//
// 背景(本测试存在的唯一理由):PR「界面散文对齐用户词汇」把 page.tsx 里三处
// `"Untitled notebook"` 当成待去黑话的英文散文,顺手替换成了中文。但这个字符串
// 不是渲染给用户看的 label——它是 `POST /notebooks` 请求体里写进 `notebooks.name`
// 的真实值,同时被多份文档和后端逻辑逐字钉死。改它 = 改数据 + 破契约,不是改文案。
//
// 因此本文件同时守三层,任一层单独改动都会红:
//   1. 前端常量的字面值;
//   2. page.tsx 的三个消费点确实用常量(而不是又内联一个字面量);
//   3. 后端与文档里的同一串仍然一致(跨栈,照 scripts/check_object_type_labels_contract.py
//      的思路:两份真源必须有守卫钉住,severity 那次漏网就是因为没有)。
import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { DEFAULT_NOTEBOOK_NAME } from "./workspace-model.ts";

const page = await readFile(new URL("./page.tsx", import.meta.url), "utf8");
const repoFile = (rel) => readFile(new URL(`../../${rel}`, import.meta.url), "utf8");

test("默认笔记本名的字面值就是文档钉死的那一串", () => {
  // 硬断言字面量。写成 assert.equal(X, X) 那种恒真式没有意义:这里要的就是
  // 「有人改了常量必须先改掉这一行」的摩擦。
  assert.equal(DEFAULT_NOTEBOOK_NAME, "Untitled notebook");
});

test("创建 notebook 的 POST payload 用常量,不内联字面量", () => {
  // openCreate():无弹窗直接建库。
  assert.match(
    page,
    /api<NotebookSummary>\("\/notebooks",\s*\{\s*method:\s*"POST",\s*body:\s*JSON\.stringify\(\{\s*name:\s*DEFAULT_NOTEBOOK_NAME,\s*purpose:\s*""\s*\}\)/,
  );
  // submitCreate():弹窗里留空名字时的兜底。
  assert.match(
    page,
    /body:\s*JSON\.stringify\(\{\s*name:\s*createName\.trim\(\)\s*\|\|\s*DEFAULT_NOTEBOOK_NAME,/,
  );
  assert.match(page, /import \{[\s\S]*?\bDEFAULT_NOTEBOOK_NAME\b[\s\S]*?\} from "\.\/workspace-model";/);
});

test("标题改名清空时回退到同一个默认名", () => {
  assert.match(page, /const nextName = titleDraft\.trim\(\) \|\| DEFAULT_NOTEBOOK_NAME;/);
});

test("page.tsx 里不再出现中文占位名", () => {
  // 三个消费点全走常量后,这个字符串不该再以任何形式出现在编排层。
  // 它一旦回来,基本就是又一次「散文替换顺带改掉持久化值」。
  assert.equal(page.includes("未命名笔记本"), false);
});

test("跨栈:后端仍把同一串当默认名与占位名", async () => {
  const schemas = await repoFile("backend/app/models/schemas.py");
  const store = await repoFile("backend/app/repositories/sqlite/notebook_store.py");
  const repository = await repoFile("backend/app/services/sqlite_repository.py");

  // 请求模型的字段默认值:前端不传 name 时落库的就是它。
  assert.match(schemas, /name: str = "Untitled notebook"/);
  // 空名兜底:前端传了空串时后端补的也是它。
  assert.match(store, /payload\.name\.strip\(\) or "Untitled notebook"/);
  // 占位名集合:决定「自动改名只覆盖占位名,不覆盖用户起的名字」。前端默认名
  // 若不在这个集合里,新建的库就再也不会被自动改名了(静默功能退化)。
  const placeholders = repository.match(/_DEFAULT_NOTEBOOK_NAMES = \{([^}]*)\}/);
  assert.ok(placeholders, "_DEFAULT_NOTEBOOK_NAMES 没找到——后端占位名集合被改名或挪走了");
  assert.ok(
    placeholders[1].includes(`"${DEFAULT_NOTEBOOK_NAME}"`),
    `前端默认名 ${DEFAULT_NOTEBOOK_NAME} 不在后端 _DEFAULT_NOTEBOOK_NAMES 里`,
  );
});

test("跨栈:文档里的默认名契约仍然一致", async () => {
  // 这几处是 review 里被点名的「文档钉死」位置。散文改动扫到默认名时,
  // 这条会连同上面的前端断言一起红,把「文案 vs 契约」的区别顶到脸上。
  for (const rel of ["AGENTS.md", "architecture.md", "README.md", "README_zh.md", "fangan_done.md"]) {
    const text = await repoFile(rel);
    assert.ok(
      text.includes(DEFAULT_NOTEBOOK_NAME),
      `${rel} 不再提到默认名 ${DEFAULT_NOTEBOOK_NAME}——文档与代码已失配`,
    );
  }
});
