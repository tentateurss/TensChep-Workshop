"use strict";
// Item Evaluator: paste an item, get trade-site listings (online sellers first, cheapest first). The server does the
// work (POST /api/evaluate): parsing, ranking what drives the price, relaxed searches, fetching listings.

const BRAND = "TensChep Workshop";
// the app may live in the pop-out window (see popOut): look there first, then in the page
const $ = (sel) => (state.pip && state.pip.document.querySelector(sel)) || document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const L = {code: "en", strings: {}, en: {}, langs: []};
const state = {result: null, text: "", pip: null, socket: null, tree: null, eqNodes: null};

function lookup(key) {
  if (L.code !== "en" && typeof L.strings[key] === "string") return [L.strings[key], false];
  return [typeof L.en[key] === "string" ? L.en[key] : key, L.code !== "en"];
}
function tt(key, vars = {}) {          // HTML; English fallback marked EN
  const [text, fb] = lookup(key);
  return esc(text).replace(/\{(\w+)\}/g, (m, k) => (k in vars ? vars[k] : m)) + (fb ? ' <sup class="en-mark">EN</sup>' : "");
}
function tx(key) { const [text, fb] = lookup(key); return (fb ? "[EN] " : "") + text; }
// server-rendered text: an untranslated key or game name carries "[EN] " - shown as the EN mark
const en = (s) => esc(s ?? "").replace(/\[EN\] /g, '<sup class="en-mark">EN</sup> ');
const fmtC = (c) => c == null ? "—" : c >= 1000 ? `${(c / 1000).toFixed(1)}k c` : `${c.toFixed(c < 10 ? 1 : 0)} c`;

function renderStatic() {
  document.documentElement.lang = L.code;
  document.title = `${tx("hub.tool.evaluator.title")} · ${BRAND}`;
  document.querySelectorAll("[data-i18n]").forEach((el) => { el.innerHTML = tt(el.dataset.i18n); });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => { el.placeholder = tx(el.dataset.i18nPlaceholder); });
  $("#langSelect").innerHTML = L.langs.map((l) =>
    `<button class="lang ${l.code === L.code ? "on" : ""}" data-lang="${esc(l.code)}" title="${esc(l.name)}">` +
    `<span class="flag">${esc(l.flag)}</span> ${esc(l.code.toUpperCase())}</button>`).join("");
}

const fmtPrice = (p) => `${esc(p.amount ?? "—")} ${esc(p.currency ?? "")}`;

// the price at a glance - right under the item line, before anything else
function summaryHtml(r) {
  const s = r.summary;
  if (!s) return (r.passes || []).length && !r.message ? `<div class="summary none">${tt("eval.none")}</div>` : "";
  const conv = s.chaos != null && s.price.currency !== "chaos" ? ` <span class="muted">≈ ${esc(fmtC(s.chaos))}</span>` : "";
  const sub = s.offline_only ? tt("eval.summary.offline") : tt("eval.summary.online", {n: esc(s.online)});
  return `<div class="summary ${s.offline_only ? "offline" : ""}"><span class="summary-label">${tt("eval.summary.cheapest")}</span>
    <span class="summary-price">${fmtPrice(s.price)}</span>${conv}<span class="summary-sub">${sub}</span></div>`;
}

function listingsHtml(r) {
  if (r.listings && r.listings.length) {
    return `<div class="card detail"><h2>${tt("eval.listings")}</h2><div class="table-wrap"><table class="listings">
      <thead><tr><th>#</th><th>${tt("eval.col.price")}</th><th>${tt("eval.col.chaos")}</th><th>${tt("eval.col.seller")}</th>
        <th>${tt("eval.col.ilvl")}</th><th>${tt("eval.col.mods")}</th></tr></thead><tbody>
      ${r.listings.map((l, i) => `<tr><td>${i + 1}</td><td>${fmtPrice(l.price)}</td>
        <td>${esc(fmtC(l.chaos))}</td><td><span class="badge ${l.online ? "online" : "offline"}">${tt(l.online ? "eval.online" : "eval.offline")}</span></td>
        <td>${l.ilvl ? esc(l.ilvl) : ""}</td><td class="mods">${l.mods.map(esc).join("<br>")}</td></tr>`).join("")}
      </tbody></table></div></div>`;
  }
  return "";
}

// ---- Timeless Jewel calculator (inline, when the pasted item is a Timeless Jewel)
function socketLabel(s) {
  const what = s.keystones.length ? s.keystones.join(", ") : s.notables.slice(0, 3).join(", ");
  return `${s.class} · ${what}${s.large ? ` (${tx("eval.tj.large")})` : ""}`;
}

function treeSvg(t) {
  if (!state.tree) return `<div class="tj-tree muted">…</div>`;
  const {points, sockets, radius} = state.tree;
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const [x, y] of points) { x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y); }
  const pad = 400, sel = sockets.find((s) => s.id === state.socket);
  const dots = points.map(([x, y, k]) => k === 4 ? "" :
    `<circle cx="${x}" cy="${y}" r="${k === 2 ? 70 : k === 1 ? 45 : 22}" class="pt k${k}"/>`).join("");
  const socks = sockets.map((s) => `<circle cx="${s.x}" cy="${s.y}" r="150" class="sock ${s.id === state.socket ? "on" : ""}" data-socket="${esc(s.id)}"><title>${esc(socketLabel(t.sockets.find((x) => x.id === s.id) || {class: "", keystones: [], notables: []}))}</title></circle>`).join("");
  const ring = sel ? `<circle cx="${sel.x}" cy="${sel.y}" r="${radius}" class="ring"/>` : "";
  return `<svg class="tj-tree" viewBox="${x0 - pad} ${y0 - pad} ${x1 - x0 + 2 * pad} ${y1 - y0 + 2 * pad}" role="img" aria-label="${esc(tx("eval.tj.socket"))}">${dots}${ring}${socks}</svg>`;
}

function effectLines(e) {
  if (e.change === "missing") return `<span class="muted">${tt("eval.tj.missing")}</span>`;
  if (e.change === "unchanged") return `<span class="muted">${tt("eval.tj.unchanged")}</span>`;
  if (e.change === "added") return `<span class="tj-add">${tt("eval.tj.added")}:</span> ${e.added.map(esc).join("; ")}`;
  return `→ <b>${en(e.new_name)}</b>${e.new.length ? ": " + e.new.map(esc).join("; ") : ` <span class="muted">(${tt("eval.tj.blank")})</span>`}`;
}

// "Find Equivalent Seeds": other seeds with the same must-have notable changes, priced on the trade site
function equivalentsHtml(q) {
  if (!q) return "";
  if (!q.seeds.length) return `<div class="tj-equiv"><h3>${tt("eval.tj.equivTitle")}</h3><p class="muted">${tt("eval.tj.equivNone", {n: esc(q.checked)})}</p></div>`;
  const head = q.closest ? tt("eval.tj.equivClosest", {n: esc(q.checked)})
                         : tt("eval.tj.equivFound", {exact: esc(q.exact), n: esc(q.checked), scanned: esc(q.scanned)});
  const rows = q.seeds.slice(0, 15).map((s) => {
    const l = s.listing;
    const price = l ? `${fmtPrice(l.price)}${l.chaos != null && l.price.currency !== "chaos" ? ` <span class="muted">≈ ${esc(fmtC(l.chaos))}</span>` : ""}
      <span class="badge ${l.online ? "online" : "offline"}">${tt(l.online ? "eval.online" : "eval.offline")}</span>` : `<span class="muted">${tt("eval.tj.equivUnlisted")}</span>`;
    const match = tt(s.matched === s.of ? (s.better ? "eval.tj.equivBetter" : "eval.tj.equivSame") : "eval.tj.equivPartial",
                     {m: esc(s.matched), n: esc(s.of)});
    return `<tr><td><b>${esc(s.seed)}</b></td><td>${match}</td><td>${price}</td>
      <td><a href="${esc(s.url)}" target="_blank" rel="noopener">${tt("eval.tj.equivOpen")}</a></td></tr>`;
  }).join("");
  return `<div class="tj-equiv"><h3>${tt("eval.tj.equivTitle")}</h3><p class="hint">${head}</p>
    <table class="tj-table"><thead><tr><th>${tt("eval.tj.seed")}</th><th>${tt("eval.tj.equivMatch")}</th><th>${tt("eval.col.price")}</th><th></th></tr></thead>
    <tbody>${rows}</tbody></table>
    ${q.search_url ? `<a href="${esc(q.search_url)}" target="_blank" rel="noopener">${tt("eval.tj.equivAll")}</a>` : ""}</div>`;
}

function timelessHtml(r) {
  const t = r.timeless;
  if (!t) return "";
  const provenance = t.provenance || {};
  const warning = `<div class="warn tj-compatibility" role="status" data-compatibility="${esc(provenance.compatibility || "unverified")}">${tt("eval.tj.compatibility")}: <b>${tt("eval.tj.state." + (provenance.compatibility || "unverified"))}</b> &middot; ${tt("eval.tj.gameVersion")}: ${esc(provenance.game_version || "?")} &middot; ${tt("eval.tj.revision")}: ${esc(provenance.revision || "?")}<p>${tt("m.tj." + (provenance.reason || "version_unknown"))}</p></div>`;
  const head = warning + `<div class="tj-facts"><span>${tt("eval.tj.jewel")}: <b>${esc(t.jewel)}</b></span>
    <span>${tt("eval.tj.conqueror")}: <b>${esc(t.conqueror)}</b></span><span>${tt("eval.tj.seed")}: <b>${esc(t.seed)}</b></span></div>`;
  if (!t.data || !t.sockets) return `<div class="card detail"><h2>${tt("eval.tj.title")}</h2>${head}</div>`;
  const opts = `<option value="">${esc(tx("eval.tj.pick"))}</option>` + t.sockets.map((s) =>
    `<option value="${esc(s.id)}" ${s.id === state.socket ? "selected" : ""}>${esc(socketLabel(s))}</option>`).join("");
  let res = "";
  const c = t.calc;
  if (c) {
    // notables carry a "must-have" box for the equivalent-seed search (all changed ones ticked by default)
    const must = (e) => e.kind === "notable" && e.change !== "unchanged" && e.change !== "missing"
      ? `<input type="checkbox" class="tj-must" data-node="${esc(e.id)}" ${state.eqNodes === null || state.eqNodes.includes(e.id) ? "checked" : ""} title="${esc(tx("eval.tj.must"))}"> ` : "";
    const row = (e) => `<tr><td>${must(e)}<b>${en(e.name)}</b>${e.old.length ? `<div class="muted small">${e.old.map(esc).join("; ")}</div>` : ""}</td><td>${effectLines(e)}</td></tr>`;
    const ks = c.nodes.filter((e) => e.kind === "keystone"), nt = c.nodes.filter((e) => e.kind === "notable");
    const smalls = c.nodes.filter((e) => e.kind === "normal");
    const groups = {};
    for (const e of smalls) {
      const key = e.change === "added" ? `+ ${e.added.join("; ")}` : e.change === "replaced" ? `${e.new_name}${e.new.length ? ": " + e.new.join("; ") : ""}` : tx("eval.tj.unchanged");
      groups[key] = (groups[key] || 0) + 1;
    }
    const grp = Object.entries(groups).sort((a, b) => b[1] - a[1]).map(([k, n]) => `<li>${en(k)} <span class="muted">×${n}</span></li>`).join("");
    res = `${ks.length ? `<h3>${tt("eval.tj.keystones")}</h3><table class="tj-table">${ks.map(row).join("")}</table>` : ""}
      ${nt.length ? `<h3>${tt("eval.tj.notables")}</h3><table class="tj-table">${nt.map(row).join("")}</table>` : ""}
      ${smalls.length ? `<details><summary>${tt("eval.tj.smalls", {n: smalls.length})}</summary><ul class="tj-smalls">${grp}</ul></details>` : ""}
      <p class="hint">${tt("eval.tj.namesNote")} ${tt("eval.tj.credit")}</p>
      <div class="row wrap"><button id="tjEquiv" class="primary">${tt("eval.tj.equivBtn")}</button>
        <span class="hint">${tt("eval.tj.equivHint")}</span></div>
      ${equivalentsHtml(t.equivalents)}`;
  }
  return `<div class="card detail tj"><h2>${tt("eval.tj.title")}</h2>${head}
    <div class="row wrap"><label>${tt("eval.tj.socket")} <select id="tjSocket">${opts}</select></label>
      <button id="tjCalc" class="primary" ${state.socket ? "" : "disabled"}>${tt("eval.tj.calc")}</button></div>
    ${treeSvg(t)}${res}</div>`;
}

async function loadTree() {
  if (state.tree) return;
  try {
    const r = await fetch("/api/timeless/tree");
    if (r.ok) { state.tree = await r.json(); renderResult(); }
  } catch (e) { /* the list still works without the picture */ }
}

function pickSocket(id) {
  state.socket = id || null;
  try { localStorage.setItem("poecraft.tjSocket", state.socket || ""); } catch (e) { /* not persisted */ }
  renderResult();
}

function noticesHtml(notes) {
  if (!notes.length) return "";
  const items = notes.map((n, i) => `<li>${en(n.text)}${n.copy ? ` <button class="copy-mod" data-copy="${i}" title="${esc(tx("eval.notes.copyHint"))}">${tt("eval.notes.copy")}</button>` : ""}</li>`).join("");
  state.notes = notes;
  return `<details class="notices detail"><summary>${tt("eval.notes.summary", {n: notes.length})}</summary><ul>${items}</ul></details>`;
}

async function copyMod(btn) {
  const n = (state.notes || [])[+btn.dataset.copy];
  if (!n || !n.copy) return;
  try { await navigator.clipboard.writeText(n.copy); btn.textContent = tx("eval.notes.copied"); }
  catch (e) { window.prompt(tx("eval.notes.copyHint"), n.copy); }      // no clipboard permission: show it to copy
}

function renderResult() {
  const r = state.result;
  if (!r) return;
  const out = [];
  if (r.message) out.push(`<div class="card notice">${en(r.message)}</div>`);
  const it = r.item || {};
  const head = [it.name, it.type].filter(Boolean).map(esc).join(" · ");
  if (head) {
    out.push(`<div class="meta"><b>${head}</b>${it.ilvl ? ` · ${tt("eval.ilvl", {n: it.ilvl})}` : ""} · ${tt("eval.league", {league: esc(r.league)})}` +
             `${r.cached ? ` · <span class="muted">${tt("eval.cached")}</span>` : ""}</div>`);
  }
  if (r.timeless) out.push(timelessHtml(r));
  if (r.timeless && (r.passes || []).length) out.push(`<h2 class="detail tj-trade">${tt("eval.tj.trade")}</h2>`);
  out.push(summaryHtml(r));
  if (r.ninja_price != null) out.push(`<div class="meta">${tt("eval.ninja", {c: esc(fmtC(r.ninja_price))})}</div>`);
  // real failures stay visible (small); everything else goes into one collapsible notice at the bottom
  const notes = r.notes || (r.warnings || []).map((w) => ({text: w, level: "info", copy: null}));
  for (const n of notes.filter((n) => n.level === "error")) out.push(`<div class="note-error">${en(n.text)}</div>`);
  out.push(listingsHtml(r));
  if ((r.ranking || []).length) {
    const dropped = new Set(((r.passes || []).slice(-1)[0] || {}).dropped || []);
    out.push(`<div class="card detail"><h2>${tt("eval.ranking")}</h2><p class="hint">${tt("eval.rankingHint")}</p>
      <table class="ranking"><thead><tr><th>${tt("eval.col.property")}</th><th>${tt("eval.col.why")}</th><th></th></tr></thead><tbody>
      ${r.ranking.map((e) => `<tr class="${dropped.has(e.label) ? "dropped" : ""}"><td>${en(e.label)}</td><td class="muted">${en(e.why)}</td>
        <td>${e.core ? `<span class="badge core">${tt("eval.core")}</span>` : dropped.has(e.label) ? `<span class="badge">${tt("eval.droppedBadge")}</span>` : ""}</td></tr>`).join("")}
      </tbody></table></div>`);
  }
  if ((r.passes || []).length) {
    out.push(`<div class="card detail"><h2>${tt("eval.passes")}</h2><ol class="passes">${r.passes.map((p) =>
      `<li>${p.dropped.length ? tt("eval.pass.without", {list: p.dropped.map(en).join(", ")}) : tt("eval.pass.all")}
        ${p.status === "any" ? ` <span class="muted">(${tt("eval.pass.offline")})</span>` : ""}
        — <b>${p.total == null ? "…" : tt("eval.pass.found", {n: p.total})}</b></li>`).join("")}</ol>
      ${r.search_url ? `<a href="${esc(r.search_url)}" target="_blank" rel="noopener">${tt("eval.open")}</a>` : ""}</div>`);
  }
  out.push(noticesHtml(notes.filter((n) => n.level !== "error")));
  $("#result").innerHTML = out.join("");
  if (r.timeless && r.timeless.sockets) loadTree();
}

async function evaluate(withSocket = false, equivalents = null) {
  const text = $("#itemText").value.trim();
  if (!text) return;
  $("#evalBtn").disabled = true;
  const same = text === state.text;
  const prev = state.result && state.result.timeless;
  // a re-evaluation of the same jewel (language switch, Evaluate again) keeps the calculation and the seed search
  const eq = equivalents || (same && prev && prev.equivalents ? (state.eqNodes || []) : null);
  $("#result").innerHTML = `<div class="card muted">${tt(eq ? "eval.tj.equivBusy" : document.body.classList.contains("compact") || state.pip ? "eval.busyShort" : "eval.busy")}</div>`;
  try {
    const r = await fetch("/api/evaluate", {method: "POST", headers: {"Content-Type": "application/json", "X-Poecraft-Lang": L.code},
      body: JSON.stringify({text, lang: L.code, league: $("#league").value || null, equivalents: eq,
        socket: (withSocket === true || (prev && prev.calc)) && same ? state.socket : null})});
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || r.statusText);
    state.result = j; state.text = text;
    renderResult();
  } catch (e) {
    $("#result").innerHTML = `<div class="card error">${en(e.message)}</div>`;
  } finally {
    $("#evalBtn").disabled = false;
  }
}

async function setLang(code) {
  const loc = await (await fetch(`/api/locale?lang=${encodeURIComponent(code)}`)).json();
  Object.assign(L, {code: loc.code, strings: loc.strings, en: loc.en});
  try { localStorage.setItem("poecraft.lang", loc.code); } catch (e) { /* not persisted */ }
  renderStatic();
  // the result's texts come from the server in a language: ask again (cached there) in the new one
  if (state.result && state.text) evaluate(); else renderResult();
}

async function init() {
  try { L.langs = await (await fetch("/api/locales")).json(); } catch (e) { L.langs = [{code: "en", name: "English", flag: "EN"}]; }
  let want = "en";
  try { want = localStorage.getItem("poecraft.lang") || "en"; } catch (e) { /* default */ }
  await setLang(L.langs.some((l) => l.code === want) ? want : "en");
  try {
    const lg = await (await fetch("/api/trade/leagues")).json();
    $("#league").innerHTML = `<option value="">${esc(tx("trade.auto").replace("{league}", lg.league))}</option>` +
      lg.known.filter((l) => l !== lg.league).map((l) => `<option value="${esc(l)}">${esc(l)}</option>`).join("");
  } catch (e) { $("#league").innerHTML = `<option value="">—</option>`; }
}

// Compact mode ("overlay"): only the input and the price - for a small browser window in a corner of the screen.
// Remembered per browser; /evaluator?compact=1 opens in it (for a bookmark).
function setCompact(on, save = true) {
  document.body.classList.toggle("compact", on);
  for (const b of [document.body, state.pip && state.pip.document.body]) if (b) b.classList.toggle("compact", on);
  $("#compactToggle").checked = on;
  $("#itemText").rows = on ? 3 : 9;
  if (save) try { localStorage.setItem("poecraft.evalCompact", on ? "1" : "0"); } catch (e) { /* not persisted */ }
}

async function pasteAndEvaluate() {
  try {
    const text = await navigator.clipboard.readText();
    if (text && text.trim()) { $("#itemText").value = text; evaluate(); return; }
  } catch (e) { /* no clipboard permission: paste by hand */ }
  $("#itemText").focus();
}

// "Pop out": Chrome / Edge can keep a small always-on-top window (Document Picture-in-Picture) - the evaluator
// moves into it in compact mode and comes back when it is closed. Other browsers do not show the button.
async function popOut() {
  if (state.pip) { state.pip.close(); return; }
  const pip = await window.documentPictureInPicture.requestWindow({width: 380, height: 300});
  for (const link of document.querySelectorAll('link[rel="stylesheet"]')) {
    const l = pip.document.createElement("link"); l.rel = "stylesheet"; l.href = link.href; pip.document.head.appendChild(l);
  }
  pip.document.body.className = "hub compact pip";
  pip.document.title = document.title;
  const app = $("#evalApp");
  pip.document.body.appendChild(app);
  state.pip = pip;
  $("#itemText").rows = 3;
  pip.addEventListener("pagehide", () => {
    document.body.insertBefore(app, document.querySelector("script"));
    state.pip = null;
    setCompact($("#compactToggle").checked, false);
  });
}

// the calculator lives in the result area, which is re-rendered: listen on the container
$("#result").addEventListener("change", (e) => { if (e.target.id === "tjSocket") pickSocket(e.target.value); });
$("#result").addEventListener("click", (e) => {
  const s = e.target.closest("[data-socket]");
  if (s) { pickSocket(s.dataset.socket); return; }
  const copyBtn = e.target.closest(".copy-mod");
  if (copyBtn) { copyMod(copyBtn); return; }
  if (e.target.id === "tjCalc" && state.socket) { state.eqNodes = null; evaluate(true); }
  if (e.target.id === "tjEquiv" && state.socket) {
    state.eqNodes = [...document.querySelectorAll(".tj-must:checked"), ...(state.pip ? state.pip.document.querySelectorAll(".tj-must:checked") : [])]
      .map((b) => b.dataset.node);
    evaluate(true, state.eqNodes);
  }
});
try { state.socket = localStorage.getItem("poecraft.tjSocket") || null; } catch (e) { /* none remembered */ }

$("#compactToggle").addEventListener("change", (e) => setCompact(e.target.checked));
$("#pasteBtn").addEventListener("click", pasteAndEvaluate);
$("#itemText").addEventListener("paste", () => {
  // in compact mode a paste is the whole interaction: evaluate right away
  if (document.body.classList.contains("compact") || state.pip) setTimeout(evaluate, 0);
});
$("#itemText").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) evaluate(); });
if ("documentPictureInPicture" in window) {
  $("#pipBtn").classList.remove("hidden");
  $("#pipBtn").addEventListener("click", () => popOut().catch(() => {}));
}
{
  let on = new URLSearchParams(location.search).get("compact") === "1";
  try { on = on || localStorage.getItem("poecraft.evalCompact") === "1"; } catch (e) { /* default off */ }
  setCompact(on, false);
}

$("#langSelect").addEventListener("click", (e) => { const b = e.target.closest("[data-lang]"); if (b) setLang(b.dataset.lang); });
$("#evalBtn").addEventListener("click", evaluate);
init();
