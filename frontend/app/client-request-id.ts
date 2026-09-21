/**
 * 前端生成的幂等 id / 本地记录 id（提问的 `client_request_id`、问题理解镜像的 persistId）。
 *
 * ⚠ 不许裸调 `crypto.randomUUID()`。它是 **Secure Context 限定** 的 API：浏览器只在
 * HTTPS 或 localhost 下暴露它，用 `http://<内网 IP>:3000` 访问的部署里它是 `undefined`，
 * 调用即同步抛 `TypeError`。全局问答曾经在提交路径上裸调它——生产环境（裸 IP + HTTP）里
 * 每一次提问都在发出请求之前就死了：通用问答悄无声息地卡住，逐步推理则被外层的 catch
 * 接成一句毫不相干的「问题理解没能完成，请重试」，而本机（localhost）永远复现不了。
 *
 * 退路用 `crypto.getRandomValues`——它**不**受 Secure Context 限制，出来的仍是一枚合规的
 * UUID v4；连它都没有（极老的环境、某些测试运行时）才退到时间戳 + `Math.random`。
 * 这些 id 只需要在「同一用户的提交」范围内不撞，不是安全令牌。
 */
export function newClientRequestId(): string {
  try {
    if (typeof crypto !== "undefined") {
      if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
      if (typeof crypto.getRandomValues === "function") {
        const bytes = crypto.getRandomValues(new Uint8Array(16));
        bytes[6] = (bytes[6] & 0x0f) | 0x40; // version 4
        bytes[8] = (bytes[8] & 0x3f) | 0x80; // RFC 4122 variant
        const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
        return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
      }
    }
  } catch {
    // 落到下面的退路。
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}
