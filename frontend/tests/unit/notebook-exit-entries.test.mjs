// 分享编辑与协作 P1-T12:读进来的库给当前用户哪几个「退出」入口、每个走哪几条出口
// (规格 §4.4「交叉态两个退出」)。逐格表在 notebook-exit-entries.ts 文件头。
import test from "node:test";
import assert from "node:assert/strict";

import { doneItemDestination } from "../../app/workspace-transitions.ts";
import { readerBadgeWords, readerExitEntries, STILL_EDITOR_NOTE } from "../../app/notebook-exit-entries.ts";

const ME = "u-me";
const GROUP = [{ group_id: "g1", group_name: "封装项目", kind: "project" }];
const shape = (entries) => entries.map((entry) => ({
  label: entry.label,
  steps: entry.plan.steps.map((step) => (step.kind === "membership" ? "membership" : `collaborator:${step.userId}`)),
  note: entry.note,
}));

test("逐格:没有点名授权 / 编辑者 / 只读或管理权限点名授权 × 有无成员行 × 群组", () => {
  const cases = [
    [{ access: "owner" }, []],
    [{ access: "reader" }, [{ label: "退出共享", steps: ["membership"], note: "" }]],
    [{ access: "reader", granted_via: GROUP }, []],
    [{ access: "reader", collaborator_role: "editor" }, [{ label: "退出协作", steps: [`collaborator:${ME}`], note: "" }]],
    [{ access: "reader", collaborator_role: "editor", granted_via: GROUP }, [
      { label: "退出协作（保留只读）", steps: [`collaborator:${ME}`], note: "" },
    ]],
    [{ access: "reader", collaborator_role: "editor", collaborator_is_member: true }, [
      { label: "退出协作（保留只读）", steps: [`collaborator:${ME}`], note: "" },
      { label: "退出共享", steps: ["membership"], note: STILL_EDITOR_NOTE },
    ]],
    [{ access: "reader", collaborator_role: "viewer" }, [{ label: "退出共享", steps: [`collaborator:${ME}`], note: "" }]],
    [{ access: "reader", collaborator_role: "admin" }, [{ label: "退出共享", steps: [`collaborator:${ME}`], note: "" }]],
    [{ access: "reader", collaborator_role: "viewer", collaborator_is_member: true }, [
      { label: "退出共享", steps: ["membership", `collaborator:${ME}`], note: "" },
    ]],
  ];
  for (const [notebook, expected] of cases) {
    assert.deepEqual(shape(readerExitEntries(notebook, ME)), expected, JSON.stringify(notebook));
  }
});

test("collaborator_is_member 只在有点名授权时生效:没有点名授权时照旧走成员关系", () => {
  assert.deepEqual(
    shape(readerExitEntries({ access: "reader", collaborator_role: "", collaborator_is_member: true }, ME)),
    [{ label: "退出共享", steps: ["membership"], note: "" }],
  );
});

test("拿不到当前用户 id:点名授权那几颗不给,成员关系那颗照给", () => {
  assert.deepEqual(readerExitEntries({ access: "reader", collaborator_role: "editor" }, ""), []);
  assert.equal(readerExitEntries({ access: "reader" }, "").length, 1);
});

test("徽章身份词:管理权「可管理」、编辑权「可编辑」、其余「只读」;点名编辑者写库主不写群组", () => {
  assert.deepEqual(readerBadgeWords({ can_manage_content: true, can_manage_notebook: true, granted_via: GROUP }), { accessWord: "可管理", viaGroup: true });
  assert.deepEqual(readerBadgeWords({ can_manage_content: true, collaborator_role: "editor", granted_via: GROUP }), { accessWord: "可编辑", viaGroup: false });
  assert.deepEqual(readerBadgeWords({ granted_via: GROUP }), { accessWord: "只读", viaGroup: true });
  assert.deepEqual(readerBadgeWords({}), { accessWord: "只读", viaGroup: false });
});

test("铃铛里「被添加为编辑者」点开停在来源面板", () => {
  assert.equal(doneItemDestination("notebook_collaborator_granted"), "sources");
});
