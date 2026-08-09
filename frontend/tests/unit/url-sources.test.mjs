import test from "node:test";
import assert from "node:assert/strict";

import { parseUrlLines } from "../../app/url-sources.ts";

test("parseUrlLines: 保留 http/https、trim、去重、丢空行与非 URL", () => {
  const input = "  https://a/x.pdf \n\nhttp://b/y.pdf\nftp://c/z.pdf\nnot a url\nhttps://a/x.pdf\n";
  assert.deepEqual(parseUrlLines(input), ["https://a/x.pdf", "http://b/y.pdf"]);
});

test("parseUrlLines: 纯空白 -> []", () => {
  assert.deepEqual(parseUrlLines("   \n  \n"), []);
});
