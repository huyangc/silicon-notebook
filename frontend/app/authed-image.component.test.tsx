// 双评审 P1-3: AuthedImage 改成可见性触发的懒加载(IntersectionObserver),覆盖:
//   1. IntersectionObserver 不可用(如 jsdom 测试环境)时立即加载,不等可见性——
//      这条回退路径本身要有专门用例,不能只是"jsdom 恰好没有 IO 所以现有测试
//      顺带跑过去了"。
//   2. IO 可用时:进入视口前不发鉴权请求,进入视口后才触发。
//   3. 卸载时 observer 断开(尚未可见)/ blob URL 回收(已加载完成),两条路径
//      都不能回归成悬挂的 observer 或泄漏的 object URL。
import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

vi.mock("./source-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./source-api")>();
  return {
    ...actual,
    fetchInternalAssetBlob: vi.fn(async () => new Blob(["fake-bytes"], { type: "image/png" })),
  };
});

import { fetchInternalAssetBlob } from "./source-api";
import { AuthedImage } from "./authed-image";

// jsdom 没有真实的 IntersectionObserver;这里自己装一个可控 mock,专门覆盖"IO
// 可用"这条分支——与"IO 不可用"分支各自独立断言,互不依赖对方的环境假设。
class MockIntersectionObserver {
  static instances: MockIntersectionObserver[] = [];
  callback: IntersectionObserverCallback;
  disconnectSpy = vi.fn();
  observeSpy = vi.fn();

  constructor(callback: IntersectionObserverCallback) {
    this.callback = callback;
    MockIntersectionObserver.instances.push(this);
  }

  observe(target: Element) {
    this.observeSpy(target);
  }

  unobserve() {}

  disconnect() {
    this.disconnectSpy();
  }

  trigger(isIntersecting: boolean) {
    this.callback(
      [{ isIntersecting } as IntersectionObserverEntry],
      this as unknown as IntersectionObserver,
    );
  }
}

beforeEach(() => {
  MockIntersectionObserver.instances = [];
  if (typeof URL.createObjectURL !== "function") {
    URL.createObjectURL = vi.fn(() => "blob:mock-url");
  }
  if (typeof URL.revokeObjectURL !== "function") {
    URL.revokeObjectURL = vi.fn();
  }
});

afterEach(() => {
  vi.unstubAllGlobals();
});


test("IntersectionObserver 不可用(如 jsdom)时立即加载,不等可见性", async () => {
  expect(typeof (globalThis as { IntersectionObserver?: unknown }).IntersectionObserver).toBe("undefined");
  render(<AuthedImage url="/assets/1" alt="test" />);
  await screen.findByRole("img");
  expect(fetchInternalAssetBlob).toHaveBeenCalledWith("/assets/1");
});


test("IntersectionObserver 可用时:进入视口前不发请求", () => {
  vi.stubGlobal("IntersectionObserver", MockIntersectionObserver as unknown as typeof IntersectionObserver);
  render(<AuthedImage url="/assets/2" alt="test" />);
  expect(screen.getByText("图片加载中…")).toBeInTheDocument();
  expect(fetchInternalAssetBlob).not.toHaveBeenCalled();
  expect(MockIntersectionObserver.instances).toHaveLength(1);
  expect(MockIntersectionObserver.instances[0].observeSpy).toHaveBeenCalled();
});


test("IntersectionObserver 可用时:进入视口后才触发加载", async () => {
  vi.stubGlobal("IntersectionObserver", MockIntersectionObserver as unknown as typeof IntersectionObserver);
  render(<AuthedImage url="/assets/3" alt="test" />);
  MockIntersectionObserver.instances[0].trigger(true);
  await screen.findByRole("img");
  expect(fetchInternalAssetBlob).toHaveBeenCalledWith("/assets/3");
});


test("未进入视口前的 not-intersecting 回调不触发加载(只有 isIntersecting=true 才算数)", () => {
  vi.stubGlobal("IntersectionObserver", MockIntersectionObserver as unknown as typeof IntersectionObserver);
  render(<AuthedImage url="/assets/4" alt="test" />);
  MockIntersectionObserver.instances[0].trigger(false);
  expect(fetchInternalAssetBlob).not.toHaveBeenCalled();
  expect(screen.getByText("图片加载中…")).toBeInTheDocument();
});


test("卸载时(尚未可见):断开 observer,不留下悬挂的观察者", () => {
  vi.stubGlobal("IntersectionObserver", MockIntersectionObserver as unknown as typeof IntersectionObserver);
  const { unmount } = render(<AuthedImage url="/assets/5" alt="test" />);
  const observer = MockIntersectionObserver.instances[0];
  unmount();
  expect(observer.disconnectSpy).toHaveBeenCalledTimes(1);
});


test("卸载时(已加载完成):回收 object URL,不泄漏 blob 引用", async () => {
  vi.stubGlobal("IntersectionObserver", MockIntersectionObserver as unknown as typeof IntersectionObserver);
  const revokeSpy = vi.fn();
  URL.revokeObjectURL = revokeSpy;
  const { unmount } = render(<AuthedImage url="/assets/6" alt="test" />);
  MockIntersectionObserver.instances[0].trigger(true);
  await screen.findByRole("img");
  unmount();
  expect(revokeSpy).toHaveBeenCalled();
});


test("加载失败时显示失败文案,而不是无限期停在加载中", async () => {
  vi.mocked(fetchInternalAssetBlob).mockRejectedValueOnce(new Error("network"));
  render(<AuthedImage url="/assets/7" alt="test" />);
  await screen.findByText("图片加载失败");
});
