import { act, cleanup, render } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { useSessionSidebarMorph } from "../../app/use-session-sidebar-morph.ts";

// 会话栏 ⇄「历史」按钮的形变:jsdom 没有布局也没有 Web Animations,这里桩出两者,
// 只钉「起点/终点量对了、影子在动画结束后消失、按钮落定时轻跳」这几条接线。
const TOGGLE = { left: 900, top: 90, width: 80, height: 34 };
const SIDEBAR = { left: 24, top: 90, width: 248, height: 700 };

type Call = { el: Element; frames: Keyframe[]; options: KeyframeAnimationOptions; animation: { onfinish: (() => void) | null; cancel: () => void } };
let calls: Call[] = [];

function stubRect(el: Element, rect: typeof TOGGLE) {
  el.getBoundingClientRect = () => ({ ...rect, right: rect.left + rect.width, bottom: rect.top + rect.height, x: rect.left, y: rect.top, toJSON: () => rect }) as DOMRect;
}

beforeEach(() => {
  calls = [];
  Element.prototype.animate = vi.fn(function (this: Element, frames: Keyframe[], options: KeyframeAnimationOptions) {
    const animation = { onfinish: null as (() => void) | null, cancel: vi.fn() };
    calls.push({ el: this, frames, options, animation });
    return animation as unknown as Animation;
  }) as unknown as Element["animate"];
  window.matchMedia = vi.fn(() => ({ matches: false })) as unknown as typeof window.matchMedia;
});

afterEach(() => {
  cleanup();
  delete (Element.prototype as Partial<Element>).animate;
});

let toggleSidebar: (next: boolean) => void = () => {};

function Harness({ initial }: { initial: boolean }) {
  const [shown, setShown] = useState(initial);
  const morph = useSessionSidebarMorph(shown);
  toggleSidebar = (next) => { morph.capture(); setShown(next); };
  return (
    <div>
      {shown ? (
        <aside data-testid="sidebar" ref={(el) => { morph.sidebarRef.current = el; if (el) stubRect(el, SIDEBAR); }} />
      ) : (
        <button className="chat-session-toggle" ref={(el) => { if (el) stubRect(el, TOGGLE); }}>历史</button>
      )}
      {morph.ghost && <div data-testid="ghost" ref={morph.ghostRef} style={morph.ghost} />}
    </div>
  );
}

test("恢复来源栏:会话栏的影子从原位缩进历史按钮，结束后消失、按钮轻跳", () => {
  const { queryByTestId, getByText } = render(<Harness initial />);
  act(() => toggleSidebar(false));

  const ghost = queryByTestId("ghost")!;
  expect(ghost).not.toBeNull();
  expect(ghost.style.left).toBe("24px");
  expect(ghost.style.height).toBe("700px");
  expect(calls).toHaveLength(1);
  expect(calls[0].el).toBe(ghost);
  expect(Number(calls[0].options.duration)).toBeGreaterThanOrEqual(500);
  expect(Number(calls[0].frames[1].opacity)).toBeGreaterThanOrEqual(0.5);
  expect(calls[0].frames[1].transform).toBe(
    `translate(876px, 0px) scale(${80 / 248}, ${34 / 700})`,
  );

  act(() => calls[0].animation.onfinish?.());
  expect(queryByTestId("ghost")).toBeNull();
  expect(calls).toHaveLength(2);
  expect(calls[1].el).toBe(getByText("历史"));
  expect(calls[1].frames.some((frame) => frame.boxShadow)).toBe(true);
});

test("收起来源栏:会话栏从历史按钮的位置长出来", () => {
  const { getByTestId } = render(<Harness initial={false} />);
  act(() => toggleSidebar(true));

  expect(calls).toHaveLength(1);
  expect(calls[0].el).toBe(getByTestId("sidebar"));
  expect(calls[0].frames[0].transform).toBe(
    `translate(876px, 0px) scale(${80 / 248}, ${34 / 700})`,
  );
  expect(calls[0].frames[1].transform).toBe("none");
});

test("减弱动态效果时直接切换，不留影子", () => {
  window.matchMedia = vi.fn(() => ({ matches: true })) as unknown as typeof window.matchMedia;
  const { queryByTestId } = render(<Harness initial />);
  act(() => toggleSidebar(false));
  expect(queryByTestId("ghost")).toBeNull();
  expect(calls).toHaveLength(0);
});

test("快速重新展开会话栏取消旧收起动画和影子", () => {
  const { queryByTestId } = render(<Harness initial />);
  act(() => toggleSidebar(false));
  const previous = calls[0].animation;
  act(() => toggleSidebar(true));
  expect(queryByTestId("ghost")).toBeNull();
  expect(previous.cancel).toHaveBeenCalled();
  expect(previous.onfinish).toBeNull();
});
