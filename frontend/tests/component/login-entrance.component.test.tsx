import { StrictMode } from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { LoginEntrance } from "../../app/auth-book";
import { consumeLoginEntrance, rememberLoginEntrance } from "../../app/login-entrance";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  sessionStorage.clear();
});

test("the cue belongs to the successful account and is consumed only once", () => {
  expect(consumeLoginEntrance("u1")).toBe(false);
  rememberLoginEntrance("u1");
  expect(consumeLoginEntrance("u1")).toBe(true);
  expect(consumeLoginEntrance("u1")).toBe(false);
  rememberLoginEntrance("u1");
  expect(consumeLoginEntrance("u2")).toBe(false);
  expect(consumeLoginEntrance("u1")).toBe(false);
});

test("storage failure cannot stop sign-in", () => {
  const browser = window;
  vi.stubGlobal("window", new Proxy(browser, { get(target, key) {
    if (key === "sessionStorage") throw new Error("unavailable");
    return Reflect.get(target, key);
  } }));
  expect(() => rememberLoginEntrance("u1")).not.toThrow();
  expect(consumeLoginEntrance("u1")).toBe(false);
});

test("the reveal animation releases entry; unrelated animation events do not", () => {
  const onComplete = vi.fn();
  render(<LoginEntrance onComplete={onComplete} />);
  const overlay = screen.getByRole("status").closest(".auth-book-entrance")!;
  fireEvent.animationEnd(overlay, { animationName: "auth-book-open" });
  expect(onComplete).not.toHaveBeenCalled();
  // jsdom does not provide AnimationEvent, so explicitly carry its public name.
  const end = new Event("animationend", { bubbles: true });
  Object.defineProperty(end, "animationName", { value: "auth-book-reveal" });
  fireEvent(overlay, end);
  expect(onComplete).toHaveBeenCalledOnce();
});

test("entry is released even without animation events, including StrictMode replay", () => {
  vi.useFakeTimers();
  const onComplete = vi.fn();
  render(<StrictMode><LoginEntrance onComplete={onComplete} /></StrictMode>);
  expect(onComplete).not.toHaveBeenCalled();
  act(() => vi.runAllTimers());
  expect(onComplete).toHaveBeenCalledOnce();
});

test("reduced motion enters immediately without waiting for an animation", () => {
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: true })));
  const onComplete = vi.fn();
  render(<LoginEntrance onComplete={onComplete} />);
  expect(onComplete).toHaveBeenCalledOnce();
});

test("enabling reduced motion during the transition releases entry", () => {
  const preference = { matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() };
  vi.stubGlobal("matchMedia", vi.fn(() => preference));
  const onComplete = vi.fn();
  const view = render(<LoginEntrance onComplete={onComplete} />);
  preference.matches = true;
  act(() => preference.addEventListener.mock.calls[0][1]());
  expect(onComplete).toHaveBeenCalledOnce();
  view.unmount();
  expect(preference.removeEventListener).toHaveBeenCalledWith("change", expect.any(Function));
});

test("unmounting cancels the fallback", () => {
  vi.useFakeTimers();
  const onComplete = vi.fn();
  const view = render(<LoginEntrance onComplete={onComplete} />);
  view.unmount();
  act(() => vi.runAllTimers());
  expect(onComplete).not.toHaveBeenCalled();
});
