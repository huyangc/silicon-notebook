// 「退出共享」各种结果的用户文案(纯函数)。
//
// 规矩:每句话里的数字都取自服务端的响应(deleted_memory_count / memory_count / 重新读到
// 的告知条数),客户端不自己算。核对不出来的事实不写成数字——比如网络中断后只能重读到
// 「还剩几条」,那就只说还剩几条,不说「删了几条」。

export const EXIT_UNAVAILABLE_TEXT = "暂时无法确认将删除多少条记忆，请稍后重试。";
export const EXIT_FAILED_TEXT = "退出没有成功，请重试";
export const EXIT_UNVERIFIED_TEXT = "暂时无法确认是否已经退出，请刷新页面查看笔记本列表。";
export const EXIT_CHANGED_TEXT = "记忆数量有变化，请重新确认。";
export const EXIT_CHANGED_TO_EMPTY_TEXT = "现在没有需要删除的记忆了，可以直接退出。";

/** 成功之后的提示:200 带服务端数到的条数,204(没有删)只说退出了。 */
export const leftText = (deleted: number): string =>
  deleted > 0 ? `已退出共享，已删除 ${deleted} 条记忆` : "已退出共享";

/** 409 exit_incomplete:面板还开着,就地说明(已删的已经没了,新增的要重新确认)。 */
export const incompleteConfirmText = (deleted: number, remaining: number): string =>
  deleted > 0
    ? `已删除 ${deleted} 条记忆。退出期间又新增了 ${remaining} 条，确认后会删除它们并退出。`
    : `退出期间又新增了 ${remaining} 条记忆，确认后会删除它们并退出。`;

/** 503 exit_incomplete:清理中途出错,你仍是成员。 */
export const incompleteFailedText = (deleted: number, remaining: number): string =>
  deleted > 0
    ? `退出没有完成：已删除 ${deleted} 条记忆，还剩 ${remaining} 条，你仍是成员。可以重试。`
    : `退出没有完成：没有删除任何记忆，还剩 ${remaining} 条，你仍是成员。可以重试。`;

/** 409 exit_incomplete 的结果在面板已经关掉(取消/离开页面)之后才到:用提示告知。 */
export const incompleteLateText = (deleted: number, remaining: number): string =>
  deleted > 0
    ? `已删除 ${deleted} 条记忆，但退出没有完成：期间又新增了 ${remaining} 条，你仍是成员。`
    : `退出没有完成：期间又新增了 ${remaining} 条记忆，你仍是成员。`;

/**
 * 网络中断/超时之后重读到「仍是成员」:只陈述重读到的剩余条数;和确认时的条数比较只
 * 用来判断「有没有删过」,不把差值当数字说出来。
 */
export function stillMemberText(remaining: number, acknowledged: number): string {
  const lead = "退出的结果没有收到，重新核对后：你仍是成员，";
  if (remaining === 0) return `${lead}没有需要删除的记忆。可以重试。`;
  const trail = remaining < acknowledged
    ? "，另有一部分已经被删除"
    : remaining === acknowledged
      ? "，都还在，没有被删除"
      : "";
  return `${lead}还有 ${remaining} 条记忆${trail}。可以重试。`;
}
