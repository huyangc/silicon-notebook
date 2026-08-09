// 「重新合并」/「补上关联」终态刷新 vs 释放忙碌位的时序回归测试（双 opus 评审 P1）。
//
// page.tsx 太大（8500+ 行、几十个模块级 API 依赖），没有现成的整页挂载测试基础设施，
// 直接 `render(<Home />)` 不现实。这个文件因此不挂载 page.tsx，而是用一个**逐字镜像**
// page.tsx 里 finish() 那段选择结构的最小组件，把「release 在刷新之前 / 之后」这条
// 因果机制单独钉住——它验证的是这套时序编排本身，不是 page.tsx 的具体像素；page.tsx
// 侧的真实回归由 kg-relink-wiring-guard.test.mjs / kg-rebuild-wiring-guard.test.mjs 的
// 源码顺序断言覆盖（那两条直接读 page.tsx 源码，已用变异验证钉过）。
//
// 复现的因果链（两处轮询 effect 逐字同构）：
//   release 在刷新之前 → setState 改了 effect 的依赖 → React 对上一次 effect 实例跑
//   cleanup → cleanup 把这次刷新自己闭包里的 `cancelled` 置 true → 异步 fetch 回来时
//   守卫直接把 setState 整段丢掉。
//   release 在刷新之后（且守卫改用不受 cleanup 影响的 ref）→ 不会被自己取消。
//
// 两个结构细节必须同时到位，否则测试本身就是假的：
//   ① finish() 必须由一次**真实的宏任务**（镜像 page.tsx 的 `window.setInterval` 轮询）
//     触发，而不是在挂载时同步跑在 `render()` 自己的 act() 里——挂载期间的 act() 会把
//     setState 与 cleanup 同步flush 完，此时即使 fetch 立即 resolve、bug 也“恰好”复现，
//     这是一个与真实场景无关的假信号（见下面第三个测试的反例）。
//   ② mock 的 fetch 延迟必须 **≥5ms**：evaluator 实测 0ms/1ms 立即 resolve 会让 Promise
//     的 microtask 抢在 React 处理完 setState/cleanup 的宏任务之前 resolve，「release 在
//     刷新之前」的坏代码反而看起来正确（假绿）；只有 5ms/20ms 这种真正跨到下一个宏任务
//     边界的延迟才会让 cleanup 先跑、暴露丢失。这里用 20ms（回归测试）与 0ms（反例，
//     证明为什么不能用立即 resolve 的 mock）。
import { render, screen, waitFor } from "@testing-library/react";
import { useEffect, useRef, useState } from "react";
import { expect, test, vi } from "vitest";

type Mode = "release-before-refresh" | "release-after-refresh";

function FinishRaceHarness({
  mode,
  fetchGraph,
}: {
  mode: Mode;
  fetchGraph: () => Promise<string>;
}) {
  // 镜像 rebuildingNotebookIds/relinkingNotebookIds 命中当前库时的忙碌位：挂载即认领。
  const [busy, setBusy] = useState(true);
  const [graph, setGraph] = useState("stale");
  // 镜像 activeNotebookIdRef —— 不受 effect cleanup 影响的守卫。
  const activeRef = useRef("nb-1");

  useEffect(() => {
    if (!busy) return;
    let cancelled = false;
    const finish = () => {
      if (mode === "release-before-refresh") {
        // 修复前的形态：release 在 await 之前。
        setBusy(false);
        void (async () => {
          const g = await fetchGraph();
          if (cancelled) return; // 被自己触发的 cleanup 提前置真
          setGraph(g);
        })();
      } else {
        // 修复后的形态：release 在 await 之后，守卫用 ref 不用局部 cancelled。
        void (async () => {
          const g = await fetchGraph();
          if (activeRef.current !== "nb-1") return;
          setGraph(g);
          setBusy(false);
        })();
      }
    };
    // 镜像 page.tsx 的 `window.setInterval(poll, 3000)`：finish 由一次真实的宏任务
    // 触发，不在挂载时同步跑——否则挂载自己的 act() 会把 bug 的时序前提冲掉（见下面
    // 「0ms 延迟会假绿」那条测试的对照）。
    const timer = window.setTimeout(finish, 0);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [busy, mode, fetchGraph]);

  return (
    <div>
      <div data-testid="graph">{graph}</div>
      <div data-testid="busy">{String(busy)}</div>
    </div>
  );
}

function delayedFetch(delayMs: number) {
  return vi.fn(
    () =>
      new Promise<string>((resolve) => {
        if (delayMs <= 0) {
          resolve("fresh");
          return;
        }
        setTimeout(() => resolve("fresh"), delayMs);
      }),
  );
}

test("release 在刷新之前：终态刷新被自己触发的 cleanup 取消（20ms 延迟下必现）", async () => {
  const fetchGraph = delayedFetch(20);
  render(<FinishRaceHarness mode="release-before-refresh" fetchGraph={fetchGraph} />);

  // 忙碌位应声解除（release 在最开始就跑了）。
  await waitFor(() => expect(screen.getByTestId("busy").textContent).toBe("false"));
  // 但真正的刷新结果永远丢失 —— 等到远超 fetch 延迟之后仍是 stale。
  await new Promise((resolve) => setTimeout(resolve, 80));
  expect(screen.getByTestId("graph").textContent).toBe("stale");
  expect(fetchGraph).toHaveBeenCalledTimes(1);
});

test("release 在刷新之后：终态结果被正确应用，随后才解除忙碌位（20ms 延迟下同样成立）", async () => {
  const fetchGraph = delayedFetch(20);
  render(<FinishRaceHarness mode="release-after-refresh" fetchGraph={fetchGraph} />);

  await waitFor(() => expect(screen.getByTestId("graph").textContent).toBe("fresh"));
  await waitFor(() => expect(screen.getByTestId("busy").textContent).toBe("false"));
  expect(fetchGraph).toHaveBeenCalledTimes(1);
});

test("反例（不是回归门本体）：0ms 立即 resolve 的 mock 会把上面第一条测试的 bug 掩盖成假绿", async () => {
  // 这条测试记录的是「为什么不能用 mockResolvedValue/立即 resolve 的 mock」这条约束本身
  // ——0ms 时 Promise 的 microtask 抢在 React 处理 setState/cleanup 的宏任务之前完成，
  // 「release 在刷新之前」的坏代码反而表现得像是对的。上面两条回归测试因此必须使用
  // ≥5ms 的延迟（这里用 20ms），这条测试只是把「为什么」钉成一个可执行的事实。
  const fetchGraph = delayedFetch(0);
  render(<FinishRaceHarness mode="release-before-refresh" fetchGraph={fetchGraph} />);
  await new Promise((resolve) => setTimeout(resolve, 30));
  expect(screen.getByTestId("graph").textContent).toBe("fresh"); // 假绿，如实记录
});
