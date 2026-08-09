import test from "node:test";
import assert from "node:assert/strict";

import { copyTextSafely } from "../../app/copy-text.ts";


function replaceGlobal(name, value) {
  const original = Object.getOwnPropertyDescriptor(globalThis, name);
  Object.defineProperty(globalThis, name, {
    configurable: true,
    writable: true,
    value,
  });
  return () => {
    if (original) Object.defineProperty(globalThis, name, original);
    else delete globalThis[name];
  };
}


test("copyTextSafely falls back when Clipboard API is unavailable", async () => {
  let selected = false;
  let removed = false;
  let appended = false;
  const textarea = {
    value: "",
    style: {},
    setAttribute() {},
    select() { selected = true; },
    remove() { removed = true; },
  };
  const restoreNavigator = replaceGlobal("navigator", {});
  const restoreDocument = replaceGlobal("document", {
    createElement(tag) {
      assert.equal(tag, "textarea");
      return textarea;
    },
    execCommand(command) {
      assert.equal(command, "copy");
      return true;
    },
    body: {
      appendChild(node) {
        assert.equal(node, textarea);
        appended = true;
      },
    },
  });

  try {
    assert.equal(await copyTextSafely("https://example.test/?share=shr-1"), true);
    assert.equal(textarea.value, "https://example.test/?share=shr-1");
    assert.equal(appended, true);
    assert.equal(selected, true);
    assert.equal(removed, true);
  } finally {
    restoreDocument();
    restoreNavigator();
  }
});


test("copyTextSafely contains Clipboard API rejection", async () => {
  const restoreNavigator = replaceGlobal("navigator", {
    clipboard: {
      async writeText() {
        throw new Error("clipboard denied");
      },
    },
  });

  try {
    assert.equal(await copyTextSafely("https://example.test/?share=shr-2"), false);
  } finally {
    restoreNavigator();
  }
});


test("copyTextSafely reports a failed DOM fallback and always removes its textarea", async () => {
  let removed = false;
  const textarea = {
    value: "",
    style: {},
    setAttribute() {},
    select() {},
    remove() { removed = true; },
  };
  const restoreNavigator = replaceGlobal("navigator", {});
  const restoreDocument = replaceGlobal("document", {
    createElement() { return textarea; },
    execCommand() { return false; },
    body: { appendChild() {} },
  });

  try {
    assert.equal(await copyTextSafely("https://example.test/?share=shr-3"), false);
    assert.equal(removed, true);
  } finally {
    restoreDocument();
    restoreNavigator();
  }
});
