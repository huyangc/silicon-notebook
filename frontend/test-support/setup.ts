import "@testing-library/jest-dom/vitest";
import { cleanup, configure } from "@testing-library/react";
import { afterEach } from "vitest";

// Node ≥ 24 自带 Web Storage 全局(`localStorage`/`sessionStorage`),而它们的 getter
// 在没给 `--localstorage-file` 时**返回 undefined**(只打一条 ExperimentalWarning)。
// vitest 的 jsdom 环境按「globalThis 上已有同名 key 就跳过」的规则填充全局,于是
// Node 那份内建 getter 胜出、jsdom 自己那份**根本没被装上**:`"localStorage" in window`
// 为真、描述符是 get/set,取值却是 undefined,`delete` 掉也不会从原型链上露出来
// (原型是普通 Object)。结果是任何读 `localStorage` 的组件在 Node 26 上当场抛
// 「Cannot read properties of undefined」,而同一份代码在 CI 钉的 Node 22 上全绿——
// 一个只在开发者本机现形、CI 永远看不到的静默分歧。
//
// 修法是补一份**真的** jsdom Storage,而不是手搓 polyfill:配额、`key()`/`length`
// 语义和 Storage 原型都保持 jsdom 原样,免得测试在两个 Node 版本上对着两套语义写。
// 取法是一个即用即弃的同源 iframe——它是独立 realm,`contentWindow` 上的 Storage 由
// jsdom 自己装(不受本 realm 被 Node 抢占的影响)。刻意**不** `import { JSDOM }`:那需要
// 另装 `@types/jsdom`,否则 `next build` 的类型检查当场红(实测过);iframe 只用 DOM API。
// 取到对象后立刻把 iframe 摘掉——Storage 挂在那个 realm 的存储区上,摘掉后读写与
// `instanceof` 都照常(实测过),所以文档树里不留任何残留节点。
//
// 判据是「取值为 undefined」而不是 Node 版本号,所以 Node 22 上这段整个不执行、行为
// 逐字不变;将来 Node 把内建 Storage 修成默认可用,它也会自动让路。属性写成
// configurable + writable,`vi.stubGlobal` 与 `vi.spyOn(...)` 照常能改。
function ensureWebStorage(): void {
  const target = globalThis as unknown as Record<string, unknown>;
  const missing = (["localStorage", "sessionStorage"] as const).filter(
    (key) => typeof target[key] === "undefined",
  );
  if (missing.length === 0) return;

  const frame = document.createElement("iframe");
  document.documentElement.appendChild(frame);
  const donor = frame.contentWindow;
  if (!donor) {
    frame.remove();
    throw new Error(
      "组件测试环境拿不到可用的 Web Storage:临时 iframe 没有 contentWindow",
    );
  }
  // `Storage` 类必须跟着实例一起补:测试用的是 `vi.spyOn(Storage.prototype, "setItem")`
  // (打类而不是打实例,这样两个 storage 一起断掉)。只补实例的话,全局 `Storage` 还是
  // Node 内建那个类,而实例的原型在 donor realm 里——spy 打在一个谁都不用的原型上,
  // **静默失效**:被测代码的 setItem 照常成功,于是「落盘失败」这类用例反过来测了个空。
  // 走索引而不是 `donor.Storage`:lib.dom 把 `Storage` 构造器声明在 globalThis 上、
  // 不在 `Window` 接口里,点号取值过不了 `next build` 的类型检查。
  const donorGlobals = donor as unknown as Record<string, unknown>;
  const replacements: Array<[string, unknown]> = [...missing, "Storage"].map(
    (key) => [key, donorGlobals[key]],
  );
  frame.remove();

  for (const [key, value] of replacements) {
    Object.defineProperty(target, key, {
      value,
      configurable: true,
      writable: true,
      enumerable: true,
    });
  }
}

ensureWebStorage();

// CI 把前端泳道与后端 pytest、契约泳道**并发**跑在同一台 runner 上,CPU 争用下
// 本地 <1s 完成的组件渲染会被拖到数秒。testing-library 的 findBy*/waitFor 默认
// asyncUtilTimeout 只有 1000ms,于是最重的用例(如 knowhow-row-completion 的整条
// 审阅补全流程)会在渲染尚未完成时超时——报「Unable to find …」的负载敏感 flake
// (本地单跑/全量跑都稳过,只在 CI 三泳道并发下现形)。放宽异步等待上限,让
// findBy*/waitFor 容忍「慢但正确」的渲染,而不是与它赛跑。
configure({ asyncUtilTimeout: 5000 });

afterEach(cleanup);
