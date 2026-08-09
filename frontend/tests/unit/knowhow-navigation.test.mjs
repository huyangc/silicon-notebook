import assert from "node:assert/strict";
import test from "node:test";

import {
  closeKnowhowNavigation,
  openKnowhowNavigation,
} from "../../app/knowhow-navigation.ts";

test("closing Knowhow clears the open flag, health filter, and jump target together", () => {
  const open = openKnowhowNavigation({
    healthFilter: "stale_code",
    jumpTarget: { tableId: "table-a", rowId: "row-a" },
  });

  assert.deepEqual(closeKnowhowNavigation(open), {
    isOpen: false,
    healthFilter: "all",
    jumpTarget: null,
  });
});
