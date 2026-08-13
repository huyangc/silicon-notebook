import { sourceUploadProxyBodyMaxBytes } from "./next-upload-limit.mjs";

// 同源反代：浏览器只与前端(:3000)同源通信，Next 把 /api/* 转发到本机后端。
// 好处：免 CORS、后端无需对外暴露(可留 127.0.0.1)、前端包不写死后端地址。
// 前端把 NEXT_PUBLIC_API_BASE_URL 设为相对的 /api 即走此代理。
// BACKEND_PROXY_TARGET 可覆盖后端地址（默认本机 127.0.0.1:8000）。
const backendTarget = process.env.BACKEND_PROXY_TARGET ?? "http://127.0.0.1:8000";

// 离线打包(scripts/pack.sh)时设 NEXT_OUTPUT_STANDALONE=1：next build 额外产出
// .next/standalone —— 一个自带精简 node_modules 的 server.js，可用便携 node 直接
// `node server.js` 运行，无需 npm、无需全量 node_modules、无需 `next` CLI。
// 不设该变量时行为与从前完全一致(npm run dev / next start 不受影响)。
// standalone 服务器仍会加载下面的 rewrites，`/api/*` 同源反代照旧生效。
const output =
  process.env.NEXT_OUTPUT_STANDALONE === "1" ? "standalone" : undefined;

/** @type {import('next').NextConfig} */
const nextConfig = {
  typedRoutes: true,
  output,
  experimental: {
    // Next 15 otherwise clones only the first 10 MiB before an external rewrite.
    // This is a whole-multipart transport envelope derived from the backend's
    // per-file setting and fixed batch rail; backend streaming validation stays
    // authoritative. prod.sh exports the root .env before build, while pack.sh
    // deliberately builds against the protocol maximum for runtime-configurable
    // offline bundles.
    middlewareClientMaxBodySize: sourceUploadProxyBodyMaxBytes(
      process.env.SOURCE_UPLOAD_MAX_MB,
    ),
  },
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${backendTarget}/api/:path*` },
    ];
  },
};

export default nextConfig;
