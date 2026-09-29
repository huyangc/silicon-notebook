"use client";

// 报告「分享」前的确认条。
//
// 作者公开一份引用了自己个人记忆的报告时,公开页会带上那些记忆的摘录。产品裁决(M4):
// 分享的那一刻明确告诉作者「几条」,由作者决定。所以这一条**就地**长在「分享」按钮旁边
// (不是页面顶部那条会滚出视口的横幅),只有条数 > 0 时才出现;0 条时分享流程没有任何
// 新增步骤。
//
// 数字全部来自服务端:条数由披露端点给出,作者确认后随 POST 一起交回,服务端按即将公开
// 的确切范围重算,不相等就 409 把确数带回来——这里只负责把数字画出来,不自己数。
//
// `refusal` 非空表示服务端拒绝了这次公开(非作者):那句中文原因就地显示,并且不再给
// 「确认公开」——再点一次只会得到同一个拒绝。

export type ReportShareConfirmProps = {
  /** 服务端数出来的个人记忆条数;还没拿到确数时为 null。 */
  count: number | null;
  /** 服务端拒绝这次公开的中文原因;非空时替换披露句并收起「确认公开」。 */
  refusal: string | null;
  /** 公开请求在飞:两个按钮都禁用(结果马上落到「分享」按钮上)。 */
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
};

export function ReportShareConfirm({
  count,
  refusal,
  busy,
  onConfirm,
  onCancel,
}: ReportShareConfirmProps) {
  const text = refusal ?? (count === null ? "" : `公开页会包含 ${count} 条你引用到的个人记忆摘录。`);
  return (
    <div className="report-share-confirm" role="group" aria-label="公开前确认">
      <span className="report-share-confirm-text" role="status">{text}</span>
      {refusal === null && (
        <button className="report-action" type="button" disabled={busy} onClick={onConfirm}>
          确认公开
        </button>
      )}
      <button className="report-action" type="button" disabled={busy} onClick={onCancel}>
        取消
      </button>
    </div>
  );
}
