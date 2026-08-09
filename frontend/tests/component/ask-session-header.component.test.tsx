import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { AskSessionHeaderActions } from "../../app/ask-session-header";


afterEach(cleanup);


test("shows one history entry and starts a new session directly", async () => {
  const onToggleSessionPanel = vi.fn();
  const onStartNewSession = vi.fn();
  const user = userEvent.setup();

  render(
    <AskSessionHeaderActions
      sessionCount={14}
      sessionPanelOpen={false}
      onToggleSessionPanel={onToggleSessionPanel}
      onStartNewSession={onStartNewSession}
    />,
  );

  const history = screen.getByRole("button", { name: "历史 14" });
  expect(history).toHaveAttribute("aria-expanded", "false");
  expect(history).toHaveAttribute("aria-controls", "ask-session-manager");
  await user.click(history);
  expect(onToggleSessionPanel).toHaveBeenCalledTimes(1);

  const newSession = screen.getByRole("button", { name: "新会话" });
  expect(newSession).toHaveAttribute("title", "新会话");
  await user.click(newSession);
  expect(onStartNewSession).toHaveBeenCalledTimes(1);
});
