/* slidedesk GUI: D3-powered zoom/pan view of individual slides, with four
 * layouts driven by `state.mode`:
 *   - "pool":   all slides, most recently changed deck first (initial view).
 *   - "search": only slides matching the current search query.
 *   - "deck":   the full deck of a clicked slide, laid out left-to-right,
 *               keeping the clicked slide in sight while fitting the row.
 *   - "similar": embedding-based similar slides, laid out in a deck-style
 *               horizontal wrap, like the slide-deck view.
 */
(function () {
  "use strict";

  const SLIDE_HEIGHT = 140;
  const SLIDE_WIDTH = Math.round((SLIDE_HEIGHT * 16) / 9);
  const GAP = 18;
  const CELL_W = SLIDE_WIDTH + GAP;
  const CELL_H = SLIDE_HEIGHT + 16 + GAP; // Include the label below each slide.
  const PADDING = 24;
  const DEFAULT_SCALE = 1;
  const MIN_SCALE = 0.05;
  const MAX_SCALE = 32;
  const MODE_TRANSITION_MS = 500;
  const LONG_PRESS_MS = 1000;
  const MOVE_CANCEL_PX = 6;
  const KEY_PAN_PX = 40;
  const FAST_PAN_MULTIPLIER = 4;
  const ZOOM_NAV_MODES = new Set(["search", "deck", "similar"]);
  const ZOOM_NAV_THRESHOLD = 0.6; // show prev/next/selection buttons once a slide fills this much of the canvas
  const DOUBLE_CLICK_ZOOM_FRACTION = 0.8;

  const state = {
    decks: [],
    deckById: new Map(),
    slidesById: new Map(), // slide id -> { slide, deck }
    mode: "pool", // "pool" | "search" | "deck" | "similar"
    searchQuery: "",
    searchMode: "keyword",
    advancedSearch: false,
    searchOrder: [], // slide ids, relevance order (search mode)
    matchedSlideIds: new Set(),
    focusDeckId: null,
    focusSlideId: null,
    selection: new Set(),
    showHidden: false,
    showLayout: true,
    refreshingDeckIds: new Set(),
    simulation: null, // active d3-force simulation, if mode === "similar"
  };

  const canvas = d3.select("#canvas");
  let navigationRevision = 0;
  const canvasNode = canvas.node();
  const viewport = document.getElementById("viewport");
  let activeListGenerationId = null;

  // -- zoom / pan (mouse drag, touch pinch/pan, arrow keys / WASD) ---------
  let renderedItems = [];
  let layoutCols = 1;
  let layoutShift = 0;
  let layoutAnchor = null; // { id, left }: slide and its column, kept across re-renders
  let programmaticView = false;
  let lastTransform = d3.zoomIdentity;
  let viewTimer = null;

  // The slides form one reflowing list: columns follow from the zoom scale and
  // the viewport width, so zooming in shows fewer slides per row.
  function columnsFor(k) {
    return Math.max(1, Math.floor(((viewport.clientWidth - PADDING * 2) / k + GAP) / CELL_W));
  }

  // `shift` offsets the list so a chosen slide lands in a chosen column.
  function assignPositions(cols, shift = 0) {
    layoutCols = cols;
    layoutShift = shift;
    renderedItems.forEach((item, i) => {
      const j = i + shift;
      item.x = (j % cols) * CELL_W;
      item.y = Math.floor(j / cols) * CELL_H;
    });
  }

  function layoutItems(cols, shift) {
    assignPositions(cols, shift);
    const current = new Set(renderedItems);
    canvas.selectAll(".slide-thumb").filter((d) => current.has(d)).interrupt()
      .style("left", (d) => `${d.x}px`)
      .style("top", (d) => `${d.y}px`)
      .style("opacity", 1);
  }

  // Slide under (or nearest to) a screen point, with the point's fractional offset in its cell.
  function anchorAt(sx, sy, t) {
    const cx = (sx - t.x) / t.k;
    const cy = (sy - t.y) / t.k;
    let best = null;
    let bestDist = Infinity;
    renderedItems.forEach((item) => {
      const dx = Math.max(item.x - cx, 0, cx - (item.x + CELL_W));
      const dy = Math.max(item.y - cy, 0, cy - (item.y + CELL_H));
      const dist = Math.hypot(dx, dy);
      if (dist < bestDist) {
        bestDist = dist;
        best = item;
      }
    });
    return best && { id: best.slide.id, fx: (cx - best.x) / CELL_W, fy: (cy - best.y) / CELL_H };
  }

  // Wrap the rows around the anchor: it keeps its exact screen position, and
  // as many whole columns fit left and right of it as the viewport allows.
  function viewFor(k, anchor, sx, sy) {
    k = Math.min(MAX_SCALE, Math.max(MIN_SCALE, k));
    let index = anchor ? renderedItems.findIndex((it) => it.slide.id === anchor.id) : -1;
    if (index === -1 && renderedItems.length) {
      index = 0;
      anchor = { id: renderedItems[0].slide.id, fx: 0, fy: 0 };
    }
    if (index === -1) return d3.zoomIdentity.translate(sx, sy).scale(k);
    const cellW = CELL_W * k;
    const ax = sx - anchor.fx * cellW;
    const left = Math.max(0, Math.floor((ax - PADDING) / cellW));
    const originX = ax - left * cellW;
    const right = Math.floor(((viewport.clientWidth - PADDING - originX) / k - SLIDE_WIDTH) / CELL_W);
    const cols = Math.max(left + 1, right + 1);
    const shift = (((left - index) % cols) + cols) % cols;
    layoutAnchor = { id: anchor.id, left };
    if (cols !== layoutCols || shift !== layoutShift) layoutItems(cols, shift);
    const item = renderedItems[index];
    return clampTransform(d3.zoomIdentity
      .translate(originX, sy - (item.y + anchor.fy * CELL_H) * k)
      .scale(k));
  }

  function applyView(transform) {
    programmaticView = true;
    try {
      d3.select(viewport).call(zoomBehavior.transform, transform);
    } finally {
      programmaticView = false;
    }
  }

  function interruptView() {
    if (viewTimer) viewTimer.stop();
    viewTimer = null;
  }

  function animateView(k1, anchor, sx1, sy1, ms = 300) {
    interruptView();
    const t0 = d3.zoomTransform(viewport);
    const item = renderedItems.find((it) => it.slide.id === anchor.id);
    if (!item) return;
    const sx0 = t0.x + (item.x + anchor.fx * CELL_W) * t0.k;
    const sy0 = t0.y + (item.y + anchor.fy * CELL_H) * t0.k;
    k1 = Math.min(MAX_SCALE, Math.max(MIN_SCALE, k1));
    const timer = d3.timer((elapsed) => {
      const e = d3.easeCubicInOut(Math.min(1, elapsed / ms));
      applyView(viewFor(t0.k * Math.pow(k1 / t0.k, e), anchor, sx0 + (sx1 - sx0) * e, sy0 + (sy1 - sy0) * e));
      if (elapsed >= ms) timer.stop();
    });
    viewTimer = timer;
  }

  function focalPoint(sourceEvent) {
    const points = sourceEvent ? d3.pointers(sourceEvent, viewport) : [];
    if (!points.length) return [viewport.clientWidth / 2, viewport.clientHeight / 2];
    return [d3.mean(points, (p) => p[0]), d3.mean(points, (p) => p[1])];
  }

  const zoomBehavior = d3
    .zoom()
    .scaleExtent([MIN_SCALE, MAX_SCALE])
    .interpolate(d3.interpolate)
    // Wheel events are handled by handleWheel so zooming can reflow around the pointer.
    .filter((event) => event.type !== "wheel" && !event.ctrlKey && !event.button)
    .on("start", (event) => {
      viewport.classList.add("grabbing");
      if (event.sourceEvent) interruptView();
    })
    .on("end", () => viewport.classList.remove("grabbing"))
    .on("zoom", (event) => {
      let t = event.transform;
      if (!programmaticView && renderedItems.length && t.k !== lastTransform.k) {
        // Pinch zoom: reflow around the gesture, keeping the slide there in place.
        const [fx, fy] = focalPoint(event.sourceEvent);
        t = viewFor(t.k, anchorAt(fx, fy, lastTransform), fx, fy);
        viewport.__zoom = t;
      }
      lastTransform = t;
      canvas.style("transform", `translate(${t.x}px, ${t.y}px) scale(${t.k})`);
      updateZoomNav(t);
    });

  function bindViewport() {
    d3.select(viewport).call(zoomBehavior);
    d3.select(viewport).on("dblclick.zoom", null);
    d3.select(viewport).on("wheel.pan", handleWheel);
  }
  bindViewport();

  // D3 prevents the mouse's default focus change when starting a drag.
  // Explicitly leave toolbar inputs so subsequent navigation keys pan the view.
  viewport.addEventListener("pointerdown", () => {
    viewport.focus({ preventScroll: true });
  });

  // Clamp panning/zooming so the occupied content can't be scrolled far off-screen.
  function clampTransform(transform) {
    const rect = viewport.getBoundingClientRect();
    const items = renderedItems;
    const contentWidth = items.reduce((right, item) => Math.max(right, item.x + SLIDE_WIDTH), SLIDE_WIDTH);
    const contentHeight = items.reduce((bottom, item) => Math.max(bottom, item.y + (CELL_H - GAP)), CELL_H - GAP);
    const margin = 80; // keep at least this much of the content visible, in screen px
    const scaledWidth = contentWidth * transform.k;
    const scaledHeight = contentHeight * transform.k;
    const minX = margin - scaledWidth;
    const maxX = rect.width - margin;
    const minY = margin - scaledHeight;
    const maxY = rect.height - margin;
    const x = Math.max(Math.min(minX, maxX), Math.min(transform.x, Math.max(minX, maxX)));
    const y = Math.max(Math.min(minY, maxY), Math.min(transform.y, Math.max(minY, maxY)));
    return d3.zoomIdentity.translate(x, y).scale(transform.k);
  }

  // Wheel pans the canvas; shift+wheel zooms around the pointer, reflowing the slides.
  function handleWheel(event) {
    event.preventDefault();
    interruptView();
    const lineHeight = 16;
    const pageFactor = viewport.clientHeight;
    const factor = event.deltaMode === 1 ? lineHeight : event.deltaMode === 2 ? pageFactor : 1;
    const transform = d3.zoomTransform(viewport);
    if (event.shiftKey) {
      const rect = viewport.getBoundingClientRect();
      const sx = event.clientX - rect.left;
      const sy = event.clientY - rect.top;
      const delta = (event.deltaY || event.deltaX) * factor;
      applyView(viewFor(transform.k * Math.pow(2, -delta * 0.002), anchorAt(sx, sy, transform), sx, sy));
      return;
    }
    const next = transform.translate(-event.deltaX * factor / transform.k, -event.deltaY * factor / transform.k);
    applyView(clampTransform(next));
  }

  document.addEventListener("keydown", (event) => {
    if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey || event.isComposing) return;
    if (event.target.closest("input, textarea, select") || event.target.isContentEditable) return;
    if (document.getElementById("slide-context-menu").classList.contains("visible")) return;

    // Move the view toward the arrow, as when scrolling through the slides.
    let dx = 0;
    let dy = 0;
    switch (event.key.toLowerCase()) {
      case "a":
      case "arrowleft": dx = 1; break;
      case "d":
      case "arrowright": dx = -1; break;
      case "w":
      case "arrowup": dy = 1; break;
      case "s":
      case "arrowdown": dy = -1; break;
      default: return;
    }
    event.preventDefault();
    const step = KEY_PAN_PX * (event.shiftKey ? FAST_PAN_MULTIPLIER : 1);
    const transform = d3.zoomTransform(viewport);
    // D3 translations use canvas units; keep keyboard speed constant on screen.
    interruptView();
    d3.select(viewport).call(zoomBehavior.translateBy,
      dx * step / transform.k, dy * step / transform.k);
  });

  function screenToCanvas(clientX, clientY) {
    const t = d3.zoomTransform(viewport);
    const rect = viewport.getBoundingClientRect();
    return {
      x: (clientX - rect.left - t.x) / t.k,
      y: (clientY - rect.top - t.y) / t.k,
    };
  }

  // A clicked slide keeps its scale and screen position; other views reset to the default scale.
  function fitView(clickedPosition = null) {
    interruptView();
    const rect = viewport.getBoundingClientRect();
    const breadcrumb = document.getElementById("view-breadcrumb").getBoundingClientRect();
    const top = Math.max(0, breadcrumb.bottom - rect.top) + PADDING;
    if (!renderedItems.length) return;
    const focus = renderedItems.find((item) => item.slide.id === state.focusSlideId);
    let target;
    if (clickedPosition && focus && (state.mode === "deck" || state.mode === "similar")) {
      target = viewFor(d3.zoomTransform(viewport).k, { id: focus.slide.id, fx: 0, fy: 0 },
        clickedPosition.x, clickedPosition.y);
      // Don't leave empty canvas above the first row.
      if (target.y > top) target = d3.zoomIdentity.translate(target.x, top).scale(target.k);
    } else {
      target = viewFor(DEFAULT_SCALE, { id: renderedItems[0].slide.id, fx: 0, fy: 0 }, PADDING, top);
    }
    applyView(target);
  }

  // -- data loading -------------------------------------------------------
  async function loadDecks() {
    const res = await fetch("/api/decks");
    const decks = await res.json();
    state.decks = decks;
    indexData();
    if (state.mode === "pool") {
      updateSelectionUi();
      render();
    }
  }

  function indexData() {
    state.deckById = new Map(state.decks.map((d) => [d.id, d]));
    state.slidesById = new Map();
    state.decks.forEach((deck) => {
      deck.slides.forEach((slide) => state.slidesById.set(slide.id, { slide, deck }));
    });
  }

  function showStatusProgress(el, label, value, detail = label) {
    el.replaceChildren();
    el.title = detail;
    const caption = document.createElement("span");
    caption.textContent = label;
    const progress = document.createElement("progress");
    progress.max = 100;
    if (value !== null) progress.value = value;
    progress.setAttribute("aria-label", detail);
    el.append(caption, progress);
  }

  async function pollScanStatus() {
    try {
      const res = await fetch("/api/scan/status");
      const status = await res.json();
      const el = document.getElementById("scan-status");
      el.title = "";
      if (status.running) {
        const currentFile = status.current_file || "";
        const folder = currentFile.includes("/")
          ? currentFile.slice(0, currentFile.lastIndexOf("/"))
          : ".";
        showStatusProgress(el, `Scanning… ${folder}`, null,
          "Scanning… " + (currentFile || folder));
      } else if (status.error) {
        el.textContent = "Scan error: " + status.error;
      } else if (status.last_run_finished) {
        el.textContent = "Up to date";
      } else {
        el.textContent = "";
      }
    } catch (e) {
      /* ignore transient network errors */
    }
  }

  async function pollEmbeddingStatus() {
    try {
      const res = await fetch("/api/embeddings/status");
      const status = await res.json();
      const el = document.getElementById("embedding-status");
      el.title = "";
      const caches = [status, status.vision].filter((cache) => cache && cache.available);
      const total = caches.reduce((sum, cache) => sum + cache.total, 0);
      const embedded = caches.reduce((sum, cache) => sum + cache.embedded, 0);
      if (!total) {
        el.textContent = "";
      } else if (embedded < total) {
        const pct = Math.min(99, Math.round((embedded / total) * 100));
        showStatusProgress(el, `Embedding… ${pct}%`, pct,
          `Embedding slides: ${embedded}/${total}`);
      } else {
        el.textContent = "";
      }
    } catch (e) {
      /* ignore transient network errors */
    }
  }

  // -- layouts --------------------------------------------------------------
  function poolItems() {
    // Startup/overview shows only each deck's first (lowest-index) visible slide.
    const items = [];
    state.decks.forEach((deck) => {
      const candidates = deck.slides
        .filter((s) => state.showHidden || !s.hidden)
        .sort((a, b) => a.index_in_deck - b.index_in_deck);
      if (candidates.length) items.push({ slide: candidates[0], deck });
    });
    items.sort((a, b) => b.deck.pptx_mtime - a.deck.pptx_mtime);
    return items;
  }

  function searchItems() {
    return state.searchOrder
      .map((id) => state.slidesById.get(id))
      .filter((entry) => entry && (state.showHidden || !entry.slide.hidden))
      .map((entry) => ({ ...entry }));
  }

  function deckItems() {
    const deck = state.deckById.get(state.focusDeckId);
    if (!deck) return [];
    const slides = deck.slides
      .filter((s) => state.showHidden || !s.hidden)
      .slice()
      .sort((a, b) => a.index_in_deck - b.index_in_deck);
    return slides.map((slide) => ({ slide, deck }));
  }

  // -- rendering (pool / search / deck) --------------------------------------
  function slideImageUrl(id) {
    return `/api/slides/${id}/image${state.showLayout ? "" : "?layout=false"}`;
  }

  function thumbClasses(item) {
    const classes = ["slide-thumb"];
    if (state.selection.has(item.slide.id)) classes.push("selected");
    if (item.slide.hidden) classes.push("hidden-slide");
    if (state.mode === "search" && state.matchedSlideIds.has(item.slide.id)) classes.push("matched");
    return classes.join(" ");
  }

  function thumbInnerHTML(item) {
    const badge = item.slide.hidden ? '<span class="badge">hidden</span>' : "";
    const label = `<span class="deck-label">${escapeHtml(item.deck.name)} #${item.slide.index_in_deck + 1} / ${item.deck.slides.length}</span>`;
    return `<img loading="lazy" src="${slideImageUrl(item.slide.id)}" alt="slide ${item.slide.index_in_deck + 1}" />${badge}${label}`;
  }

  function renderStatic(items) {
    renderedItems = items;
    // Periodic refreshes must not rewrap the rows: keep the anchored slide in its column.
    const anchorIndex = layoutAnchor ? items.findIndex((it) => it.slide.id === layoutAnchor.id) : -1;
    if (anchorIndex === -1) {
      assignPositions(columnsFor(d3.zoomTransform(viewport).k));
    } else {
      assignPositions(layoutCols, (((layoutAnchor.left - anchorIndex) % layoutCols) + layoutCols) % layoutCols);
    }
    if (state.simulation) {
      state.simulation.stop();
      state.simulation = null;
    }
    updateBreadcrumb();

    const sel = canvas.selectAll(".slide-thumb").data(items, (d) => d.slide.id);

    sel.exit().transition().duration(250).style("opacity", 0).remove();

    const entered = sel
      .enter()
      .append("div")
      .style("width", `${SLIDE_WIDTH}px`)
      .style("height", `${SLIDE_HEIGHT}px`)
      .style("opacity", 0)
      .style("left", (d) => `${(d.enterFrom || d).x}px`)
      .style("top", (d) => `${(d.enterFrom || d).y}px`);

    entered
      .merge(sel)
      .attr("class", thumbClasses)
      .attr("data-slide-id", (d) => d.slide.id)
      .attr("title", (d) => `${d.deck.name} — slide ${d.slide.index_in_deck + 1}`)
      .html(thumbInnerHTML);

    entered
      .merge(sel)
      .transition()
      .duration(400)
      .style("left", (d) => `${d.x}px`)
      .style("top", (d) => `${d.y}px`)
      .style("opacity", 1);

    updateSelectionUi();
    updateZoomNav(d3.zoomTransform(viewport));
  }

  function render() {
    if (state.mode === "similar") {
      const source = state.slidesById.get(state.focusSlideId);
      if (source) {
        const similar = (state.similarSlideIds || [])
          .map((id) => state.slidesById.get(id)?.slide).filter(Boolean);
        startSimilarSimulation(source.slide, similar);
        return;
      }
    }
    let items;
    if (state.mode === "deck") items = deckItems();
    else if (state.mode === "search") items = searchItems();
    else items = poolItems();
    renderStatic(items);
  }

  // Entries stay newest-first; moving through history only changes the cursor.
  const navigationHistory = [];
  let historyIndex = 0;

  function captureLocation() {
    return {
      mode: state.mode,
      searchQuery: state.searchQuery,
      searchMode: state.searchMode,
      advancedSearch: state.advancedSearch,
      searchOrder: [...state.searchOrder],
      matchedSlideIds: new Set(state.matchedSlideIds),
      focusDeckId: state.focusDeckId,
      focusSlideId: state.focusSlideId,
      similarSlideIds: [...(state.similarSlideIds || [])],
      showHidden: state.showHidden,
      transform: d3.zoomTransform(viewport),
    };
  }

  function saveLocation() {
    if (navigationHistory.length) navigationHistory[historyIndex] = captureLocation();
  }

  function recordLocation() {
    navigationHistory.splice(0, historyIndex);
    historyIndex = 0;
    navigationHistory.unshift(captureLocation());
    updateBreadcrumb();
  }

  function locationLabel(location) {
    if (location.mode === "search" && location.advancedSearch) return `Advanced search (${location.searchOrder.length} slides)`;
    if (location.mode === "search") return `Search for "${location.searchQuery}"${location.searchMode === "semantic" ? " (AI)" : ""}`;
    if (location.mode === "deck") return state.deckById.get(location.focusDeckId)?.name || "Slide deck";
    if (location.mode === "similar") {
      const entry = state.slidesById.get(location.focusSlideId);
      return entry ? `Similarity search: ${entry.deck.name}, slide ${entry.slide.index_in_deck + 1}` : "Similarity search";
    }
    return "Pool";
  }

  function updateBreadcrumb() {
    const select = document.getElementById("history-select");
    select.replaceChildren(...navigationHistory.map((location, index) => {
      const option = document.createElement("option");
      option.value = String(index);
      option.textContent = locationLabel(location);
      return option;
    }));
    select.value = String(historyIndex);
    select.title = navigationHistory.length ? locationLabel(navigationHistory[historyIndex]) : "Pool";
    document.getElementById("history-back-btn").disabled = historyIndex >= navigationHistory.length - 1;
    document.getElementById("history-forward-btn").disabled = historyIndex === 0;
  }

  function visitHistory(index) {
    if (!Number.isInteger(index) || index < 0 || index >= navigationHistory.length || index === historyIndex) return;
    navigationRevision += 1;
    d3.select(viewport).interrupt();
    saveLocation();
    historyIndex = index;
    const { transform, ...location } = navigationHistory[index];
    Object.assign(state, location);
    document.getElementById("search-box").value = state.searchQuery;
    document.getElementById("search-mode").value = state.searchMode;
    document.getElementById("toggle-hidden").checked = state.showHidden;
    hideContextMenu();
    render();
    fitView();
    updateBreadcrumb();
  }

  // -- similar-slides force layout --------------------------------------------
  async function showSimilar(slideId, el) {
    const revision = ++navigationRevision;
    hideContextMenu();
    const entry = state.slidesById.get(slideId);
    if (!entry) return;
    let similar;
    try {
      const res = await fetch(`/api/slides/${slideId}/similar`);
      similar = await res.json();
      if (revision !== navigationRevision) return;
      if (similar && !Array.isArray(similar) && similar.error) throw new Error(similar.error);
    } catch (err) {
      if (revision !== navigationRevision) return;
      window.alert("Could not load similar slides: " + err.message);
      return;
    }
    if (!Array.isArray(similar) || similar.length === 0) {
      window.alert("No similar slides found (this slide may not be embedded yet).");
      return;
    }

    saveLocation();
    const clickedPosition = clickedSlidePosition(el);
    state.mode = "similar";
    state.similarSlideIds = similar.map((slide) => slide.id);
    state.focusSlideId = slideId;
    recordLocation();
    startSimilarSimulation(entry.slide, similar);
    fitView(clickedPosition);
  }

  async function copySlideToClipboard(slideId) {
    try {
      const res = await fetch(`/api/slides/${slideId}/copy`, { method: "POST" });
      const data = await res.json();
      if (!res.ok || data.error) throw new Error(data.error || "Copy failed");
    } catch (error) {
      window.alert("Could not copy slide: " + error.message);
    }
  }

  function startSimilarSimulation(sourceSlide, similarSlides) {
    if (state.simulation) state.simulation.stop();

    const items = [sourceSlide, ...similarSlides]
      .filter((slide) => state.showHidden || !slide.hidden)
      .map((slide) => ({
        slide,
        deck: state.deckById.get(slide.deck_id) || { id: slide.deck_id, name: "(deck)" },
      }));

    renderStatic(items);
    updateBreadcrumb();
    state.simulation = null;
  }

  // -- click / long-press handling ------------------------------------------
  let pressTimer = null;
  let pressStart = null;
  let pressEl = null;
  let longPressFired = false;
  let suppressNextDocumentClick = false;

  canvasNode.addEventListener("pointerdown", (event) => {
    const el = event.target.closest(".slide-thumb");
    if (!el || event.button !== 0) return;
    pressEl = el;
    pressStart = { x: event.clientX, y: event.clientY };
    longPressFired = false;
    clearTimeout(pressTimer);
    pressTimer = setTimeout(() => {
      longPressFired = true;
      suppressNextDocumentClick = true;
      showContextMenu(el, pressStart.x, pressStart.y);
    }, LONG_PRESS_MS);
  });

  canvasNode.addEventListener("pointermove", (event) => {
    if (!pressStart) return;
    const dx = event.clientX - pressStart.x;
    const dy = event.clientY - pressStart.y;
    if (Math.hypot(dx, dy) > MOVE_CANCEL_PX) {
      clearTimeout(pressTimer);
      pressStart = null;
    }
  });

  canvasNode.addEventListener("pointerup", (event) => {
    if (event.button !== 0) return;
    clearTimeout(pressTimer);
    const el = pressEl;
    pressEl = null;
    if (el && !longPressFired && pressStart) {
      const slideId = Number(el.getAttribute("data-slide-id"));
      onSlideClick(slideId, el);
    }
    pressStart = null;
  });

  canvasNode.addEventListener("pointerleave", () => clearTimeout(pressTimer));

  canvasNode.addEventListener("contextmenu", (event) => {
    const el = event.target.closest(".slide-thumb");
    if (!el) return;
    event.preventDefault();
    clearTimeout(pressTimer);
    pressEl = null;
    pressStart = null;
    showContextMenu(el, event.clientX, event.clientY);
  });

  canvasNode.addEventListener("dblclick", (event) => {
    const el = event.target.closest(".slide-thumb");
    if (!el) return;
    event.preventDefault();
    // Slides have no default double-click zoom; search/deck/similar zoom in on the slide instead.
    event.stopPropagation();
    if (!ZOOM_NAV_MODES.has(state.mode)) return;
    zoomToSlideFraction(Number(el.getAttribute("data-slide-id")), DOUBLE_CLICK_ZOOM_FRACTION);
  });

  function zoomToSlide(slideId, scale) {
    animateView(scale,
      { id: slideId, fx: SLIDE_WIDTH / 2 / CELL_W, fy: SLIDE_HEIGHT / 2 / CELL_H },
      viewport.clientWidth / 2, viewport.clientHeight / 2);
  }

  function zoomToSlideFraction(slideId, fraction) {
    zoomToSlide(slideId, Math.min(
      (viewport.clientWidth * fraction) / SLIDE_WIDTH,
      (viewport.clientHeight * fraction) / SLIDE_HEIGHT));
  }

  // -- zoomed-in slide navigation overlay (prev / next / selection toggle) ---
  const zoomNavOverlay = document.getElementById("slide-zoom-nav");
  const zoomNavPrev = document.getElementById("zoom-nav-prev");
  const zoomNavNext = document.getElementById("zoom-nav-next");
  const zoomNavToggle = document.getElementById("zoom-nav-toggle");
  let zoomNavSlideId = null;

  function hideZoomNav() {
    zoomNavSlideId = null;
    zoomNavOverlay.classList.add("hidden");
  }

  function updateZoomNav(transform) {
    if (!ZOOM_NAV_MODES.has(state.mode) || !renderedItems.length) {
      hideZoomNav();
      return;
    }
    const widthFrac = (SLIDE_WIDTH * transform.k) / viewport.clientWidth;
    const heightFrac = (SLIDE_HEIGHT * transform.k) / viewport.clientHeight;
    if (Math.max(widthFrac, heightFrac) <= ZOOM_NAV_THRESHOLD) {
      hideZoomNav();
      return;
    }
    const centerX = (viewport.clientWidth / 2 - transform.x) / transform.k;
    const centerY = (viewport.clientHeight / 2 - transform.y) / transform.k;
    let active = null;
    let bestDist = Infinity;
    renderedItems.forEach((item) => {
      const dist = Math.hypot(item.x + SLIDE_WIDTH / 2 - centerX, item.y + SLIDE_HEIGHT / 2 - centerY);
      if (dist < bestDist) {
        bestDist = dist;
        active = item;
      }
    });
    if (!active) {
      hideZoomNav();
      return;
    }
    // Only show the overlay while the closest slide is actually on screen.
    const screenCenterX = transform.x + (active.x + SLIDE_WIDTH / 2) * transform.k;
    const screenCenterY = transform.y + (active.y + SLIDE_HEIGHT / 2) * transform.k;
    if (screenCenterX < 0 || screenCenterX > viewport.clientWidth || screenCenterY < 0 || screenCenterY > viewport.clientHeight) {
      hideZoomNav();
      return;
    }
    zoomNavSlideId = active.slide.id;
    zoomNavOverlay.style.left = `${transform.x + active.x * transform.k}px`;
    zoomNavOverlay.style.top = `${transform.y + active.y * transform.k}px`;
    zoomNavOverlay.style.width = `${SLIDE_WIDTH * transform.k}px`;
    zoomNavOverlay.style.height = `${SLIDE_HEIGHT * transform.k}px`;
    const index = renderedItems.indexOf(active);
    zoomNavPrev.disabled = index <= 0;
    zoomNavNext.disabled = index === -1 || index >= renderedItems.length - 1;
    const selected = state.selection.has(zoomNavSlideId);
    zoomNavToggle.textContent = selected ? "−" : "+";
    zoomNavToggle.setAttribute("aria-label", selected ? "Remove from selection" : "Add to selection");
    zoomNavOverlay.classList.remove("hidden");
  }

  // Pan so the previous/next slide in the current list takes the on-screen
  // position of the currently zoomed slide, keeping the same zoom level.
  function navigateZoomNav(direction) {
    if (zoomNavSlideId == null) return;
    const index = renderedItems.findIndex((it) => it.slide.id === zoomNavSlideId);
    const nextIndex = index + direction;
    if (index === -1 || nextIndex < 0 || nextIndex >= renderedItems.length) return;
    const current = renderedItems[index];
    const next = renderedItems[nextIndex];
    const t = d3.zoomTransform(viewport);
    const screenX = t.x + current.x * t.k;
    const screenY = t.y + current.y * t.k;
    state.focusSlideId = next.slide.id;
    animateView(t.k, { id: next.slide.id, fx: 0, fy: 0 }, screenX, screenY, 250);
  }

  zoomNavPrev.addEventListener("click", (event) => {
    event.stopPropagation();
    navigateZoomNav(-1);
  });
  zoomNavNext.addEventListener("click", (event) => {
    event.stopPropagation();
    navigateZoomNav(1);
  });
  zoomNavToggle.addEventListener("click", (event) => {
    event.stopPropagation();
    if (zoomNavSlideId != null) toggleSelection(zoomNavSlideId);
  });

  function onSlideClick(slideId, el) {
    navigationRevision += 1;
    const entry = state.slidesById.get(slideId);
    if (!entry) return;
    saveLocation();
    const clickedPosition = clickedSlidePosition(el);
    state.focusDeckId = entry.deck.id;
    state.focusSlideId = slideId;
    state.mode = "deck";
    recordLocation();
    render();
    fitView(clickedPosition);
  }

  function toggleSelection(slideId) {
    if (state.selection.has(slideId)) {
      state.selection.delete(slideId);
    } else {
      state.selection.add(slideId);
    }
    canvas.selectAll(".slide-thumb").attr("class", thumbClasses);
    updateSelectionUi();
    updateZoomNav(d3.zoomTransform(viewport));
  }

  function updateSelectionUi() {
    for (const id of state.selection) {
      if (!state.slidesById.has(id)) state.selection.delete(id);
    }
    const count = state.selection.size;
    document.getElementById("selection-count").textContent = `Selected slides (${count})`;
    document.getElementById("export-btn").disabled = count === 0;
    document.getElementById("copy-selection-btn").disabled = count === 0;
    document.getElementById("clear-selection-btn").disabled = count === 0;
    renderSelectionBar();
  }

  function clickedSlidePosition(el) {
    if (!el || !canvasNode.contains(el)) return null;
    const slideRect = el.getBoundingClientRect();
    const viewportRect = viewport.getBoundingClientRect();
    return { x: slideRect.left - viewportRect.left, y: slideRect.top - viewportRect.top };
  }

  const selectionList = document.getElementById("selected-slides");
  let draggedSlideId = null;
  let selectionPressTimer = null;
  let selectionPressStart = null;
  let selectionLongPress = false;

  function renderSelectionBar() {
    if (draggedSlideId !== null) return;
    const cards = d3.select(selectionList).selectAll(".selection-slide")
      .data(Array.from(state.selection), (id) => id)
      .join("button")
      .attr("type", "button")
      .attr("class", (id) => `selection-slide${id === state.focusSlideId ? " active" : ""}`)
      .attr("draggable", "true")
      .attr("data-slide-id", (id) => id)
      .attr("title", "Click to show in deck. Hold or right-click for actions. Alt+Arrow keys to reorder.");
    cards.each(function (id, i) {
      const { slide, deck } = state.slidesById.get(id);
      const label = `${i + 1}. ${deck.name} #${slide.index_in_deck + 1} / ${deck.slides.length}`;
      this.setAttribute("aria-label", label);
      const markup = `<img loading="lazy" draggable="false" src="${slideImageUrl(id)}" alt="" /><span>${escapeHtml(label)}</span>`;
      if (this.innerHTML !== markup) this.innerHTML = markup;
    });
    cards.order();
  }

  function cancelSelectionPress() {
    clearTimeout(selectionPressTimer);
    selectionPressStart = null;
  }

  selectionList.addEventListener("pointerdown", (event) => {
    const card = event.target.closest(".selection-slide");
    if (!card || event.button !== 0) return;
    cancelSelectionPress();
    selectionLongPress = false;
    selectionPressStart = { x: event.clientX, y: event.clientY };
    selectionPressTimer = setTimeout(() => {
      selectionLongPress = true;
      suppressNextDocumentClick = true;
      showContextMenu(card, event.clientX, event.clientY);
    }, LONG_PRESS_MS);
  });
  selectionList.addEventListener("pointermove", (event) => {
    if (selectionPressStart && Math.hypot(event.clientX - selectionPressStart.x, event.clientY - selectionPressStart.y) > MOVE_CANCEL_PX) cancelSelectionPress();
  });
  document.addEventListener("pointerup", cancelSelectionPress);
  selectionList.addEventListener("pointercancel", cancelSelectionPress);
  selectionList.addEventListener("pointerleave", cancelSelectionPress);
  selectionList.addEventListener("click", (event) => {
    const card = event.target.closest(".selection-slide");
    if (card && !selectionLongPress) {
      hideContextMenu();
      onSlideClick(Number(card.dataset.slideId), card);
    }
    selectionLongPress = false;
  });
  selectionList.addEventListener("contextmenu", (event) => {
    const card = event.target.closest(".selection-slide");
    if (!card) return;
    event.preventDefault();
    cancelSelectionPress();
    showContextMenu(card, event.clientX, event.clientY);
  });
  selectionList.addEventListener("dragstart", (event) => {
    const card = event.target.closest(".selection-slide");
    if (!card) return;
    cancelSelectionPress();
    hideContextMenu();
    draggedSlideId = Number(card.dataset.slideId);
    event.dataTransfer.setData("text/plain", String(draggedSlideId));
    event.dataTransfer.effectAllowed = "move";
    card.classList.add("dragging");
  });
  function clearDropMarkers() {
    selectionList.querySelectorAll(".drop-before, .drop-after").forEach((card) => card.classList.remove("drop-before", "drop-after"));
  }
  selectionList.addEventListener("dragover", (event) => {
    if (draggedSlideId === null) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    clearDropMarkers();
    const card = event.target.closest(".selection-slide");
    if (card) {
      const rect = card.getBoundingClientRect();
      card.classList.add(event.clientY < rect.top + rect.height / 2 ? "drop-before" : "drop-after");
    }
    const bar = document.getElementById("selection-bar");
    const bounds = bar.getBoundingClientRect();
    if (event.clientY > bounds.bottom - 40) bar.scrollTop += 16;
    if (event.clientY < bounds.top + 40) bar.scrollTop -= 16;
  });
  selectionList.addEventListener("drop", (event) => {
    if (draggedSlideId === null) return;
    event.preventDefault();
    const card = event.target.closest(".selection-slide");
    const targetId = card ? Number(card.dataset.slideId) : null;
    if (targetId !== draggedSlideId) {
      const ids = Array.from(state.selection).filter((id) => id !== draggedSlideId);
      const index = card ? ids.indexOf(targetId) + (card.classList.contains("drop-after") ? 1 : 0) : ids.length;
      ids.splice(index, 0, draggedSlideId);
      state.selection = new Set(ids);
    }
    draggedSlideId = null;
    updateSelectionUi();
  });
  selectionList.addEventListener("dragend", () => {
    draggedSlideId = null;
    clearDropMarkers();
    updateSelectionUi();
  });
  selectionList.addEventListener("keydown", (event) => {
    const card = event.target.closest(".selection-slide");
    if (!card) return;
    if (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10")) {
      event.preventDefault();
      const rect = card.getBoundingClientRect();
      showContextMenu(card, rect.left, rect.top);
    }
    if (event.altKey && ["ArrowUp", "ArrowDown"].includes(event.key)) {
      event.preventDefault();
      const ids = Array.from(state.selection);
      const index = ids.indexOf(Number(card.dataset.slideId));
      const next = index + (event.key === "ArrowUp" ? -1 : 1);
      if (next < 0 || next >= ids.length) return;
      [ids[index], ids[next]] = [ids[next], ids[index]];
      state.selection = new Set(ids);
      updateSelectionUi();
      card.focus();
    }
  });

  function escapeHtml(str) {
    return String(str).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  // -- context menu ------------------------------------------------------------
  function setPanZoomEnabled(enabled) {
    if (enabled) {
      bindViewport();
    } else {
      d3.select(viewport).on(".zoom", null);
      d3.select(viewport).on("wheel.pan", null);
    }
  }

  function showContextMenu(el, clientX, clientY) {
    const menu = document.getElementById("slide-context-menu");
    menu.dataset.slideId = el.getAttribute("data-slide-id");
    menu.dataset.source = selectionList.contains(el) ? "selection" : "canvas";
    const { slide, deck } = state.slidesById.get(Number(menu.dataset.slideId));
    document.getElementById("menu-show-in-deck").disabled = state.mode === "deck";
    document.getElementById("menu-copy-slide").hidden = state.mode === "pool";
    document.getElementById("menu-toggle-slide-hidden").hidden = state.mode === "pool";
    document.getElementById("menu-toggle-deck-hidden").hidden = state.mode !== "pool";
    document.getElementById("menu-refresh-deck").hidden = state.mode !== "pool";
    document.getElementById("menu-toggle-slide-hidden").textContent = slide.hidden ? "Unhide slide" : "Hide slide";
    document.getElementById("menu-toggle-deck-hidden").textContent = deck.slides.every((s) => s.hidden) ? "Unhide slide deck" : "Hide slide deck";
    document.getElementById("menu-remove").hidden = menu.dataset.source !== "selection";
    document.getElementById("menu-add-selection").disabled = state.selection.has(Number(menu.dataset.slideId));
    menu.style.left = `${clientX}px`;
    menu.style.top = `${clientY}px`;
    menu.classList.add("visible");
    const bounds = menu.getBoundingClientRect();
    const left = menu.dataset.source === "selection" ? el.getBoundingClientRect().left - bounds.width - 8 : clientX;
    menu.style.left = `${Math.max(8, Math.min(left, window.innerWidth - bounds.width - 8))}px`;
    menu.style.top = `${Math.max(8, Math.min(clientY, window.innerHeight - bounds.height - 8))}px`;
    setPanZoomEnabled(false);
  }

  function hideContextMenu() {
    document.getElementById("slide-context-menu").classList.remove("visible");
    setPanZoomEnabled(true);
  }

  async function toggleHidden(entireDeck) {
    const menu = document.getElementById("slide-context-menu");
    const entry = state.slidesById.get(Number(menu.dataset.slideId));
    hideContextMenu();
    if (!entry) return;
    const { slide, deck } = entry;
    const hidden = entireDeck ? !deck.slides.every((s) => s.hidden) : !slide.hidden;
    const url = entireDeck ? `/api/decks/${deck.id}/slides/hidden` : `/api/slides/${slide.id}/hidden`;
    try {
      const res = await fetch(url, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ hidden }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Could not change visibility");
      await loadDecks();
      updateSelectionUi();
      render();
    } catch (error) {
      window.alert(error.message);
    }
  }

  document.getElementById("menu-toggle-slide-hidden").addEventListener("click", () => toggleHidden(false));
  document.getElementById("menu-toggle-deck-hidden").addEventListener("click", () => toggleHidden(true));

  document.getElementById("menu-zoom-slide").addEventListener("click", () => {
    const menu = document.getElementById("slide-context-menu");
    const slideId = Number(menu.dataset.slideId);
    let el = canvasNode.querySelector(`[data-slide-id="${slideId}"]`);
    hideContextMenu();
    if (!el) {
      onSlideClick(slideId, contextSlideElement(menu, slideId));
      el = canvasNode.querySelector(`[data-slide-id="${slideId}"]`);
    }
    if (!el) return;
    zoomToSlide(slideId, Math.max(0.2, Math.min(4,
      (viewport.clientWidth - 48) / SLIDE_WIDTH,
      (viewport.clientHeight - 120) / SLIDE_HEIGHT)));
  });

  document.getElementById("menu-add-selection").addEventListener("click", () => {
    const slideId = Number(document.getElementById("slide-context-menu").dataset.slideId);
    hideContextMenu();
    if (!state.selection.has(slideId)) toggleSelection(slideId);
  });

  document.getElementById("menu-show-in-deck").addEventListener("click", () => {
    const menu = document.getElementById("slide-context-menu");
    const slideId = Number(menu.dataset.slideId);
    hideContextMenu();
    const el = contextSlideElement(menu, slideId);
    if (el) onSlideClick(slideId, el);
  });

  document.getElementById("menu-show-similar").addEventListener("click", () => {
    const menu = document.getElementById("slide-context-menu");
    const slideId = Number(menu.dataset.slideId);
    const el = contextSlideElement(menu, slideId);
    if (el) showSimilar(slideId, el);
  });

  document.getElementById("menu-copy-slide").addEventListener("click", () => {
    const slideId = Number(document.getElementById("slide-context-menu").dataset.slideId);
    hideContextMenu();
    copySlideToClipboard(slideId);
  });

  async function openDeckAction(action) {
    const menu = document.getElementById("slide-context-menu");
    const entry = state.slidesById.get(Number(menu.dataset.slideId));
    hideContextMenu();
    if (!entry) return;
    try {
      const res = await fetch(`/api/decks/${entry.deck.id}/${action}`, { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Could not open slide deck");
    } catch (error) {
      window.alert(error.message);
    }
  }

  document.getElementById("menu-open-deck").addEventListener("click", () => openDeckAction("open"));
  document.getElementById("menu-open-deck-folder").addEventListener("click", () => openDeckAction("open-folder"));

  document.addEventListener("click", (event) => {
    if (suppressNextDocumentClick) {
      suppressNextDocumentClick = false;
      return;
    }
    const menu = document.getElementById("slide-context-menu");
    if (menu.classList.contains("visible") && !menu.contains(event.target)) hideContextMenu();
  });

  function contextSlideElement(menu, slideId) {
    const container = menu.dataset.source === "selection" ? selectionList : canvasNode;
    return container.querySelector(`[data-slide-id="${slideId}"]`);
  }

  document.getElementById("menu-remove").addEventListener("click", () => {
    const id = Number(document.getElementById("slide-context-menu").dataset.slideId);
    hideContextMenu();
    if (state.selection.has(id)) toggleSelection(id);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") hideContextMenu();
  });

  // -- deck refresh -------------------------------------------------------------
  document.getElementById("menu-refresh-deck").addEventListener("click", () => {
    const menu = document.getElementById("slide-context-menu");
    const entry = state.slidesById.get(Number(menu.dataset.slideId));
    hideContextMenu();
    if (entry) refreshDeck(entry.deck.id);
  });

  async function refreshDeck(deckId) {
    if (state.refreshingDeckIds.has(deckId)) return;
    state.refreshingDeckIds.add(deckId);
    try {
      const res = await fetch(`/api/decks/${deckId}/refresh`, { method: "POST" });
      const data = await res.json();
      if (!res.ok || data.error) throw new Error(data.error || "Refresh failed");
      await loadDecks();
      updateSelectionUi();
      render();
    } catch (error) {
      window.alert(error.message);
    } finally {
      state.refreshingDeckIds.delete(deckId);
    }
  }

  document.getElementById("history-back-btn").addEventListener("click", () => visitHistory(historyIndex + 1));
  document.getElementById("history-forward-btn").addEventListener("click", () => visitHistory(historyIndex - 1));
  document.getElementById("history-select").addEventListener("change", (event) => {
    visitHistory(Number(event.target.value));
    viewport.focus({ preventScroll: true });
  });

  // -- search --------------------------------------------------------------
  async function runSearch(query) {
    const revision = ++navigationRevision;
    query = query.trim();
    if (!query) {
      saveLocation();
      state.searchQuery = "";
      state.mode = "pool";
      state.matchedSlideIds = new Set();
      state.searchOrder = [];
      recordLocation();
      render();
      fitView();
      return;
    }
    const mode = document.getElementById("search-mode").value;
    const endpoint = mode === "semantic" ? "/api/search/semantic" : "/api/search";
    const res = await fetch(`${endpoint}?q=${encodeURIComponent(query)}`);
    const results = await res.json();
    if (revision !== navigationRevision) return;
    if (results && !Array.isArray(results) && results.error) {
      window.alert("Search failed: " + results.error);
      return;
    }
    const order = [];
    const matched = new Set();
    results.forEach((deck) => {
      (deck.matched_slides || []).forEach((s) => {
        order.push(s.id);
        matched.add(s.id);
      });
    });
    saveLocation();
    state.searchQuery = query;
    state.searchMode = mode;
    state.advancedSearch = false;
    state.advancedSearch = false;
    state.searchOrder = order;
    state.matchedSlideIds = matched;
    state.mode = "search";
    recordLocation();
    render();
    fitView();
  }

  function advancedSearchTopics(text) {
    return text.split(/\r?\n/)
      .map((line) => line.trim().replace(/^(?:[-*•]+|\d+[.)])\s*/, "").trim())
      .filter(Boolean);
  }

  async function generateAdvancedList() {
    const prompt = document.getElementById("advanced-search-prompt").value.trim();
    const button = document.getElementById("advanced-generate-list-btn");
    const cancelButton = document.getElementById("advanced-cancel-generation-btn");
    const status = document.getElementById("advanced-search-status");
    if (!prompt) {
      status.textContent = "Enter a prompt first.";
      return;
    }
    const generationId = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    activeListGenerationId = generationId;
    button.disabled = true;
    cancelButton.hidden = false;
    status.textContent = "Generating list…";
    const list = document.getElementById("advanced-search-list");
    list.value = "";
    try {
      const res = await fetch("/api/search/advanced/generate-list/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt, generation_id: generationId }),
      });
      if (!res.ok) {
        const data = await res.json();
        throw new Error(data.error || "List generation failed");
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let pending = "";
      let wasCancelled = false;
      while (true) {
        const { value, done } = await reader.read();
        pending += decoder.decode(value || new Uint8Array(), { stream: !done });
        const lines = pending.split("\n");
        pending = lines.pop();
        for (const line of lines) {
          if (!line) continue;
          const event = JSON.parse(line);
          if (event.error) throw new Error(event.error);
          if (event.text) {
            list.value += event.text;
            list.scrollTop = list.scrollHeight;
          }
          if (event.cancelled) wasCancelled = true;
        }
        if (done) break;
      }
      status.textContent = wasCancelled ? "Generation cancelled." : "List ready to edit.";
    } catch (error) {
      status.textContent = error.message;
    } finally {
      activeListGenerationId = null;
      button.disabled = false;
      cancelButton.hidden = true;
    }
  }

  async function cancelAdvancedListGeneration() {
    if (!activeListGenerationId) return;
    const generationId = activeListGenerationId;
    document.getElementById("advanced-search-status").textContent = "Cancelling generation…";
    try {
      await fetch("/api/search/advanced/generate-list/cancel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ generation_id: generationId }),
      });
    } catch (error) {
      document.getElementById("advanced-search-status").textContent = error.message;
    }
  }

  async function runAdvancedSearch() {
    const topics = advancedSearchTopics(document.getElementById("advanced-search-list").value);
    const perTopic = Number(document.getElementById("advanced-slides-per-topic").value);
    const dialog = document.getElementById("advanced-search-dialog");
    if (!topics.length) {
      document.getElementById("advanced-search-status").textContent = "Enter at least one bullet point.";
      return;
    }
    if (!Number.isInteger(perTopic) || perTopic < 1 || perTopic > 50) {
      document.getElementById("advanced-search-status").textContent = "Choose between 1 and 50 slides per bullet point.";
      return;
    }

    const revision = ++navigationRevision;
    saveLocation();
    state.searchQuery = "Advanced Search";
    state.searchMode = "semantic";
    state.advancedSearch = true;
    state.searchOrder = [];
    state.matchedSlideIds = new Set();
    state.mode = "search";
    recordLocation();
    render();
    fitView();
    dialog.close();

    for (let index = 0; index < topics.length; index += 1) {
      if (revision !== navigationRevision) return;
      try {
        const res = await fetch("/api/search/advanced/semantic", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ query: topics[index], top_k: perTopic }),
        });
        const slides = await res.json();
        if (revision !== navigationRevision) return;
        if (!res.ok || !Array.isArray(slides)) throw new Error(slides.error || "Semantic search failed");
        slides.forEach((slide) => {
          if (!state.matchedSlideIds.has(slide.id)) {
            state.matchedSlideIds.add(slide.id);
            state.searchOrder.push(slide.id);
          }
        });
        saveLocation();
        render();
        fitView();
      } catch (error) {
        if (revision === navigationRevision) window.alert(`Advanced search failed for "${topics[index]}": ${error.message}`);
        return;
      }
    }
  }

  // -- export --------------------------------------------------------------
  async function exportSelection() {
    const res = await fetch("/api/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slide_ids: Array.from(state.selection) }),
    });
    const data = await res.json();
    if (data.error) {
      window.alert("Export failed: " + data.error);
      return;
    }
    window.location = `/api/export/${encodeURIComponent(data.path)}/download`;
  }

  async function copySelection() {
    const res = await fetch("/api/copy", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slide_ids: Array.from(state.selection) }),
    });
    const data = await res.json();
    if (!res.ok || data.error) window.alert("Copy failed: " + (data.error || "unknown error"));
  }

  // -- wiring ----------------------------------------------------------------
  document.getElementById("exit-btn").addEventListener("click", async () => {
    if (!window.confirm("Exit SlideDesk? This will stop the server.")) return;
    try {
      await fetch("/api/shutdown", { method: "POST", keepalive: true });
    } catch (err) {
      // Server process exits before it can respond; ignore the resulting fetch error.
    }
    window.close();
    // Some browsers refuse to close tabs not opened by script; show a fallback message.
    document.body.innerHTML = "<p style=\"padding:2rem;font-family:sans-serif;color:#e8eaed;background:#1e2126;\">SlideDesk server stopped. You may close this tab.</p>";
  });

  document.getElementById("home-btn").addEventListener("click", () => {
    saveLocation();
    // Ignore pending search/similarity responses after returning home.
    navigationRevision += 1;
    state.mode = "pool";
    state.searchQuery = "";
    state.searchMode = "keyword";
    state.advancedSearch = false;
    state.searchOrder = [];
    state.matchedSlideIds = new Set();
    state.focusDeckId = null;
    state.focusSlideId = null;
    state.showHidden = false;
    document.getElementById("search-box").value = "";
    document.getElementById("search-mode").value = "keyword";
    document.getElementById("toggle-hidden").checked = false;
    hideContextMenu();
    recordLocation();
    render();
    fitView();
  });

  document.getElementById("search-btn").addEventListener("click", () => {
    runSearch(document.getElementById("search-box").value);
  });

  document.getElementById("advanced-search-btn").addEventListener("click", () => {
    document.getElementById("advanced-search-status").textContent = "";
    document.getElementById("advanced-search-dialog").showModal();
  });
  document.getElementById("advanced-generate-list-btn").addEventListener("click", generateAdvancedList);
  document.getElementById("advanced-cancel-generation-btn").addEventListener("click", cancelAdvancedListGeneration);
  document.getElementById("advanced-search-dialog").addEventListener("close", cancelAdvancedListGeneration);
  document.getElementById("advanced-generate-btn").addEventListener("click", runAdvancedSearch);

  document.getElementById("search-box").addEventListener("keydown", (event) => {
    if (event.key === "Enter") runSearch(event.target.value);
  });

  document.getElementById("search-mode").addEventListener("change", () => {
    const query = document.getElementById("search-box").value;
    if (query.trim()) runSearch(query);
  });

  document.getElementById("toggle-hidden").addEventListener("change", (event) => {
    state.showHidden = event.target.checked;
    render();
  });

  document.getElementById("toggle-layout").addEventListener("change", (event) => {
    state.showLayout = event.target.checked;
    render();
    updateSelectionUi();
  });

  document.getElementById("export-btn").addEventListener("click", exportSelection);
  document.getElementById("copy-selection-btn").addEventListener("click", copySelection);

  document.getElementById("clear-selection-btn").addEventListener("click", () => {
    state.selection.clear();
    canvas.selectAll(".slide-thumb").attr("class", thumbClasses);
    updateSelectionUi();
  });

  window.addEventListener("resize", () => {
    const t = d3.zoomTransform(viewport);
    const [fx, fy] = focalPoint(null);
    applyView(viewFor(t.k, anchorAt(fx, fy, t), fx, fy));
  });

  recordLocation();
  loadDecks().then(() => fitView());
  pollScanStatus();
  pollEmbeddingStatus();
  setInterval(loadDecks, 5000);
  setInterval(pollScanStatus, 2000);
  setInterval(pollEmbeddingStatus, 2000);
})();
