import test from "node:test";
import assert from "node:assert/strict";

import { listWishes } from "../../app/wish-wall-api.ts";
import { WISH_PAGE_MAX } from "../../app/wish-wall-model.ts";


test("许愿墙 API 在发请求前拒绝越界分页窗口", async () => {
  assert.equal(WISH_PAGE_MAX, 100);
  await assert.rejects(
    listWishes({ sort: "priority", limit: WISH_PAGE_MAX + 1 }),
    { name: "RangeError", message: "每页数量必须是 1 到 100 之间的整数" },
  );
  await assert.rejects(listWishes({ sort: "priority", limit: 0 }), { name: "RangeError" });
  await assert.rejects(listWishes({ sort: "priority", limit: 1.5 }), { name: "RangeError" });
});
