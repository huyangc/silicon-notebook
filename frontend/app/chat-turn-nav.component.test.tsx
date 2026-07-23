import { useRef } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeAll, describe, expect, test, vi } from "vitest";

import { ChatTurnNav } from "./chat-turn-nav";
import { chatTurnDomId } from "./chat-turn-nav-model";

beforeAll(() => {
  // jsdom 未实现 scrollIntoView，桩掉以便断言被调用。
  Element.prototype.scrollIntoView = vi.fn();
});

function Harness({ questions }: { questions: string[] }) {
  const ref = useRef<HTMLDivElement>(null);
  return (
    <div>
      <div ref={ref} style={{ overflow: "auto" }}>
        {questions.map((q, i) => (
          <div id={chatTurnDomId(i)} key={i} className="chat-turn">
            <div className="chat-user">{q}</div>
            <div className="chat-assistant">回答 {i + 1}</div>
          </div>
        ))}
      </div>
      <ChatTurnNav questions={questions} scrollRef={ref} />
    </div>
  );
}

describe("ChatTurnNav", () => {
  test("只有一条提问时不渲染导航", () => {
    render(<Harness questions={["只有一个问题"]} />);
    expect(
      screen.queryByRole("navigation", { name: "本次会话的提问" }),
    ).not.toBeInTheDocument();
  });

  test("逐条列出提问，点击后跳转、切换高亮并给气泡加动画", async () => {
    const user = userEvent.setup();
    const questions = ["第一个问题", "第二个问题", "第三个问题"];
    render(<Harness questions={questions} />);

    const nav = screen.getByRole("navigation", { name: "本次会话的提问" });
    expect(nav).toBeInTheDocument();
    for (const q of questions) {
      expect(screen.getByRole("button", { name: q })).toBeInTheDocument();
    }

    const firstTurn = document.getElementById(chatTurnDomId(0));
    await user.click(screen.getByRole("button", { name: "第一个问题" }));

    expect(Element.prototype.scrollIntoView).toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "第一个问题" })).toHaveAttribute(
      "aria-current",
      "true",
    );
    expect(screen.getByRole("button", { name: "第二个问题" })).not.toHaveAttribute(
      "aria-current",
    );
    expect(firstTurn?.querySelector(".chat-user")).toHaveClass("chat-user-flash");
  });

  test("reduced-motion 下点击跳转改用瞬时滚动", async () => {
    const user = userEvent.setup();
    const w = window as unknown as { matchMedia?: unknown };
    const prev = w.matchMedia;
    w.matchMedia = vi.fn().mockImplementation((query: string) => ({
      matches: query.includes("reduce"),
      media: query,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      addListener: () => undefined,
      removeListener: () => undefined,
      onchange: null,
      dispatchEvent: () => false,
    }));
    try {
      render(<Harness questions={["一", "二", "三"]} />);
      await user.click(screen.getByRole("button", { name: "二" }));
      expect(Element.prototype.scrollIntoView).toHaveBeenCalledWith(
        expect.objectContaining({ behavior: "auto" }),
      );
    } finally {
      w.matchMedia = prev;
    }
  });
});
