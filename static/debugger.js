/* gbserver hidden debugger - loaded on demand by app.js when the secret
 * button sequence is entered. Talks to the server over the existing game
 * WebSocket through the bridge app.js passes in. */
(() => {
  "use strict";

  const REGION_JUMPS = [
    ["ROM0", 0x0000], ["ROMX", 0x4000], ["VRAM", 0x8000], ["SRAM", 0xA000],
    ["WRAM", 0xC000], ["ECHO", 0xE000], ["OAM", 0xFE00], ["I/O", 0xFF00], ["HRAM", 0xFF80],
  ];
  const SEARCH_REGIONS = [
    ["wram", "WRAM", true], ["hram", "HRAM", true], ["sram", "SRAM", false],
    ["vram", "VRAM", false], ["oam", "OAM", false], ["io", "I/O", false],
  ];
  const SEARCH_CONDS = [
    ["eq", "= value"], ["ne", "\u2260 value"], ["gt", "> value"], ["lt", "< value"],
    ["changed", "changed"], ["unchanged", "unchanged"], ["increased", "increased"], ["decreased", "decreased"],
  ];
  const WATCH_CONDS = [["change", "changes"], ["eq", "becomes ="], ["ne", "leaves ="], ["gt", "goes >"], ["lt", "goes <"]];
  const ROWS = 16;
  const REFRESH_MS = 400;

  const hex = (n, w) => n.toString(16).toUpperCase().padStart(w, "0");

  // "C0A3", "$C0A3", "0xC0A3", "C0A3h" -> hex; "#123" -> decimal
  function parseNum(text, max) {
    if (typeof text !== "string") return null;
    let t = text.trim();
    if (!t) return null;
    let n;
    if (t.startsWith("#")) {
      if (!/^#\d+$/.test(t)) return null;
      n = parseInt(t.slice(1), 10);
    } else {
      t = t.replace(/^\$|^0x/i, "").replace(/h$/i, "");
      if (!/^[0-9a-f]+$/i.test(t)) return null;
      n = parseInt(t, 16);
    }
    return n >= 0 && n <= max ? n : null;
  }

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v === undefined || v === null || v === false) continue;
        if (k === "class") node.className = v;
        else if (k === "text") node.textContent = v;
        else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
        else node.setAttribute(k, v === true ? "" : v);
      }
    }
    for (const c of children) {
      if (c === null || c === undefined) continue;
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return node;
  }

  function create(bridge) {
    let state = null;
    let isController = false;
    let open = false;
    let activeTab = "memory";
    let memBase = 0xC000;
    let memBytes = null;
    let prevMem = null;
    let memStart = -1;
    let selected = null;
    let editing = null;
    let cols = 16;
    let inflight = false;
    let timer = null;
    let search = null;
    let lastSearchRefresh = 0;

    // ---- chrome ---------------------------------------------------------------

    const statusText = el("span", { class: "gbd-status-text", text: "\u2026" });
    const roleText = el("span", { class: "gbd-role" });
    const pauseBtn = el("button", { class: "gbd-btn gbd-primary", type: "button", onclick: togglePause }, "Pause");
    const stepBtn = el("button", { class: "gbd-btn", type: "button", onclick: () => call({ op: "step_frame" }) }, "Step frame");
    const toast = el("div", { class: "gbd-toast", hidden: true });
    const closeBtn = el("button", { class: "gbd-close", type: "button", "aria-label": "Close debugger", onclick: hide }, "\u00D7");
    const titleBar = el("div", { class: "gbd-titlebar" },
      el("span", { class: "gbd-title", text: "gbserver debugger" }), closeBtn);

    const tabs = {};
    const panes = {};
    const tabBar = el("div", { class: "gbd-tabs", role: "tablist" });
    for (const [id, label] of [["memory", "Memory"], ["search", "Search"], ["breaks", "Breakpoints"], ["cpu", "CPU"]]) {
      tabs[id] = el("button", { class: "gbd-tab", type: "button", role: "tab", onclick: () => setTab(id) }, label);
      tabBar.appendChild(tabs[id]);
      panes[id] = el("div", { class: "gbd-pane", role: "tabpanel" });
    }

    const win = el("section", { class: "gbd-window", hidden: true, "aria-label": "Debugger" },
      titleBar,
      el("div", { class: "gbd-toolbar" }, pauseBtn, stepBtn, statusText, roleText),
      tabBar,
      el("div", { class: "gbd-body" }, ...Object.values(panes)),
      toast,
    );
    document.body.appendChild(win);

    // ---- memory tab -----------------------------------------------------------

    const gotoInput = el("input", { class: "gbd-input gbd-w6", placeholder: "C000", spellcheck: "false", "aria-label": "Go to address" });
    const jumpSelect = el("select", { class: "gbd-input", "aria-label": "Jump to region" },
      el("option", { value: "", text: "Region\u2026" }),
      ...REGION_JUMPS.map(([n, a]) => el("option", { value: String(a), text: `${n} $${hex(a, 4)}` })));
    const grid = el("div", { class: "gbd-grid", tabindex: "0" });
    const selInfo = el("div", { class: "gbd-selinfo", text: "Click a byte to select it." });
    const selActions = el("div", { class: "gbd-row gbd-selactions" },
      el("button", { class: "gbd-btn", type: "button", onclick: () => selected !== null && beginEdit(selected) }, "Edit"),
      el("button", { class: "gbd-btn", type: "button", onclick: freezeSelected }, "Freeze"),
      el("button", { class: "gbd-btn", type: "button", onclick: watchSelected }, "Watch"),
      el("button", { class: "gbd-btn", type: "button", onclick: breakSelected }, "Break here"),
    );
    const pokeAddr = el("input", { class: "gbd-input gbd-w6", placeholder: "C000", spellcheck: "false", "aria-label": "Poke address" });
    const pokeBytes = el("input", { class: "gbd-input gbd-grow", placeholder: "bytes, e.g. 3E 01 C9", spellcheck: "false", "aria-label": "Bytes to write" });

    panes.memory.append(
      el("div", { class: "gbd-row" },
        gotoInput,
        el("button", { class: "gbd-btn", type: "button", onclick: () => { const a = parseNum(gotoInput.value, 0xFFFF); a === null ? flash("Enter a hex address like C0A3") : jumpTo(a, true); } }, "Go"),
        jumpSelect,
        el("span", { class: "gbd-spacer" }),
        el("button", { class: "gbd-btn", type: "button", "aria-label": "Previous page", onclick: () => jumpTo(memBase - cols * ROWS) }, "\u25B2"),
        el("button", { class: "gbd-btn", type: "button", "aria-label": "Next page", onclick: () => jumpTo(memBase + cols * ROWS) }, "\u25BC"),
      ),
      grid,
      selInfo,
      selActions,
      el("div", { class: "gbd-row" },
        el("span", { class: "gbd-label", text: "Poke" }), pokeAddr, pokeBytes,
        el("button", { class: "gbd-btn", type: "button", onclick: doPoke }, "Write")),
      el("p", { class: "gbd-hint", text: "Addresses and values are hex; prefix # for decimal. Scroll the grid to move. Double-click a byte (or select it and type) to edit. ROM ($0000-$7FFF) is read-only." }),
    );

    gotoInput.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); const a = parseNum(gotoInput.value, 0xFFFF); if (a !== null) jumpTo(a, true); } });
    jumpSelect.addEventListener("change", () => { if (jumpSelect.value !== "") jumpTo(Number(jumpSelect.value), true); jumpSelect.value = ""; });
    pokeBytes.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); doPoke(); } });
    grid.addEventListener("wheel", (e) => {
      e.preventDefault();
      jumpTo(memBase + Math.sign(e.deltaY) * cols * (Math.abs(e.deltaY) > 80 ? 4 : 1));
    }, { passive: false });
    grid.addEventListener("click", (e) => {
      const cell = e.target.closest("[data-addr]");
      if (cell) select(Number(cell.dataset.addr));
    });
    grid.addEventListener("dblclick", (e) => {
      const cell = e.target.closest("[data-addr]");
      if (cell) beginEdit(Number(cell.dataset.addr));
    });
    grid.addEventListener("keydown", (e) => {
      if (editing !== null || selected === null) return;
      const moves = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -cols, ArrowDown: cols, PageUp: -cols * ROWS, PageDown: cols * ROWS };
      if (moves[e.key] !== undefined) {
        e.preventDefault();
        e.stopPropagation();
        const next = Math.max(0, Math.min(0xFFFF, selected + moves[e.key]));
        select(next);
        if (next < memBase || next >= memBase + cols * ROWS) jumpTo(next - (next % cols) - (moves[e.key] < 0 ? 0 : cols * (ROWS - 1)));
      } else if (/^[0-9a-f]$/i.test(e.key)) {
        e.preventDefault();
        e.stopPropagation();
        beginEdit(selected, e.key);
      } else if (e.key === "Enter") {
        e.preventDefault();
        beginEdit(selected);
      }
    });

    function jumpTo(addr, highlight) {
      addr = Math.max(0, Math.min(0x10000 - cols * ROWS, addr));
      memBase = addr - (addr % cols);
      if (highlight) select(Math.min(0xFFFF, addr));
      memBytes = null;
      renderGrid();
      refreshNow();
    }

    function select(addr) {
      selected = addr;
      renderGrid();
      renderSelInfo();
      grid.focus({ preventScroll: true });
    }

    function byteAt(addr) {
      if (!memBytes || addr < memStart || addr >= memStart + memBytes.length) return null;
      return memBytes[addr - memStart];
    }

    function renderSelInfo() {
      if (selected === null) return;
      const v = byteAt(selected);
      const v2 = byteAt(selected + 1);
      let text = `$${hex(selected, 4)}  `;
      if (v === null) text += "(not loaded)";
      else {
        text += `= $${hex(v, 2)}  (${v}, ${v.toString(2).padStart(8, "0")}b)`;
        if (v2 !== null) { const w = v | (v2 << 8); text += `   16-bit: $${hex(w, 4)} (${w})`; }
      }
      selInfo.textContent = text;
    }

    function renderGrid() {
      cols = window.matchMedia("(max-width: 600px)").matches ? 8 : 16;
      memBase -= memBase % cols;
      const frag = document.createDocumentFragment();
      const head = el("div", { class: "gbd-grid-row gbd-grid-head" }, el("span", { class: "gbd-addr", text: "" }));
      for (let c = 0; c < cols; c++) head.appendChild(el("span", { class: "gbd-byte", text: hex(c, 2) }));
      head.appendChild(el("span", { class: "gbd-ascii", text: "" }));
      frag.appendChild(head);

      const frozen = new Set((state && state.freezes || []).flatMap((f) => f.size === 2 ? [f.addr, f.addr + 1] : [f.addr]));
      const breaks = new Set((state && state.breakpoints || []).map((b) => b.addr));
      const watched = new Set((state && state.watches || []).flatMap((w) => w.size === 2 ? [w.addr, w.addr + 1] : [w.addr]));
      const pc = state && state.registers ? state.registers.PC : -1;

      for (let r = 0; r < ROWS; r++) {
        const rowAddr = memBase + r * cols;
        if (rowAddr > 0xFFFF) break;
        const row = el("div", { class: "gbd-grid-row" }, el("span", { class: "gbd-addr", text: hex(rowAddr, 4) }));
        let ascii = "";
        for (let c = 0; c < cols; c++) {
          const a = rowAddr + c;
          if (a > 0xFFFF) break;
          const v = byteAt(a);
          const old = prevMem && prevMem.start <= a && a < prevMem.start + prevMem.bytes.length ? prevMem.bytes[a - prevMem.start] : null;
          let cls = "gbd-byte";
          if (a === selected) cls += " sel";
          if (v !== null && old !== null && v !== old) cls += " changed";
          if (frozen.has(a)) cls += " frozen";
          if (breaks.has(a)) cls += " brk";
          if (watched.has(a)) cls += " watched";
          if (a === pc && state && state.paused) cls += " pc";
          if (a === editing) {
            const input = el("input", { class: "gbd-byte-edit", maxlength: "2", spellcheck: "false", value: v === null ? "" : hex(v, 2), "aria-label": `Edit $${hex(a, 4)}` });
            row.appendChild(el("span", { class: cls }, input));
          } else {
            row.appendChild(el("span", { class: cls, "data-addr": String(a), text: v === null ? "--" : hex(v, 2) }));
          }
          ascii += v !== null && v >= 0x20 && v < 0x7F ? String.fromCharCode(v) : ".";
        }
        row.appendChild(el("span", { class: "gbd-ascii", text: ascii }));
        frag.appendChild(row);
      }
      grid.replaceChildren(frag);
      const input = grid.querySelector(".gbd-byte-edit");
      if (input) wireEditInput(input);
    }

    let pendingEditChar = null;
    function beginEdit(addr, firstChar) {
      if (!isController) return flash("Only the current controller can edit memory");
      if (addr < 0x8000) return flash("ROM ($0000-$7FFF) is read-only");
      editing = addr;
      selected = addr;
      pendingEditChar = firstChar || null;
      renderGrid();
      renderSelInfo();
    }

    function wireEditInput(input) {
      input.focus();
      if (pendingEditChar) { input.value = pendingEditChar; pendingEditChar = null; }
      else input.select();
      const commit = async (advance) => {
        const addr = editing;
        const val = parseNum(input.value, 0xFF);
        editing = null;
        if (val === null) { renderGrid(); grid.focus(); return; }
        const res = await call({ op: "write", addr, values: [val] });
        if (res && res.ok) {
          if (memBytes && addr >= memStart && addr < memStart + memBytes.length) memBytes[addr - memStart] = val;
          if (advance && addr + 1 <= 0xFFFF) {
            selected = addr + 1;
            if (selected >= memBase + cols * ROWS) memBase += cols;
            beginEdit(selected);
            return;
          }
        }
        renderGrid();
        renderSelInfo();
        grid.focus({ preventScroll: true });
      };
      input.addEventListener("keydown", (e) => {
        e.stopPropagation();
        if (e.key === "Enter") { e.preventDefault(); commit(false); }
        else if (e.key === "Escape") { e.preventDefault(); editing = null; renderGrid(); grid.focus(); }
        else if (e.key === "Tab") { e.preventDefault(); commit(true); }
      });
      input.addEventListener("input", () => {
        input.value = input.value.replace(/[^0-9a-f]/gi, "").toUpperCase();
        if (input.value.length === 2) commit(true);
      });
      input.addEventListener("blur", () => {
        setTimeout(() => { if (editing !== null && document.activeElement !== input) { editing = null; renderGrid(); } }, 150);
      });
    }

    async function doPoke() {
      const addr = parseNum(pokeAddr.value, 0xFFFF);
      const bytes = pokeBytes.value.trim().split(/[\s,]+/).filter(Boolean).map((b) => parseNum(b, 0xFF));
      if (addr === null) return flash("Enter a hex address to write to");
      if (!bytes.length || bytes.some((b) => b === null)) return flash("Bytes must be hex values 00-FF separated by spaces");
      const res = await call({ op: "write", addr, values: bytes });
      if (res && res.ok) { flash(`Wrote ${bytes.length} byte${bytes.length === 1 ? "" : "s"} at $${hex(addr, 4)}`, true); refreshNow(); }
    }

    function freezeSelected() {
      if (selected === null) return;
      const v = byteAt(selected);
      if (v === null) return;
      call({ op: "freeze_set", addr: selected, value: v, size: 1 }).then((r) => r && r.ok && flash(`Froze $${hex(selected, 4)} at $${hex(v, 2)}`, true));
    }
    function watchSelected() {
      if (selected === null) return;
      call({ op: "watch_add", addr: selected, size: 1, cond: "change" }).then((r) => r && r.ok && flash(`Watching $${hex(selected, 4)} - pauses when it changes`, true));
    }
    function breakSelected() {
      if (selected === null) return;
      call({ op: "bp_add", addr: selected }).then((r) => r && r.ok && flash(`Breakpoint at ${hex(r.result.bank, 2)}:${hex(r.result.addr, 4)}`, true));
    }

    // ---- search tab -----------------------------------------------------------

    const sizeSelect = el("select", { class: "gbd-input", "aria-label": "Value size" },
      el("option", { value: "1", text: "8-bit" }), el("option", { value: "2", text: "16-bit (LE)" }));
    const regionBoxes = SEARCH_REGIONS.map(([id, label, on]) => {
      const box = el("input", { type: "checkbox", value: id });
      box.checked = on;
      return [box, el("label", { class: "gbd-check" }, box, label)];
    });
    const condSelect = el("select", { class: "gbd-input", "aria-label": "Condition" },
      ...SEARCH_CONDS.map(([v, t]) => el("option", { value: v, text: t })));
    const searchValue = el("input", { class: "gbd-input gbd-w6", placeholder: "value", spellcheck: "false", "aria-label": "Value" });
    const searchStatus = el("div", { class: "gbd-selinfo", text: "Start a new search to snapshot memory." });
    const resultsBody = el("tbody");
    condSelect.addEventListener("change", () => { searchValue.hidden = !["eq", "ne", "gt", "lt"].includes(condSelect.value); });
    searchValue.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); doFilter(); } });

    panes.search.append(
      el("div", { class: "gbd-row" }, sizeSelect, ...regionBoxes.map(([, l]) => l)),
      el("div", { class: "gbd-row" },
        el("button", { class: "gbd-btn gbd-primary", type: "button", onclick: newSearch }, "New search"),
        el("button", { class: "gbd-btn", type: "button", onclick: resetSearch }, "Reset")),
      el("div", { class: "gbd-row" }, condSelect, searchValue,
        el("button", { class: "gbd-btn gbd-primary", type: "button", onclick: doFilter }, "Filter")),
      searchStatus,
      el("div", { class: "gbd-table-wrap" },
        el("table", { class: "gbd-table" },
          el("thead", null, el("tr", null, el("th", { text: "Address" }), el("th", { text: "Value" }), el("th", { text: "Previous" }), el("th", { text: "" }))),
          resultsBody)),
      el("p", { class: "gbd-hint", text: "Know the number? Filter \"= value\" (use # for decimal, e.g. #150). Don't? Start a search, change it in-game, then filter increased / decreased / unchanged until only a few addresses remain." }),
    );

    async function newSearch() {
      const regions = regionBoxes.filter(([b]) => b.checked).map(([b]) => b.value);
      if (!regions.length) return flash("Pick at least one region");
      const res = await call({ op: "search_new", regions, size: Number(sizeSelect.value) });
      if (res && res.ok) { search = res; renderSearch(); }
    }
    async function resetSearch() {
      await call({ op: "search_reset" });
      search = null;
      renderSearch();
    }
    async function doFilter() {
      if (!search) return flash("Start a new search first");
      const req = { op: "search_filter", cond: condSelect.value };
      if (!searchValue.hidden) {
        const v = parseNum(searchValue.value, search.size === 1 ? 0xFF : 0xFFFF);
        if (v === null) return flash(`Value must be hex 0-${search.size === 1 ? "FF" : "FFFF"} (or #decimal)`);
        req.value = v;
      }
      const res = await call(req);
      if (res && res.ok) { search = res; renderSearch(); }
    }
    function renderSearch() {
      resultsBody.replaceChildren();
      if (!search) { searchStatus.textContent = "Start a new search to snapshot memory."; return; }
      const w = search.size === 1 ? 2 : 4;
      searchStatus.textContent = `${search.count.toLocaleString()} candidate${search.count === 1 ? "" : "s"} after ${search.steps} filter${search.steps === 1 ? "" : "s"}` +
        (search.count > search.results.length ? ` (showing first ${search.results.length})` : "");
      for (const r of search.results) {
        resultsBody.appendChild(el("tr", null,
          el("td", null, el("button", { class: "gbd-link", type: "button", onclick: () => { setTab("memory"); jumpTo(r.addr - (r.addr % cols) - cols * 4, false); select(r.addr); } }, `$${hex(r.addr, 4)}`)),
          el("td", { text: `$${hex(r.value, w)} (${r.value})` }),
          el("td", { text: `$${hex(r.prev, w)} (${r.prev})` }),
          el("td", { class: "gbd-actions" },
            el("button", { class: "gbd-btn gbd-sm", type: "button", onclick: () => call({ op: "freeze_set", addr: r.addr, value: r.value, size: search.size }).then((x) => x && x.ok && flash(`Froze $${hex(r.addr, 4)}`, true)) }, "Freeze"),
            el("button", { class: "gbd-btn gbd-sm", type: "button", onclick: () => call({ op: "watch_add", addr: r.addr, size: search.size, cond: "change" }).then((x) => x && x.ok && flash(`Watching $${hex(r.addr, 4)}`, true)) }, "Watch")),
        ));
      }
    }

    // ---- breakpoints tab ------------------------------------------------------

    const bpInput = el("input", { class: "gbd-input gbd-grow", placeholder: "0150, 01:4A2F, or a .sym label", spellcheck: "false", "aria-label": "Breakpoint address" });
    const bpList = el("div", { class: "gbd-list" });
    const watchAddr = el("input", { class: "gbd-input gbd-w6", placeholder: "D347", spellcheck: "false", "aria-label": "Watch address" });
    const watchSize = el("select", { class: "gbd-input", "aria-label": "Watch size" }, el("option", { value: "1", text: "8-bit" }), el("option", { value: "2", text: "16-bit" }));
    const watchCond = el("select", { class: "gbd-input", "aria-label": "Watch condition" }, ...WATCH_CONDS.map(([v, t]) => el("option", { value: v, text: t })));
    const watchValue = el("input", { class: "gbd-input gbd-w6", placeholder: "value", spellcheck: "false", hidden: true, "aria-label": "Watch value" });
    const watchList = el("div", { class: "gbd-list" });
    const freezeAddr = el("input", { class: "gbd-input gbd-w6", placeholder: "D347", spellcheck: "false", "aria-label": "Freeze address" });
    const freezeValue = el("input", { class: "gbd-input gbd-w6", placeholder: "value", spellcheck: "false", "aria-label": "Freeze value" });
    const freezeSize = el("select", { class: "gbd-input", "aria-label": "Freeze size" }, el("option", { value: "1", text: "8-bit" }), el("option", { value: "2", text: "16-bit" }));
    const freezeList = el("div", { class: "gbd-list" });
    watchCond.addEventListener("change", () => { watchValue.hidden = watchCond.value === "change"; });
    bpInput.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); addBreakpoint(); } });

    panes.breaks.append(
      el("h3", { class: "gbd-h", text: "Execution breakpoints" }),
      el("div", { class: "gbd-row" }, bpInput,
        el("button", { class: "gbd-btn gbd-primary", type: "button", onclick: addBreakpoint }, "Add"),
        el("button", { class: "gbd-btn", type: "button", onclick: () => call({ op: "bp_clear" }) }, "Clear all")),
      bpList,
      el("h3", { class: "gbd-h", text: "Watches (checked every frame)" }),
      el("div", { class: "gbd-row" }, watchAddr, watchSize, watchCond, watchValue,
        el("button", { class: "gbd-btn gbd-primary", type: "button", onclick: addWatch }, "Add")),
      watchList,
      el("h3", { class: "gbd-h", text: "Frozen values" }),
      el("div", { class: "gbd-row" }, freezeAddr, freezeValue, freezeSize,
        el("button", { class: "gbd-btn gbd-primary", type: "button", onclick: addFreeze }, "Freeze")),
      freezeList,
      el("p", { class: "gbd-hint", text: "Execution breakpoints stop on the exact instruction. For $4000-$7FFF the ROM bank is detected automatically when it's unambiguous; otherwise enter BB:AAAA. Everything here is cleared when control changes hands or a ROM loads." }),
    );

    async function addBreakpoint() {
      const text = bpInput.value.trim();
      if (!text) return;
      let req;
      const banked = /^([0-9a-f]{1,3}):\$?([0-9a-f]{1,4})$/i.exec(text);
      if (banked) req = { op: "bp_add", bank: parseInt(banked[1], 16), addr: parseInt(banked[2], 16) };
      else if (/^(\$|0x)?[0-9a-f]{1,4}h?$/i.test(text)) req = { op: "bp_add", addr: parseNum(text, 0xFFFF) };
      else req = { op: "bp_add", addr: text };
      const res = await call(req);
      if (res && res.ok) { bpInput.value = ""; flash(`Breakpoint at ${hex(res.result.bank, 2)}:${hex(res.result.addr, 4)}`, true); refreshNow(); }
    }
    async function addWatch() {
      const size = Number(watchSize.value);
      const addr = parseNum(watchAddr.value, 0xFFFF);
      if (addr === null) return flash("Enter a hex address to watch");
      const req = { op: "watch_add", addr, size, cond: watchCond.value };
      if (watchCond.value !== "change") {
        const v = parseNum(watchValue.value, size === 1 ? 0xFF : 0xFFFF);
        if (v === null) return flash("Enter the value to compare against");
        req.value = v;
      }
      const res = await call(req);
      if (res && res.ok) { watchAddr.value = ""; refreshNow(); }
    }
    async function addFreeze() {
      const size = Number(freezeSize.value);
      const addr = parseNum(freezeAddr.value, 0xFFFF);
      const value = parseNum(freezeValue.value, size === 1 ? 0xFF : 0xFFFF);
      if (addr === null || value === null) return flash("Enter a hex address and value");
      const res = await call({ op: "freeze_set", addr, value, size });
      if (res && res.ok) { freezeAddr.value = ""; freezeValue.value = ""; refreshNow(); }
    }

    function renderLists() {
      const bps = (state && state.breakpoints) || [];
      bpList.replaceChildren(...(bps.length ? bps.map((b) => el("div", { class: "gbd-item" + (b.error ? " err" : "") },
        el("button", { class: "gbd-link", type: "button", onclick: () => { setTab("memory"); jumpTo(b.addr - cols * 4, false); select(b.addr); } }, `${hex(b.bank, 2)}:${hex(b.addr, 4)}`),
        el("span", { class: "gbd-meta", text: b.error ? b.error : `${b.hits} hit${b.hits === 1 ? "" : "s"}${b.installed ? "" : " \u00B7 applying\u2026"}` }),
        el("button", { class: "gbd-btn gbd-sm", type: "button", onclick: () => call({ op: "bp_remove", bank: b.bank, addr: b.addr }) }, "Remove"),
      )) : [el("div", { class: "gbd-empty", text: "None" })]));

      const ws = (state && state.watches) || [];
      watchList.replaceChildren(...(ws.length ? ws.map((w) => {
        const width = w.size === 1 ? 2 : 4;
        const cond = WATCH_CONDS.find(([v]) => v === w.cond)[1];
        return el("div", { class: "gbd-item" },
          el("button", { class: "gbd-link", type: "button", onclick: () => { setTab("memory"); jumpTo(w.addr - cols * 4, false); select(w.addr); } }, `$${hex(w.addr, 4)}`),
          el("span", { class: "gbd-meta", text: `${cond}${w.cond === "change" ? "" : " $" + hex(w.value, width)} \u00B7 now $${hex(w.last, width)} \u00B7 ${w.hits} hit${w.hits === 1 ? "" : "s"}` }),
          el("button", { class: "gbd-btn gbd-sm", type: "button", onclick: () => call({ op: "watch_remove", id: w.id }) }, "Remove"));
      }) : [el("div", { class: "gbd-empty", text: "None" })]));

      const fs = (state && state.freezes) || [];
      freezeList.replaceChildren(...(fs.length ? fs.map((f) => el("div", { class: "gbd-item" },
        el("button", { class: "gbd-link", type: "button", onclick: () => { setTab("memory"); jumpTo(f.addr - cols * 4, false); select(f.addr); } }, `$${hex(f.addr, 4)}`),
        el("span", { class: "gbd-meta", text: `held at $${hex(f.value, f.size === 1 ? 2 : 4)} (${f.value})` }),
        el("button", { class: "gbd-btn gbd-sm", type: "button", onclick: () => call({ op: "freeze_remove", addr: f.addr }) }, "Unfreeze"),
      )) : [el("div", { class: "gbd-empty", text: "None" })]));
    }

    // ---- CPU tab --------------------------------------------------------------

    const regCells = {};
    const regGrid = el("div", { class: "gbd-regs" });
    for (const [name, width] of [["A", 2], ["F", 2], ["B", 2], ["C", 2], ["D", 2], ["E", 2], ["HL", 4], ["SP", 4], ["PC", 4]]) {
      const value = el("button", { class: "gbd-regval", type: "button", onclick: () => editRegister(name, width) }, "--");
      regCells[name] = value;
      regGrid.appendChild(el("div", { class: "gbd-reg" }, el("span", { class: "gbd-regname", text: name }), value));
    }
    const flagCells = {};
    const flagRow = el("div", { class: "gbd-flags" });
    for (const [flag, bit] of [["Z", 7], ["N", 6], ["H", 5], ["C", 4]]) {
      flagCells[flag] = el("span", { class: "gbd-flag", text: flag, "data-bit": String(bit) });
      flagRow.appendChild(flagCells[flag]);
    }
    const breakInfo = el("div", { class: "gbd-selinfo", text: "" });
    panes.cpu.append(
      regGrid, flagRow, breakInfo,
      el("div", { class: "gbd-row" },
        el("button", { class: "gbd-btn", type: "button", onclick: () => { if (state && state.registers) { setTab("memory"); jumpTo(state.registers.PC - cols * 4, false); select(state.registers.PC); } } }, "View PC in memory"),
        el("button", { class: "gbd-btn", type: "button", onclick: () => { if (state && state.registers) { setTab("memory"); jumpTo(state.registers.SP - cols * 4, false); select(state.registers.SP); } } }, "View stack")),
      el("p", { class: "gbd-hint", text: "Registers are exact while stopped. While running they're a snapshot taken between frames. Click a register to change it (controller only)." }),
    );

    async function editRegister(name, width) {
      if (!state || !state.registers) return;
      if (!isController) return flash("Only the current controller can change registers");
      const text = window.prompt(`New value for ${name} (hex)`, hex(state.registers[name], width));
      if (text === null) return;
      const v = parseNum(text, width === 2 ? 0xFF : 0xFFFF);
      if (v === null) return flash("Invalid value");
      const res = await call({ op: "set_register", name, value: v });
      if (res && res.ok) refreshNow();
    }

    function renderCpu() {
      const regs = state && state.registers;
      for (const [name, cell] of Object.entries(regCells)) {
        cell.textContent = regs ? hex(regs[name], name.length === 2 ? 4 : 2) : "--";
      }
      for (const cell of Object.values(flagCells)) {
        cell.classList.toggle("on", !!regs && ((regs.F >> Number(cell.dataset.bit)) & 1) === 1);
      }
      breakInfo.textContent = describeBreak();
    }

    // ---- shared state rendering ---------------------------------------------

    function describeBreak() {
      if (!state) return "";
      const b = state.break;
      if (!state.paused || !b) return "";
      if (b.type === "breakpoint") return `Stopped at breakpoint ${hex(b.bank, 2)}:${hex(b.addr, 4)}`;
      if (b.type === "watch") {
        const w = b.size === 1 ? 2 : 4;
        return `Watch $${hex(b.addr, 4)} changed $${hex(b.old, w)} \u2192 $${hex(b.new, w)} (PC $${hex(b.pc, 4)}, end of frame)`;
      }
      if (b.type === "step") return `Stepped one frame (PC $${hex(b.pc, 4)})`;
      return `Paused (PC $${hex(b.pc, 4)})`;
    }

    function renderChrome() {
      win.classList.toggle("paused", !!(state && state.paused));
      win.classList.toggle("viewonly", !isController);
      if (!state) { statusText.textContent = "Connecting\u2026"; return; }
      if (!state.running) statusText.textContent = "No ROM running";
      else if (!state.available) statusText.textContent = "Needs the pyboy engine";
      else if (state.paused) statusText.textContent = describeBreak() || "Paused";
      else statusText.textContent = "Running";
      pauseBtn.textContent = state.paused ? "Continue" : "Pause";
      pauseBtn.disabled = !isController || !state.available;
      stepBtn.disabled = !isController || !state.paused;
      roleText.textContent = isController ? "" : "view-only (you're not the controller)";
    }

    function renderAll() {
      renderChrome();
      renderLists();
      renderCpu();
      if (editing === null) renderGrid();
      renderSelInfo();
    }

    function setTab(id) {
      activeTab = id;
      for (const [k, t] of Object.entries(tabs)) {
        t.classList.toggle("active", k === id);
        t.setAttribute("aria-selected", k === id ? "true" : "false");
        panes[k].hidden = k !== id;
      }
      refreshNow();
    }

    function togglePause() {
      if (!state) return;
      call({ op: state.paused ? "continue" : "pause" });
    }

    // ---- transport ------------------------------------------------------------

    let toastTimer = null;
    function flash(message, good) {
      toast.textContent = message;
      toast.classList.toggle("good", !!good);
      toast.hidden = false;
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => { toast.hidden = true; }, 3500);
    }

    async function call(req) {
      let res;
      try {
        for (let attempt = 0; attempt < 4; attempt++) {
          res = await bridge.request(req);
          if (res.ok || res.error !== "slow down") break;
          await new Promise((r) => setTimeout(r, 250));
        }
      } catch (err) {
        flash(err && err.message ? err.message : "No response from server");
        return null;
      }
      if (!res.ok) {
        if (res.error === "disabled") flash("The debugger is disabled on this server");
        else if (res.error === "slow down") flash("Too many requests - try again in a moment");
        else flash(res.error || "Request failed");
      } else if (!["read", "state", "search_results"].includes(req.op)) {
        setTimeout(refreshNow, 30);
      }
      return res;
    }

    async function refresh() {
      if (!open || inflight) return;
      inflight = true;
      try {
        const st = await bridge.request({ op: "state" });
        if (st.ok) {
          state = st.state;
          isController = st.is_controller;
        } else if (st.error === "disabled") {
          statusText.textContent = "Disabled on this server";
          return;
        }
        if (activeTab === "memory" && editing === null && state && state.available) {
          const len = cols * ROWS;
          const mem = await bridge.request({ op: "read", start: memBase, length: len });
          if (mem.ok) {
            if (memBytes && memStart === mem.start) prevMem = { start: memStart, bytes: memBytes };
            else prevMem = null;
            memStart = mem.start;
            memBytes = Uint8Array.from(mem.hex.match(/../g) || [], (h) => parseInt(h, 16));
          }
        }
        if (activeTab === "search" && search && search.count <= 100 && Date.now() - lastSearchRefresh > 1000) {
          lastSearchRefresh = Date.now();
          const sr = await bridge.request({ op: "search_results" });
          if (sr.ok) { search = sr; renderSearch(); }
        }
        renderAll();
      } catch (_) {
        statusText.textContent = "Waiting for connection\u2026";
      } finally {
        inflight = false;
      }
    }

    function refreshNow() {
      setTimeout(refresh, 0);
    }

    bridge.onEvent((evt) => {
      if (!open) return;
      if (evt.state) state = evt.state;
      if (evt.event === "break") {
        if (state.break && state.break.type !== "step") flash(describeBreak());
        if (state.break && state.break.type === "breakpoint" && editing === null) {
          const target = state.break.addr;
          if (target < memBase || target >= memBase + cols * ROWS) {
            memBase = Math.max(0, target - (target % cols) - cols * 4);
            memBytes = null;
          }
        }
      } else if (evt.event === "reset") {
        flash("Debugger state was reset (ROM changed or control moved)");
        search = null;
        renderSearch();
      }
      renderAll();
      refreshNow();
    });

    // ---- dragging (desktop) ---------------------------------------------------

    titleBar.addEventListener("pointerdown", (e) => {
      if (e.target === closeBtn || window.matchMedia("(max-width: 600px)").matches) return;
      const rect = win.getBoundingClientRect();
      const dx = e.clientX - rect.left;
      const dy = e.clientY - rect.top;
      titleBar.setPointerCapture(e.pointerId);
      const move = (ev) => {
        win.style.left = `${Math.max(0, Math.min(window.innerWidth - 120, ev.clientX - dx))}px`;
        win.style.top = `${Math.max(0, Math.min(window.innerHeight - 40, ev.clientY - dy))}px`;
        win.style.transform = "none";
      };
      const up = () => { titleBar.removeEventListener("pointermove", move); titleBar.removeEventListener("pointerup", up); };
      titleBar.addEventListener("pointermove", move);
      titleBar.addEventListener("pointerup", up);
    });

    // keep game keys from leaking out while typing in the debugger
    win.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && editing === null) { e.stopPropagation(); hide(); }
      else if (e.target.closest("input, select, textarea, .gbd-grid")) e.stopPropagation();
    });
    window.addEventListener("resize", () => { if (open && editing === null) renderGrid(); });

    function show() {
      open = true;
      win.hidden = false;
      document.body.classList.add("gbd-open");
      setTab(activeTab);
      clearInterval(timer);
      timer = setInterval(refresh, REFRESH_MS);
      renderAll();
    }

    function hide() {
      open = false;
      win.hidden = true;
      editing = null;
      document.body.classList.remove("gbd-open");
      clearInterval(timer);
      timer = null;
    }

    return { show, hide, toggle: () => (open ? hide() : show()), isOpen: () => open };
  }

  let instance = null;
  window.__gbserverDebugger = {
    open(bridge) {
      if (!instance) instance = create(bridge);
      instance.show();
      return instance;
    },
  };
})();
