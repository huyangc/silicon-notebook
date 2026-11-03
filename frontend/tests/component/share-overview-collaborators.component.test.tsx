// 「我分享的」卡片的点名共享部分(分享编辑 P1-T12)。只因点名共享而出现的行(无链接、无群组)
// 靠它才不是一张空卡片;page.tsx 的挂载由 group-sharing-guard 钉住。
import { render, screen, within } from "@testing-library/react";
import { expect, test } from "vitest";

import {
  ShareOverviewCollaborators,
  ShareOverviewCollaboratorsChip,
} from "../../app/share-overview-collaborators";

const people = [
  { user_id: "u-1", username: "zhangsan", display_name: "张三", role: "editor" },
  { user_id: "u-2", username: "lisi", display_name: "", role: "viewer" },
];

test("列出每个点名协作者与他的权限,头部小标写人数", () => {
  render(
    <>
      <ShareOverviewCollaboratorsChip collaborators={people} />
      <ShareOverviewCollaborators collaborators={people} />
    </>,
  );
  expect(screen.getByText(/点名共享 2 人/)).toBeInTheDocument();
  const list = screen.getByRole("list", { name: "点名共享" });
  const items = within(list).getAllByRole("listitem").map((item) => item.textContent);
  expect(items).toEqual(["张三（zhangsan） · 可编辑", "lisi · 只读"]);
});

test("没有点名协作者(或旧后端不发这个字段)时两部分都不渲染", () => {
  for (const collaborators of [undefined, []]) {
    const { container, unmount } = render(
      <>
        <ShareOverviewCollaboratorsChip collaborators={collaborators} />
        <ShareOverviewCollaborators collaborators={collaborators} />
      </>,
    );
    expect(container).toBeEmptyDOMElement();
    unmount();
  }
});
