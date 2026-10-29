"use client";

import { useEffect } from "react";

/** 打印/存 PDF 时必须展开的折叠：报告正文的参考文献、分享页的引用出处清单。 */
export const PRINT_EXPANDED_FOLDS = "details.report-references-fold, details.public-references-fold";

/**
 * 打印前把默认收起的引用折叠全部展开，打印后只收回本次展开的那几个。
 *
 * 收起的 `<details>` 内容不进打印输出：读者没手动点开就打印/存 PDF，摘录与参考文献会
 * 整段消失（分享页常被打印存档）。读者自己点开的那些保持原样——屏幕上的状态不被打印
 * 改动。globals.css 的 `@media print` 另有一条 `::details-content` 兜底。
 */
export function usePrintExpandedFolds(): void {
  useEffect(() => {
    let opened: HTMLDetailsElement[] = [];
    const expand = () => {
      opened = Array.from(document.querySelectorAll<HTMLDetailsElement>(PRINT_EXPANDED_FOLDS))
        .filter((fold) => !fold.open);
      for (const fold of opened) fold.open = true;
    };
    const restore = () => {
      for (const fold of opened) fold.open = false;
      opened = [];
    };
    window.addEventListener("beforeprint", expand);
    window.addEventListener("afterprint", restore);
    return () => {
      window.removeEventListener("beforeprint", expand);
      window.removeEventListener("afterprint", restore);
    };
  }, []);
}
