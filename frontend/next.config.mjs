import { proxyBodyMaxBytes } from "./next-upload-limit.mjs";

// 同源反代：Next 把 /api/* 和公开的 /mcp 转发到本机后端。
// 好处：免 CORS、后端无需对外暴露(可留 127.0.0.1)、前端包不写死后端地址。
// 前端把 NEXT_PUBLIC_API_BASE_URL 设为相对的 /api 即走此代理。
// BACKEND_PROXY_TARGET 可覆盖后端地址（默认本机 127.0.0.1:8000）。
const backendTarget = process.env.BACKEND_PROXY_TARGET ?? "http://127.0.0.1:8000";

// 可选：把 /canvas 同源转发给本机的 auto-canvas 配套服务。默认关闭——未设或设为
// 空串时 rewrites() 与没有这段代码时完全相同。与 BACKEND_PROXY_TARGET 一样在构建时
// 写入路由清单。目标既向本站来源提供页面代码、又会收到用户 token，所以只接受回环
// 地址；`[::1]` 也拒绝：Next 15 编译不了带方括号 IPv6 主机的转发目标。写错时在加载
// 配置时抛错让构建失败，不静默忽略；报错不回显原值（它可能带凭据）。
const CANVAS_TARGET_PATTERN = /^(https?:\/\/(?:127\.0\.0\.1|localhost))(?::(\d{1,5}))?\/?$/;

function canvasProxyTarget(raw) {
  if (raw === undefined || raw === "") return undefined;
  const match = CANVAS_TARGET_PATTERN.exec(raw);
  const port = match?.[2] === undefined ? undefined : Number(match[2]);
  if (!match || (port !== undefined && (port < 1 || port > 65535))) {
    throw new Error(
      "CANVAS_PROXY_TARGET must be http(s)://127.0.0.1[:port] or " +
        "http(s)://localhost[:port], with no path, query, fragment or credentials",
    );
  }
  return port === undefined ? match[1] : `${match[1]}:${port}`;
}

const canvasTarget = canvasProxyTarget(process.env.CANVAS_PROXY_TARGET);

// 转发不得离开上游挂载点：Next 按原始路径匹配并原样拼接，上游收到后才规范化 `..`。
// 所以通配段逐段受限——不匹配等于 `.` 或 `..` 的路径段、反斜杠，以及 %2e / %2f / %5c
// （不分大小写）；不匹配的请求不会被转发。
// 控制字符与空格（十六进制 00–20 和 7f）的原始形式和 % 转义同样不匹配：URL 解析器会
// 删掉或裁掉其中一部分，先解码再解析的上游会因此把 `.<TAB>.` 读成 `..`。画布的路径
// 从不含这些字符（自由文本只走查询串），所以这里不必区分哪几个真的危险。
// 一个允许的非点单元：除 / \ % . 和上述字符之外的任意字符，或一个未被拒绝的 % 转义。
const CANVAS_SEGMENT_UNIT = String.raw`(?:[^/\\%.\x00-\x20\x7f]|%(?![01][0-9a-fA-F]|2[0eEfF]|5[cC]|7[fF]))`;
// 合法的路径段：至少含一个非点单元，或是三个及以上的点。
const CANVAS_SEGMENT =
  String.raw`\.*` + CANVAS_SEGMENT_UNIT + "(?:" + CANVAS_SEGMENT_UNIT + String.raw`|\.)*|\.{3,}`;

function canvasRewrites(target) {
  if (target === undefined) return [];
  // `/@notebook` 是只有整合模式的 auto-canvas 才应答的私有上游挂载点，不可配置。
  return [
    // 根路径单独一条并显式带斜杠（与 /mcp 同一手法）：只靠通配规则时空尾段展开成
    // 不带斜杠的地址，上游重定向后会与 Next 自己的去斜杠重定向循环。
    { source: "/canvas", destination: `${target}/@notebook/canvas/` },
    {
      source: "/canvas/:path(" + CANVAS_SEGMENT + ")*",
      destination: `${target}/@notebook/canvas/:path*`,
    },
  ];
}

// 离线打包(scripts/pack.sh)时设 NEXT_OUTPUT_STANDALONE=1：next build 额外产出
// .next/standalone —— 一个自带精简 node_modules 的 server.js，可用便携 node 直接
// `node server.js` 运行，无需 npm、无需全量 node_modules、无需 `next` CLI。
// 不设该变量时行为与从前完全一致(npm run dev / next start 不受影响)。
// standalone 服务器仍会加载下面的 rewrites，API 与 MCP 同源反代照旧生效。
const output =
  process.env.NEXT_OUTPUT_STANDALONE === "1" ? "standalone" : undefined;

/** @type {import('next').NextConfig} */
const nextConfig = {
  typedRoutes: true,
  output,
  // ESLint is its own gate (`npm run lint`, with the eslint-suppressions.json
  // baseline). Next's build-time lint would see eslint.config.mjs, lint again
  // without that baseline and fail the build on pre-existing violations.
  eslint: { ignoreDuringBuilds: true },
  experimental: {
    // Idle timeout in milliseconds; API/MCP stream heartbeats keep active calls alive.
    proxyTimeout: 300_000,
    // Next 15 otherwise clones only the first 10 MiB before an external rewrite.
    // One ceiling covers every proxied request, so it is the larger of two
    // transport envelopes: the whole-multipart source upload (the backend's
    // per-file setting times the fixed batch rail) and one raw notebook-import
    // body (NOTEBOOK_TRANSFER_WEB_MAX_MB plus 1 MiB). Backend streaming
    // validation stays authoritative for both. prod.sh exports the root .env
    // before build, while pack.sh deliberately builds against the protocol
    // maximum for runtime-configurable offline bundles.
    middlewareClientMaxBodySize: proxyBodyMaxBytes(
      process.env.SOURCE_UPLOAD_MAX_MB,
      process.env.NOTEBOOK_TRANSFER_WEB_MAX_MB,
    ),
  },
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${backendTarget}/api/:path*` },
      // Public MCP_PUBLIC_URL ends in /mcp; the mounted backend needs /mcp/.
      // Rewrite directly to avoid a backend 307 and Next's slash-removal redirect.
      { source: "/mcp", destination: `${backendTarget}/mcp/` },
      ...canvasRewrites(canvasTarget),
    ];
  },
};

export default nextConfig;
