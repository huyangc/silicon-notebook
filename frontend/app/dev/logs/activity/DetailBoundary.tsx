"use client";
// 右栏详情的渲染兜底。
//
// 这一页的职责恰恰是渲染**任意历史** payload：`ActivityDetail` 里 asAnswer/asTrace
// 只做了最小的形状确认（`as` 断言，不是校验），一条早年写坏的答案完全可能在
// AnswerView 深处抛出来。没有兜底的话整页白屏——而白屏正好发生在管理员最需要看
// 那条坏记录的时候。所以这里只保住「其余两栏还能用、并且明说这一条渲染不了」，
// 不试图修复内容。
//
// 用类组件是因为 React 至今没有 hook 形态的 error boundary。

import { Component, type ReactNode } from "react";

import { logDiagnostic } from "../../../errors.ts";

type Props = {
  /** 换一条选中项就换一个 key，让边界重新挂载（否则一次失败会把右栏钉死）。 */
  resetKey: string;
  children: ReactNode;
};

export class ActivityDetailBoundary extends Component<Props, { failedKey: string }> {
  state = { failedKey: "" };

  static getDerivedStateFromError() {
    return { failedKey: "__failed__" };
  }

  componentDidCatch(error: unknown) {
    // 原始异常只进 console 诊断通道，绝不上屏（同 errors.ts 的既有分工）。
    logDiagnostic("activity-detail", error);
  }

  componentDidUpdate(previous: Props) {
    if (previous.resetKey !== this.props.resetKey && this.state.failedKey) {
      this.setState({ failedKey: "" });
    }
  }

  render() {
    if (this.state.failedKey) {
      return (
        <div className="empty">
          这条记录的内容没能显示出来，可能是早期写入的旧格式。换一条试试，或到「模型调用」视图查看当时的原始记录。
        </div>
      );
    }
    return this.props.children;
  }
}
