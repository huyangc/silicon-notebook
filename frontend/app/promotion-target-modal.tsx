"use client";

import { FloatingModalCard } from "./floating-modal-card";
import { promotionTargetOptions, type MountedBase } from "./notebook-bases";

/**
 * 多领域基准库 A5:「选择贡献目标」弹窗——page.tsx(知识条目晋升)与
 * memory-panel.tsx(Memory 晋升)此前各写了一份逐字相同的 JSX。判定规则只有
 * 一份 `resolvePromotionTarget`(notebook-bases.ts),这里只抽取**呈现**。
 *
 * 外层弹窗骨架(role="dialog"、背景点击关闭、z-index/inert 由谁的弹窗协调器管)
 * 两个宿主并不相同——page.tsx 接入 rootModals 的多弹窗层叠协调,memory-panel.tsx
 * 是独立的局部弹窗——所以 `<section role="dialog">` 仍由各宿主自己包一层;这里
 * 只负责卡片本体,props 保持最小(bases/onPick/onClose/文案)。
 *
 * `bases` 传的是宿主此刻的**完整**当前挂载候选列表(不是宿主先用
 * resolvePromotionTarget 判过一次、只在 "choose" 分支才有值的 options)——弹窗
 * 打开期间挂载集合可能变化(任何 refreshActiveNotebook),用后者在候选缩到
 * 1 个时会把"已挂载 1 个"误判成"未挂载",显示一句断言错误的 0-态提示。
 *
 * 完整列表里混着私有/共享库,它们不是合格的晋升目标(后端必回 400),所以先经
 * 唯一定义点 `promotionTargetOptions`(与 resolvePromotionTarget 同一条规则)过滤,
 * 再判 0 项 / 渲染列表。
 *
 * 因此这里**不**做"1 项自动选中并跳过渲染"的静默写入——1 项也照常渲染成一个
 * 可点击项,由用户自己点(哪怕列表只有一个,也不代自动提交);0 项时原地给出
 * 一句中性、不断言状态的提示（可选集合可能只是这一刻恰好变化，不代表整个
 * 笔记本从未挂载过），而不是复用「提交晋升」按钮 disabled 时那句更强的
 * 「需先挂载一个公共知识库」——那句话在这里可能是假的。
 */
export function PromotionTargetModal({
  storageKey,
  title,
  description,
  bases,
  onPick,
  onClose,
}: {
  storageKey: string;
  title: string;
  description: string;
  bases: readonly MountedBase[];
  onPick: (baseId: string) => void;
  onClose: () => void;
}) {
  const options = promotionTargetOptions(bases);
  return (
    <FloatingModalCard storageKey={storageKey} className="utility-modal-card narrow">
      {(floating) => (<>
        <div className="source-modal-header" {...floating.dragHandleProps}>
          <div>
            <h2>{title}</h2>
            <p>{description}</p>
          </div>
          <button className="icon-button" onClick={onClose} title="Close">×</button>
        </div>
        <div className="promotion-target-list">
          {options.length === 0 ? (
            <p className="tool-hint">可选的公共知识库已变化，请关闭后重新提交</p>
          ) : (
            options.map((base) => (
              <button
                key={base.id}
                type="button"
                className="sort-button promotion-target-option"
                onClick={() => onPick(base.id)}
              >
                <span className="promotion-target-name" title={base.name}>{base.name}</span>
              </button>
            ))
          )}
        </div>
      </>)}
    </FloatingModalCard>
  );
}
