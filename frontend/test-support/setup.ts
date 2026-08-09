import "@testing-library/jest-dom/vitest";
import { cleanup, configure } from "@testing-library/react";
import { afterEach } from "vitest";

// CI 把前端泳道与后端 pytest、契约泳道**并发**跑在同一台 runner 上,CPU 争用下
// 本地 <1s 完成的组件渲染会被拖到数秒。testing-library 的 findBy*/waitFor 默认
// asyncUtilTimeout 只有 1000ms,于是最重的用例(如 knowhow-row-completion 的整条
// 审阅补全流程)会在渲染尚未完成时超时——报「Unable to find …」的负载敏感 flake
// (本地单跑/全量跑都稳过,只在 CI 三泳道并发下现形)。放宽异步等待上限,让
// findBy*/waitFor 容忍「慢但正确」的渲染,而不是与它赛跑。
configure({ asyncUtilTimeout: 5000 });

afterEach(cleanup);
