const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync("src/slidedesk/server/static/js/app.js", "utf8");
const autoZoomSource = source.slice(source.indexOf("  function scheduleAutoZoom("), source.indexOf("  function animateView("));

function autoZoom(k) {
  const calls = [];
  const item = { slide: { id: 12 }, x: 40, y: 60 };
  const viewport = { clientWidth: 1400, clientHeight: 900 };
  const transform = {
    k,
    x: viewport.clientWidth / 2 - (item.x + 249 / 2) * k,
    y: viewport.clientHeight / 2 - (item.y + 140 / 2) * k,
  };
  const context = {
    AUTO_ZOOM_THRESHOLD: 0.7,
    AUTO_ZOOM_TARGET: 0.95,
    ZOOM_IDLE_MS: 350,
    SLIDE_WIDTH: 249,
    SLIDE_HEIGHT: 140,
    CELL_W: 267,
    CELL_H: 174,
    renderedItems: [item],
    viewport,
    autoZooming: false,
    zoomIdleTimer: null,
    clearTimeout() {},
    setTimeout(callback, delay) { assert.equal(delay, 350); callback(); return 1; },
    d3: { zoomTransform: () => transform },
    animateView(...args) { calls.push(args); },
  };
  vm.runInNewContext(autoZoomSource + "scheduleAutoZoom();", context);
  return calls;
}

test("slides over 70% width are centered and adjusted to 95%", () => {
  const calls = autoZoom(1400 * 0.8 / 249);
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], 1400 * 0.95 / 249);
  assert.equal(calls[0][1].id, 12);
  assert.equal(calls[0][2], 700);
  assert.equal(calls[0][3], 450);
});

test("slides at or below 70% width are not auto-zoomed", () => {
  assert.equal(autoZoom(1400 * 0.7 / 249).length, 0);
});
