import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { ChatQuestion } from "../../app/chat-question";

afterEach(() => {
  vi.useRealTimers();
});

test("temporarily shows the local question time on hover", () => {
  render(
    <ChatQuestion
      question="什么时候提问的？"
      askedAt="2026-07-29T09:05:00"
      now={new Date(2026, 6, 29, 12, 0)}
    />,
  );

  const question = screen.getByRole("button", { name: /什么时候提问的/ });
  expect(screen.queryByText("09:05")).not.toBeInTheDocument();
  fireEvent.pointerEnter(question.parentElement as HTMLElement);
  expect(screen.getByText("09:05")).toBeInTheDocument();
  fireEvent.pointerLeave(question.parentElement as HTMLElement);
  expect(screen.queryByText("09:05")).not.toBeInTheDocument();
});

test("pins the time on question click and restores hover behavior after an outside click", async () => {
  const user = userEvent.setup();
  render(
    <div>
      <ChatQuestion
        question="固定时间"
        askedAt="2026-07-27T09:05:00"
        now={new Date(2026, 6, 29, 12, 0)}
      />
      <button type="button">其他位置</button>
    </div>,
  );

  const question = screen.getByRole("button", { name: /固定时间/ });
  await user.click(question);
  expect(question).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByText("星期一 09:05")).toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "其他位置" }));
  expect(question).toHaveAttribute("aria-pressed", "false");
  expect(screen.queryByText("星期一 09:05")).not.toBeInTheDocument();
});

test("refreshes a pinned label when the browser-local date rolls over", () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date(2026, 6, 29, 23, 59, 59, 900));
  const askedAt = new Date(2026, 6, 29, 9, 5).toISOString();
  render(
    <ChatQuestion
      question="跨过午夜"
      askedAt={askedAt}
    />,
  );

  const question = screen.getByRole("button", { name: /跨过午夜/ });
  fireEvent.click(question);
  expect(screen.getByText("09:05")).toBeInTheDocument();

  act(() => vi.advanceTimersByTime(200));
  expect(screen.getByText("星期三 09:05")).toBeInTheDocument();
});
