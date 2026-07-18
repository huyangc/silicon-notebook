import test from "node:test";
import assert from "node:assert/strict";

import { humanizeHttpError } from "./errors.ts";

test("按状态码映射成中文人话", () => {
  assert.equal(humanizeHttpError(401), "登录状态已失效，请重新登录");
  assert.equal(humanizeHttpError(403), "没有权限进行这个操作");
  assert.equal(humanizeHttpError(404), "没找到，可能已被删除");
  assert.equal(humanizeHttpError(409), "操作有冲突，请刷新后重试");
  assert.equal(humanizeHttpError(413), "文件太大");
  assert.equal(humanizeHttpError(422), "提交的内容有误");
});

test("5xx 一律归「服务暂时不可用」", () => {
  assert.equal(humanizeHttpError(500), "服务暂时不可用，请稍后再试");
  assert.equal(humanizeHttpError(502), "服务暂时不可用，请稍后再试");
  assert.equal(humanizeHttpError(503), "服务暂时不可用，请稍后再试");
});

test("未知状态码退兜底文案", () => {
  assert.equal(humanizeHttpError(400), "操作失败，请重试");
  assert.equal(humanizeHttpError(429), "操作失败，请重试");
  assert.equal(humanizeHttpError(0), "操作失败，请重试");
});

test("detail 入参不影响基础映射(只看 status)", () => {
  // 后端英文 detail 仍传得进来,但基础映射只按状态码;文案不泄漏英文。
  assert.equal(humanizeHttpError(403, "admin only"), "没有权限进行这个操作");
  assert.equal(humanizeHttpError(500, "Internal Server Error"), "服务暂时不可用，请稍后再试");
  assert.equal(humanizeHttpError(422, "field required"), "提交的内容有误");
});
