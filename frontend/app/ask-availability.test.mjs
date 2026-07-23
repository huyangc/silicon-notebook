import test from "node:test";
import assert from "node:assert/strict";

import { isAskBlocked } from "./ask-availability.ts";

const nb = (over = {}) => ({
  id: "nb",
  name: "n",
  purpose: "",
  primary_domain: "",
  status: "ready",
  counts: { sources: 0, memories: 0 },
  created_label: "",
  ...over,
});

test("有可见来源 → 放行(不看其它信号)", () => {
  assert.equal(isAskBlocked(nb(), 1), false);
  assert.equal(isAskBlocked(nb(), 42), false);
});

test("无来源但有知识图谱(kg_ready) → 放行(覆盖 knowhow-only 笔记本)", () => {
  // knowhow 表格子写入 knowledge_objects 使 kg_ready 为真,且 knowhow 内容在 chunk 层恒可检索。
  assert.equal(isAskBlocked(nb({ kg_ready: true }), 0), false);
});

test("无来源但挂载参考库有知识图谱(base_kg_available) → 放行", () => {
  assert.equal(isAskBlocked(nb({ base_kg_available: true }), 0), false);
});

test("无来源但有 memory 计数 → 放行(宽松:含候选也放)", () => {
  assert.equal(isAskBlocked(nb({ counts: { sources: 0, memories: 3 } }), 0), false);
});

test("无来源/无KG/无带图参考库/无memory → 禁止对话", () => {
  assert.equal(isAskBlocked(nb(), 0), true);
  assert.equal(
    isAskBlocked(nb({ kg_ready: false, base_kg_available: false, counts: { sources: 0, memories: 0 } }), 0),
    true,
  );
});

test("反向护栏(P2-2):挂了参考库但其无知识图谱(base_kg_available=false) → 仍禁止", () => {
  // 无 KG 的参考库对检索零贡献(基线 chunk 只查当前笔记本,联邦路径都要 KG)——
  // 不能只因「挂了参考库」就放行。base_notebooks 有值但 base_kg_available 为假时应禁止。
  const mountedButNoKg = nb({
    base_notebooks: [{ id: "b", name: "B" }],
    base_kg_available: false,
  });
  assert.equal(isAskBlocked(mountedButNoKg, 0), true);
});

test("来源数为负(异常)按无来源处理", () => {
  assert.equal(isAskBlocked(nb(), -1), true);
  assert.equal(isAskBlocked(nb({ kg_ready: true }), -1), false);
});

test("currentNotebook 为 null 且无来源 → 禁止(安全默认);已知有来源 → 放行", () => {
  assert.equal(isAskBlocked(null, 0), true);
  assert.equal(isAskBlocked(null, 3), false);
});
