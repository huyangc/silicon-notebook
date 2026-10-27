import test from "node:test";
import assert from "node:assert/strict";

import { clearToken } from "../../app/auth-session.ts";
import {
  NotebookDeletedError,
  cancelNotebookDeletion,
  exportDeletedNotebookMemories,
  getNotebookOrDeletionNotice,
  listNotebookDeletions,
  parseDeletionNotice,
  transferOutOfDeletedNotebook,
} from "../../app/notebook-deletion-api.ts";
import { deleteNotebook } from "../../app/notebook-api.ts";
import {
  deleteConfirmText,
  deleteMountText,
  deleteScheduledText,
  deletionDue,
  deletionNoticeText,
  formatDeleteAt,
  transferDoneText,
} from "../../app/notebook-deletion-copy.ts";

// 删除整本笔记本的一天窗口:客户端按 `detail.code` 分流,数字与时刻全取自服务端。

const storage = new Map();
const originalFetch = globalThis.fetch;
const originalWindow = globalThis.window;
globalThis.window = {
  localStorage: {
    getItem: (key) => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, String(value)),
    removeItem: (key) => storage.delete(key),
    get length() { return storage.size; },
    key: (index) => [...storage.keys()][index] ?? null,
  },
  location: { reload: () => undefined },
};

test.afterEach(() => {
  clearToken();
  storage.clear();
});

test.after(() => {
  globalThis.fetch = originalFetch;
  globalThis.window = originalWindow;
});

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const NOTICE = {
  notebook_id: "nb1", name: "封装工艺库", delete_after: "2026-10-23T08:00:00+00:00",
  is_owner: false, memory_count: 3, transferable_count: 1,
};

test("说明码:读者打开窗口期内的笔记本得到 NotebookDeletedError,普通 404 仍是普通错误", async () => {
  globalThis.fetch = async () => json({ detail: { code: "notebook_pending_delete", ...NOTICE } }, 404);
  await assert.rejects(getNotebookOrDeletionNotice("nb1"), (error) => {
    assert.ok(error instanceof NotebookDeletedError);
    assert.deepEqual(error.notice, NOTICE);
    return true;
  });
  globalThis.fetch = async () => json({ detail: "Notebook not found" }, 404);
  await assert.rejects(getNotebookOrDeletionNotice("nb1"), (error) => !(error instanceof NotebookDeletedError));
  globalThis.fetch = async () => json({ id: "nb1", name: "x" });
  assert.equal((await getNotebookOrDeletionNotice("nb1")).id, "nb1");
});

test("告知列表:形状不对的条目丢掉;读不懂的整份回答当失败", async () => {
  globalThis.fetch = async () => json({ items: [NOTICE, { notebook_id: "", delete_after: "x" }] });
  assert.deepEqual(await listNotebookDeletions(), [NOTICE]);
  globalThis.fetch = async () => json({ nope: true });
  await assert.rejects(listNotebookDeletions());
  assert.equal(parseDeletionNotice({ ...NOTICE, memory_count: -1 }), null);
});

test("确认删除返回服务端定下的截止时刻", async () => {
  globalThis.fetch = async (_url, init) => {
    assert.equal(init.method, "DELETE");
    return json({ status: "pending_delete", delete_after: NOTICE.delete_after }, 202);
  };
  assert.deepEqual(await deleteNotebook("nb1"), { deleteAfter: NOTICE.delete_after });
  // 202 但读不出截止时刻:仍是「已安排删除」,时刻留空(不编),提示不写时刻。
  globalThis.fetch = async () => json({ status: "pending_delete" }, 202);
  assert.deepEqual(await deleteNotebook("nb1"), { deleteAfter: null });
  globalThis.fetch = async () => json({ status: "pending_delete", delete_after: "not a time" }, 202);
  assert.deepEqual(await deleteNotebook("nb1"), { deleteAfter: null });
  const text = deleteScheduledText("旧库", null);
  assert.ok(text.startsWith("已安排删除“旧库”。"), text);
  assert.ok(text.includes("一天后永久删除"), text);
  assert.ok(!/\d{4}年/.test(text), text);
});

test("撤销的三种结局:成功 / 截止已过 / 已不存在", async () => {
  globalThis.fetch = async () => json({ id: "nb1", name: "x" });
  assert.equal((await cancelNotebookDeletion("nb1")).kind, "cancelled");
  globalThis.fetch = async () => json({ detail: { code: "notebook_delete_due", message: "m" } }, 409);
  assert.deepEqual(await cancelNotebookDeletion("nb1"), { kind: "due" });
  globalThis.fetch = async () => json({ detail: "Notebook not found" }, 404);
  assert.deepEqual(await cancelNotebookDeletion("nb1"), { kind: "gone" });
});

test("导出的结局:成功 / 截止已过 / 超限 / 没有可导出的 / 失败", async () => {
  globalThis.fetch = async () => new Response("# m", {
    status: 200, headers: { "Content-Disposition": "attachment; filename=\"a.md\"" },
  });
  const ok = await exportDeletedNotebookMemories("nb1");
  assert.equal(ok.kind, "exported");
  assert.equal(ok.filename, "a.md");
  for (const [response, kind] of [
    [json({ detail: { code: "delete_window_closed" } }, 404), "closed"],
    [json({ detail: { code: "export_too_large", memory_count: 3, limit_bytes: 1 } }, 413), "too_large"],
    [json({ detail: { code: "nothing_to_export", memory_count: 0 } }, 404), "nothing"],
    [json({ detail: "boom" }, 500), "failed"],
  ]) {
    globalThis.fetch = async () => response;
    assert.equal((await exportDeletedNotebookMemories("nb1")).kind, kind);
  }
});

test("转移的结局:逐条结果 / 截止已过 / 读不到", async () => {
  globalThis.fetch = async (_url, init) => {
    assert.deepEqual(JSON.parse(init.body), { target_notebook_id: "nb-mine" });
    return json({
      results: [{ source_id: "m", new_id: "n", ok: true, error: null, error_code: null, status: "moved" }],
      remaining: 4,
      next_after: "m",
    });
  };
  const page = await transferOutOfDeletedNotebook("nb1", "nb-mine");
  assert.equal(page.kind, "transferred");
  assert.equal(page.remaining, 4);
  assert.equal(page.nextAfter, "m");
  globalThis.fetch = async (_url, init) => {
    assert.deepEqual(JSON.parse(init.body), { target_notebook_id: "nb-mine", after: "m" });
    return json({ results: [], remaining: 0, next_after: "m" });
  };
  assert.equal((await transferOutOfDeletedNotebook("nb1", "nb-mine", "m")).remaining, 0);
  // 回答里没有剩余条数:读不懂就当失败,不会被当成「转完了」。
  globalThis.fetch = async () => json({ results: [] });
  assert.deepEqual(await transferOutOfDeletedNotebook("nb1", "nb-mine"), { kind: "failed" });
  globalThis.fetch = async () => json({ detail: { code: "transfer_in_progress" } }, 409);
  assert.deepEqual(await transferOutOfDeletedNotebook("nb1", "nb-mine"), { kind: "busy" });
  globalThis.fetch = async () => json({ detail: { code: "notebook_not_pending_delete" } }, 409);
  assert.deepEqual(await transferOutOfDeletedNotebook("nb1", "nb-mine"), { kind: "restored" });
  globalThis.fetch = async () => json({ detail: { code: "delete_window_closed" } }, 404);
  assert.deepEqual(await transferOutOfDeletedNotebook("nb1", "nb-mine"), { kind: "closed" });
  globalThis.fetch = async () => json({ detail: "Notebook not found" }, 404);
  assert.deepEqual(await transferOutOfDeletedNotebook("nb1", "nb-mine"), { kind: "gone" });
});

test("文案:时刻是本地绝对时间;确认弹窗写明转走的记忆不会回来,不出现任何数字", () => {
  const at = formatDeleteAt(NOTICE.delete_after);
  assert.match(at, /^\d{4}年\d{1,2}月\d{1,2}日 \d{2}:\d{2}$/);
  assert.equal(formatDeleteAt("not a time"), "");
  const confirm = deleteConfirmText("资料库");
  assert.match(confirm, /已经被转移走的记忆不会回到这本笔记本/);
  assert.match(confirm, /撤销删除/);
  assert.doesNotMatch(confirm, /\d/);
  assert.match(deleteMountText(2), /^2 个笔记本/);
  assert.ok(deleteScheduledText("资料库", NOTICE.delete_after).includes(at));
  assert.ok(deletionNoticeText(NOTICE).includes("你在这里的 3 条记忆"));
  assert.ok(!deletionNoticeText({ ...NOTICE, memory_count: 0 }).includes("条记忆"));
  assert.ok(deletionNoticeText({ ...NOTICE, is_owner: true }).includes("在此之前可以撤销"));
  assert.equal(transferDoneText(2, 0, "我的"), "已把 2 条记忆转移到“我的”。");
  assert.ok(transferDoneText(0, 1, "我的").includes("1 条没有转移成功"));
  assert.equal(deletionDue(NOTICE.delete_after, Date.parse(NOTICE.delete_after)), true);
  assert.equal(deletionDue(NOTICE.delete_after, Date.parse(NOTICE.delete_after) - 1), false);
});
