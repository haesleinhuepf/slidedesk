const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync("src/slidedesk/server/static/js/app.js", "utf8").replace(/\r\n/g, "\n");

// Extract the top-level helpers that fitView depends on.
function extract(name) {
  const start = source.indexOf(`  function ${name}(`);
  assert(start !== -1, `${name} not found in app.js`);
  const end = source.indexOf("\n  }\n", start);
  return source.slice(start, end + 4);
}
const fitSource = ["assignPositions", "layoutItems", "viewFor", "applyView", "interruptView", "clampTransform", "fitView"]
  .map(extract).join("\n");

const SLIDE_HEIGHT = 140;
const SLIDE_WIDTH = Math.round((SLIDE_HEIGHT * 16) / 9);
const GAP = 18;
const CELL_W = SLIDE_WIDTH + GAP;
const CELL_H = SLIDE_HEIGHT + 16 + GAP;
const PADDING = 24;
const TOP = 96 - 52 + PADDING; // below the breadcrumb

function transform(x = 0, y = 0, k = 1) {
  return { x, y, k, translate: (a, b) => transform(x + a * k, y + b * k, k), scale: (s) => transform(x, y, k * s) };
}

function fit(count, focusIndex, position, { mode = "deck", width = 1400, height = 900, current = transform(-100, -200, 0.7), timer = null } = {}) {
  const applied = [];
  const viewport = { clientWidth: width, clientHeight: height, getBoundingClientRect: () => ({ top: 52, width, height }) };
  const chain = new Proxy({}, { get: (_, key) => (key === "then" ? undefined : () => chain) });
  const context = {
    viewport,
    document: { getElementById: () => ({ getBoundingClientRect: () => ({ bottom: 96 }) }) },
    renderedItems: Array.from({ length: count }, (_, i) => ({ slide: { id: i }, x: 0, y: i * CELL_H })),
    state: { mode, focusSlideId: focusIndex },
    SLIDE_WIDTH, SLIDE_HEIGHT, CELL_W, CELL_H, GAP, PADDING,
    DEFAULT_SCALE: 1, MIN_SCALE: 0.05, MAX_SCALE: 32, MODE_TRANSITION_MS: 500,
    layoutCols: 1, layoutShift: 0, layoutAnchor: null, programmaticView: false,
    viewTimer: timer, zoomIdleTimer: 123, autoZooming: true,
    clearTimeout() {},
    canvas: { selectAll: () => chain },
    zoomBehavior: { transform: "transform" },
    d3: {
      zoomIdentity: transform(),
      zoomTransform: () => current,
      easeCubicOut: (t) => t,
      select: (el) => ({
        call(fn, target) {
          assert.equal(el, viewport);
          assert.equal(fn, "transform");
          assert.equal(context.programmaticView, true, "view changes must be flagged as programmatic");
          applied.push(target);
          return this;
        },
      }),
    },
    position,
  };
  vm.runInNewContext(fitSource + "fitView(position);", context);
  return { target: applied.at(-1), applied, items: context.renderedItems, context };
}

const screen = (target, item) => ({ x: target.x + item.x * target.k, y: target.y + item.y * target.k });

test("default view shows the first slide at the default scale below the breadcrumb", () => {
  const { target, items, context } = fit(30, null, null, { mode: "search" });
  assert.equal(target.k, 1);
  assert.deepEqual(screen(target, items[0]), { x: PADDING, y: TOP });
  assert.equal(context.programmaticView, false);
});
test("columns follow the available viewport width", () => {
  for (const [width, cols] of [[1400, 5], [600, 2], [200, 1]]) {
    const { items, context } = fit(30, null, null, { mode: "search", width });
    assert.equal(context.layoutCols, cols);
    assert.equal(items[cols - 1].y, 0);
    if (cols < items.length) assert.equal(items[cols].y, CELL_H);
  }
});
test("clicked slide keeps its scale and screen position", () => {
  for (const mode of ["deck", "similar"]) {
    for (const index of [48, 99]) {
      const position = { x: 900, y: 400 };
      const { target, items } = fit(100, index, position, { mode });
      assert.equal(target.k, 0.7);
      const pos = screen(target, items[index]);
      assert(Math.abs(pos.x - position.x) < 1e-8);
      assert(Math.abs(pos.y - position.y) < 1e-8);
    }
  }
});
test("empty space above the first row is removed", () => {
  for (const index of [0, 4, 5]) {
    const { target, items } = fit(30, index, { x: 900, y: 400 });
    assert.equal(target.y, TOP);
    assert(Math.abs(screen(target, items[index]).x - 900) < 1e-8);
  }
});
test("clicked position is ignored outside deck and similar modes", () => {
  const { target, items } = fit(30, 7, { x: 900, y: 400 }, { mode: "search" });
  assert.equal(target.k, 1);
  assert.deepEqual(screen(target, items[0]), { x: PADDING, y: TOP });
});
test("missing focus slide falls back to the default view", () => {
  const { target, items } = fit(10, 42, { x: 900, y: 400 });
  assert.equal(target.k, 1);
  assert.deepEqual(screen(target, items[0]), { x: PADDING, y: TOP });
});
test("running animations are interrupted", () => {
  let stopped = false;
  const { context } = fit(5, null, null, { timer: { stop() { stopped = true; } } });
  assert(stopped);
  assert.equal(context.viewTimer, null);
  assert.equal(context.zoomIdleTimer, null);
  assert.equal(context.autoZooming, false);
});
test("empty results leave the view unchanged", () => {
  const { applied } = fit(0, null, null);
  assert.equal(applied.length, 0);
});
