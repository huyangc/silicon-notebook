"use client";

// 报告「分享」前的确认条。
//
// 作者公开一份用到了自己个人记忆的报告时,公开页可能带上那些记忆的内容——不只是被引用的
// 摘录,规划大纲和撰写章节时看过的记忆也可能被正文转述。产品裁决(M4):分享的那一刻明确
// 告诉作者「来自几条」,由作者决定。所以这一条**就地**长在「分享」按钮下面(不是页面顶部
// 那条会滚出视口的横幅),只有条数 > 0 或不能公开时才出现;0 条时分享流程没有任何新增步骤。
//
// 数字全部来自服务端:条数由披露端点给出,作者确认后随 POST 一起交回,服务端按即将公开
// 的确切范围重算,不相等就 409 把确数带回来——这里只负责把数字画出来,不自己数。409 之后
// 句子先说「条数有变化」,作者看得出这是一个新数字。
//
// `refusal` 非空表示这份报告不能公开(引用了其他成员的个人记忆,披露端点在点击前就说了;
// 或服务端拒绝了这次公开):那句中文原因就地显示,并且不再给「确认公开」。
//
// 键盘:出现时焦点落进条里(落在「取消」上,不把公开这个动作当默认落点);Esc 等同「取消」;
// 条收起时焦点回到「分享」按钮,结果也正落在那颗按钮上。

import { useEffect, useLayoutEffect, useRef, useState, type RefObject } from "react";

export const shareDisclosureSentence = (count: number, added: number | null): string => {
  const sentence = `公开页可能包含来自 ${count} 条个人记忆的内容。`;
  if (added === null) return sentence;
  return added > 0 ? `条数有变化（新增 ${added} 条）：${sentence}` : `条数有变化：${sentence}`;
};

export type ReportShareConfirmProps = {
  /** 服务端数出来的个人记忆条数;还没拿到确数时为 null。 */
  count: number | null;
  /** 409 带回新条数时,比作者确认时多出的条数;null = 这是第一次给出的数字。 */
  added: number | null;
  /** 不能公开的中文原因;非空时替换披露句并收起「确认公开」。 */
  refusal: string | null;
  /** 公开请求在飞:两个按钮都禁用(结果马上落到「分享」按钮上)。 */
  busy: boolean;
  /** 焦点的归处(「分享」按钮):出现时焦点若还在它上面,就移进条里。 */
  returnFocusRef: RefObject<HTMLElement | null>;
  /** 条收起时焦点在条里或已丢失:把焦点还给「分享」按钮(它可能还在忙,由调用方择时)。 */
  onReturnFocus: () => void;
  onConfirm: () => void;
  onCancel: () => void;
};

export function ReportShareConfirm({
  count,
  added,
  refusal,
  busy,
  returnFocusRef,
  onReturnFocus,
  onConfirm,
  onCancel,
}: ReportShareConfirmProps) {
  const groupRef = useRef<HTMLDivElement | null>(null);
  const cancelRef = useRef<HTMLButtonElement | null>(null);

  // 刚出现(焦点还在「分享」上或已丢失)、或刚被点的「确认公开」因拒绝而消失时,焦点落到
  // 「取消」上;焦点已在条里或被用户带去别处时不抢。
  useLayoutEffect(() => {
    const current = document.activeElement;
    if (!current || current === document.body || current === returnFocusRef.current) {
      cancelRef.current?.focus();
    }
  }, [refusal, returnFocusRef]);

  // 收起时(取消、Esc、公开成功)焦点在条里或已丢失,就还给「分享」按钮。
  const returnFocus = useRef(onReturnFocus);
  returnFocus.current = onReturnFocus;
  useLayoutEffect(() => {
    const group = groupRef.current;
    return () => {
      const current = document.activeElement;
      if (!current || current === document.body || group?.contains(current)) {
        returnFocus.current();
      }
    };
  }, []);

  const text = refusal ?? (count === null ? "" : shareDisclosureSentence(count, added));
  // 读屏只播报「已挂载的状态区里的变化」:状态区先以空文字挂上,句子在挂载之后才填进去,
  // 所以条一出现就会被读出来(同时挂上的文字可能不播)。
  const [spoken, setSpoken] = useState("");
  useEffect(() => {
    setSpoken(text);
  }, [text]);
  return (
    <div
      ref={groupRef}
      className="report-share-confirm"
      role="group"
      aria-label="公开前确认"
      onKeyDown={(event) => {
        if (event.key === "Escape" && !busy) {
          event.stopPropagation();
          onCancel();
        }
      }}
    >
      <span className="report-share-confirm-text" role="status">{spoken}</span>
      {refusal === null && (
        <button className="report-action" type="button" disabled={busy} onClick={onConfirm}>
          确认公开
        </button>
      )}
      <button ref={cancelRef} className="report-action" type="button" disabled={busy} onClick={onCancel}>
        取消
      </button>
    </div>
  );
}
