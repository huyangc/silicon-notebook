// 活动详情里打开一条留存期内的全局问答（参与笔记本已被删除）：与全局问答窗口同一份
// 口径——已删库的引用显示「来源已删除」、点不开、名下附图不取，其余引用照常可点。
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { ActivityDetail } from "../../app/dev/logs/activity/ActivityDetail";
import type { ActivityAsk, AskDetail } from "../../app/dev/logs/activity/types";

const NOW = new Date(2026, 9, 19, 12, 0);

const ITEM: ActivityAsk = {
  type: "ask",
  id: "gask-1",
  scope: "global",
  notebook_id: "",
  created_at: "2026-10-19T10:30:00+00:00",
  asked_at: "2026-10-19T10:29:00+00:00",
  conversation_id: "gconv-1",
  question: "低温会怎样？",
  mode: "chunk",
  status: "done",
  answer_id: "gask-1",
  error: "",
} as ActivityAsk;

const DETAIL: AskDetail = {
  job_id: "gask-1",
  scope: "global",
  notebook_id: "",
  conversation_id: "gconv-1",
  question: "低温会怎样？",
  mode: "chunk",
  status: "done",
  asked_at: "2026-10-19T10:29:00+00:00",
  answered_at: "2026-10-19T10:31:00+00:00",
  error: "",
  trace: [],
  answer: {
    answer_id: "gask-1", conversation_id: "gconv-1", conclusion: "结论。",
    answer: "容量下降 [k1]。内阻上升 [k2]。", grounded: true, evidence_level: "grounded",
    anchors: [
      { key: "k1", object_id: "", object_type: "element", label: "甲", snippet: "甲摘录。",
        source_id: "s-1", element_id: "e-1", notebook_id: "nb-0" },
      { key: "k2", object_id: "", object_type: "element", label: "乙", snippet: "乙摘录。",
        source_id: "s-2", element_id: "e-2", notebook_id: "nb-gone",
        images: [{ element_id: "img-2", asset_id: "asset-2", caption: "乙图" }] },
    ],
    related_knowledge: [], citations: [], llm_mode: "chunk",
  },
  notebook_ids: ["nb-0", "nb-gone"],
  notebook_names: { "nb-0": "材料研究" },
  retained_until: "2027-04-17T10:31:00+00:00",
  deleted_notebook_ids: ["nb-gone"],
};

const fetchSpy = vi.fn();

beforeEach(() => {
  fetchSpy.mockImplementation(async () => new Response(new Blob(["x"]), { status: 200 }));
  vi.stubGlobal("fetch", fetchSpy);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

test("a retained global turn degrades only the deleted library's citation", async () => {
  const { container } = render(
    <ActivityDetail
      askDetail={DETAIL}
      askDetailError=""
      askDetailLoading={false}
      item={ITEM}
      notebookNames={{}}
      now={NOW}
      reportDetail={null}
      reportDetailError=""
      reportDetailLoading={false}
    />,
  );
  expect(await screen.findByText(/容量下降/)).toBeInTheDocument();
  const deleted = container.querySelector(".cite-chip-deleted")!;
  expect(deleted).toHaveTextContent("[2]来源已删除");
  fireEvent.click(deleted);
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(screen.getByRole("button", { name: "[1]" })).toBeInTheDocument();
  for (const [url] of fetchSpy.mock.calls) {
    expect(String(url)).not.toContain("nb-gone");
  }
  expect(container.querySelector('[alt="乙图"]')).toBeNull();
});
