const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync("src/slidedeck/server/static/js/app.js", "utf8");
const fitSource = source.slice(source.indexOf("  function fitView("), source.indexOf("  // -- data loading"));

function fit(count, focusIndex, position, width = 1400, height = 900) {
  const calls = [];
  function transform(x = 0, y = 0, k = 1) {
    return { x, y, k, translate: (a, b) => transform(x + a, y + b, k), scale: (s) => transform(x, y, k * s) };
  }
  const selection = {
    interrupt() { return this; },
    transition() { return { duration(ms) { assert.equal(ms, 500); return this; }, call(fn, target) { calls.push({ animated: true, target }); } }; },
    call(fn, target) { calls.push({ animated: false, target }); return this; },
  };
  const nodes = { interrupt() { return this; }, filter() { return this; }, style() { return this; }, remove() { return this; } };
  const items = Array.from({ length: count }, (_, i) => ({ slide: { id: i }, x: i % 5 * 267, y: Math.floor(i / 5) * 174 }));
  const context = {
    viewport: { clientWidth: width, clientHeight: height, getBoundingClientRect: () => ({ top: 52 }) },
    document: { getElementById: () => ({ getBoundingClientRect: () => ({ bottom: 96 }) }) },
    renderedItems: items, state: { mode: focusIndex === null ? "search" : "deck", focusSlideId: focusIndex },
    SLIDE_WIDTH: 249, CELL_H: 174, GAP: 18, MODE_TRANSITION_MS: 500,
    canvas: { selectAll: () => nodes }, zoomBehavior: { transform() {} },
    d3: { zoomIdentity: transform(), zoomTransform: () => transform(-100, -200, 0.7), select: () => selection },
    position,
  };
  vm.runInNewContext(fitSource + "fitView(position);", context);
  assert.equal(calls.at(-1).animated, true, "camera transform must be applied to a D3 transition");
  const target = calls.at(-1).target;
  if (position) {
    const start = calls[0].target;
    assert(Math.abs(start.x + items[focusIndex].x * start.k - position.x) < 1e-8);
    assert(Math.abs(start.y + items[focusIndex].y * start.k - position.y) < 1e-8);
  }
  return { target, items };
}

test("five columns use the available width", () => {
  const { target } = fit(30, 0);
  assert.equal(target.x, 24);
  assert(Math.abs(1317 * target.k - 1352) < 1e-8);
});
test("short results fit their actual occupied width", () => {
  const { target } = fit(2, null);
  assert(Math.abs(516 * target.k - 1352) < 1e-8);
});
test("empty space above the first row is removed during entry", () => {
  for (const index of [0, 4, 5]) {
    const { target } = fit(30, index, { x: 900, y: 400 });
    assert.equal(target.y, 68);
  }
});
test("later rows retain their position when there is no empty space above the grid", () => {
  const { target, items } = fit(100, 48, { x: 900, y: 400 });
  assert(target.y < 68);
  assert(Math.abs(target.y + items[48].y * target.k - 400) < 1e-8);
});
test("clicked slides in every column and distant rows stay visible", () => {
  for (const index of [0, 4, 16, 27, 48, 99]) {
    for (const position of [{ x: 900, y: 400 }, { x: -50, y: 2000 }]) {
      const { target, items } = fit(100, index, position);
      const item = items[index];
      const x = target.x + item.x * target.k;
      const y = target.y + item.y * target.k;
      assert(x >= 24 - 1e-8 && x + 249 * target.k <= 1376 + 1e-8);
      assert(y >= 68 - 1e-8 && y + 156 * target.k <= 876 + 1e-8);
    }
  }
});
test("short viewport keeps a complete slide visible", () => {
  const { target } = fit(1, 0, null, 1800, 300);
  assert(target.k * 156 <= 208);
});
test("empty results yield a finite viewport", () => {
  const { target } = fit(0, null);
  assert(Number.isFinite(target.k) && Number.isFinite(target.y));
});
