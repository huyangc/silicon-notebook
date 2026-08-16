// 回归门:组件测试环境里的 Web Storage 必须真的可用,且 `Storage` 类与实例同源。
//
// 真机踩过(开发者本机 Node 26.7,CI 钉 Node 22 看不见):Node ≥ 24 自带
// `localStorage`/`sessionStorage` 全局,没给 `--localstorage-file` 时它们的 getter
// 返回 **undefined**;vitest 的 jsdom 环境按「globalThis 上已有同名 key 就跳过」填充
// 全局,于是 Node 那份胜出、jsdom 自己那份根本没装上。任何读 localStorage 的组件
// 当场抛「Cannot read properties of undefined」——本机 5 条用例红,CI 全绿。
//
// 第二条断言(spy 能拦住实例的 setItem)是另一半、且更阴:补实例而不补 `Storage` 类时,
// `vi.spyOn(Storage.prototype, "setItem")` 打的是 Node 内建那个类的原型,而实例的原型
// 在补进来的那个 realm 里。spy **静默失效**,于是所有「落盘失败」类用例都在测空气——
// 它们照样绿,只是什么都没验证。修复见 `test-support/setup.ts` 的 `ensureWebStorage`。
import { expect, test, vi } from "vitest";

test("localStorage / sessionStorage 在组件环境里真的可用", () => {
  for (const storage of [window.localStorage, window.sessionStorage]) {
    expect(storage).toBeTypeOf("object");
    storage.setItem("probe", "v1");
    expect(storage.getItem("probe")).toBe("v1");
    expect(storage.length).toBeGreaterThan(0);
    storage.removeItem("probe");
    expect(storage.getItem("probe")).toBeNull();
    storage.clear();
  }
});

test("全局 Storage 类与 storage 实例同源(spy 打得中)", () => {
  expect(window.localStorage).toBeInstanceOf(Storage);

  // 「断掉落盘」的标准写法:打类,两个 storage 一起断。它必须真的拦得住实例。
  const spy = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("quota exceeded");
  });
  expect(() => window.localStorage.setItem("k", "v")).toThrow("quota exceeded");
  expect(spy).toHaveBeenCalledTimes(1);
  spy.mockRestore();

  window.localStorage.setItem("k", "v");
  expect(window.localStorage.getItem("k")).toBe("v");
  window.localStorage.clear();
});
