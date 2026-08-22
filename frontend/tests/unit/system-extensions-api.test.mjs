import test from "node:test";
import assert from "node:assert/strict";

import { parseSystemExtensions } from "../../app/system-api.ts";


const valid = {
  api_version: "1",
  extensions: [{
    plugin_id: "sample-plugin",
    display_name: "Sample",
    version: "1.0.0",
    contribution_id: "sample-panel",
    available: true,
    unavailable_reason: null,
  }],
};


test("system extension projection parser accepts only the closed wire shape", () => {
  assert.deepEqual(parseSystemExtensions(valid), {
    apiVersion: "1",
    extensions: [{
      pluginId: "sample-plugin",
      displayName: "Sample",
      version: "1.0.0",
      contributionId: "sample-panel",
      available: true,
      unavailableReason: null,
    }],
  });
  for (const malformed of [
    null,
    { ...valid, api_version: "2" },
    { ...valid, extensions: [{ ...valid.extensions[0], available: "yes" }] },
    { ...valid, extensions: [{ ...valid.extensions[0], unavailable_reason: "raw_error" }] },
    { ...valid, extensions: [{ ...valid.extensions[0], secret_endpoint: "https://secret" }] },
    { ...valid, secret_endpoint: "https://secret" },
    { ...valid, extensions: [{ ...valid.extensions[0], contribution_id: "Bad Alias" }] },
    { ...valid, extensions: [valid.extensions[0], valid.extensions[0]] },
    { ...valid, extensions: [{ ...valid.extensions[0], available: false }] },
  ]) assert.throws(() => parseSystemExtensions(malformed), /格式无效/);
});
