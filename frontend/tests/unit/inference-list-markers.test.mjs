// normalizeInferenceListMarkers 单元测试。
//
// 覆盖 docs/superpowers/specs/2026-09-03-ask-understanding-echo-and-mcp-clarification-design_zh.md
// §T2-d:标记在列表序号前的行要被归一成「序号在前、标记在后」,其它行逐字不动。
import test from "node:test";
import assert from "node:assert/strict";

import { normalizeInferenceListMarkers } from "../../app/inference-list-markers.ts";

test("标记在序号前 -> 序号在前、标记在后", () => {
  assert.equal(
    normalizeInferenceListMarkers("（推断）1. 世界模型闭环。"),
    "1. （推断）世界模型闭环。",
  );
});

test("已在序号后的行不变", () => {
  const line = "1. （推断）世界模型闭环。";
  assert.equal(normalizeInferenceListMarkers(line), line);
});

test("段首无序号的行不变", () => {
  const line = "（推断）以下为论文未描述的方向：";
  assert.equal(normalizeInferenceListMarkers(line), line);
});

test("围栏代码块内的行不处理(``` 与 ~~~ 都算,闭合后恢复处理)", () => {
  // 评审 P2-1:模型在代码块里逐字引用「（推断）1. …」当反例,改了等于把反例改成正例。
  const input = [
    "（推断）1. 围栏外,要改",
    "```text",
    "（推断）1. 围栏内,不改",
    "```",
    "（推断）2. 围栏后,要改",
    "~~~",
    "（推断）3. 波浪围栏内,不改",
    "~~~",
  ].join("\n");
  assert.equal(
    normalizeInferenceListMarkers(input),
    [
      "1. （推断）围栏外,要改",
      "```text",
      "（推断）1. 围栏内,不改",
      "```",
      "2. （推断）围栏后,要改",
      "~~~",
      "（推断）3. 波浪围栏内,不改",
      "~~~",
    ].join("\n"),
  );
  // 未闭合的围栏一直到文末都不处理;短于开启长度的反引号串不算闭合。
  assert.equal(
    normalizeInferenceListMarkers("````\n（推断）1. x\n```\n（推断）2. y"),
    "````\n（推断）1. x\n```\n（推断）2. y",
  );
});

test("列表语法后的制表符也算进入正文(CommonMark 接受),且原有空白逐字保留", () => {
  // codex #670 R1 P2:只交换两个 token,不合成空格——两段空白各自跟着后面的内容走。
  assert.equal(
    normalizeInferenceListMarkers("（推断）1.\t内容"),
    "1.\t（推断）内容",
  );
  assert.equal(
    normalizeInferenceListMarkers("（推断）\t- 内容"),
    "- （推断）\t内容",
  );
  assert.equal(
    normalizeInferenceListMarkers("（推断）  1.   内容"),
    "1.   （推断）  内容",
  );
});

test("反引号围栏的 info string 里再出现反引号时不是围栏(CommonMark),后续行照常处理", () => {
  assert.equal(
    normalizeInferenceListMarkers("````foo`bar\n（推断）1. x"),
    "````foo`bar\n1. （推断）x",
  );
  // 波浪线围栏没有这条限制。
  assert.equal(
    normalizeInferenceListMarkers("~~~foo`bar\n（推断）1. x"),
    "~~~foo`bar\n（推断）1. x",
  );
});

test("已知覆盖缺口:4 空格缩进的子列表与引用块前缀保持现状(不改坏,也不修)", () => {
  const nested = "1. 顶层\n    （推断）1. 子项";
  assert.equal(normalizeInferenceListMarkers(nested), nested);
  const quoted = "> （推断）1. 引用块里";
  assert.equal(normalizeInferenceListMarkers(quoted), quoted);
});

test("无序列表 - 前缀的标记也被调换", () => {
  assert.equal(
    normalizeInferenceListMarkers("（推断）- 这是分点说明"),
    "- （推断）这是分点说明",
  );
});

test("1) 形态的列表语法也被识别", () => {
  assert.equal(
    normalizeInferenceListMarkers("（推断）1) 世界模型闭环。"),
    "1) （推断）世界模型闭环。",
  );
});

test("缩进 3 个空格时保留缩进", () => {
  assert.equal(
    normalizeInferenceListMarkers("   （推断）1. 世界模型闭环。"),
    "   1. （推断）世界模型闭环。",
  );
});

test("缩进 4 个空格时不处理(代码块)", () => {
  const line = "    （推断）1. 世界模型闭环。";
  assert.equal(normalizeInferenceListMarkers(line), line);
});

test("句中出现的标记字面量不动", () => {
  const line = "参见前文提到的（推断）1. 世界模型闭环一节。";
  assert.equal(normalizeInferenceListMarkers(line), line);
});

test("四种标记字面量各一：全角括号", () => {
  assert.equal(
    normalizeInferenceListMarkers("（推断）1. 内容"),
    "1. （推断）内容",
  );
});

test("四种标记字面量各一：半角括号", () => {
  assert.equal(
    normalizeInferenceListMarkers("(推断)1. 内容"),
    "1. (推断)内容",
  );
});

test("四种标记字面量各一：Likely,", () => {
  assert.equal(
    normalizeInferenceListMarkers("Likely, 1. content"),
    "1. Likely, content",
  );
});

test("四种标记字面量各一：【通识】", () => {
  assert.equal(
    normalizeInferenceListMarkers("【通识】1. 内容"),
    "1. 【通识】内容",
  );
});

test("多行混合时只改命中的那一行", () => {
  const input = [
    "（推断）以下为论文未描述的方向：",
    "（推断）1. 世界模型闭环。",
    "（推断）2. 长时程一致。",
    "1. （推断）已经写对的行不动。",
  ].join("\n");
  const expected = [
    "（推断）以下为论文未描述的方向：",
    "1. （推断）世界模型闭环。",
    "2. （推断）长时程一致。",
    "1. （推断）已经写对的行不动。",
  ].join("\n");
  assert.equal(normalizeInferenceListMarkers(input), expected);
});

test("空输入原样返回", () => {
  assert.equal(normalizeInferenceListMarkers(""), "");
});
