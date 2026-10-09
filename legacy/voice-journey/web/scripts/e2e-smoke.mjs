// One-shot end-to-end gate: mount the REAL built bundle (web/dist/app.js) in happy-dom
// against the REAL running corpus browser on 127.0.0.1:8787, and verify the SPA
// actually boots: first rows page lands, table renders, view switching works.
// Run: npm run e2e --prefix web   (requires the launchd service up + web/dist built)
import { Window } from "happy-dom";
import { pathToFileURL } from "node:url";
import path from "node:path";

const BASE = "http://127.0.0.1:8787";
const window = new Window({ url: `${BASE}/` });
const { document } = window;

for (const key of ["document", "window", "navigator", "location", "history", "CustomEvent", "Event", "HTMLElement", "Element", "Node", "IntersectionObserver", "ResizeObserver", "requestAnimationFrame", "cancelAnimationFrame", "getComputedStyle", "MutationObserver", "Audio", "HTMLIFrameElement", "DocumentFragment", "SVGElement"]) {
  if (window[key] === undefined) continue;
  try {
    Object.defineProperty(globalThis, key, { value: window[key], configurable: true, writable: true });
  } catch {
    // non-configurable global (e.g. navigator on some Node versions) — leave it
  }
}
globalThis.IntersectionObserver ??= class { observe() {} disconnect() {} unobserve() {} };
globalThis.Audio ??= class { pause() {} play() { return Promise.resolve(); } addEventListener() {} };
// happy-dom's fetch resolves relative URLs against the window URL; route globals to it.
globalThis.fetch = window.fetch.bind(window);
globalThis.addEventListener = window.addEventListener.bind(window);
globalThis.removeEventListener = window.removeEventListener.bind(window);

document.body.innerHTML = '<div id="app"></div>';

const failures = [];
function check(label, ok, detail = "") {
  console.log(`${ok ? "PASS" : "FAIL"} ${label}${detail ? ` — ${detail}` : ""}`);
  if (!ok) failures.push(label);
}

const bundlePath = path.join(path.dirname(new URL(import.meta.url).pathname), "..", "dist", "app.js");
await import(pathToFileURL(bundlePath).href);

const settle = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
await settle(2500);

check("app shell mounted", Boolean(document.querySelector(".vj-app")));
check("nav rendered", document.querySelectorAll(".vj-nav button, .vj-nav a, .vj-nav span").length >= 4);
const rowCount = document.querySelectorAll(".mc-data-table tbody tr").length;
check("first rows page rendered (100 lean rows)", rowCount === 100, `saw ${rowCount}`);
check("infinite scroll sentinel present", Boolean(document.querySelector('[data-mc-component="InfiniteScrollSentinel"]')));
const statusLine = document.querySelector(".vj-status-line")?.textContent ?? "";
check("status line shows totals", /Showing 100 of 2,433 takes/.test(statusLine), statusLine);
check("filters populated from summary", document.querySelectorAll(".vj-filters select option").length > 10);

// Switch to Journey via the nav button (real store dispatch path).
const journeyButton = [...document.querySelectorAll(".vj-nav button")].find((b) => b.textContent === "Journey");
journeyButton.click();
await settle(2500);
check("journey view swapped in", Boolean(document.querySelector(".vj-stats")));
const tabCount = document.querySelectorAll(".vj-subtabs button").length;
check("journey subject tabs rendered", tabCount >= 5, `${tabCount} tabs`);
let tabSvgTotal = 0;
let eduCount = 0;
let sectionedCount = 0;
for (let i = 0; i < tabCount; i += 1) {
  const button = [...document.querySelectorAll(".vj-subtabs button")][i];
  const label = button.textContent;
  button.click();
  await settle(250);
  const activeLabel = document.querySelector('.vj-subtabs button[data-active="true"]')?.textContent;
  check(`tab "${label}" activates`, activeLabel === label, `active=${activeLabel}`);
  tabSvgTotal += document.querySelectorAll(".vj-card svg").length;
  if (document.querySelector(".vj-edu")) eduCount += 1;
  if (document.querySelectorAll(".vj-card h2").length >= 2) sectionedCount += 1;
}
check("education card under every tab", eduCount === tabCount, `${eduCount}/${tabCount}`);
check("every tab renders its section + education pair", sectionedCount === tabCount, `${sectionedCount}/${tabCount}`);
check("journey charts drawn across tabs", tabSvgTotal >= 6, `${tabSvgTotal} svgs total`);

// Switch to Referee.
const refereeButton = [...document.querySelectorAll(".vj-nav button")].find((b) => b.textContent === "Referee");
refereeButton.click();
await settle(1500);
const refereeText = document.body.textContent;
check("referee view active", /The Referee|This pair|release-gated playback/.test(refereeText));
check("referee trial or gate present", Boolean(document.querySelector(".vj-pair") || document.querySelector(".vj-notice")));

// Switch to Practice (the coach).
const practiceButton = [...document.querySelectorAll(".vj-nav button")].find((b) => b.textContent === "Practice");
practiceButton.click();
await settle(1500);
const practiceText = document.body.textContent;
check("practice view renders coach sections", /Due for a take/.test(practiceText) && /The frontier/.test(practiceText));
check("practice goals card renders", /pre-registered experiments/.test(practiceText));
check("practice cards present", document.querySelectorAll(".vj-card").length >= 3);

console.log(failures.length ? `\n${failures.length} FAILURES` : "\nALL CHECKS PASSED");
process.exit(failures.length ? 1 : 0);
