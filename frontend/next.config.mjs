// 同源反代：浏览器只与前端(:3000)同源通信，Next 把 /api/* 转发到本机后端。
// 好处：免 CORS、后端无需对外暴露(可留 127.0.0.1)、前端包不写死后端地址。
// 前端把 NEXT_PUBLIC_API_BASE_URL 设为相对的 /api 即走此代理。
// BACKEND_PROXY_TARGET 可覆盖后端地址（默认本机 127.0.0.1:8000）。
const backendTarget = process.env.BACKEND_PROXY_TARGET ?? "http://127.0.0.1:8000";

/** @type {import('next').NextConfig} */
const nextConfig = {
  typedRoutes: true,
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${backendTarget}/api/:path*` },
    ];
  },
};

export default nextConfig;
