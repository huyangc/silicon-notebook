import { defineConfig } from "vitest/config";

export default defineConfig({
  esbuild: {
    jsx: "automatic",
    jsxImportSource: "react",
  },
  test: {
    environment: "jsdom",
    include: ["app/**/*.component.test.tsx"],
    setupFiles: ["./app/test/setup.ts"],
    clearMocks: true,
    restoreMocks: true,
    // Headroom for the raised asyncUtilTimeout (see app/test/setup.ts): under
    // CI's concurrent-lane CPU contention a single heavy render can wait several
    // seconds, so the default 5000ms per-test budget would itself flake.
    testTimeout: 15000,
  },
});
