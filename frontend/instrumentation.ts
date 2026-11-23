import { installProxyFailureLogRedaction } from "./proxy-failure-log.ts";

// Next 在服务进程启动时调用一次（next dev / next start / 离线包的 standalone server.js）。
export function register(): void {
  installProxyFailureLogRedaction(console);
}
