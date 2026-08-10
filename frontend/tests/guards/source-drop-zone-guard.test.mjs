import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

// 拖放接管守卫：来源文件的 <input type=file accept=…> 铺满其父 label，浏览器会把
// 拖进来的文件直接交给原生 input 并按 accept **静默**过滤不支持的类型——用户得不到
// 任何提示（正是「批量上传时不支持的文档无声消失」的根因）。所以每个来源文件 input
// 的**外层 label** 必须显式带 onDragOver + onDrop（preventDefault 后走
// stageIncomingFiles，让被跳过的文件进弹窗内的持久「已跳过」列表）。
//
// 判据按源码结构而非行号：找到 page.tsx 里每处 accept={SUPPORTED_SOURCE_ACCEPT}
// 的 input，向前回溯最近的 <label 开标签，断言其属性里有 onDragOver 与 onDrop。

const pagePath = path.join(
  path.dirname(fileURLToPath(import.meta.url)),
  "..", "..", "app", "page.tsx",
);

test("来源上传的每个 file input 外层 label 都显式接管拖放", () => {
  const source = readFileSync(pagePath, "utf8");
  const marker = "accept={SUPPORTED_SOURCE_ACCEPT}";
  const positions = [];
  for (let at = source.indexOf(marker); at !== -1; at = source.indexOf(marker, at + 1)) {
    positions.push(at);
  }
  assert.ok(positions.length >= 2, `page.tsx 里应至少有 2 个来源文件 input，实际 ${positions.length}`);
  for (const at of positions) {
    const labelStart = source.lastIndexOf("<label", at);
    assert.ok(labelStart !== -1, "来源文件 input 应位于 <label> 内");
    // 不能用 indexOf(">") 找开标签结尾——属性里的箭头函数 `=>` 会截断它。
    // 取 <label 到它包裹的 <input 之间的片段：label 的全部属性都在这段里。
    const inputStart = source.lastIndexOf("<input", at);
    assert.ok(inputStart > labelStart, "来源文件 input 应位于其 label 开标签之后");
    const labelOpenTag = source.slice(labelStart, inputStart);
    assert.match(
      labelOpenTag,
      /onDragOver=/,
      `来源文件 input 的外层 label 缺 onDragOver（原生 input 会按 accept 静默吞掉拖放）：\n${labelOpenTag}`,
    );
    assert.match(
      labelOpenTag,
      /onDrop=/,
      `来源文件 input 的外层 label 缺 onDrop（原生 input 会按 accept 静默吞掉拖放）：\n${labelOpenTag}`,
    );
  }
});
