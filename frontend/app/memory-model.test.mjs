import test from "node:test";
import assert from "node:assert/strict";
import * as memoryModel from "./memory-model.ts";

import {
  answerIdBatches,
  canEditMemory,
  memoryListPath,
  memoryOriginMeta,
  memoryProvenanceRows,
  memoryStatusMeta,
  memoryEvidenceRows,
  EVIDENCE_TYPE,
  EVIDENCE_STATUS,
  MEMORY_INPUT_LIMITS,
  validateMemoryDraft,
} from "./memory-model.ts";
import { label } from "./vocabulary.ts";
import { ASK_MODES } from "./ask-modes.ts";

test("session abort tears down active Memory reads and writes synchronously", () => {
  const session = new AbortController();
  const list = new AbortController();
  const mutation = new AbortController();
  const unsubscribe = memoryModel.subscribeMemorySessionAbort(session.signal, () => {
    list.abort();
    mutation.abort();
  });

  session.abort();

  assert.equal(list.signal.aborted, true);
  assert.equal(mutation.signal.aborted, true);
  unsubscribe();
});

test("aborted deferred hydration cannot clear a just-saved answer flag", async () => {
  const controller = new AbortController();
  let resolveBatch;
  const deferredBatch = new Promise((resolve) => { resolveBatch = resolve; });
  let savedFlags = {};
  const hydration = memoryModel.collectSavedAnswerFlags(
    [["answer-1"]],
    () => deferredBatch,
    controller.signal,
  );

  controller.abort();
  savedFlags = { ...savedFlags, "answer-1": true };
  resolveBatch({ links: {} });
  const hydratedFlags = await hydration;
  if (hydratedFlags) savedFlags = hydratedFlags;

  assert.deepEqual(savedFlags, { "answer-1": true });
  assert.equal(hydratedFlags, null);
});

test("answer-link hydration uses bounded batches without per-answer requests", () => {
  const ids = Array.from({ length: 405 }, (_, index) => `answer-${index}`);
  const batches = answerIdBatches(ids);
  assert.deepEqual(batches.map((batch) => batch.length), [200, 200, 5]);
  assert.equal(new Set(batches.flat()).size, 405);
});

test("answer-link hydration deduplicates answer ids before batching", () => {
  assert.deepEqual(answerIdBatches(["a", "a", "", "b"]), [["a", "b"]]);
});

test("candidate labels remain distinct from confirmed", () => {
  assert.equal(memoryStatusMeta("candidate").label, "待确认");
  assert.equal(memoryStatusMeta("confirmed").label, "已确认");
});

test("all terminal memory statuses have explicit review labels", () => {
  assert.equal(memoryStatusMeta("rejected").label, "已拒绝");
  assert.equal(memoryStatusMeta("deprecated").label, "已停用");
});

test("origin metadata distinguishes Ask capture from agent proposals", () => {
  assert.equal(memoryOriginMeta("ask_answer").label, "Ask 回答");
  assert.equal(memoryOriginMeta("external_agent").label, "Agent 提议");
});

test("memory list paths preserve pagination and encode filters", () => {
  assert.equal(
    memoryListPath({
      scope: "notebook",
      notebookId: "nb/1",
      status: "candidate",
      origin: "external_agent",
      query: "power rail",
      offset: 20,
      limit: 20,
    }),
    "/notebooks/nb%2F1/memories?status=candidate&origin=external_agent&query=power+rail&offset=20&limit=20",
  );
});

test("global memory list paths include the selected notebook filter", () => {
  assert.equal(
    memoryListPath({
      scope: "global",
      notebookId: "nb/1",
      status: "all",
      origin: "all",
      query: "",
      offset: 0,
      limit: 20,
    }),
    "/memories?notebook_id=nb%2F1&offset=0&limit=20",
  );
});

test("only live candidate and confirmed memories are editable", () => {
  assert.equal(canEditMemory("candidate"), true);
  assert.equal(canEditMemory("confirmed"), true);
  assert.equal(canEditMemory("rejected"), false);
  assert.equal(canEditMemory("deprecated"), false);
});

test("candidate provenance exposes safe agent and client identities", () => {
  const rows = memoryProvenanceRows({
    origin: "external_agent",
    provenance: {
      reason: "Reusable constraint",
      task_context: { task: "review" },
      agent_profile: { id: "profile-1", name: "Codex" },
      client_request_id: "request-safe-1",
      evidence_refs: [],
      created_by: "private-user-id",
      agent_profile_id: "private-agent-id",
    },
  });
  assert.deepEqual(rows, [
    ["创建 Agent", "Codex (profile-1)"],
    ["客户端请求", "request-safe-1"],
    ["提议原因", "Reusable constraint"],
    ["任务上下文", "task: review"],
  ]);
  assert.equal(JSON.stringify(rows).includes("private-user-id"), false);
  assert.equal(JSON.stringify(rows).includes("private-agent-id"), false);
});

test("candidate review exposes every normalized evidence ref and validation result", () => {
  const rows = memoryEvidenceRows({
    origin: "external_agent",
    provenance: {
      evidence_refs: [
        {
          type: "source_element",
          source_id: "source-1",
          element_id: "element-1",
          trusted: true,
          validation: { status: "validated", reason: "live_source_element" },
        },
        {
          type: "memory",
          memory_id: "memory-missing",
          trusted: false,
          validation: { status: "invalid", reason: "missing_or_cross_owner" },
        },
      ],
    },
  });
  assert.deepEqual(rows, [
    {
      type: "source_element",
      identity: "source-1 / element-1",
      status: "validated",
      reason: "live_source_element",
      trusted: true,
    },
    {
      type: "memory",
      identity: "memory-missing",
      status: "invalid",
      reason: "missing_or_cross_owner",
      trusted: false,
    },
  ]);
});

// --- P2-A（round 6 评审）：跨 notebook 复制/移动而来的 memory，其 provenance
// 嵌套在 provenance.imported_from.source_provenance 之下（memory_service.py
// transfer() 的既有约定，见 backend 侧 test_copy_preserves_source_provenance_
// nested_under_imported_from）——anchors/citations 指向源 notebook，不能当活
// 引用渲染。修复前 memoryProvenanceRows/memoryEvidenceRows 只读顶层字段，对
// 这种嵌套形状视而不见：复制来的 ask-answer memory 显示零引用，agent 来的
// 显示零证据。

test("跨库复制而来的 ask-answer memory：来源+问题+引用计数渲染为仅存档", () => {
  const rows = memoryProvenanceRows({
    origin: "ask_answer",
    provenance: {
      imported_from: {
        notebook_id: "nb-src-1",
        memory_id: "memory-old-1",
        action: "copy",
        source_provenance: {
          question: "为什么这个电源轨要求纹波低于 5%？",
          mode: "chunk",
          evidence_level: "grounded",
          citations: [{ source_id: "s1" }, { source_id: "s2" }],
        },
      },
    },
  });
  assert.deepEqual(rows, [
    ["来源", "复制自笔记本 nb-src-1"],
    ["原笔记本问题（仅存档）", "为什么这个电源轨要求纹波低于 5%？"],
    ["原笔记本引用（仅存档）", "2 条"],
  ]);
});

test("跨库移动而来的 agent memory：来源+证据引用计数渲染为仅存档", () => {
  const rows = memoryProvenanceRows({
    origin: "external_agent",
    provenance: {
      imported_from: {
        notebook_id: "nb-src-2",
        memory_id: "memory-old-2",
        action: "move",
        source_provenance: {
          agent_profile: { id: "profile-1", name: "Codex" },
          client_request_id: "request-1",
          reason: "Reusable constraint",
          evidence_refs: [
            { type: "source_element" },
            { type: "memory" },
            { type: "knowledge" },
          ],
        },
      },
    },
  });
  assert.deepEqual(rows, [
    ["来源", "移动自笔记本 nb-src-2"],
    ["原笔记本证据引用（仅存档）", "3 条"],
  ]);
});

test("跨库传输的证据引用不进 memoryEvidenceRows（不是可操作的活审核项）", () => {
  const rows = memoryEvidenceRows({
    origin: "external_agent",
    provenance: {
      imported_from: {
        notebook_id: "nb-src-2",
        memory_id: "memory-old-2",
        action: "move",
        source_provenance: {
          evidence_refs: [{ type: "source_element", trusted: true }],
        },
      },
    },
  });
  assert.deepEqual(rows, []);
});

test("跨库传输但缺问题/引用/证据时只渲染来源一行（不为空字段造行）", () => {
  const rows = memoryProvenanceRows({
    origin: "ask_answer",
    provenance: {
      imported_from: {
        notebook_id: "nb-src-3",
        memory_id: "memory-old-3",
        action: "copy",
        source_provenance: {},
      },
    },
  });
  assert.deepEqual(rows, [["来源", "复制自笔记本 nb-src-3"]]);
});

// --- round 8 P2-B：重复传输的嵌套 provenance 链必须逐层展开 ------------------
// 一条 memory 被传输两次（A → B → C）：B 里的副本 provenance 是
// {imported_from: {notebook_id: A, action, source_provenance: P0}}（P0 = 原始
// ask-answer/agent 载荷，单跳形状，上面几条既有测试测的就是这个）。第二次传输
// （B → C）时，memory_service.py transfer() 读到的 source.provenance 就是这整
// 个嵌套对象，新副本的 provenance 变成
// {imported_from: {notebook_id: B, action, source_provenance: {imported_from:
// {notebook_id: A, action, source_provenance: P0}}}}——旧代码的 archivalProvenanceRows
// 只读一层 source_provenance，指望它直接是 P0（有 question/citations 字段），
// 但这里它是"另一层 imported_from 包装"，没有 question/citations 字段，两条
// 存档行全部消失，只剩"来源"这一行——第二个目的地丢失了原始问题/引用/证据数。
test("跨库传输两次（A→B→C）：来源逐层展开为两行，存档问题/引用取自最深层原始 provenance", () => {
  const rows = memoryProvenanceRows({
    origin: "ask_answer",
    provenance: {
      imported_from: {
        notebook_id: "nb-B",
        memory_id: "memory-b-copy",
        action: "move",
        source_provenance: {
          imported_from: {
            notebook_id: "nb-A",
            memory_id: "memory-a-original",
            action: "copy",
            source_provenance: {
              question: "为什么这个电源轨要求纹波低于 5%？",
              mode: "chunk",
              evidence_level: "grounded",
              citations: [{ source_id: "s1" }, { source_id: "s2" }],
            },
          },
        },
      },
    },
  });
  assert.deepEqual(rows, [
    ["来源", "移动自笔记本 nb-B"],
    ["上级来源 1", "复制自笔记本 nb-A"],
    ["原笔记本问题（仅存档）", "为什么这个电源轨要求纹波低于 5%？"],
    ["原笔记本引用（仅存档）", "2 条"],
  ]);
});

test("跨库传输三次（A→B→C→D）：三跳全部展开，存档数据仍取自最深层", () => {
  const rows = memoryProvenanceRows({
    origin: "external_agent",
    provenance: {
      imported_from: {
        notebook_id: "nb-C",
        action: "copy",
        source_provenance: {
          imported_from: {
            notebook_id: "nb-B",
            action: "move",
            source_provenance: {
              imported_from: {
                notebook_id: "nb-A",
                action: "move",
                source_provenance: {
                  evidence_refs: [{ type: "source_element" }, { type: "memory" }],
                },
              },
            },
          },
        },
      },
    },
  });
  assert.deepEqual(rows, [
    ["来源", "复制自笔记本 nb-C"],
    ["上级来源 1", "移动自笔记本 nb-B"],
    ["上级来源 2", "移动自笔记本 nb-A"],
    ["原笔记本证据引用（仅存档）", "2 条"],
  ]);
});

test("跨库传输两次但最深层缺问题/引用/证据：只展开两条来源行，不为空字段造行", () => {
  const rows = memoryProvenanceRows({
    origin: "ask_answer",
    provenance: {
      imported_from: {
        notebook_id: "nb-B",
        action: "move",
        source_provenance: {
          imported_from: {
            notebook_id: "nb-A",
            action: "copy",
            source_provenance: {},
          },
        },
      },
    },
  });
  assert.deepEqual(rows, [
    ["来源", "移动自笔记本 nb-B"],
    ["上级来源 1", "复制自笔记本 nb-A"],
  ]);
});

test("非传输 memory 的 ask-answer 来源渲染保持不变（回归闸）", () => {
  const rows = memoryProvenanceRows({
    origin: "ask_answer",
    provenance: {
      question: "为什么稳定？",
      mode: "chunk",
      evidence_level: "grounded",
      citations: [{ source_id: "s1" }],
    },
  });
  assert.deepEqual(rows, [
    ["原问题", "为什么稳定？"],
    ["提问方式", "通用问答"],
    ["依据", "有据"],
    ["引用", "1 条"],
  ]);
});

test("confirm body includes extract_kg when the notebook KG is eligible", () => {
  const checked = memoryModel.confirmMemoryBody({
    title: "Rail budget",
    content_md: "Keep IR drop under 5%",
    tags: ["power", "rail"],
    eligible: true,
    extractKg: true,
  });
  assert.deepEqual(checked, {
    title: "Rail budget",
    content_md: "Keep IR drop under 5%",
    tags: ["power", "rail"],
    extract_kg: true,
  });

  const unchecked = memoryModel.confirmMemoryBody({
    title: "Rail budget",
    content_md: "Keep IR drop under 5%",
    tags: ["power", "rail"],
    eligible: true,
    extractKg: false,
  });
  assert.equal(unchecked.extract_kg, false);
});

test("confirm body omits extract_kg entirely when the notebook is not KG-eligible", () => {
  const body = memoryModel.confirmMemoryBody({
    title: "Rail budget",
    content_md: "Keep IR drop under 5%",
    tags: ["power"],
    eligible: false,
    extractKg: true,
  });
  assert.ok(!("extract_kg" in body));
  assert.deepEqual(body, {
    title: "Rail budget",
    content_md: "Keep IR drop under 5%",
    tags: ["power"],
  });
});

test("from-answer body maps answer_id and gates extract_kg on eligibility", () => {
  const eligible = memoryModel.fromAnswerMemoryBody({
    answerId: "answer-9",
    title: "Rail budget",
    content_md: "Keep IR drop under 5%",
    tags: ["power"],
    eligible: true,
    extractKg: false,
  });
  assert.deepEqual(eligible, {
    answer_id: "answer-9",
    title: "Rail budget",
    content_md: "Keep IR drop under 5%",
    tags: ["power"],
    extract_kg: false,
  });

  const notEligible = memoryModel.fromAnswerMemoryBody({
    answerId: "answer-9",
    title: "Rail budget",
    content_md: "Keep IR drop under 5%",
    tags: ["power"],
    eligible: false,
    extractKg: true,
  });
  assert.ok(!("extract_kg" in notEligible));
  assert.equal(notEligible.answer_id, "answer-9");
});

test("memory bodies pass title, content, and tags through untouched", () => {
  const tags = ["a", "b"];
  const confirmBody = memoryModel.confirmMemoryBody({
    title: "Trimmed title",
    content_md: "Trimmed body",
    tags,
    eligible: true,
    extractKg: true,
  });
  assert.equal(confirmBody.title, "Trimmed title");
  assert.equal(confirmBody.content_md, "Trimmed body");
  assert.deepEqual(confirmBody.tags, ["a", "b"]);

  const fromAnswerBody = memoryModel.fromAnswerMemoryBody({
    answerId: "answer-1",
    title: "Trimmed title",
    content_md: "Trimmed body",
    tags,
    eligible: false,
    extractKg: false,
  });
  assert.equal(fromAnswerBody.title, "Trimmed title");
  assert.equal(fromAnswerBody.content_md, "Trimmed body");
  assert.deepEqual(fromAnswerBody.tags, ["a", "b"]);
});

test("frontend Memory validation mirrors server title content and tag limits", () => {
  assert.equal(validateMemoryDraft({ title: " ", content_md: "Body", tags: [] }), "标题不能为空");
  assert.equal(
    validateMemoryDraft({
      title: "Title",
      content_md: "x".repeat(MEMORY_INPUT_LIMITS.contentMaxChars + 1),
      tags: [],
    }),
    `内容不能超过 ${MEMORY_INPUT_LIMITS.contentMaxChars} 个字符`,
  );
  assert.equal(
    validateMemoryDraft({
      title: "Title",
      content_md: "Body",
      tags: Array.from({ length: MEMORY_INPUT_LIMITS.tagMaxCount + 1 }, (_, index) => `t${index}`),
    }),
    `标签不能超过 ${MEMORY_INPUT_LIMITS.tagMaxCount} 个`,
  );
  assert.equal(
    validateMemoryDraft({
      title: "Title",
      content_md: "Body",
      tags: Array.from({ length: MEMORY_INPUT_LIMITS.tagMaxCount + 1 }, () => "duplicate"),
    }),
    `标签不能超过 ${MEMORY_INPUT_LIMITS.tagMaxCount} 个`,
  );
  assert.equal(
    validateMemoryDraft({ title: "Title", content_md: "Body", tags: ["valid", "  "] }),
    "标签不能为空",
  );
  assert.equal(
    validateMemoryDraft({
      title: "Title",
      content_md: "Body",
      tags: [" analog ", "analog", ...Array.from({ length: 18 }, (_, index) => `tag-${index}`)],
    }),
    "",
  );
  assert.equal(validateMemoryDraft({ title: " Title ", content_md: " Body ", tags: [" analog "] }), "");
});

test("提问方式复用 ask-modes 的 label,不直出 chunk", () => {
  const modeLabels = Object.fromEntries(ASK_MODES.map((m) => [m.id, m.label]));
  assert.equal(label(modeLabels, "chunk", "—"), "通用问答");
  assert.notEqual(label(modeLabels, "chunk", "—"), "chunk");
});

// EVIDENCE_LEVEL 已在 vocabulary.test.mjs 覆盖，这里不重复。
// EVIDENCE_TYPE / EVIDENCE_STATUS 是 memory 面板自己的枚举（不进 vocabulary.ts），
// 真新覆盖：已知取值译对，未知取值退中性兜底、绝不泄漏原始 wire 值。
test("Agent 证据类型:已知取值译对,未知取值退兜底不泄漏原值", () => {
  assert.equal(label(EVIDENCE_TYPE, "source_element", "未知来源"), "原文片段");
  assert.equal(label(EVIDENCE_TYPE, "source", "未知来源"), "原文出处");
  assert.equal(label(EVIDENCE_TYPE, "knowledge", "未知来源"), "知识条目");
  assert.equal(label(EVIDENCE_TYPE, "memory", "未知来源"), "记忆");
  assert.equal(label(EVIDENCE_TYPE, "unsupported", "未知来源"), "无法识别");
  assert.equal(label(EVIDENCE_TYPE, "some_future_type", "未知来源"), "未知来源");
  assert.notEqual(label(EVIDENCE_TYPE, "some_future_type", "未知来源"), "some_future_type");
});

test("Agent 证据校验状态:已知取值译对,未知取值退兜底不泄漏原值", () => {
  assert.equal(label(EVIDENCE_STATUS, "validated", "未能核对"), "已核对");
  assert.notEqual(label(EVIDENCE_STATUS, "validated", "未能核对"), "validated");
  assert.equal(label(EVIDENCE_STATUS, "invalid", "未能核对"), "未能核对");
  assert.equal(label(EVIDENCE_STATUS, "unverified", "未能核对"), "未能核对");
  assert.notEqual(label(EVIDENCE_STATUS, "unverified", "未能核对"), "unverified");
});
