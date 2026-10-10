import test from "node:test";
import assert from "node:assert/strict";

const storage = new Map();
const listeners = new Set();
const originalWindow = globalThis.window;

globalThis.window = {
  localStorage: {
    getItem: (key) => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, String(value)),
    removeItem: (key) => storage.delete(key),
    get length() { return storage.size; },
    key: (index) => [...storage.keys()][index] ?? null,
  },
  addEventListener: (type, handler) => { if (type === "storage") listeners.add(handler); },
  removeEventListener: (type, handler) => { if (type === "storage") listeners.delete(handler); },
};

const { setToken, subscribeTokenChanges } = await import("../../app/auth-session.ts");

const TOKEN_KEY = "silicon_notebook_token";

/** Another tab wrote `key`: storage already holds the value, then the event arrives here. */
function otherTabWrites(key, newValue) {
  if (key === null) storage.clear();
  else if (newValue === null) storage.delete(key);
  else storage.set(key, newValue);
  for (const handler of [...listeners]) handler({ key, newValue });
}

function subscribed(token = "auto-account") {
  storage.clear();
  storage.set(TOKEN_KEY, token);
  let reloads = 0;
  const unsubscribe = subscribeTokenChanges(() => { reloads += 1; });
  return { reloads: () => reloads, unsubscribe };
}

test.after(() => { globalThis.window = originalWindow; });

for (const [label, key, value] of [
  ["a migrated account's token", TOKEN_KEY, "legacy-account"],
  ["a sign-out", TOKEN_KEY, null],
  ["another account's sign-in", TOKEN_KEY, "other-account"],
  ["localStorage.clear()", null, null],
]) {
  test(`${label} in another tab reloads this tab exactly once`, () => {
    const tab = subscribed();
    otherTabWrites(key, value);
    assert.equal(tab.reloads(), 1);
    otherTabWrites(key, value);
    assert.equal(tab.reloads(), 1, "a repeated identical value is not another switch");
    tab.unsubscribe();
  });
}

test("unrelated keys never reload", () => {
  const tab = subscribed();
  otherTabWrites("silicon_notebook_sso_return_location", "/");
  otherTabWrites("some_other_key", "value");
  assert.equal(tab.reloads(), 0);
  tab.unsubscribe();
});

test("an event carrying this tab's own token does not reload", () => {
  const tab = subscribed("auto-account");
  otherTabWrites(TOKEN_KEY, "auto-account");
  assert.equal(tab.reloads(), 0);
  tab.unsubscribe();
});

test("this tab's own sign-in or migration becomes the baseline, so it does not loop", () => {
  const tab = subscribed("");
  setToken("legacy-account");
  otherTabWrites(TOKEN_KEY, "legacy-account");
  assert.equal(tab.reloads(), 0);
  otherTabWrites(TOKEN_KEY, null);
  assert.equal(tab.reloads(), 1);
  tab.unsubscribe();
});

test("unsubscribing stops listening", () => {
  const tab = subscribed();
  tab.unsubscribe();
  otherTabWrites(TOKEN_KEY, "legacy-account");
  assert.equal(tab.reloads(), 0);
});

test("unreadable storage degrades to not listening instead of throwing", () => {
  const { getItem } = globalThis.window.localStorage;
  globalThis.window.localStorage.getItem = () => { throw new DOMException("denied", "SecurityError"); };
  try {
    const unsubscribe = subscribeTokenChanges(() => assert.fail("must not subscribe"));
    assert.equal(listeners.size, 0);
    unsubscribe();
  } finally {
    globalThis.window.localStorage.getItem = getItem;
  }
});
