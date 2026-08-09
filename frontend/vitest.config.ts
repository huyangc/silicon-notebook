import { defineConfig } from "vitest/config";

export default defineConfig({
  esbuild: {
    jsx: "automatic",
    jsxImportSource: "react",
  },
  test: {
    environment: "jsdom",
    include: ["tests/component/**/*.component.test.tsx"],
    // G1 already runs 12 backend pytest workers in parallel. Letting Vitest
    // also claim every logical CPU inflates the backend critical path; four
    // component workers finish well before that path while preserving headroom.
    maxWorkers: 4,
    setupFiles: ["./test-support/setup.ts"],
    clearMocks: true,
    restoreMocks: true,
    // Headroom for the raised asyncUtilTimeout (see test-support/setup.ts): under
    // CI's concurrent-lane CPU contention a single heavy render can wait several
    // seconds, so the default 5000ms per-test budget would itself flake.
    testTimeout: 15000,
  },
});
