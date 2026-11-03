import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test } from "vitest";
import { BASE_PROPS, recordDetail, registerKnowhowPanelSuite, renderPanel, wireDetail, wireSummary } from "../../test-support/knowhow-panel-harness";

const suite = registerKnowhowPanelSuite();

test("a remotely deleted or moved selected Knowhow table converges to the refreshed list", async () => {
  const server = suite.server();
  const detail = recordDetail();
  server.on("GET", "/notebooks/nb-1/knowhow", { json: [wireSummary({ id: detail.id, title: detail.title })] });
  server.on("GET", "/notebooks/nb-1/knowhow/t-rec", { json: wireDetail(detail) });
  const panel = renderPanel({ initialTableId: "t-rec", collabRevision: 0 });
  await screen.findByRole("button", { name: "检查电源" });
  server.on("GET", "/notebooks/nb-1/knowhow", { json: [wireSummary({ id: "other", title: "仍在当前库的表" })] });
  server.on("GET", "/notebooks/nb-1/knowhow/t-rec", { status: 404, json: { detail: "not found" } });
  panel.update({ ...BASE_PROPS, initialTableId: "t-rec", collabRevision: 1 });
  await screen.findByRole("button", { name: "打开表格：仍在当前库的表" });
  expect(screen.queryByRole("button", { name: "检查电源" })).toBeNull();
  expect(screen.queryByText("刷新没成功，请重试")).toBeNull();
});

test("a temporary collaboration detail failure preserves the selected table and can be retried", async () => {
  const server = suite.server();
  const detail = recordDetail();
  server.on("GET", "/notebooks/nb-1/knowhow", { json: [wireSummary({ id: detail.id, title: detail.title })] });
  server.on("GET", "/notebooks/nb-1/knowhow/t-rec", { json: wireDetail(detail) });
  const panel = renderPanel({ initialTableId: "t-rec", collabRevision: 0 });
  await screen.findByRole("button", { name: "检查电源" });
  server.once("GET", "/notebooks/nb-1/knowhow/t-rec", { status: 503, json: { detail: "temporary" } });
  panel.update({ ...BASE_PROPS, initialTableId: "t-rec", collabRevision: 1 });
  await screen.findByText("刷新没成功，请重试");
  expect(screen.getByRole("button", { name: "检查电源" })).toBeVisible();
  expect(screen.queryByText(/未能定位/)).toBeNull();
  await userEvent.setup().click(screen.getByRole("button", { name: "刷新" }));
  await screen.findByText("已刷新");
});

test("a remotely removed table never discards an editing draft and converges after the user closes it", async () => {
  const server = suite.server();
  const detail = recordDetail();
  server.on("GET", "/notebooks/nb-1/knowhow", { json: [wireSummary({ id: detail.id, title: detail.title })] });
  server.on("GET", "/notebooks/nb-1/knowhow/t-rec", { json: wireDetail(detail) });
  const panel = renderPanel({ initialTableId: "t-rec", collabRevision: 0 });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "检查电源" }));
  await user.click(within(await screen.findByRole("dialog", { name: /› 处理方法$/ })).getByRole("button", { name: "编辑" }));
  const editor = screen.getByRole("dialog", { name: /› 处理方法$/ });
  await user.clear(within(editor).getByRole("textbox"));
  await user.type(within(editor).getByRole("textbox"), "不能丢失的草稿");
  server.on("GET", "/notebooks/nb-1/knowhow", { json: [] });
  server.on("GET", "/notebooks/nb-1/knowhow/t-rec", { status: 404, json: { detail: "gone" } });
  const before = server.log.length;
  panel.update({ ...BASE_PROPS, initialTableId: "t-rec", collabRevision: 1 });
  expect(within(editor).getByRole("textbox")).toHaveValue("不能丢失的草稿");
  expect(server.log).toHaveLength(before);
  await user.click(within(editor).getByRole("button", { name: "取消" }));
  await user.click(screen.getByRole("button", { name: "刷新" }));
  await screen.findByText("未能定位到引用指向的表格，它可能已被删除。");
  expect(screen.queryByRole("button", { name: "检查电源" })).toBeNull();
});

test("a missing-table reply cannot close an editor opened while the collaboration read was in flight", async () => {
  const server = suite.server();
  const detail = recordDetail();
  server.on("GET", "/notebooks/nb-1/knowhow", { json: [wireSummary({ id: detail.id, title: detail.title })] });
  server.on("GET", "/notebooks/nb-1/knowhow/t-rec", { json: wireDetail(detail) });
  const panel = renderPanel({ initialTableId: "t-rec", collabRevision: 0 });
  await screen.findByRole("button", { name: "检查电源" });
  const release = server.hold("GET", "/notebooks/nb-1/knowhow/t-rec");
  panel.update({ ...BASE_PROPS, initialTableId: "t-rec", collabRevision: 1 });
  await waitFor(() => expect(server.log.filter((request) => request.path.endsWith("/t-rec"))).toHaveLength(2));
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "管理" }));
  const editor = await screen.findByRole("dialog", { name: "表管理：排障记录" });
  await user.type(within(editor).getByRole("textbox", { name: "标题" }), "新的草稿");
  release({ status: 404, json: { detail: "gone" } });
  await screen.findByText("刷新没成功，请重试");
  expect(within(editor).getByRole("textbox", { name: "标题" })).toHaveValue("排障记录新的草稿");
});

test("a real Knowhow editor preserves its draft on a pulse and refreshes the visible table only after closing the editor", async () => {
  const server = suite.server();
  let detail = recordDetail({ mutationSeq: 1 });
  server.on("GET", "/notebooks/nb-1/knowhow", { json: [wireSummary({ id: detail.id, title: detail.title, rowCount: 2 })] });
  server.on("GET", "/notebooks/nb-1/knowhow/t-rec", () => ({ json: wireDetail(detail) }));
  const panel = renderPanel({ initialTableId: "t-rec", collabRevision: 0 });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "检查电源" }));
  const preview = await screen.findByRole("dialog", { name: /› 处理方法$/ });
  await user.click(within(preview).getByRole("button", { name: "编辑" }));
  const editor = screen.getByRole("dialog", { name: /› 处理方法$/ });
  await user.clear(within(editor).getByRole("textbox"));
  await user.type(within(editor).getByRole("textbox"), "尚未保存的草稿");
  detail = { ...detail, mutationSeq: 2, rows: detail.rows.map((row) => row.id === "r1" ? { ...row, cells: { ...row.cells, "c-fix": "同事的新值" } } : row) };
  const before = server.log.length;
  panel.update({ ...BASE_PROPS, initialTableId: "t-rec", collabRevision: 1 });
  expect(screen.getByText(/有新的改动/)).toBeVisible();
  expect(within(editor).getByRole("textbox")).toHaveValue("尚未保存的草稿");
  expect(server.log).toHaveLength(before);
  await user.click(within(editor).getByRole("button", { name: "取消" }));
  await user.click(screen.getByRole("button", { name: "刷新" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "同事的新值" })).toBeVisible());
  expect(screen.getByText("已刷新")).toBeVisible();
});

test("a mounted read-only Knowhow table applies a pulse without adding any mutation request", async () => {
  const server = suite.server();
  let detail = recordDetail();
  server.on("GET", "/notebooks/nb-1/knowhow", { json: [wireSummary({ id: detail.id, title: detail.title, rowCount: 2 })] });
  server.on("GET", "/notebooks/nb-1/knowhow/t-rec", () => ({ json: wireDetail(detail) }));
  const panel = renderPanel({ initialTableId: "t-rec", canEdit: false, collabRevision: 0 });
  await screen.findByRole("button", { name: "检查电源" });
  detail = { ...detail, rows: detail.rows.map((row) => row.id === "r1" ? { ...row, cells: { ...row.cells, "c-fix": "新资料" } } : row) };
  panel.update({ ...BASE_PROPS, canEdit: false, initialTableId: "t-rec", collabRevision: 1 });
  await screen.findByRole("button", { name: "新资料" });
  expect(server.log.every((request) => request.method === "GET")).toBe(true);
});

test("a second safe Knowhow revision arriving during a held detail read automatically fetches its newer snapshot", async () => {
  const server = suite.server();
  const initial = recordDetail({ mutationSeq: 1 });
  let current = initial;
  const old = { ...initial, mutationSeq: 2, rows: initial.rows.map((row) => row.id === "r1" ? { ...row, cells: { ...row.cells, "c-fix": "第一版协作值" } } : row) };
  server.on("GET", "/notebooks/nb-1/knowhow", { json: [wireSummary({ id: initial.id, title: initial.title, rowCount: 2 })] });
  server.on("GET", "/notebooks/nb-1/knowhow/t-rec", () => ({ json: wireDetail(current) }));
  const panel = renderPanel({ initialTableId: "t-rec", collabRevision: 0 });
  await screen.findByRole("button", { name: "检查电源" });
  const release = server.hold("GET", "/notebooks/nb-1/knowhow/t-rec");
  panel.update({ ...BASE_PROPS, initialTableId: "t-rec", collabRevision: 1 });
  await waitFor(() => expect(server.log.filter((request) => request.path === "/notebooks/nb-1/knowhow/t-rec")).toHaveLength(2));
  current = { ...old, mutationSeq: 3, rows: old.rows.map((row) => row.id === "r1" ? { ...row, cells: { ...row.cells, "c-fix": "第二版协作值" } } : row) };
  panel.update({ ...BASE_PROPS, initialTableId: "t-rec", collabRevision: 2 });
  release({ json: wireDetail(old) });
  await screen.findByRole("button", { name: "第二版协作值" });
  expect(server.log.filter((request) => request.path === "/notebooks/nb-1/knowhow/t-rec")).toHaveLength(3);
});
