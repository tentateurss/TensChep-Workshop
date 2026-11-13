"use strict";
// TensChep Workshop - landing page. The brand is a fixed English name (never translated); everything else comes from
// poecraft/locales/<code>.json like the planner, and the chosen language is shared with it (localStorage).

const BRAND = "TensChep Workshop";

// One entry per service. A new tool = one more object here (plus its hub.tool.<id>.* texts in the locale files).
const TOOLS = [
  {id: "craft", href: "/craft", icon: "craft"},
  {id: "evaluator", href: "/evaluator", icon: "evaluator"},
];

// inline SVG icons (the CSP allows no external images); unknown ids fall back to a generic tool icon
const ICONS = {
  craft: '<svg viewBox="0 0 48 48" aria-hidden="true"><path d="M8 40 28 20" stroke-width="4"/><path d="M24 8l16 16-6 6-16-16z" stroke-width="3"/><circle cx="36" cy="38" r="5" stroke-width="3"/></svg>',
  evaluator: '<svg viewBox="0 0 48 48" aria-hidden="true"><path d="M24 6v36M10 14h28" stroke-width="3"/><path d="M10 14l-6 14h12zM38 14l-6 14h12z" stroke-width="3"/><path d="M16 42h16" stroke-width="3"/></svg>',
  tool: '<svg viewBox="0 0 48 48" aria-hidden="true"><rect x="10" y="10" width="28" height="28" rx="6" stroke-width="3"/></svg>',
};

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const L = {code: "en", strings: {}, en: {}, langs: []};

function tt(key) {           // translated text; English fallback carries the EN mark, like the planner
  if (L.code !== "en" && typeof L.strings[key] === "string") return esc(L.strings[key]);
  const en = typeof L.en[key] === "string" ? L.en[key] : key;
  return esc(en) + (L.code !== "en" ? ' <sup class="en-mark">EN</sup>' : "");
}

function renderTools() {
  $("#tools").innerHTML = TOOLS.map((t) => `
    <li class="tool-card card">
      <div class="tool-icon">${ICONS[t.icon] || ICONS.tool}</div>
      <h2>${tt(`hub.tool.${t.id}.title`)}</h2>
      <p class="tool-desc">${tt(`hub.tool.${t.id}.desc`)}</p>
      <a class="launch" href="${esc(t.href)}">${tt("hub.launch")}</a>
    </li>`).join("");
}

function render() {
  document.documentElement.lang = L.code;
  document.title = BRAND;
  document.querySelectorAll("[data-i18n]").forEach((el) => { el.innerHTML = tt(el.dataset.i18n); });
  $("#langSelect").innerHTML = L.langs.map((l) =>
    `<button class="lang ${l.code === L.code ? "on" : ""}" data-lang="${esc(l.code)}" title="${esc(l.name)}">` +
    `<span class="flag">${esc(l.flag)}</span> ${esc(l.code.toUpperCase())}</button>`).join("");
  renderTools();
}

async function setLang(code) {
  const loc = await (await fetch(`/api/locale?lang=${encodeURIComponent(code)}`)).json();
  Object.assign(L, {code: loc.code, strings: loc.strings, en: loc.en});
  try { localStorage.setItem("poecraft.lang", loc.code); } catch (e) { /* private mode: not persisted */ }
  render();
}

async function init() {
  try { L.langs = await (await fetch("/api/locales")).json(); } catch (e) { L.langs = [{code: "en", name: "English", flag: "EN"}]; }
  let want = "en";
  try { want = localStorage.getItem("poecraft.lang") || (navigator.language || "en").slice(0, 2); } catch (e) { /* default */ }
  await setLang(L.langs.some((l) => l.code === want) ? want : "en");
}

$("#langSelect").addEventListener("click", (e) => { const b = e.target.closest("[data-lang]"); if (b) setLang(b.dataset.lang); });
init().catch(() => render());       // the page still shows the brand and the English cards if the API is down
