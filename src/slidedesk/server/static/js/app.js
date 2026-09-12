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
  const POOL_COLUMNS = 5;
  const DECK_ROW_LENGTH = POOL_COLUMNS; // wrap the full-deck view every this many slides
  const MODE_TRANSITION_MS = 500;
  const LONG_PRESS_MS = 1000;
  const MOVE_CANCEL_PX = 6;
  const KEY_PAN_PX = 40;
  const FAST_PAN_MULTIPLIER = 4;

  const state = {
    decks: [],
    deckById: new Map(),
    slidesById: new Map(), // slide id -> { slide, deck }
    mode: "pool", // "pool" | "search" | "deck" | "similar"
    searchQuery: "",
    searchMode: "keyword",
    searchOrder: [], // slide ids, relevance order (search mode)
    matchedSlideIds: new Set(),
    focusDeckId: null,
    focusSlideId: null,
    selection: new Set(),
    showHidden: false,
    refreshingDeckIds: new Set(),
    simulation: null, // active d3-force simulation, if mode === "similar"
  };

  const canvas = d3.select("#canvas");
  let navigationRevision = 0;
  const canvasNode = canvas.node();
  const viewport = document.getElementById("viewport");

  // -- zoom / pan (mouse drag, touch pinch/pan, arrow keys / WASD) ---------
  const zoomBehavior = d3
    .zoom()
    .scaleExtent([0.01, 32])
    .interpolate(d3.interpolate)
    .on("start", () => viewport.classList.add("grabbing"))
    .on("end", () => viewport.classList.remove("grabbing"))
    .on("zoom", (event) => {
      canvas.style("transform", `translate(${event.transform.x}px, ${event.transform.y}px) scale(${event.transform.k})`);
    });

  d3.select(viewport).call(zoomBehavior);

  // D3 prevents the mouse's default focus change when starting a drag.
  // Explicitly leave toolbar inputs so subsequent navigation keys pan the view.
  viewport.addEventListener("pointerdown", () => {
    viewport.focus({ preventScroll: true });
  });

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
    d3.select(viewport).interrupt().call(zoomBehavior.translateBy,
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

  let renderedItems = [];

  function fitView(clickedPosition = null) {
    const padding = 24;
    const rect = viewport.getBoundingClientRect();
    const breadcrumb = document.getElementById("view-breadcrumb").getBoundingClientRect();
    const top = Math.max(0, breadcrumb.bottom - rect.top) + padding;
    const width = Math.max(1, viewport.clientWidth - padding * 2);
    const height = Math.max(1, viewport.clientHeight - top - padding);
    const items = renderedItems;
    const contentWidth = items.reduce((right, item) => Math.max(right, item.x + SLIDE_WIDTH), SLIDE_WIDTH);
    // Fit the actual occupied columns, including decks/results shorter than a row.
    // Height only limits zoom when a single complete slide would not fit.
    const k = Math.min(width / contentWidth, height / (CELL_H - GAP));
    const focus = items.find((item) => item.slide.id === state.focusSlideId) || items[0];
    const hasFocus = state.mode === "deck" || state.mode === "similar";
    const focusY = hasFocus && focus ? focus.y : 0;
    const desiredY = clickedPosition ? clickedPosition.y : top;
    const screenY = Math.max(top, Math.min(desiredY, top + height - (CELL_H - GAP) * k));
    // Canonical grids start at y=0. Keep their first row at the top whenever
    // preserving the clicked position would leave empty canvas above it.
    const y = Math.min(top, (hasFocus ? screenY : top) - focusY * k);
    const target = d3.zoomIdentity.translate(padding, y).scale(k);
    const selection = d3.select(viewport).interrupt();

    // Layout has canonical coordinates. Rebase the camera to keep the clicked
    // slide at its old screen position before animating into the fitted view.
    if (clickedPosition && focus && hasFocus) {
      const current = d3.zoomTransform(viewport);
      selection.call(zoomBehavior.transform, d3.zoomIdentity
        .translate(clickedPosition.x - focus.x * current.k, clickedPosition.y - focus.y * current.k)
        .scale(current.k));
    }
    canvas.selectAll(".slide-thumb").interrupt()
      .filter((item) => items.includes(item))
      .style("left", (item) => `${item.x}px`)
      .style("top", (item) => `${item.y}px`)
      .style("opacity", 1);
    canvas.selectAll(".slide-thumb").filter((item) => !items.includes(item)).remove();
    // Let D3 own its transition: calling transform on a plain selection from
    // inside a tween interrupts that very transition on the first frame.
    selection.transition().duration(MODE_TRANSITION_MS)
      .call(zoomBehavior.transform, target);
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
    return items.map((item, i) => ({
      ...item,
      x: (i % POOL_COLUMNS) * CELL_W,
      y: Math.floor(i / POOL_COLUMNS) * CELL_H,
    }));
  }

  function searchItems() {
    const items = state.searchOrder
      .map((id) => state.slidesById.get(id))
      .filter((entry) => entry && (state.showHidden || !entry.slide.hidden));
    return items.map((item, i) => ({
      ...item,
      x: (i % POOL_COLUMNS) * CELL_W,
      y: Math.floor(i / POOL_COLUMNS) * CELL_H,
    }));
  }

  function deckItems() {
    const deck = state.deckById.get(state.focusDeckId);
    if (!deck) return [];
    const slides = deck.slides
      .filter((s) => state.showHidden || !s.hidden)
      .slice()
      .sort((a, b) => a.index_in_deck - b.index_in_deck);
    return slides.map((slide, i) => ({
      slide,
      deck,
      x: (i % DECK_ROW_LENGTH) * CELL_W,
      y: Math.floor(i / DECK_ROW_LENGTH) * CELL_H,
    }));
  }

  // -- rendering (pool / search / deck) --------------------------------------
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
    return `<img loading="lazy" src="/api/slides/${item.slide.id}/image?dpi=90" alt="slide ${item.slide.index_in_deck + 1}" />${badge}${label}`;
  }

  function renderStatic(items) {
    renderedItems = items;
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
    document.getElementById("deck-refresh-btn").classList.toggle("hidden", state.mode !== "deck");
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

  function startSimilarSimulation(sourceSlide, similarSlides) {
    if (state.simulation) state.simulation.stop();

    const items = [sourceSlide, ...similarSlides]
      .filter((slide) => state.showHidden || !slide.hidden)
      .map((slide, i) => ({
      slide,
      deck: state.deckById.get(slide.deck_id) || { id: slide.deck_id, name: "(deck)" },
      x: (i % DECK_ROW_LENGTH) * CELL_W,
      y: Math.floor(i / DECK_ROW_LENGTH) * CELL_H,
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
    if (!event.target.closest(".slide-thumb")) return;
    event.preventDefault();
    // Slides have no double-click action, including the viewport's default zoom.
    event.stopPropagation();
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
  }

  function updateSelectionUi() {
    for (const id of state.selection) {
      if (!state.slidesById.has(id)) state.selection.delete(id);
    }
    const count = state.selection.size;
    document.getElementById("selection-count").textContent = `${count} selected`;
    document.getElementById("export-btn").disabled = count === 0;
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
      const markup = `<img loading="lazy" draggable="false" src="/api/slides/${id}/image?dpi=90" alt="" /><span>${escapeHtml(label)}</span>`;
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
      d3.select(viewport).call(zoomBehavior);
    } else {
      d3.select(viewport).on(".zoom", null);
    }
  }

  function showContextMenu(el, clientX, clientY) {
    const menu = document.getElementById("slide-context-menu");
    menu.dataset.slideId = el.getAttribute("data-slide-id");
    menu.dataset.source = selectionList.contains(el) ? "selection" : "canvas";
    const { slide, deck } = state.slidesById.get(Number(menu.dataset.slideId));
    document.getElementById("menu-show-in-deck").disabled = state.mode === "deck";
    document.getElementById("menu-toggle-slide-hidden").hidden = state.mode === "pool";
    document.getElementById("menu-toggle-deck-hidden").hidden = state.mode !== "pool";
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
    const { x, y } = d3.select(el).datum();
    const scale = Math.max(0.2, Math.min(4,
      (viewport.clientWidth - 48) / SLIDE_WIDTH,
      (viewport.clientHeight - 120) / SLIDE_HEIGHT));
    const transform = d3.zoomIdentity
      .translate(viewport.clientWidth / 2, viewport.clientHeight / 2)
      .scale(scale)
      .translate(-x - SLIDE_WIDTH / 2, -y - SLIDE_HEIGHT / 2);
    d3.select(viewport).transition().duration(300).call(zoomBehavior.transform, transform);
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
  document.getElementById("deck-refresh-btn").addEventListener("click", () => {
    if (state.focusDeckId != null) refreshDeck(state.focusDeckId);
  });

  async function refreshDeck(deckId) {
    if (state.refreshingDeckIds.has(deckId)) return;
    state.refreshingDeckIds.add(deckId);
    try {
      const res = await fetch(`/api/decks/${deckId}/refresh`, { method: "POST" });
      const data = await res.json();
      if (!res.ok || data.error) throw new Error(data.error || "Refresh failed");
      await loadDecks();
      if (state.mode === "deck") render();
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
    state.searchOrder = order;
    state.matchedSlideIds = matched;
    state.mode = "search";
    recordLocation();
    render();
    fitView();
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

  // -- wiring ----------------------------------------------------------------
  document.getElementById("home-btn").addEventListener("click", () => {
    saveLocation();
    // Ignore pending search/similarity responses after returning home.
    navigationRevision += 1;
    state.mode = "pool";
    state.searchQuery = "";
    state.searchMode = "keyword";
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

  document.getElementById("export-btn").addEventListener("click", exportSelection);

  document.getElementById("clear-selection-btn").addEventListener("click", () => {
    state.selection.clear();
    canvas.selectAll(".slide-thumb").attr("class", thumbClasses);
    updateSelectionUi();
  });

  recordLocation();
  loadDecks().then(() => fitView());
  pollScanStatus();
  pollEmbeddingStatus();
  setInterval(loadDecks, 5000);
  setInterval(pollScanStatus, 2000);
  setInterval(pollEmbeddingStatus, 2000);
})();
