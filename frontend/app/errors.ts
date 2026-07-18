// 错误人话层:把后端的 HTTP 状态码 / 英文 detail 翻成给用户看的中文。
//
// 设计约定(PR C):
//   - 后端 detail 保持英文——供 MCP agent / 日志 / 排查,前端不改后端。
//   - 前端只在「展示层」按 HTTP 状态码映射成中文;原始诊断(状态码 + 后端
//     英文 detail + requestId)由各调用点写进 console.error / hover title,
//     不进用户可见文案。
//   - detail 入参当前不参与文案(基础映射只看 status)。保留它是为了:①与
//     携带 detail 的调用点签名对齐;②给调用点做「场景化已知 detail 覆盖」
//     留位(如登录页 401 → 用户名或密码不对,由 auth.ts 在调用侧决定)。
export function humanizeHttpError(status: number, detail?: string): string {
  void detail; // 保留入参:基础映射只看 status,场景化覆盖在调用侧。
  switch (status) {
    case 401:
      return "登录状态已失效，请重新登录";
    case 403:
      return "没有权限进行这个操作";
    case 404:
      return "没找到，可能已被删除";
    case 409:
      return "操作有冲突，请刷新后重试";
    case 413:
      return "文件太大";
    case 422:
      return "提交的内容有误";
    default:
      if (status >= 500) return "服务暂时不可用，请稍后再试";
      return "操作失败，请重试";
  }
}
