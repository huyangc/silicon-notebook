/**
 * 「N 分钟前」这类相对时刻文案。原本住在 page.tsx 的模块级；知识图谱视图搬出去之后
 * 两边都要用（page 里还有会话列表与两个索引面板共 3 个调用点），从 page.tsx 反向
 * import 会成环，所以提到共享模块，保持单一定义。
 */
export function formatRelativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return "";
  const diffSec = Math.round((Date.now() - then) / 1000);
  if (diffSec < 60) return "刚刚";
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)} 分钟前`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)} 小时前`;
  if (diffSec < 86400 * 30) return `${Math.floor(diffSec / 86400)} 天前`;
  return new Date(then).toLocaleDateString();
}
