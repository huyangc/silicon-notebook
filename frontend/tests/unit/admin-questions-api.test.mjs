import test from "node:test";
import assert from "node:assert/strict";

import {
  ADMIN_QUESTIONS_DEFAULT_LIMIT,
  ADMIN_QUESTIONS_MAX_LIMIT,
  fetchAdminQuestions,
} from "../../app/admin/questions/api.ts";


test("管理员提问分页 rail 与后端协议同名且 API 层拒绝越界值", async () => {
  assert.equal(ADMIN_QUESTIONS_DEFAULT_LIMIT, 50);
  assert.equal(ADMIN_QUESTIONS_MAX_LIMIT, 200);
  await assert.rejects(
    fetchAdminQuestions({ limit: ADMIN_QUESTIONS_MAX_LIMIT + 1 }),
    { name: "RangeError", message: "每页数量必须是 1 到 200 之间的整数" },
  );
  await assert.rejects(fetchAdminQuestions({ limit: 0 }), { name: "RangeError" });
  await assert.rejects(fetchAdminQuestions({ limit: 1.5 }), { name: "RangeError" });
});
