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
  },
});
