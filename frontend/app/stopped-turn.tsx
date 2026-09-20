import { Pencil } from "lucide-react";

/**
 * 全站统一的取消风格：一条被用户停止的提问在对话里长什么样。
 *
 * 规则只有一条，两个问答面（笔记本内问答、全局问答）共用：
 *   · 还**没有过程输出**（含「问题理解」阶段）就停止——问题弹回输入框，对话里不留记录；
 *   · **已有检索 / 推理过程上屏**再停止——这一轮留在对话里（问题 + 已有过程 + 本提示），
 *     问题不弹回；「编辑问题」把它放回输入框，**下一次提问会替换这条记录**。
 *
 * 「是否已有过程输出」由各面自己判定（笔记本内问答看本次运行收到的轨迹步，全局问答
 * 看轨迹步与各库的检索回执），判定之后长相与文案只有这里一份。
 */
export const STOPPED_TURN_TEXT = "已停止回答。重新提问后，这条记录会被新的回答替换。";

export function StoppedTurnNotice({ onEdit, disabled = false }: {
  /** 把这条问题放回输入框并聚焦。不传则只显示提示（例如不是最新一轮、已不可替换）。 */
  onEdit?: () => void;
  disabled?: boolean;
}) {
  return (
    <div className="stopped-turn-notice">
      <span role="status">{STOPPED_TURN_TEXT}</span>
      {onEdit && (
        <button type="button" className="stopped-turn-edit" disabled={disabled} onClick={onEdit}>
          <Pencil size={13} aria-hidden="true" />编辑问题
        </button>
      )}
    </div>
  );
}
