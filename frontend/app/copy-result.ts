/**
 * copy-result.ts
 *
 * 复制类按钮的**结果**态。
 *
 * 与按下态的分工:按下态由 globals.css 里那条全站的
 * `button:not(:disabled):not([aria-disabled="true"]):active` 兜底,只回答「点上了没有」,
 * 松手由 `:active` 自动还原。结果(复制成没成)是另一回事——它是 JS 状态,**不会**自己
 * 还原,忘了摘掉就一直挂着「已复制」,下一次点击反而看不出变化。所以「置位 + 到点回
 * idle」收在这里一处,让群组邀请链接、笔记本分享链接、报告分享链接共用同一份计时与语义。
 *
 * 刻意**不**做成组件,只做 hook,有两个理由:
 *  - 结果的 class 名必须以**字面量**留在各自的 JSX 里(`"new-pill copy-result-copied"`
 *    这种)。包进组件、或用 `` `${base} ${modifier(result)}` `` 拼,className 采集就只
 *    看得见一个变量,group-page-style-guard 与 button-press-feedback-guard 当场空转。
 *  - 三处的壳层本来就不一样:黑色药丸 `.new-pill`、描边 `.sort-button`、带图标的
 *    `.report-action`,连忙碌态文案都各说各的。共用的只有这个状态机。
 *
 * `key` 是给列表用的:「已分享」弹窗里每行一颗复制按钮,共用一个无 key 的状态会让整列
 * 一起变绿。单按钮的调用点传一个固定串即可。
 */

import { useCallback, useEffect, useState } from "react";

export type CopyResult = "idle" | "copied" | "failed";

/** 结果态在按钮上停留多久。够读完两个字,又短到不会盖住下一次点击的反馈。 */
export const COPY_RESULT_HOLD_MS = 1600;

export function useCopyResult(): {
  /** 记下某颗按钮这一次复制成没成。 */
  report: (key: string, copied: boolean) => void;
  /** 该按钮当前该显示的结果;别的按钮报的结果对它恒为 idle。 */
  resultFor: (key: string) => CopyResult;
} {
  const [state, setState] = useState<{ key: string; result: CopyResult }>({
    key: "",
    result: "idle",
  });

  useEffect(() => {
    if (state.result === "idle") return;
    const timer = window.setTimeout(
      () => setState({ key: "", result: "idle" }),
      COPY_RESULT_HOLD_MS,
    );
    return () => window.clearTimeout(timer);
  }, [state]);

  const report = useCallback((key: string, copied: boolean) => {
    setState({ key, result: copied ? "copied" : "failed" });
  }, []);

  const resultFor = useCallback(
    (key: string): CopyResult => (state.key === key ? state.result : "idle"),
    [state],
  );

  return { report, resultFor };
}
