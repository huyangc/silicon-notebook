// KnowhowPanel: keyboard focus stays inside the full-screen panel (debt-fix S7).
//
// The panel covers the whole page and declares aria-modal, but until S7 nothing
// kept keyboard focus in it: Tab walked out to the citation buttons behind it,
// and Enter there re-targeted the open panel. The panel now uses the shared
// non-root overlay convention (use-dialog-focus.ts): focus moves into the panel
// when it opens, Tab / Shift+Tab cycle inside it, and closing it, including the
// page unmounting it on a notebook switch, returns focus to the element that
// opened it. page.tsx makes the page behind it inert (pinned in
// tests/unit/workspace-layout.test.mjs). Layers above the panel keep their own
// keyboard handling, and Escape still closes only the top layer.
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { expect, test } from "vitest";

import { KnowhowPanel } from "../../app/knowhow-panel.tsx";
import { focusableIn } from "../../app/use-dialog-focus.ts";
import {
  BASE_PROPS,
  recordDetail,
  routeTable,
  settle,
  useKnowhowPanelSuite,
} from "../../test-support/knowhow-panel-harness.tsx";

const suite = useKnowhowPanelSuite();

type HostProps = { open: boolean; notebookId?: string; initialTableId?: string; initialRowId?: string };

/** Stands in for page.tsx: content behind the panel, the opener, and the panel. */
function Host({ open, notebookId = "nb-1", initialTableId, initialRowId }: HostProps) {
  const [closedByPanel, setClosedByPanel] = useState(false);
  return (
    <>
      <button type="button">背后的引用</button>
      <button type="button">打开 Knowhow</button>
      {open && !closedByPanel && (
        <KnowhowPanel
          {...BASE_PROPS}
          notebookId={notebookId}
          initialTableId={initialTableId}
          initialRowId={initialRowId}
          onClose={() => setClosedByPanel(true)}
        />
      )}
      <button type="button">页面末尾</button>
    </>
  );
}

function panel(): HTMLElement {
  return screen.getByRole("dialog", { name: "Knowhow 表" });
}

async function openFrom(opener: string, props: Omit<HostProps, "open"> = {}) {
  const view = render(<Host open={false} {...props} />);
  screen.getByRole("button", { name: opener }).focus();
  view.rerender(<Host open {...props} />);
  await screen.findByRole("button", { name: "打开表格：排障记录" });
  await settle();
  return view;
}

test("opening moves focus into the panel; its close button returns focus to the opener", async () => {
  routeTable(suite.server(), "nb-1", recordDetail());
  const user = userEvent.setup();
  await openFrom("打开 Knowhow");
  expect(document.activeElement).toBe(panel());

  await user.click(within(panel()).getAllByRole("button", { name: "关闭" })[0]);
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "Knowhow 表" })).not.toBeInTheDocument());
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "打开 Knowhow" }));
});

test("Tab and Shift+Tab cycle inside the panel and never reach the page behind it", async () => {
  routeTable(suite.server(), "nb-1", recordDetail());
  const user = userEvent.setup();
  await openFrom("打开 Knowhow");
  const focusable = focusableIn(panel());
  expect(focusable.length).toBeGreaterThan(2);

  for (let step = 0; step < focusable.length + 2; step += 1) {
    await user.tab();
    expect(panel().contains(document.activeElement)).toBe(true);
  }
  focusable.at(-1)!.focus();
  await user.tab();
  expect(document.activeElement).toBe(focusable[0]);
  await user.tab({ shift: true });
  expect(document.activeElement).toBe(focusable.at(-1));
  for (const outside of ["背后的引用", "打开 Knowhow", "页面末尾"]) {
    expect(document.activeElement).not.toBe(screen.getByRole("button", { name: outside }));
  }
});

test("right after opening, Shift+Tab wraps to the panel's last control and Tab goes to its first", async () => {
  // Opening leaves focus on the panel container (tabIndex=-1), which is neither
  // the first nor the last focusable control: native reverse Tab from there would
  // leave the panel, so the container counts as a boundary in both directions.
  routeTable(suite.server(), "nb-1", recordDetail());
  const user = userEvent.setup();
  await openFrom("打开 Knowhow");
  const focusable = focusableIn(panel());
  expect(document.activeElement).toBe(panel());
  await user.tab({ shift: true });
  expect(document.activeElement).toBe(focusable.at(-1));

  panel().focus();
  await user.tab();
  expect(document.activeElement).toBe(focusable[0]);
});

test("when the page unmounts the panel (notebook switch), focus returns to the opener", async () => {
  routeTable(suite.server(), "nb-1", recordDetail());
  const view = await openFrom("背后的引用");
  expect(document.activeElement).toBe(panel());

  // page.tsx closes the panel as the first step of opening another notebook.
  view.rerender(<Host open={false} notebookId="nb-2" />);
  expect(screen.queryByRole("dialog", { name: "Knowhow 表" })).not.toBeInTheDocument();
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "背后的引用" }));
});

test("an opener that is gone by the time the panel closes is not focused", async () => {
  routeTable(suite.server(), "nb-1", recordDetail());
  const opener = document.createElement("button");
  opener.textContent = "临时入口";
  document.body.appendChild(opener);
  const view = render(<Host open={false} />);
  opener.focus();
  view.rerender(<Host open />);
  await screen.findByRole("button", { name: "打开表格：排障记录" });
  opener.remove();
  view.rerender(<Host open={false} />);
  expect(document.activeElement).not.toBe(opener);
});

test("a dialog above the panel keeps its own Tab order", async () => {
  routeTable(suite.server(), "nb-1", recordDetail());
  const user = userEvent.setup();
  await openFrom("打开 Knowhow");
  // A root modal opened over the panel (e.g. release notes) is another dialog
  // outside the panel: the panel's trap must not pull focus back from it.
  const layer = document.createElement("div");
  layer.setAttribute("role", "dialog");
  layer.innerHTML = "<button>上层一</button><button>上层二</button>";
  document.body.appendChild(layer);
  within(layer).getByRole("button", { name: "上层一" }).focus();
  await user.tab();
  expect(document.activeElement).toBe(within(layer).getByRole("button", { name: "上层二" }));
  layer.remove();
});

test("Escape closes only the row drawer on top; focus stays in the panel", async () => {
  routeTable(suite.server(), "nb-1", recordDetail());
  const user = userEvent.setup();
  render(<Host open initialTableId="t-rec" initialRowId="r1" />);
  const drawer = await screen.findByRole("dialog", { name: /^供电异常/ });
  await settle();
  await user.keyboard("{Escape}");
  await waitFor(() => expect(drawer).not.toBeInTheDocument());
  await settle();
  expect(panel()).toBeInTheDocument();
  expect(panel().contains(document.activeElement)).toBe(true);
  await user.keyboard("{Escape}");
  await settle();
  expect(panel()).toBeInTheDocument();
});
