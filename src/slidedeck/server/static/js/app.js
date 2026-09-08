/* slidedeck GUI: D3-powered zoom/pan view of individual slides, with four
 * layouts driven by `state.mode`:
 *   - "pool":   all slides, most recently changed deck first (initial view).
 *   - "search": only slides matching the current search query.
 *   - "deck":   the full deck of a clicked slide, laid out left-to-right,
 *               with the clicked slide staying exactly where it was.
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
  const POOL_COLUMNS = 8;
  const DECK_ROW_LENGTH = 10; // wrap the full-deck view every this many slides
  const LONG_PRESS_MS = 1000;
  const MOVE_CANCEL_PX = 6;

  const state = {
    decks: [],
    deckById: new Map(),
    slidesById: new Map(), // slide id -> { slide, deck }
    mode: "pool", // "pool" | "search" | "deck" | "similar"
    searchQuery: "",
    searchOrder: [], // slide ids, relevance order (search mode)
    matchedSlideIds: new Set(),
    focusDeckId: null,
    focusSlideId: null,
    viewAnchor: { x: 0, y: 0 }, // canvas-space position the focus slide keeps
    selection: new Set(),
    showHidden: false,
    refreshingDeckIds: new Set(),
    simulation: null, // active d3-force simulation, if mode === "similar"
  };

  const canvas = d3.select("#canvas");
  const canvasNode = canvas.node();
  const viewport = document.getElementById("viewport");

  // -- zoom / pan (mouse drag + touch pinch/pan) -----------------------
  const zoomBehavior = d3
    .zoom()
    .scaleExtent([0.2, 4])
    .on("start", () => viewport.classList.add("grabbing"))
    .on("end", () => viewport.classList.remove("grabbing"))
    .on("zoom", (event) => {
      canvas.style("transform", `translate(${event.transform.x}px, ${event.transform.y}px) scale(${event.transform.k})`);
    });

  d3.select(viewport).call(zoomBehavior);

  function screenToCanvas(clientX, clientY) {
    const t = d3.zoomTransform(viewport);
    const rect = viewport.getBoundingClientRect();
    return {
      x: (clientX - rect.left - t.x) / t.k,
      y: (clientY - rect.top - t.y) / t.k,
    };
  }

  // -- data loading -------------------------------------------------------
  async function loadDecks() {
    const res = await fetch("/api/decks");
    const decks = await res.json();
    state.decks = decks;
    indexData();
    updateSelectionUi();
    if (state.mode === "pool" || state.mode === "search") render();
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
        showStatusProgress(el, "Scanning…", null, "Scanning… " + (status.current_file || ""));
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
      if (!status.available || !status.total) {
        el.textContent = "";
      } else if (status.embedded < status.total) {
        const pct = Math.round((status.embedded / status.total) * 100);
        showStatusProgress(el, `Embedding… ${pct}%`, pct,
          `Embedding slides: ${status.embedded}/${status.total}`);
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
      .filter((s) => state.showHidden || !s.hidden || s.id === state.focusSlideId)
      .slice()
      .sort((a, b) => a.index_in_deck - b.index_in_deck);
    const rawFocusIdx = slides.findIndex((s) => s.id === state.focusSlideId);
    const focusIdx = rawFocusIdx < 0 ? 0 : rawFocusIdx;
    const focusRow = Math.floor(focusIdx / DECK_ROW_LENGTH);
    const focusCol = focusIdx % DECK_ROW_LENGTH;
    const anchor = state.viewAnchor;
    return slides.map((slide, i) => ({
      slide,
      deck,
      x: anchor.x + ((i % DECK_ROW_LENGTH) - focusCol) * CELL_W,
      y: anchor.y + (Math.floor(i / DECK_ROW_LENGTH) - focusRow) * CELL_H,
      enterFrom: rawFocusIdx < 0 ? null : { x: anchor.x, y: anchor.y },
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
    const label = `<span class="deck-label">${escapeHtml(item.deck.name)} #${item.slide.index_in_deck + 1}</span>`;
    return `<img loading="lazy" src="/api/slides/${item.slide.id}/image?dpi=90" alt="slide ${item.slide.index_in_deck + 1}" />${badge}${label}`;
  }

  function renderStatic(items) {
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
    let items;
    if (state.mode === "deck") items = deckItems();
    else if (state.mode === "search") items = searchItems();
    else items = poolItems();
    renderStatic(items);
  }

  function updateBreadcrumb() {
    const bar = document.getElementById("view-breadcrumb");
    const label = document.getElementById("view-breadcrumb-label");
    const refreshBtn = document.getElementById("deck-refresh-btn");
    if (state.mode === "deck") {
      const deck = state.deckById.get(state.focusDeckId);
      bar.classList.remove("hidden");
      label.textContent = deck ? `Slide deck: ${deck.name}` : "Slide deck";
      refreshBtn.classList.remove("hidden");
    } else if (state.mode === "similar") {
      const entry = state.slidesById.get(state.focusSlideId);
      bar.classList.remove("hidden");
      label.textContent = entry ? `Similar to slide #${entry.slide.index_in_deck + 1} in ${entry.deck.name}` : "Similar slides";
      refreshBtn.classList.add("hidden");
    } else {
      bar.classList.add("hidden");
    }
  }

  // -- similar-slides force layout --------------------------------------------
  async function showSimilar(slideId, el) {
    hideContextMenu();
    const entry = state.slidesById.get(slideId);
    if (!entry) return;
    const anchor = navigationAnchor(el);

    let similar;
    try {
      const res = await fetch(`/api/slides/${slideId}/similar`);
      similar = await res.json();
      if (similar && !Array.isArray(similar) && similar.error) throw new Error(similar.error);
    } catch (err) {
      window.alert("Could not load similar slides: " + err.message);
      return;
    }
    if (!Array.isArray(similar) || similar.length === 0) {
      window.alert("No similar slides found (this slide may not be embedded yet).");
      return;
    }

    state.mode = "similar";
    state.focusSlideId = slideId;
    state.viewAnchor = anchor;
    startSimilarSimulation(entry.slide, similar, anchor);
  }

  function startSimilarSimulation(sourceSlide, similarSlides, anchor) {
    if (state.simulation) state.simulation.stop();

    const items = [sourceSlide, ...similarSlides].map((slide, i) => ({
      slide,
      deck: state.deckById.get(slide.deck_id) || { id: slide.deck_id, name: "(deck)" },
      x: anchor.x + (i % DECK_ROW_LENGTH) * CELL_W,
      y: anchor.y + Math.floor(i / DECK_ROW_LENGTH) * CELL_H,
      enterFrom: { x: anchor.x, y: anchor.y },
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
    if (!el) return;
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

  canvasNode.addEventListener("pointerup", () => {
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

  canvasNode.addEventListener("dblclick", (event) => {
    const el = event.target.closest(".slide-thumb");
    if (!el) return;
    event.preventDefault();
    toggleSelection(Number(el.getAttribute("data-slide-id")));
  });

  function onSlideClick(slideId, el) {
    const entry = state.slidesById.get(slideId);
    if (!entry) return;
    const anchor = navigationAnchor(el);
    state.viewAnchor = anchor;
    state.focusDeckId = entry.deck.id;
    state.focusSlideId = slideId;
    state.mode = "deck";
    render();
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

  function navigationAnchor(el) {
    if (el && canvasNode.contains(el)) {
      const rect = el.getBoundingClientRect();
      return screenToCanvas(rect.left, rect.top);
    }
    // Sidebar navigation starts inside the visible canvas, independent of pan/zoom.
    d3.select(viewport).call(zoomBehavior.transform, d3.zoomIdentity.translate(24, 60));
    return { x: 0, y: 0 };
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
      const label = `${i + 1}. ${deck.name} #${slide.index_in_deck + 1}`;
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
    document.getElementById("menu-remove").hidden = menu.dataset.source !== "selection";
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

  document.getElementById("back-to-overview-btn").addEventListener("click", () => {
    state.mode = state.searchQuery ? "search" : "pool";
    render();
  });

  // -- search --------------------------------------------------------------
  async function runSearch(query) {
    query = query.trim();
    state.searchQuery = query;
    if (!query) {
      state.mode = "pool";
      state.matchedSlideIds = new Set();
      state.searchOrder = [];
      render();
      return;
    }
    const mode = document.getElementById("search-mode").value;
    const endpoint = mode === "semantic" ? "/api/search/semantic" : "/api/search";
    const res = await fetch(`${endpoint}?q=${encodeURIComponent(query)}`);
    const results = await res.json();
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
    state.searchOrder = order;
    state.matchedSlideIds = matched;
    state.mode = "search";
    render();
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

  loadDecks();
  pollScanStatus();
  pollEmbeddingStatus();
  setInterval(loadDecks, 5000);
  setInterval(pollScanStatus, 2000);
  setInterval(pollEmbeddingStatus, 2000);
})();
