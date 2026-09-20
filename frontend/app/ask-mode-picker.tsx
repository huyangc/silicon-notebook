/**
 * ask-mode-picker.tsx
 *
 * 问答的引擎选择器(`.ask-mode-control`):分组页签(通用问答 / 深入分析 / 扩展功能)
 * + 扩展组的引擎子选择 + 「逐步推理」的检索档位。
 *
 * **只有这一份实现**。笔记本内问答(page.tsx)与全局问答(ask/global-ask-workspace)
 * 共用它:两个面的引擎控件一旦分家,改一边就只改一半,而界面上看不出来(同
 * CitationPopover 的既有理由)。守卫见
 * frontend/tests/guards/architecture-boundaries.test.mjs。
 *
 * 各面专属的提示(某个笔记本还没建知识图谱、借用参考库推理、扩展引擎缺图谱……)
 * 依赖的是调用方自己的状态,所以走 `hints` 插槽原样挂在控件末尾,而不是把
 * kgGraph / startKgBuild / borrowedBaseNames 这类页面状态搬进共享组件。
 */
"use client";

import type { ReactNode } from "react";

import {
  ASK_MODE_GROUPS,
  defaultModeForGroup,
  groupOf,
  modesInGroup,
  type AskModeDef,
} from "./ask-modes";
import { ASK_RETRIEVAL_EFFORT_OPTIONS } from "./ask-retrieval-effort";
import { EffortPicker } from "./effort-picker";
import { isAdvanced, type UiMode } from "./ui-mode.ts";


export function AskModePicker({
  modes,
  value,
  onChange,
  disabled,
  kgAvailable,
  uiMode,
  effort,
  hints,
}: {
  /** 本次可选的引擎表:内置两个 + 运行时合并进来的部署扩展引擎。 */
  modes: readonly AskModeDef[];
  /** 当前选中的引擎 id。 */
  value: string;
  onChange: (id: string) => void;
  /** 在途/加载中一律禁用。扩展引擎另有「缺知识图谱」这一层各自的禁用。 */
  disabled: boolean;
  kgAvailable: boolean;
  /** 自动模式只保留问答框：模式、引擎、档位以及相应提示整组不挂载。 */
  uiMode: UiMode;
  /** 「逐步推理」的检索档位。缺席即那一格不渲染——没有承接方就不出控件
   *  （全局问答 v1 不提供档位选择，恒用默认档）。 */
  effort?: { value: string; onChange: (id: string) => void };
  /** 调用方自己的提示块，原样挂在控件末尾。 */
  hints?: ReactNode;
}) {
  if (!isAdvanced(uiMode)) return null;
  return (
    <div className="ask-mode-control" role="group" aria-label="问答模式">
      {ASK_MODE_GROUPS.filter((group) => (
        group.id !== "extension"
        || (isAdvanced(uiMode) && modesInGroup("extension", modes).length > 0)
      )).map((g) => (
        <button
          key={g.id}
          type="button"
          className={`mode-tab${groupOf(value, modes) === g.id ? " active" : ""}`}
          disabled={disabled}
          onClick={() => onChange(defaultModeForGroup(g.id, modes))}
        >
          {g.label}
        </button>
      ))}
      {/* 深入分析只有一个引擎；扩展组保留引擎选择。 */}
      {groupOf(value, modes) === "extension" && (
        <span className="mode-engines">
          {modesInGroup(groupOf(value, modes), modes).map((m) => (
            <button
              key={m.id}
              type="button"
              className={`mode-engine${value === m.id ? " active" : ""}`}
              title={m.desc}
              disabled={disabled || (m.requiresKg && !kgAvailable)}
              onClick={() => onChange(m.id)}
            >
              {m.label}
            </button>
          ))}
        </span>
      )}
      {effort && isAdvanced(uiMode) && value === "reasoning" && (
        <span className="ask-retrieval-effort">
          {/* 与深度报告的「研究深度」共用 EffortPicker：同一套档名理应是同一个控件。
              popover 只给该档一句说明，不再铺开每档的阈值数字。 */}
          <EffortPicker
            chipLabel="档位"
            title="检索档位"
            options={ASK_RETRIEVAL_EFFORT_OPTIONS}
            value={effort.value}
            onChange={effort.onChange}
            disabled={disabled}
            compact
          />
        </span>
      )}
      {hints}
    </div>
  );
}
