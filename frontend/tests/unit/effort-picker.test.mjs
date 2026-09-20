import assert from "node:assert/strict";
import test from "node:test";

import { ASK_RETRIEVAL_EFFORTS, ASK_RETRIEVAL_EFFORT_OPTIONS } from "../../app/ask-retrieval-effort.ts";
import { declarations, importsFrom, jsxElements, parseModule } from "../../test-support/semantic-source.mjs";


const picker = await parseModule("effort-picker.tsx");
const report = await parseModule("report-view.tsx");
const page = await parseModule("page.tsx");
// 问答那侧的「检索档位」随引擎选择器一起搬进了共享控件（ask-mode-picker.tsx，
// 全局问答与笔记本内问答共用）。调用点换了文件，判据不变：仍然只有一个。
const askModePicker = await parseModule("ask-mode-picker.tsx");


function rangeSliders(module) {
  return jsxElements(module, "input").filter((element) => element.attributes?.type === "range");
}


test("研究深度 and 检索档位 render one shared control", () => {
  assert.equal(
    declarations(picker).some((item) => item.kind === "function" && item.name === "EffortPicker"),
    true,
  );

  // 两个调用点都消费共享控件,且各自只挂一个。
  assert.ok(importsFrom(report, "./effort-picker").some((item) => item.imported === "EffortPicker"));
  assert.ok(importsFrom(askModePicker, "./effort-picker").some((item) => item.imported === "EffortPicker"));
  assert.equal(jsxElements(report, "EffortPicker").length, 1);
  assert.equal(jsxElements(askModePicker, "EffortPicker").length, 1);
  // page.tsx 现在只是消费共享的引擎选择器，不再自己挂档位控件。
  assert.equal(jsxElements(page, "EffortPicker").length, 0);
});


test("the grade slider exists only inside the shared control", () => {
  // 「移动」变异守卫:把滑块 popover 复制回任一调用点(哪怕共享控件仍在),这里就报红。
  // 只查 import/JSX 存在性挡不住「一边用共享控件、一边又自造一套」的分裂回退。
  assert.equal(rangeSliders(picker).length, 1);
  assert.equal(rangeSliders(report).length, 0);
  assert.equal(rangeSliders(page).length, 0);
  assert.equal(rangeSliders(askModePicker).length, 0);
});


test("retrieval effort feeds the shared control without dropping a grade", () => {
  assert.deepEqual(
    ASK_RETRIEVAL_EFFORT_OPTIONS,
    ASK_RETRIEVAL_EFFORTS.map((effort) => ({
      id: effort.id,
      label: effort.label,
      hint: effort.description,
    })),
  );
  // 每档都要有一句说明:控件在 popover 里无条件渲染它,缺了就是一行空白。
  for (const option of ASK_RETRIEVAL_EFFORT_OPTIONS) {
    assert.ok(option.hint.trim().length > 0, `missing hint for ${option.id}`);
  }
});
