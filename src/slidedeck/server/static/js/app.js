/* slidedeck GUI: D3-powered zoom/pan grid of decks, search, selection & export. */
(function () {
  "use strict";

  const state = {
    decks: [],
    expanded: new Set(),
    selection: new Set(),
    showHidden: false,
    filterDeckIds: null, // Set of deck ids matching the current search, or null
    matchedSlideIds: new Set(),
    refreshingDeckIds: new Set(),
  };

  const SLIDE_HEIGHT = 130; // all slide thumbnails render at this height
  const SLIDES_PER_ROW = 20;
  const DECK_HEADER_HEIGHT = 40; // deck name + a small gap between decks

  const canvas = d3.select("#canvas");
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

  // -- data loading -------------------------------------------------------
  async function loadDecks() {
    const res = await fetch("/api/decks");
    const decks = await res.json();
    state.decks = decks;
    render();
  }

  async function pollScanStatus() {
    try {
      const res = await fetch("/api/scan/status");
      const status = await res.json();
      const el = document.getElementById("scan-status");
      if (status.running) {
        el.textContent = "Scanning… " + (status.current_file || "");
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
      if (!status.available || !status.total) {
        el.textContent = "";
      } else if (status.embedded < status.total) {
        const pct = Math.round((status.embedded / status.total) * 100);
        el.textContent = `Embedding slides… ${pct}% (${status.embedded}/${status.total})`;
      } else {
        el.textContent = "";
      }
    } catch (e) {
      /* ignore transient network errors */
    }
  }

  // -- rendering ------------------------------------------------------------
  function visibleDecks() {
    if (!state.filterDeckIds) return state.decks;
    return state.decks.filter((d) => state.filterDeckIds.has(d.id));
  }

  function firstSlideOf(deck) {
    const candidates = deck.slides.filter((s) => state.showHidden || !s.hidden);
    return candidates.length ? candidates[0] : deck.slides[0];
  }

  function otherSlidesOf(deck, firstSlide) {
    return deck.slides.filter(
      (s) => (state.showHidden || !s.hidden) && (!firstSlide || s.id !== firstSlide.id)
    );
  }

  function slideRows(slides) {
    const rows = [];
    for (let index = 0; index < slides.length; index += SLIDES_PER_ROW) {
      rows.push(slides.slice(index, index + SLIDES_PER_ROW));
    }
    return rows;
  }

  function deckHeight(deck) {
    const first = firstSlideOf(deck);
    if (!first) return DECK_HEADER_HEIGHT;
    const others = state.expanded.has(deck.id) ? otherSlidesOf(deck, first) : [];
    const rowCount = slideRows([first, ...others]).length;
    return DECK_HEADER_HEIGHT + rowCount * SLIDE_HEIGHT + (rowCount - 1) * 10;
  }

  function slideThumbHTML(slide) {
    const classes = ["slide-thumb"];
    if (state.selection.has(slide.id)) classes.push("selected");
    if (slide.hidden) classes.push("hidden-slide");
    if (state.matchedSlideIds.has(slide.id)) classes.push("matched");
    if (state.filterDeckIds && !state.matchedSlideIds.has(slide.id)) classes.push("unmatched");
    const badge = slide.hidden ? '<span class="badge">hidden</span>' : "";
    return `<div class="${classes.join(" ")}" data-slide-id="${slide.id}" style="height:${SLIDE_HEIGHT}px">
      <img loading="lazy" src="/api/slides/${slide.id}/image?dpi=90" alt="slide ${slide.index_in_deck + 1}" />
      ${badge}
    </div>`;
  }

  function render() {
    const decks = visibleDecks();
    const deckTops = new Map();
    let nextTop = 0;
    decks.forEach((deck) => {
      deckTops.set(deck.id, nextTop);
      nextTop += deckHeight(deck);
    });

    const rows = canvas
      .selectAll(".deck-card")
      .data(decks, (d) => d.id);

    rows.exit().remove();

    const entered = rows
      .enter()
      .append("div")
      .attr("class", "deck-card");

    entered.merge(rows)
      .style("top", (deck) => `${deckTops.get(deck.id)}px`)
      .style("left", "0px")
      .html((deck) => {
        const first = firstSlideOf(deck);
        const others = state.expanded.has(deck.id) ? otherSlidesOf(deck, first) : [];
        const nameHtml = `<div class="deck-name" title="${escapeHtml(deck.pptx_path)}">${escapeHtml(deck.name)}</div>`;
        const refreshButton = `<button class="refresh-deck-btn" data-deck-id="${deck.id}" title="Refresh deck database" aria-label="Refresh ${escapeHtml(deck.name)}"${state.refreshingDeckIds.has(deck.id) ? " disabled" : ""}>↻</button>`;
        const slidesHtml = first
          ? `<div class="deck-slide-lines">${slideRows([first, ...others])
            .map((slideRow) => `<div class="deck-slide-row">${slideRow.map((s) => slideThumbHTML(s)).join("")}</div>`)
            .join("")}</div>`
          : "<div>(no slides)</div>";
        return `${nameHtml}<div class="deck-row">${refreshButton}${slidesHtml}</div>`;
      });

    attachThumbHandlers();
    updateSelectionUi();
  }

  function attachThumbHandlers() {
    canvas.selectAll(".refresh-deck-btn").on("click", function (event) {
      event.preventDefault();
      event.stopPropagation();
      refreshDeck(Number(this.getAttribute("data-deck-id")));
    });

    canvas.selectAll(".slide-thumb").on("click", function (event) {
      const slideId = Number(this.getAttribute("data-slide-id"));
      const deck = state.decks.find((d) => d.slides.some((s) => s.id === slideId));
      if (!deck) return;
      const first = firstSlideOf(deck);
      if (first && first.id === slideId) {
        toggleExpanded(deck.id);
      }
    });

    canvas.selectAll(".slide-thumb").on("dblclick", function (event) {
      event.preventDefault();
      event.stopPropagation();
      const slideId = Number(this.getAttribute("data-slide-id"));
      toggleSelection(slideId);
    });
  }

  async function refreshDeck(deckId) {
    if (state.refreshingDeckIds.has(deckId)) return;
    state.refreshingDeckIds.add(deckId);
    render();
    try {
      const res = await fetch(`/api/decks/${deckId}/refresh`, { method: "POST" });
      const data = await res.json();
      if (!res.ok || data.error) throw new Error(data.error || "Refresh failed");
      await loadDecks();
    } catch (error) {
      window.alert(error.message);
    } finally {
      state.refreshingDeckIds.delete(deckId);
      render();
    }
  }

  function toggleExpanded(deckId) {
    if (state.expanded.has(deckId)) {
      state.expanded.delete(deckId);
    } else {
      state.expanded.add(deckId);
    }
    render();
  }

  function toggleSelection(slideId) {
    if (state.selection.has(slideId)) {
      state.selection.delete(slideId);
    } else {
      state.selection.add(slideId);
    }
    render();
  }

  function updateSelectionUi() {
    const count = state.selection.size;
    document.getElementById("selection-count").textContent = `${count} selected`;
    document.getElementById("export-btn").disabled = count === 0;
    document.getElementById("clear-selection-btn").disabled = count === 0;
  }

  function escapeHtml(str) {
    return String(str).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  // -- search --------------------------------------------------------------
  async function runSearch(query) {
    query = query.trim();
    if (!query) {
      state.filterDeckIds = null;
      state.matchedSlideIds = new Set();
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
    state.filterDeckIds = new Set(results.map((d) => d.id));
    state.matchedSlideIds = new Set();
    results.forEach((d) => {
      state.expanded.add(d.id);
      (d.matched_slides || []).forEach((s) => state.matchedSlideIds.add(s.id));
    });
    render();
  }

  // -- export --------------------------------------------------------------
  async function exportSelection() {
    const filename = window.prompt("Export filename:", "export.pptx");
    if (!filename) return;
    const res = await fetch("/api/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slide_ids: Array.from(state.selection), filename }),
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
    render();
  });

  loadDecks();
  pollScanStatus();
  pollEmbeddingStatus();
  setInterval(loadDecks, 5000);
  setInterval(pollScanStatus, 2000);
  setInterval(pollEmbeddingStatus, 2000);
})();
