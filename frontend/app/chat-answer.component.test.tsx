import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test } from "vitest";

import { ChatAnswer } from "./chat-answer";

test("shows the locally formatted answer time on hover", () => {
  render(
    <ChatAnswer
      answeredAt="2026-07-27T09:05:00"
      now={new Date(2026, 6, 29, 12, 0)}
    >
      <p>模型回答正文</p>
    </ChatAnswer>,
  );

  const answer = screen.getByText("模型回答正文").closest(".chat-assistant-message");
  expect(answer).not.toBeNull();
  const timeToggle = screen.getByRole("button", { name: "回答时间 星期一 09:05" });
  expect(timeToggle).not.toHaveClass("visible");
  fireEvent.pointerEnter(answer as HTMLElement);
  expect(timeToggle).toHaveClass("visible");
  fireEvent.pointerLeave(answer as HTMLElement);
  expect(timeToggle).not.toHaveClass("visible");
});

test("pins on answer-content click and dismisses on an outside click", async () => {
  const user = userEvent.setup();
  render(
    <div>
      <ChatAnswer
        answeredAt="2026-07-29T09:05:00"
        now={new Date(2026, 6, 29, 12, 0)}
      >
        <p>可固定的回答</p>
      </ChatAnswer>
      <button type="button">其他位置</button>
    </div>,
  );

  await user.click(screen.getByText("可固定的回答"));
  const timeToggle = screen.getByRole("button", { name: "回答时间 09:05" });
  expect(timeToggle).toHaveAttribute("aria-pressed", "true");
  expect(timeToggle).toHaveClass("visible");

  await user.click(screen.getByRole("button", { name: "其他位置" }));
  expect(timeToggle).toHaveAttribute("aria-pressed", "false");
  expect(timeToggle).not.toHaveClass("visible");
});

test("the answer-time toggle supports Tab, keyboard activation, and Escape", async () => {
  const user = userEvent.setup();
  render(
    <ChatAnswer
      answeredAt="2026-07-29T09:05:00"
      now={new Date(2026, 6, 29, 12, 0)}
    >
      <p>键盘可访问的回答</p>
    </ChatAnswer>,
  );

  const timeToggle = screen.getByRole("button", { name: "回答时间 09:05" });
  expect(timeToggle).toHaveAttribute("aria-pressed", "false");

  await user.tab();
  expect(timeToggle).toHaveFocus();
  expect(timeToggle).toHaveClass("visible");

  await user.keyboard("{Enter}");
  expect(timeToggle).toHaveAttribute("aria-pressed", "true");

  await user.keyboard("{Escape}");
  expect(timeToggle).toHaveAttribute("aria-pressed", "false");

  await user.keyboard(" ");
  expect(timeToggle).toHaveAttribute("aria-pressed", "true");
});

test("nested answer controls keep their own action and do not pin the time", async () => {
  const user = userEvent.setup();
  let nestedClicks = 0;
  render(
    <ChatAnswer
      answeredAt="2026-07-29T09:05:00"
      now={new Date(2026, 6, 29, 12, 0)}
    >
      <button type="button" onClick={() => { nestedClicks += 1; }}>
        有用
      </button>
    </ChatAnswer>,
  );

  await user.click(screen.getByRole("button", { name: "有用" }));

  expect(nestedClicks).toBe(1);
  expect(screen.getByRole("button", { name: "回答时间 09:05" }))
    .toHaveAttribute("aria-pressed", "false");
});
