// The browser's save (spawner/pages/save.js): what a player's progress looks
// like after a reload, a forged save, a server rebuild or a private window.
// save.js is a plain browser script, so it's evaluated here with a fake
// localStorage. Runs in Node or Deno; test_save_js.py runs it with the unit tests.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const src = readFileSync(new URL("../spawner/pages/save.js", import.meta.url), "utf8");
let store = {};
let broken = false;                       // a private window: storage throws
const fakeStorage = {
  getItem(k) { if (broken) throw new Error("denied"); return k in store ? store[k] : null; },
  setItem(k, v) { if (broken) throw new Error("denied"); store[k] = String(v); },
};
// Passed in as a parameter: Deno has a real, persistent localStorage global.
const { loadSave, storeSave, topLevel, mergeSave, SAVE_KEY } = new Function("localStorage",
  src + "; return {loadSave, storeSave, topLevel, mergeSave, SAVE_KEY};")(fakeStorage);
const PW = "A".repeat(32);

const saved = (levels) => { store = { [SAVE_KEY]: JSON.stringify({ v: 1, levels }) }; };
let failed = 0;
function check(name, fn) {
  store = {}; broken = false;
  try { fn(); console.log("ok   " + name); }
  catch (e) { failed++; console.log("FAIL " + name + "\n     " + e.message.split("\n").join("\n     ")); }
}

check("a new browser has nothing saved", () => {
  assert.deepEqual(loadSave(), {});
});

check("a save survives a reload", () => {
  storeSave({ 3: PW, 7: "b9" });
  assert.deepEqual(loadSave(), { 3: PW, 7: "b9" });
  assert.deepEqual(JSON.parse(store[SAVE_KEY]), { v: 1, levels: { 3: PW, 7: "b9" } });
});

check("a corrupt save reads as empty, not an error", () => {
  store = { [SAVE_KEY]: "{not json" };
  assert.deepEqual(loadSave(), {});
  store = { [SAVE_KEY]: '{"levels":"x"}' };
  assert.deepEqual(loadSave(), {});
});

check("only levels 1-20 with plain passwords are kept", () => {
  saved({
    1: PW, 20: PW,                                   // kept
    0: PW, 21: PW, "07": PW, x: PW,                  // level 0 needs none; 21 is Dave (resume caps at 20)
    5: 123, 6: "", 8: "a b", 9: "<img src=x>", 10: "A".repeat(65),
  });
  assert.deepEqual(loadSave(), { 1: PW, 20: PW });
});

check("a private window neither breaks nor saves", () => {
  broken = true;
  storeSave({ 3: PW });
  assert.deepEqual(loadSave(), {});
});

check("the top level is compared as a number", () => {
  assert.equal(topLevel({ 9: PW, 10: PW, 2: PW }), 10);
  assert.equal(topLevel({}), 0);
});

check("the server's rejections and passwords win", () => {
  // The box rejected a forged level 12 and the image was rebuilt (new level 3).
  const lv = mergeSave({ 3: "old", 5: PW, 12: "forged" }, { levels: { 3: PW, 6: PW }, drop: [12] });
  assert.deepEqual(lv, { 3: PW, 5: PW, 6: PW });
  assert.deepEqual(mergeSave({ 5: PW }, { levels: {} }), { 5: PW });   // no drop list
});

console.log(failed ? `\n${failed} failed` : "\nall passed");
if (failed) (globalThis.Deno ? Deno.exit(1) : process.exit(1));
