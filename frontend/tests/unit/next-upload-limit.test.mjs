import test from "node:test";
import assert from "node:assert/strict";

import {
  SOURCE_UPLOAD_MAX_FILES_PER_BATCH,
  SOURCE_UPLOAD_MULTIPART_OVERHEAD_BYTES,
  notebookImportProxyBodyMaxBytes,
  notebookTransferMaxMbFromEnvironment,
  proxyBodyMaxBytes,
  sourceUploadMaxMbFromEnvironment,
  sourceUploadProxyBodyMaxBytes,
} from "../../next-upload-limit.mjs";

test("Next rewrite body ceiling derives from the same per-file setting and batch rail", () => {
  const mib = 1024 * 1024;
  assert.equal(sourceUploadMaxMbFromEnvironment(undefined), 50);
  assert.equal(
    sourceUploadProxyBodyMaxBytes("50"),
    50 * mib * SOURCE_UPLOAD_MAX_FILES_PER_BATCH
      + SOURCE_UPLOAD_MULTIPART_OVERHEAD_BYTES,
  );
  assert.ok(sourceUploadProxyBodyMaxBytes("1") > 10 * mib);
});

test("Next build accepts the backend's SOURCE_UPLOAD_MAX_MB range only", () => {
  for (const value of ["0", "1.5", "1025", "not-a-number"]) {
    assert.throws(
      () => sourceUploadMaxMbFromEnvironment(value),
      /SOURCE_UPLOAD_MAX_MB must be a whole number from 1 through 1024/,
    );
  }
  assert.equal(sourceUploadMaxMbFromEnvironment("1024"), 1024);
});

test("Next build accepts the backend's NOTEBOOK_TRANSFER_WEB_MAX_MB range only", () => {
  for (const value of ["0", "1.5", "1025", "not-a-number"]) {
    assert.throws(
      () => notebookTransferMaxMbFromEnvironment(value),
      /NOTEBOOK_TRANSFER_WEB_MAX_MB must be a whole number from 1 through 1024/,
    );
  }
  assert.equal(notebookTransferMaxMbFromEnvironment(undefined), 512);
  assert.equal(notebookTransferMaxMbFromEnvironment("1024"), 1024);
});

test("the rewrite ceiling is the larger of the source-upload and notebook-import envelopes", () => {
  const mib = 1024 * 1024;
  assert.equal(notebookImportProxyBodyMaxBytes("600"), 600 * mib + mib);
  // Defaults: 20 × 50 MiB source files outweigh one 512 MiB archive.
  assert.equal(proxyBodyMaxBytes(undefined, undefined), sourceUploadProxyBodyMaxBytes("50"));
  // A small per-file source limit: the import envelope decides.
  assert.equal(proxyBodyMaxBytes("1", "512"), notebookImportProxyBodyMaxBytes("512"));
  assert.equal(proxyBodyMaxBytes("50", "1024"), notebookImportProxyBodyMaxBytes("1024"));
});

test("Next production config wires the derived transport ceiling", async () => {
  const { default: nextConfig } = await import("../../next.config.mjs");
  assert.equal(
    nextConfig.experimental.middlewareClientMaxBodySize,
    proxyBodyMaxBytes(
      process.env.SOURCE_UPLOAD_MAX_MB,
      process.env.NOTEBOOK_TRANSFER_WEB_MAX_MB,
    ),
  );
});
