"use strict";

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));

// ------------------------------------------------------------------- i18n
// UI strings come from poecraft/locales/<code>.json (via /api/locale). A key missing in the chosen language falls
// back to English and is marked "EN". Game data (base/mod/essence/fossil/currency names) is never translated here:
// the server sends it from the game's own localization by internal id; a name with no localized string stays English
// with the EN mark. Currency names inside UI strings ({divine}, {fracture} ...) come from L.names the same way.
const L = {code: "en", strings: {}, en: {}, game: {available: true}, names: {}};
const EN_MARK = () => `<sup class="en-mark" title="${esc(L.strings["mark.en.tip"] || L.en["mark.en.tip"] || "")}">EN</sup>`;
const cap = (s) => s[0].toUpperCase() + s.slice(1);
const fill = (text, vars, raw) => text.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? (raw ? String(vars[k]) : vars[k]) : m));
function lookup(key) {
  if (L.code !== "en" && typeof L.strings[key] === "string") return [L.strings[key], false];
  if (typeof L.en[key] === "string") return [L.en[key], L.code !== "en"];
  return [key, true];
}
// game currency names for UI strings: [text, localized] from the server
const nameVars = (html) => Object.fromEntries(Object.entries(L.names).map(([k, [text, ok]]) =>
  [k, html ? (ok ? "" : EN_MARK() + " ") + esc(text) : (ok ? "" : "[EN] ") + text]));
// HTML: the template is escaped, vars are inserted as given (callers escape them or pass deliberate markup)
function tt(key, vars = {}) {
  const [text, fb] = lookup(key);
  return fill(esc(text), {...nameVars(true), ...vars}) + (fb ? EN_MARK() : "");
}
// plain text (attributes, <option>, alert): fallback is prefixed "[EN] "
function tx(key, vars = {}) {
  const [text, fb] = lookup(key);
  return (fb ? "[EN] " : "") + fill(text, {...nameVars(false), ...vars}, true);
}
// server-generated English prose: escaped, marked "EN" when the UI is not English
// server-rendered text (explanations, reasons, warnings) arrives in the current language; a message key or game name
// that had no translation carries the "[EN] " prefix - shown as the EN badge
const en = (s) => s == null || s === "" ? "" : esc(s).replace(/\[EN\] /g, () => EN_MARK() + " ");
// game-data field localized by the server (by internal id); "_en" lists fields that stayed English -> EN mark
const g = (obj, field) => ((obj._en || []).includes(field) ? EN_MARK() + " " : "") + esc(obj[field]);
const enName = (s) => (L.code !== "en" ? EN_MARK() + " " : "") + esc(s);  // game names with no locale data yet
const statusText = (s) => (L.strings[`status.${s}`] || L.en[`status.${s}`]) ? tt(`status.${s}`) : en(s);
const statusBadge = (s) => `<span class="badge ${esc(s.split(" ").slice(0, 2).join("-"))}">${statusText(s)}</span>`;

const fmtC = (c) => c >= 1000 ? `${(c / 1000).toFixed(c >= 10000 ? 0 : 1)}k c` : `${c.toFixed(c < 10 ? 1 : 0)} c`;
const fmtP = (p) => p <= 0 ? "0%" : p >= 0.01 ? `${(p * 100).toFixed(1)}%`
  : `${(p * 100).toPrecision(3)}% (${tx("fmt.oneIn", {n: Math.round(1 / p).toLocaleString()})})`;

const state = {
  base: null, ilvl: 86, influences: [], maxInfluences: 2,
  families: [], affix: "", targets: [], have: [], result: null, shown: 0, meta: null, prices: null, langs: [],
};

async function api(path, opts = {}) {
  // the chosen language also as a header: answers given before the body is read (e.g. "too many requests") use it
  opts.headers = Object.assign({"X-Poecraft-Lang": L.code || "en"}, opts.headers || {});
  const r = await fetch(path, opts);
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}

function savedLang() {
  try { return localStorage.getItem("poecraft.lang"); } catch (e) { return null; }
}

async function setLang(code) {
  const loc = await api(`/api/locale?lang=${encodeURIComponent(code)}`);
  Object.assign(L, {code: loc.code, strings: loc.strings, en: loc.en, game: loc.game_data || {available: false},
                    names: loc.game_names || {}});
  try { localStorage.setItem("poecraft.lang", loc.code); } catch (e) { /* private mode: not persisted */ }
  document.documentElement.lang = loc.code;
  applyStatic();
  renderAll();
  // game data and explanations come rendered from the server: fetch them again in the new language
  if (state.meta) { refreshMeta(); searchBases(); loadPrices(); loadFamilies(); }
  if (state.result) { await localizeResult(); renderResult(); }
}

async function refreshMeta() {      // the feature card is rendered per language on the server
  try { state.meta = await api(`/api/meta?lang=${L.code}`); renderHeader(); } catch (e) { /* keep the old card */ }
}

function applyStatic() {
  document.querySelectorAll("[data-i18n]").forEach((el) => { el.innerHTML = tt(el.dataset.i18n); });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => { el.placeholder = tx(el.dataset.i18nPlaceholder); });
  document.querySelectorAll("[data-i18n-text]").forEach((el) => { el.textContent = tx(el.dataset.i18nText); });
  $("#gameDataMarker").innerHTML = L.game.available ? tt("gamedata.marker")
    : `<span class="badge en-badge">EN</span> ${tt("gamedata.missing")}`;
  $("#gameDataMarker").title = tx("gamedata.tip");
  renderClassOptions();
  document.querySelectorAll("[data-inf]").forEach((b) => { b.innerHTML = enName(cap(b.dataset.inf)); });
  document.title = `${tx("app.title")} · TensChep Workshop`;     // the brand stays English in every language
  document.querySelectorAll("[data-i18n-title]").forEach((el) => { el.title = tx(el.dataset.i18nTitle); });
  $("#langSelect").innerHTML = state.langs.map((l) =>
    `<button class="lang ${l.code === L.code ? "on" : ""}" data-lang="${esc(l.code)}" title="${esc(l.name)}">` +
    `<span class="flag">${esc(l.flag)}</span> ${esc(l.code.toUpperCase())}</button>`).join("");
  $("#langSelect").setAttribute("aria-label", tx("lang.label"));
}

function renderAll() {
  renderHeader();
  if (state.prices) renderPrices(state.prices);
  renderFamilies();
  renderTargets();
  if (state.result) renderResult();
}


// ------------------------------------------------------------------ init
async function init() {
  state.langs = await api("/api/locales");
  const want = savedLang();
  const meta = await api(`/api/meta?lang=${encodeURIComponent(savedLang() || "en")}`);
  state.meta = meta;
  state.maxInfluences = meta.max_influences;
  renderOcrNote();
  renderTemplates();
  loadTradeLeagues();
  $("#influences").innerHTML = meta.influences.map((i) =>
    `<button class="chip" data-inf="${esc(i)}">${esc(cap(i))}</button>`).join("");
  await setLang(state.langs.some((l) => l.code === want) ? want : "en");   // also loads bases and prices
}

function renderClassOptions() {     // item classes have no locale data yet: "[EN] " outside English
  const sel = $("#classFilter");
  if (!state.meta || !sel) return;
  const keep = sel.value;
  sel.innerHTML = `<option value="">${esc(tx("base.allClasses"))}</option>` + state.meta.item_classes.map((c) =>
    `<option value="${esc(c)}">${L.code !== "en" ? "[EN] " : ""}${esc(c)}</option>`).join("");
  sel.value = keep;
}

function renderHeader() {
  const meta = state.meta;
  if (!meta) return;
  $("#dataInfo").innerHTML = tt("header.data", {source: esc(meta.data.source), fetched: esc(meta.data.fetched_at)}) + ` ? Game data: ${esc(meta.data.game_version || "unverified")} (target 3.30)`;
  $("#features").innerHTML = meta.features.map((f) =>
    `<li>${statusBadge(f.status)} ${en(f.name)}${f.note ? ` <span class="muted">— ${en(f.note)}</span>` : ""}</li>`).join("");
}

// ----------------------------------------------------------------- prices
const priceSrc = (k) => (L.en[`prices.src.${k}`] ? tt(`prices.src.${k}`) : en(k));
function renderPrices(p) {
  state.prices = p;
  const live = p.state === "live", stale = p.state === "stale";
  $("#priceInfo").innerHTML = live
    ? `<span class="badge computed">${tt("prices.liveBadge", {league: esc(p.league)})}</span>`
    : `<span class="badge approximate">${tt(stale ? "prices.staleBadge" : "prices.estimatedBadge")}</span>`;
  const stateWord = L.en[`prices.state.${p.state}`] ? tt(`prices.state.${p.state}`) : en(p.state);
  const head = live
    ? tt("prices.liveHead", {league: `<b>${esc(p.league)}</b>`, at: esc(p.fetched_at), n: p.live_count})
    : stale
      ? `<span class="badge approximate">${tt("prices.state.stale")}</span> ${tt("prices.staleHead", {league: esc(p.league), at: esc(p.fetched_at)})}`
      : `<span class="badge approximate">${stateWord}</span> ${tt("prices.estHead")}`;
  $("#priceStatus").innerHTML = head + (p.message ? `<div class="warn" style="margin-top:6px">${en(p.message)}</div>` : "");
  $("#priceTable").innerHTML = `<thead><tr><th>${tt("prices.col.currency")}</th><th class="num">${tt("prices.col.chaos")}</th><th>${tt("prices.col.source")}</th></tr></thead><tbody>` +
    p.key_prices.map((k) => `<tr><td>${g(k, "name")}</td><td class="num">${k.chaos < 10 ? k.chaos.toFixed(3) : k.chaos.toFixed(1)}</td>
      <td>${k.source === "live" ? `<span class="badge ${live ? "computed" : "approximate"}">${priceSrc(live ? "live" : "snapshot")}</span>` : `<span class="badge approximate">${priceSrc(k.source)}</span>`}</td></tr>`).join("") +
    `</tbody>`;
}

async function loadPrices(tries = 0) {
  try {
    const p = await api(`/api/prices?lang=${L.code}`);
    renderPrices(p);
    // the server refreshes prices in the background at startup: re-read the local status a few times
    if (p.state !== "live" && tries < 6) setTimeout(() => loadPrices(tries + 1), 3000);
  } catch (e) {
    $("#priceStatus").innerHTML = `<div class="error">${en(e.message)}</div>`;
  }
}

async function updatePrices() {
  $("#priceBtn").disabled = true;
  $("#priceStatus").innerHTML = tt("prices.contacting");
  try {
    renderPrices(await api(`/api/prices/update?lang=${L.code}`, {method: "POST"}));
  } catch (e) {
    $("#priceStatus").innerHTML = `<div class="error">${en(e.message)}</div>`;
  } finally {
    $("#priceBtn").disabled = false;
  }
}

// ------------------------------------------------------------------ bases
let searchTimer;
async function searchBases() {
  const q = encodeURIComponent($("#baseSearch").value);
  const cls = encodeURIComponent($("#classFilter").value);
  const bases = await api(`/api/bases?q=${q}&cls=${cls}&lang=${L.code}`);
  $("#baseResults").innerHTML = bases.map((b) =>
    `<li data-id="${esc(b.id)}"><span>${g(b, "name")}</span><span class="muted">${g(b, "item_class")} · ${tt("base.lvl", {n: b.drop_level})}</span></li>`
  ).join("") || `<li class="muted">${tt("base.none")}</li>`;
}

async function selectBase(id) {
  state.base = id;
  state.targets = [];
  await loadFamilies();
}

async function loadFamilies() {
  if (!state.base) return;
  const inf = state.influences.join(",");
  try {
    const r = await api(`/api/families?base=${encodeURIComponent(state.base)}&ilvl=${state.ilvl}&inf=${inf}&lang=${L.code}`);
    state.families = r.families;
    state.search = null;
    if ($("#modFilter").value.trim()) onModFilter();          // same query, new base / language
    const props = Object.entries(r.base.properties || {}).filter(([, v]) => typeof v === "object")
      .map(([k, v]) => `${k.replace(/_/g, " ")} ${v.min}-${v.max}`).join(", ");
    $("#baseSelected").classList.remove("hidden");
    $("#baseSelected").innerHTML = `<b>${g(r.base, "name")}</b> <span class="muted">${g(r.base, "item_class")}${props ? " · " + esc(props) : ""}</span>`;
    $("#modFilter").disabled = false;
    // drop targets that can no longer appear (e.g. influence removed)
    const byKey = new Map(state.families.map((f) => [f.key || f.signature, f]));
    state.targets = state.targets.filter((t) => byKey.has(t.key));
    for (const t of state.targets) { t.fam = byKey.get(t.key); t.label = t.fam.label; }   // labels in the new language
    renderFamilies();
    renderTargets();
  } catch (e) {
    $("#families").innerHTML = `<li class="error">${en(e.message)}</li>`;
  }
}

// --------------------------------------------------------------- families
const affixText = (a) => (L.en[`affix.${a}`] ? tt(`affix.${a}`) : esc(a));
const sourceText = (s) => (L.en[`source.${s}`] ? tt(`source.${s}`) : esc(s));
function renderFamilies() {
  if (!state.base) { $("#families").innerHTML = `<li class="muted">${tt("mods.selectBase")}</li>`; return; }
  const q = $("#modFilter").value.toLowerCase().trim();
  const rollOnly = $("#rollOnly").checked;
  const chosen = new Set(state.targets.map((t) => t.key));
  // the server matches the query (poecraft/engine/modsearch.py): ordered keys of this base's families, closest first
  const found = q && state.search && state.search.query === q ? state.search : null;
  if (q && !found) return;                           // the answer for this query is on its way; keep the list
  const byKey = new Map(state.families.map((f) => [f.key || f.signature, f]));
  const fams = (found ? found.matches.map((k) => byKey.get(k)).filter(Boolean) : state.families).filter((f) =>
    (!state.affix || f.affix === state.affix) && (!rollOnly || f.sources.includes("roll")));
  const totalW = state.families.reduce((a, f) => a + f.roll_weight, 0) || 1;
  $("#families").innerHTML = fams.slice(0, 300).map((f) => {
    const badges = f.sources.map((s) => `<span class="badge ${esc(s)}">${sourceText(s)}</span>`).join("");
    const tiers = f.tiers.slice(0, 14).map((t) =>
      `<li>${g(t, "label")}: ${g(t, "text")} <span>(ilvl ${t.ilvl}${t.weight ? `, ${tt("mods.weight", {w: t.weight})}` : ""})</span></li>`).join("");
    const share = f.roll_weight ? ` · ${tt("mods.share", {p: (100 * f.roll_weight / totalW).toFixed(2)})}` : "";
    return `<li>
      <details><summary><span class="affix ${esc(f.affix)}">${affixText(f.affix)}</span>${g(f, "label")}${badges}
        <div class="meta muted" style="font-size:12px">${tt("mods.max", {v: f.max_value ?? "—"})}${share}</div></summary>
        <ul class="tiers">${tiers}</ul></details>
      <button data-sig="${esc(f.key || f.signature)}" ${chosen.has(f.key || f.signature) ? "disabled" : ""}>${tt("mods.add")}</button>
    </li>`;
  }).join("") + whyMissing(found, fams.length) || `<li class="muted">${tt("mods.none")}</li>`;
}

// Matching mods of this item class that cannot roll on the selected base, and the defence types they need
// (from the game data: every base of the class that can roll it has these defence properties).
const defName = (d) => tt(`def.${d}`);
const defList = (ds) => ds.map(defName).join("/");
function whyMissing(found, shown) {
  if (!found || !found.elsewhere.length) return "";
  const here = found.base_defence.length ? defList(found.base_defence) : tt("def.none");
  const rows = found.elsewhere.filter((e) => !state.affix || e.affix === state.affix).map((e) => {
    const req = e.requires.length ? defList(e.requires) : e.requires_any.map(defList).join(" | ");
    return `<li class="missing"><div><span class="affix ${esc(e.affix)}">${affixText(e.affix)}</span>${g(e, "label")}
      <div class="warn-text">${tt("mods.cannotRoll", {req})}</div>
      <div class="muted" style="font-size:12px">${tt("mods.thisBase", {def: here})} · ${tt("mods.rollsOn", {bases: e.examples.map(esc).join(", ")})}</div></div></li>`;
  }).join("");
  return rows ? `<li class="muted missing-head">${tt(shown ? "mods.alsoElsewhere" : "mods.onlyElsewhere")}</li>` + rows : "";
}

// typing in the filter asks the server (debounced); the list re-renders when the answer for the current text arrives
let modSearchTimer = null;
function onModFilter() {
  clearTimeout(modSearchTimer);
  const q = $("#modFilter").value.toLowerCase().trim();
  if (!q) { state.search = null; renderFamilies(); return; }
  modSearchTimer = setTimeout(async () => {
    const inf = state.influences.join(",");
    try {
      const r = await api(`/api/modsearch?base=${encodeURIComponent(state.base)}&ilvl=${state.ilvl}&inf=${inf}` +
                          `&lang=${L.code}&q=${encodeURIComponent(q)}`);
      if ($("#modFilter").value.toLowerCase().trim() === r.query) { state.search = r; renderFamilies(); }
    } catch (e) { $("#families").innerHTML = `<li class="error">${en(e.message)}</li>`; }
  }, 150);
}

function hintFor(f) {          // computed at render time, so a language switch updates it
  const best = f.tiers.find((t) => t.source === "roll") || f.tiers[0];
  const range = (k) => best && best.max != null ? tt(k, {min: best.min, max: best.max}) : "";
  if (f.eldritch) {
    const reachable = f.tiers.filter((t) => /^T[3-6]/.test(t.label)).sort((a, b) => b.max - a.max)[0];
    return tt("hint.eldritch", {side: esc(f.eldritch === "exarch" ? "Searing Exarch (Ember)" : "Eater of Worlds (Ichor)"),
                                upto: reachable ? tt("hint.upto", {v: reachable.max}) : ""});
  }
  if (f.beastcraft) return tt("hint.beastcraft");
  if (f.special_fossil) return tt("hint.specialFossil", {fossil: esc(f.special_fossil)});
  if (f.elevated) return tt("hint.elevated", {inf: esc(cap(f.elevated))});
  if (f.veiled) return tt("hint.veiled", {affix: affixText(f.affix), range: range("hint.range")});
  if (f.influence_target) return tt("hint.influence", {inf: esc(cap(f.influence_target)), range: range("hint.bestRange")});
  return best && best.min != null ? tt("hint.best", {min: best.min, max: best.max}) : tt("targets.any");
}

function addTarget(sig) {
  const f = state.families.find((x) => (x.key || x.signature) === sig);   // sig = family key
  if (!f) return;
  const sameAffix = state.targets.filter((t) => t.affix === f.affix).length;
  if (f.eldritch && sameAffix >= 1) {
    alert(tx("alert.oneEldritch", {side: f.eldritch === "exarch" ? "Searing Exarch" : "Eater of Worlds"})); return;
  }
  if (sameAffix >= 3) { alert(tx(f.affix === "prefix" ? "alert.maxPrefix" : "alert.maxSuffix")); return; }
  // no default threshold ("any tier"); the best tier's range is shown as a hint
  state.targets.push({key: sig, signature: f.signature, label: f.label, affix: f.affix, min: "", fam: f,
                     eldritch: f.eldritch || null, influence: f.influence_target || null, veiled: !!f.veiled,
                     elevated: f.elevated || null, special_fossil: f.special_fossil || null, beastcraft: !!f.beastcraft});
  renderTargets();
  renderFamilies();
}

function renderTargets() {
  $("#planBtn").disabled = !state.targets.length;
  if (!state.targets.length) { $("#targets").innerHTML = `<li class="muted">${tt("targets.none")}</li>`; return; }
  $("#targets").innerHTML = state.targets.map((t, i) => `
    <li><span><span class="affix ${esc(t.affix)}">${affixText(t.affix)}</span>${g(t.fam, "label")}</span>
      <label class="muted">${tt("targets.min")} <input type="number" step="any" data-i="${i}" value="${esc(t.min)}" placeholder="${esc(tx("targets.any"))}"></label>
      <button data-rm="${i}" title="${esc(tx("targets.remove"))}">✕</button>
      <span class="muted" style="grid-column:1/-1;font-size:12px">${hintFor(t.fam)}${t.eldritch || t.influence || t.veiled || t.elevated || t.special_fossil || t.beastcraft ? "" : `
        · <label><input type="checkbox" data-fr="${i}" ${t.fractured ? "checked" : ""}> ${tt("targets.fractured")}</label>`}</span></li>`).join("");
}

// ------------------------------------------------------------------- plan
// Reads the server's NDJSON stream: {"progress": {...}} lines while planning, then {"result": ...} or {"error": ...}.
async function streamPlan(path, body, onProgress) {
  const r = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json", "X-Poecraft-Lang": L.code || "en"},
                               body: JSON.stringify({...body, stream: true})});
  if (!r.body || !(r.headers.get("Content-Type") || "").includes("ndjson")) {
    const j = await r.json();                 // an early error (bad request) comes back as plain JSON
    if (!r.ok) throw new Error(j.error || r.statusText);
    return j;
  }
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const {value, done} = await reader.read();
    if (value) buf += dec.decode(value, {stream: true});
    let nl;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (!line) continue;
      const msg = JSON.parse(line);
      if (msg.progress) onProgress(msg.progress);
      else if (msg.result) return msg.result;
      else if (msg.error) throw new Error(msg.error);
    }
    if (done) throw new Error(buf.trim() ? JSON.parse(buf).error || "connection closed" : "connection closed");
  }
}

// "Calculating strategies… 12/17 · ~3s remaining"; after a simulation timeout the bar stops and turns amber.
// Between server events (one step can take a few seconds) the time line keeps counting locally.
const progressClock = {last: null, at: 0, timer: null};
function progressTime(p) {
  if (p.phase === "done") return tt("progress.elapsed", {s: p.elapsed.toFixed(1)});
  const since = (performance.now() - progressClock.at) / 1000;
  const elapsed = p.elapsed + since;
  if (elapsed > 1 && p.eta != null) return tt("progress.eta", {s: Math.max(1, Math.round(p.eta - since))});
  return elapsed > 1 ? tt("progress.elapsed", {s: elapsed.toFixed(0)}) : "";
}
function stopProgressClock() { clearInterval(progressClock.timer); progressClock.timer = null; }
function renderProgress(p) {
  const box = $("#result .progress");
  if (!box) return stopProgressClock();
  progressClock.last = p;
  progressClock.at = performance.now();
  if (!progressClock.timer && p.phase !== "done") {
    progressClock.timer = setInterval(() => {
      const b = $("#result .progress");
      if (!b || !progressClock.last) return stopProgressClock();
      b.querySelector(".ptime").innerHTML = progressTime(progressClock.last);
    }, 250);
  }
  if (p.phase === "done") stopProgressClock();
  if (p.phase !== "done" && !p.total) return;                // nothing counted yet: keep "Starting…"
  const timedOut = p.timeouts > 0;
  const label = p.phase === "done" ? tt("progress.done")
    : tt(`progress.${p.phase}`, {done: p.done, total: Math.max(p.total, p.done)});
  const time = progressTime(p);
  const pct = p.phase === "done" ? 100 : p.total ? Math.min(100, 100 * p.done / p.total) : 0;
  box.querySelector(".plabel").innerHTML = label;
  box.querySelector(".ptime").innerHTML = time;
  const bar = box.querySelector(".pbar > span");
  if (!timedOut || !bar.dataset.stopped) bar.style.width = pct.toFixed(1) + "%";
  if (timedOut) {
    bar.dataset.stopped = "1";
    box.classList.add("timed-out");
    box.querySelector(".pwarn").innerHTML = tt("progress.timeout");
  }
}

// ------------------------------------------------------------------- form setup (import, templates)
// targets: [{key, min, fractured?}] - family keys as the server lists them. Returns the labels that do not exist on
// this base (skipped).
async function applySetup(baseId, ilvl, influences, targets) {
  state.base = baseId;
  state.ilvl = ilvl;
  $("#ilvl").value = ilvl;
  state.influences = influences || [];
  document.querySelectorAll("[data-inf]").forEach((b) => b.classList.toggle("on", state.influences.includes(b.dataset.inf)));
  state.targets = [];
  await loadFamilies();
  const byKey = new Map(state.families.map((f) => [f.key || f.signature, f]));
  const skipped = [];
  for (const t of targets || []) {
    const f = byKey.get(t.key);
    if (!f) { skipped.push(t.label || t.key); continue; }
    state.targets.push({key: t.key, signature: f.signature, label: f.label, affix: f.affix, min: t.min ?? "", fam: f,
                        eldritch: f.eldritch || null, influence: f.influence_target || null, veiled: !!f.veiled,
                        elevated: f.elevated || null, special_fossil: f.special_fossil || null, beastcraft: !!f.beastcraft,
                        fractured: !!t.fractured});
  }
  renderTargets();
  renderFamilies();
  $("#planBtn").disabled = !state.targets.length;
  return skipped;
}

// ------------------------------------------------------------------- templates (this browser only)
const TPL_KEY = "poecraft.templates";
function readTemplates() {
  try { return JSON.parse(localStorage.getItem(TPL_KEY) || "{}") || {}; } catch (e) { return {}; }
}
function writeTemplates(all) {
  try { localStorage.setItem(TPL_KEY, JSON.stringify(all)); return true; } catch (e) { return false; }
}
function renderTemplates() {
  const all = readTemplates();
  $("#tplSelect").innerHTML = `<option value="">${esc(tx("tpl.pick"))}</option>` +
    Object.keys(all).sort().map((n) => `<option value="${esc(n)}">${esc(n)} · ${esc(all[n].base_name || "")}</option>`).join("");
}
function saveTemplate() {
  const name = $("#tplName").value.trim();
  if (!name || !state.base || !state.targets.length) { $("#tplResult").innerHTML = `<div class="warn">${tt("tpl.needName")}</div>`; return; }
  const all = readTemplates();
  all[name] = {base: state.base, base_name: (document.querySelector("#baseSelected b") || {textContent: ""}).textContent.replace(/EN/g, "").trim(), ilvl: state.ilvl,
               influences: state.influences, objective: $("#objective").value,
               targets: state.targets.map((t) => ({key: t.key, label: t.label, min: t.min, fractured: !!t.fractured}))};
  writeTemplates(all);
  renderTemplates();
  $("#tplSelect").value = name;
  $("#tplResult").innerHTML = `<div class="meta">${tt("tpl.saved", {name: esc(name)})}</div>`;
}
async function loadTemplate() {
  const name = $("#tplSelect").value;
  const t = readTemplates()[name];
  if (!t) return;
  state.have = [];
  renderHave();
  if (t.objective) $("#objective").value = t.objective;
  const skipped = await applySetup(t.base, t.ilvl, t.influences, t.targets);
  $("#tplResult").innerHTML = `<div class="meta">${tt("tpl.loaded", {name: esc(name)})}</div>` +
    (skipped.length ? `<div class="warn">${tt("import.skipped", {list: skipped.map(esc).join(", ")})}</div>` : "");
}
function deleteTemplate() {
  const name = $("#tplSelect").value;
  if (!name) return;
  const all = readTemplates();
  delete all[name];
  writeTemplates(all);
  renderTemplates();
}

// ------------------------------------------------------------------- Find on Trade (official trade site)
async function loadTradeLeagues() {
  try {
    const r = await api("/api/trade/leagues");
    $("#tradeLeague").innerHTML = `<option value="">${esc(tx("trade.auto", {league: r.league}))}</option>` +
      r.known.filter((l) => l !== r.league).map((l) => `<option value="${esc(l)}">${esc(l)}</option>`).join("");
  } catch (e) { /* the button still works: the server picks the league */ }
}
async function findOnTrade() {
  const box = $("#tradeResult");
  if (!state.base || !state.targets.length) { box.innerHTML = `<div class="warn">${tt("trade.needTargets")}</div>`; return; }
  // open the tab now, inside the click (a tab opened after the request would be blocked as a pop-up)
  const tab = window.open("", "_blank");
  box.innerHTML = `<div class="meta">${tt("trade.busy")}</div>`;
  try {
    const r = await api("/api/trade", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({
      lang: L.code, base: state.base, ilvl: state.ilvl, influences: state.influences, league: $("#tradeLeague").value || null,
      targets: state.targets.map((t) => ({signature: t.signature, key: t.key, label: t.label, min: t.min === "" ? null : +t.min,
                                          ...(t.eldritch ? {eldritch: t.eldritch} : {}), ...(t.fractured ? {fractured: true} : {})}))})});
    if (tab) { tab.opener = null; tab.location = r.url; }
    box.innerHTML = `<div class="meta">${tt("trade.opened", {league: esc(r.league), n: r.filters.length})}
      <a href="${esc(r.url)}" target="_blank" rel="noopener">${tt("trade.link")}</a></div>` +
      r.warnings.map((w) => `<div class="warn">${en(w)}</div>`).join("");
  } catch (e) {
    if (tab) tab.close();
    box.innerHTML = `<div class="error">${en(e.message)}</div>`;
  }
}

// ------------------------------------------------------------------- item import (paste / screenshot)
async function importItem(mode, fromOcr) {
  const box = $("#importResult");
  let r;
  try {
    if (fromOcr) {
      r = fromOcr;
    } else {
      r = await api("/api/import", {method: "POST", headers: {"Content-Type": "application/json"},
                                    body: JSON.stringify({text: $("#pasteBox").value, lang: L.code})});
    }
  } catch (e) { box.innerHTML = `<div class="error">${en(e.message)}</div>`; return; }
  const warn = (r.warnings || []).map((w) => `<div class="warn">${en(w)}</div>`).join("");
  if (!r.base_id) { box.innerHTML = warn; return; }
  let skipped = [];
  if (mode === "targets") {
    state.have = [];
    skipped = await applySetup(r.base_id, r.ilvl, r.influences, r.targets);
  } else {
    const keep = state.targets.map((t) => ({key: t.key, label: t.label, min: t.min, fractured: t.fractured}));
    skipped = await applySetup(r.base_id, r.ilvl, r.influences, keep);
    state.have = r.have.map((h) => ({id: h.id, text: h.text, affix: h.affix}));
  }
  renderHave();
  box.innerHTML = `<div class="meta">${tt("import.done", {base: esc(r.base_name_local || r.base_name), ilvl: r.ilvl,
    n: mode === "targets" ? state.targets.length : 0, have: mode === "have" ? state.have.length : 0})}</div>` + warn +
    (skipped.length ? `<div class="warn">${tt("import.skipped", {list: skipped.map(esc).join(", ")})}</div>` : "");
}

function renderHave() {
  const el = $("#haveList");
  if (!el) return;
  el.innerHTML = state.have.length ? `<div class="have"><b>${tt("import.haveTitle")}</b>
    <ul>${state.have.map((h) => `<li><span class="affix ${esc(h.affix)}">${affixText(h.affix)}</span>${esc(h.text)}</li>`).join("")}</ul>
    <button id="clearHave">${tt("import.clearHave")}</button> <span class="hint">${tt("import.haveHint")}</span></div>` : "";
}

async function uploadScreenshot(file) {
  const box = $("#importResult");
  if (!file) return;
  box.innerHTML = `<div class="meta">${tt("import.ocrBusy")}</div>`;
  const dataUrl = await new Promise((res, rej) => { const fr = new FileReader(); fr.onload = () => res(fr.result); fr.onerror = rej; fr.readAsDataURL(file); });
  try {
    const r = await api("/api/ocr", {method: "POST", headers: {"Content-Type": "application/json"},
                                     body: JSON.stringify({image: dataUrl, lang: L.code})});
    if (r.ocr_text) $("#pasteBox").value = r.ocr_text;       // so the recognised text can be checked and corrected
    await importItem("targets", r);
  } catch (e) { box.innerHTML = `<div class="warn">${en(e.message)}</div>`; }
}

function renderOcrNote() {
  const o = state.meta && state.meta.ocr;
  $("#ocrNote").innerHTML = o && o.available ? tt("import.ocrOn", {service: esc(o.service)}) : tt("import.ocrOff");
}

function renderRecovery(r) {
  const opts = (r.options || []).map((o) => `<tr><td>${esc(o.key)}</td><td>${en(o.label)}</td><td>${fmtC(o.expected_total)}</td>
    <td>${fmtP(o.p_progress)}</td><td>${fmtP(o.p_finish_without_restart)}</td></tr>`).join("");
  const od = r.optional_divine;
  $("#result").innerHTML = `<div class="card">
    <div class="meta"><b>${g(r.item, "base")}</b> (${g(r.item, "item_class")}) · ${tt("import.haveTitle")}
      ${r.have.map((m) => `<span class="badge">${g(m, "text")}</span>`).join(" ")}</div>
    ${r.warnings.map((w) => `<div class="warn">${en(w)}</div>`).join("")}
    <div class="meta">${en(r.state)}</div>
    <h2>${tt("rec.title")}</h2>
    <div class="summary"><div class="stat"><div class="label">${tt("rec.fromHere")}</div><div class="big">${fmtC(r.expected_cost_from_here)}</div></div>
      <div class="stat"><div class="label">${tt("rec.fresh")}</div><div class="value">${fmtC(r.fresh_plan.expected_cost)}</div>
        <div class="meta">${en(r.fresh_plan.name)}</div></div>
      ${od ? `<div class="stat optional"><div class="label">${tt("cost.divinesOptional")}</div><div class="value">+${fmtC(od.cost)}</div></div>` : ""}</div>
    ${opts ? `<div class="table-wrap"><table><thead><tr><th>${tt("rec.col.option")}</th><th></th><th>${tt("rec.col.total")}</th>
      <th>${tt("rec.col.progress")}</th><th>${tt("rec.col.noRestart")}</th></tr></thead><tbody>${opts}</tbody></table></div>` : ""}
    <ol class="steps" style="margin-top:12px">${r.steps.map((st) => `<li>${stepBody(st)}</li>`).join("")}</ol>
  </div>`;
}

async function buildPlan() {
  $("#result").innerHTML = `<div class="card progress"><div class="prow"><span class="plabel">${tt("progress.connecting")}</span>
    <span class="ptime muted"></span></div><div class="pbar"><span></span></div><div class="pwarn warn-text"></div></div>`;
  $("#planBtn").disabled = true;
  try {
    const body = {
      lang: L.code,
      base: state.base, ilvl: state.ilvl, influences: state.influences, sims: +$("#sims").value,
      objective: $("#objective").value,
      targets: state.targets.map((t) => ({signature: t.signature, min: t.min === "" ? null : +t.min, label: t.label,
                                         ...(t.eldritch ? {eldritch: t.eldritch} : {}), ...(t.influence ? {influence: t.influence} : {}),
                                         ...(t.fractured ? {fractured: true} : {}), ...(t.veiled ? {veiled: true} : {}),
                                         ...(t.elevated ? {elevated: t.elevated} : {}),
                                         ...(t.special_fossil ? {fossil: t.special_fossil} : {}), ...(t.beastcraft ? {beastcraft: true} : {})})),
    };
    if (state.have.length) body.have = state.have.map((h) => h.id);       // imported "have" mods: recovery mode
    state.result = await streamPlan(state.have.length ? "/api/recover" : "/api/plan", body, renderProgress);
    state.shown = 0;
    renderResult();
    if (state.result.prices) renderPrices(state.result.prices);
  } catch (e) {
    stopProgressClock();
    // multi-line messages (e.g. "No supported strategy reaches these targets ...") keep their line breaks
    state.result = null;
    $("#result").innerHTML = `<div class="card"><div class="error" style="white-space:pre-line">${en(e.message)}</div></div>`;
  } finally {
    $("#planBtn").disabled = !state.targets.length;
  }
}

async function localizeResult() {
  // the computed plan is kept language-free on the server: re-render it in the new language (no re-planning)
  if (!state.result || state.result.language === L.code) return;
  try {
    state.result = await api("/api/localize", {method: "POST", headers: {"Content-Type": "application/json"},
                                               body: JSON.stringify({lang: L.code, result_id: state.result.result_id})});
  } catch (e) {
    state.result = null;
    $("#result").innerHTML = `<div class="card"><div class="warn">${tt("plan.expired")} ${en(e.message)}</div></div>`;
  }
}

function simLine(labelKey, s) {
  if (!s) return "";
  const flag = s.reliable ? (s.disagrees ? ` ${statusBadge("approximate")} ${tt("sim.disagrees")}` : "") : ` <span class="muted">${tt("sim.fewHits")}</span>`;
  const low = s.low_confidence ? ` <span class="badge approximate">${tt("sim.lowConf")}</span> ${en(s.low_confidence)}` : "";
  return `<div class="meta">${tt("sim.line", {label: tt(labelKey), a: fmtP(s.analytic), s: fmtP(s.p), hits: s.hits, n: s.n,
    lo: (s.ci[0] * 100).toPrecision(3), hi: (s.ci[1] * 100).toPrecision(3)})}${flag}${low}</div>`;
}

function stepBody(st) {
  const fails = st.on_fail.map((f) => `<div class="branch"><span class="k">${tt("step.onFail")}</span>${en(f.text)} ${f.status !== "computed" ? statusBadge(f.status) : ""}</div>`).join("");
  const meta = [st.probability_text ? tt("step.chance", {p: en(st.probability_text)}) : "",
    st.attempts ? tt("step.tries", {n: st.attempts.toLocaleString(undefined, {maximumFractionDigits: 1})}) : "",
    st.cost ? tt("step.expected", {c: fmtC(st.cost)}) : ""].filter(Boolean).join(" · ");
  return `<h3>${st.kind === "recovery" ? `<span class="badge conditional">${tt("step.conditional")}</span> ` : ""}${en(st.title)} ${st.status !== "computed" ? statusBadge(st.status) : ""}</h3>
    <div class="action">${en(st.action)}</div>
    ${st.detail ? `<div class="meta">${en(st.detail)}</div>` : ""}
    ${meta ? `<div class="meta">${meta}</div>` : ""}
    <div class="state"><b>${tt("step.state")}</b> ${en(st.expected_state)}</div>
    ${st.on_success && st.on_fail.length ? `<div class="meta">${tt("step.onSuccess")} ${en(st.on_success)}</div>` : ""}
    ${fails}`;
}

function renderResult() {
  const r = state.result;
  if (r.have) return renderRecovery(r);                     // "Import as have": a recovery plan for that item
  const s = r.strategies[state.shown];
  const objLabel = tt(`objective.${r.objective.key}`);
  const warnings = r.warnings.map((w) => `<div class="warn">${en(w)}</div>`).join("");
  const sims = !s.sim ? `<div class="meta">${tt("sim.off")}</div>` : ("n" in s.sim ? simLine("sim.perAttempt", s.sim)
    : simLine("sim.alt", s.sim.alt) + simLine("sim.chain", s.sim.chain));
  // main plan: numbered steps; recovery branches (conditional, optional) sit collapsed under the step they belong to
  const branchesOf = (n) => s.steps.filter((x) => x.kind === "recovery" && x.branch_of === n);
  const steps = s.steps.filter((st) => st.kind !== "recovery").map((st) => {
    const br = st.number != null ? branchesOf(st.number) : [];
    return `<li${st.optional ? ' class="optional"' : ""}>${stepBody(st)}
      ${br.length ? `<details class="recovery"><summary>${tt("step.recoveryToggle", {n: br.length})}</summary>
        <ul class="branches">${br.map((b) => `<li>${stepBody(b)}</li>`).join("")}</ul></details>` : ""}
    </li>`;
  }).join("");
  const expanded = Object.fromEntries(r.strategies.map((x, i) => [x.name, i]));
  const rows = r.comparison.map((c) => {
    const i = expanded[c.name];
    const sc = c.simulated_cost;
    const range = sc && sc.lo != null && sc.hi != null ? `${fmtC(sc.lo)}–${fmtC(sc.hi)}` : "—";
    const flags = c.flags.map((f) => `<span class="badge approximate" title="${esc(f)}">${en(f.split(":")[0])}</span>`).join(" ");
    return `
    <tr class="${i !== undefined ? "pick" : ""} ${i === state.shown ? "active" : ""}" ${i !== undefined ? `data-s="${i}"` : ""}>
      <td>${c.why === "RECOMMENDED" ? "★ " : ""}${en(c.name)} ${flags}</td>
      ${c.cost_range ? `<td class="num" colspan="3" title="${esc(c.cost_range.low_reason + "; " + c.cost_range.high_reason)}">${en(c.cost_range.text)}</td>` : `
      <td class="num">${fmtC(c.cost)}</td>
      <td class="num">${sc ? fmtC(sc.mean) : "—"}</td>
      <td class="num">${range}</td>`}
      <td class="muted">${c.why === "RECOMMENDED" ? tt("table.recommended") : en(c.why)}</td>
    </tr>`;
  }).join("");
  const cb = s.cost_breakdown;
  const isRec = s.name === r.recommended;
  const stat = (key, v) => `<div class="stat"><div class="label">${tt(key)}</div><div class="value">${v}</div></div>`;
  $("#result").innerHTML = `
    <div class="card">
      ${(r.untranslated_keys || []).length ? `<div class="prose-marker" title="${esc(r.untranslated_keys.join(", "))}"><span class="badge en-badge">EN</span> ${tt("plan.untranslated", {n: r.untranslated_keys.length})}</div>` : ""}
      <div class="meta"><b>${g(r.item, "base")}</b> (${g(r.item, "item_class")}) · ${r.targets.map((t) => g(t, "description")).join(" · ")}</div>
      ${warnings}
      <div class="meta">${tt("plan.objective")} <b>${objLabel}</b> — ${tt(`objective.${r.objective.key}.desc`)}</div>
      <h2>${isRec ? tt(r.schematic ? "plan.schematic" : "plan.recommended") : tt("plan.other")}: ${en(s.name)} ${statusBadge(s.status)}</h2>
      ${isRec ? `<div class="meta">${en(r.selection_note)}</div>` : ""}
      <div class="summary">
        ${s.cost_range ? `<div class="stat" style="grid-column:1/-1"><div class="label">${tt("plan.costRange")}</div><div class="value">${en(s.cost_range.text)}</div>
          <div class="meta">${tt("plan.lower")} ${en(s.cost_range.low_reason)}</div>
          ${s.cost_range.assumed_reason ? `<div class="meta">${tt("plan.middle")} ${en(s.cost_range.assumed_reason)}</div>` : ""}
          <div class="meta">${tt("plan.upper")} ${en(s.cost_range.high_reason)}</div></div>`
          : `<div class="stat"><div class="label">${tt("plan.total")}</div><div class="big">${fmtC(s.expected_cost)}</div></div>`}
        ${stat("cost.rolling", fmtC(cb.rng))}
        ${stat("cost.bench", fmtC(cb.bench))}
        ${s.optional_divine ? `<div class="stat optional"><div class="label">${tt("cost.divinesOptional")}</div><div class="value">+${fmtC(s.optional_divine.cost)}</div>
          <div class="meta">${tt("plan.optionalDivine", {p: (s.optional_divine.p_values_met * 100).toFixed(1), total: fmtC(s.optional_divine.total_with)})}</div></div>` : ""}
        ${cb.eldritch ? stat("cost.eldritch", fmtC(cb.eldritch)) : ""}
        ${cb.influence ? stat("cost.influence", fmtC(cb.influence)) : ""}
        ${cb.fracture ? stat("cost.fracture", fmtC(cb.fracture)) : ""}
        ${cb.veiled ? stat("cost.veiled", fmtC(cb.veiled)) : ""}
        ${cb.dominance ? stat("cost.dominance", fmtC(cb.dominance)) : ""}
        ${stat("plan.badLuck", s.cost_range ? tt("plan.noUpper") : fmtC(s.metrics.bad_luck_cost))}
        ${stat("plan.apps", Math.round(s.metrics.applications).toLocaleString())}
      </div>
      ${r.cost_note ? `<div class="meta">${en(r.cost_note)}</div>` : ""}
      ${sims}
      ${s.notes.map((n) => `<div class="meta">${tt("plan.note")} ${en(n)}</div>`).join("")}
      <ol class="steps" style="margin-top:12px">${steps}</ol>
    </div>
    <div class="card">
      <h2>${tt("table.title")}</h2>
      <div class="table-wrap"><table>
        <thead><tr><th>${tt("table.col.strategy")}</th><th class="num">${tt("table.col.fast")}</th><th class="num">${tt("table.col.sim")}</th><th class="num">${tt("table.col.range")}</th><th>${tt("table.col.why")}</th></tr></thead>
        <tbody>${rows}</tbody></table></div>
      ${(r.not_supported || []).map((n) => `<div class="meta"><span class="badge not-supported">${tt("plan.notSupported")}</span> ${en(n.name)} — ${en(n.reason)}</div>`).join("")}
      <p class="hint">${tt("table.footer", {n: r.considered, s: r.elapsed_s, objective: objLabel})}</p>
    </div>`;
}

// ----------------------------------------------------------------- events
$("#langSelect").addEventListener("click", (e) => { const b = e.target.closest("[data-lang]"); if (b) setLang(b.dataset.lang); });
$("#baseSearch").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(searchBases, 150); });
$("#classFilter").addEventListener("change", searchBases);
$("#baseResults").addEventListener("click", (e) => { const li = e.target.closest("li[data-id]"); if (li) selectBase(li.dataset.id); });
$("#ilvl").addEventListener("change", (e) => { state.ilvl = Math.max(1, Math.min(100, +e.target.value || 86)); loadFamilies(); });
$("#influences").addEventListener("click", (e) => {
  const b = e.target.closest("[data-inf]"); if (!b) return;
  const i = b.dataset.inf;
  if (state.influences.includes(i)) state.influences = state.influences.filter((x) => x !== i);
  else if (state.influences.length < state.maxInfluences) state.influences.push(i);
  document.querySelectorAll("[data-inf]").forEach((x) => x.classList.toggle("on", state.influences.includes(x.dataset.inf)));
  loadFamilies();
});
$("#modFilter").addEventListener("input", onModFilter);
$("#importTargets").addEventListener("click", () => importItem("targets"));
$("#tplSave").addEventListener("click", saveTemplate);
$("#tplLoad").addEventListener("click", loadTemplate);
$("#tplDelete").addEventListener("click", deleteTemplate);
$("#tradeBtn").addEventListener("click", findOnTrade);
$("#importHave").addEventListener("click", () => importItem("have"));
$("#ocrFile").addEventListener("change", (e) => { uploadScreenshot(e.target.files[0]); e.target.value = ""; });
document.addEventListener("click", (e) => { if (e.target.id === "clearHave") { state.have = []; renderHave(); } });
$("#rollOnly").addEventListener("change", renderFamilies);
$("#affixTabs").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  state.affix = b.dataset.affix;
  document.querySelectorAll("#affixTabs button").forEach((x) => x.classList.toggle("active", x === b));
  renderFamilies();
});
$("#families").addEventListener("click", (e) => { const b = e.target.closest("button[data-sig]"); if (b) addTarget(b.dataset.sig); });
$("#targets").addEventListener("input", (e) => { if (e.target.dataset.i) state.targets[+e.target.dataset.i].min = e.target.value; });
$("#targets").addEventListener("change", (e) => { if (e.target.dataset.fr) state.targets[+e.target.dataset.fr].fractured = e.target.checked; });
$("#targets").addEventListener("click", (e) => {
  const b = e.target.closest("[data-rm]"); if (!b) return;
  state.targets.splice(+b.dataset.rm, 1); renderTargets(); renderFamilies();
});
$("#planBtn").addEventListener("click", buildPlan);
$("#priceBtn").addEventListener("click", updatePrices);
$("#result").addEventListener("click", (e) => { const tr = e.target.closest("tr[data-s]"); if (tr) { state.shown = +tr.dataset.s; renderResult(); } });

init().catch((e) => { document.body.insertAdjacentHTML("afterbegin", `<div class="error">${esc(e.message)}</div>`); });
