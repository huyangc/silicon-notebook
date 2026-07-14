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
  MEMORY_INPUT_LIMITS,
  validateMemoryDraft,
} from "./memory-model.ts";

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
