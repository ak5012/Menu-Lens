// The side panel: reads the active tab's menu (via content.js), asks the MenuLens API
// for estimates, and shows the results as they arrive.
//
// Free model quotas count requests, not dishes, so the whole menu is sent in groups of
// up to 20 dishes (one request each), in page order so the top of the panel fills first.
// A rate limit pauses the queue and resumes by itself instead of failing.

const API_BASE = "http://localhost:8000"; // local server for now; the deployed URL comes later
const GROUP_SIZE = 20; // the server's per-request maximum
const CONCURRENCY = 2; // groups in flight at once
const SPICY = /\b(spicy|chil[ei]|chilli|jalape[nñ]o|habanero|sriracha|cayenne|harissa|gochujang|s[zi]chuan|arrabbiata|diavola|buffalo|nashville hot|vindaloo|peri[- ]peri|hot sauce)\b/i;
const LIGHT_MAX_KCAL = 450;

const FLAME = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" role="img" aria-label="Spicy"><path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.07-2.14-.22-4.05 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.15.43-2.29 1-3a2.5 2.5 0 0 0 2.5 2.5z"/></svg>';
const LEAF = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" role="img" aria-label="Lighter dish"><path d="M11 20A7 7 0 0 1 9.8 6.1C15.5 5 17 4.48 19 2c1 2 2 4.18 2 8 0 5.5-4.78 10-10 10Z"/><path d="M2 21c0-3 1.85-5.36 5.08-6C9.5 14.52 12 13 13 12"/></svg>';

const $ = (id) => document.getElementById(id);

const state = {
  tabId: null,
  url: "",
  restaurant: { name: "", cuisine: "", price_tier: null },
  items: new Map(), // key -> { item, status, result, error, isNew, el }
  initialScanDone: false,
  opened: new Set(),
  queue: [],
  running: 0,
  serverDown: false,
  dailyOut: false,  // today's free quota is gone on every model
  pausedUntil: 0,   // rate limited: nothing is sent before this time (ms)
};
let pauseTimer = null;
const cache = new Map(); // estimates survive tab switches while the panel stays open

const keyFor = (item) => `${state.restaurant.name}|${item.name}|${item.description}`.toLowerCase();

// ---------- reading the page ----------

async function scan(tabId) {
  let result;
  try {
    await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] });
    [{ result }] = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => window.__menulens.extract(),
    });
  } catch {
    return showNoAccess(); // not clicked on this page yet, or a browser page we can't read
  }
  if (tabId !== state.tabId) return; // the user moved on while we were reading
  accept(result);
}

function reset(tabId) {
  state.tabId = tabId;
  state.url = "";
  state.items.clear();
  state.opened.clear();
  state.queue = [];
  state.initialScanDone = false;
  $("list").replaceChildren();
}

function accept(result) {
  if (!result) return;
  if (result.url !== state.url) {
    state.items.clear();
    state.queue = [];
    $("list").replaceChildren();
    state.initialScanDone = false;
  }
  state.url = result.url;
  state.restaurant = result.restaurant;
  let added = 0;
  for (const item of result.items) {
    const key = keyFor(item);
    if (state.items.has(key)) continue;
    const entry = { item, key, status: "pending", isNew: state.initialScanDone };
    if (cache.has(key)) Object.assign(entry, cache.get(key));
    state.items.set(key, entry);
    if (entry.status === "pending") {
      entry.queued = true;
      state.queue.push(entry);
    }
    added++;
  }
  if (added && state.initialScanDone) addSectionLabel("Found further down the page");
  for (const entry of state.items.values()) if (!entry.el) renderCard(entry);
  state.initialScanDone = true;
  renderHeader();
  pump();
}

// ---------- talking to the API ----------

// Estimate a group of dishes in one request. Returns either a whole-request outcome
// ({ status: "wait" | "daily" | "error" }) or { status: "results", outcomes: [...] } with one
// outcome per dish, in order.
async function estimateGroup(entries) {
  const body = {
    items: entries.map(({ item }) => ({
      name: item.name.slice(0, 120),
      description: item.description.slice(0, 500),
      section: item.section.slice(0, 80),
    })),
    restaurant: {
      name: state.restaurant.name,
      cuisine: state.restaurant.cuisine,
      price_tier: state.restaurant.price_tier,
    },
  };
  let response;
  try {
    response = await fetch(`${API_BASE}/v1/estimate/batch`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch {
    setServerDown(true);
    return { status: "error", error: "Can't reach the MenuLens server." };
  }
  setServerDown(false);
  const data = await response.json().catch(() => ({}));
  if (response.ok) {
    return {
      status: "results",
      outcomes: data.results.map((r) =>
        r.status === "ok" ? { status: "done", result: r }
        : r.status === "insufficient_signal" ? { status: "suppressed", error: "Too uncertain to estimate from the menu text." }
        : { status: "error", error: r.message || "The estimator skipped this dish. Try again." }),
    };
  }
  const message = data?.detail?.message;
  if (response.status === 429 && data?.detail?.code === "daily_quota") {
    return { status: "daily", error: message || "Today's free estimate limit is used up." };
  }
  if (response.status === 429) return { status: "wait", retryAfter: data?.detail?.retry_after_s || 20 };
  return { status: "error", error: message || "The estimator is unavailable. Try again." };
}

function pump() {
  while (state.running < CONCURRENCY && state.queue.length && !state.dailyOut && Date.now() >= state.pausedUntil) {
    const group = state.queue.splice(0, GROUP_SIZE);
    const tabId = state.tabId;
    const url = state.url;
    state.running++;
    estimateGroup(group).then((outcome) => {
      state.running--;
      const current = tabId === state.tabId && url === state.url;
      if (outcome.status === "wait") {
        if (current) state.queue.unshift(...group); // keep their place, try again after the pause
        pause(outcome.retryAfter);
      } else if (outcome.status === "daily") {
        if (current) outOfQuota(outcome.error);
      } else {
        const outcomes = outcome.status === "results" ? outcome.outcomes : group.map(() => outcome);
        group.forEach((entry, i) => {
          const result = outcomes[i];
          if (result.status !== "error") cache.set(entry.key, result);
          entry.queued = false;
          if (current) {
            Object.assign(entry, result);
            renderCard(entry);
          }
        });
      }
      renderHeader();
      pump();
    });
  }
  renderHeader();
}

function pause(seconds) {
  state.pausedUntil = Math.max(state.pausedUntil, Date.now() + seconds * 1000);
  renderBanner();
  clearTimeout(pauseTimer);
  pauseTimer = setTimeout(() => {
    renderBanner();
    pump();
  }, state.pausedUntil - Date.now());
}

function outOfQuota(message) {
  state.dailyOut = true;
  state.queue = [];
  for (const e of state.items.values()) {
    if (e.status !== "pending") continue;
    Object.assign(e, { status: "error", error: message, queued: false });
    renderCard(e);
  }
  renderBanner();
}

function retry(entry) {
  state.dailyOut = false;
  renderBanner();
  entry.status = "pending";
  entry.queued = true;
  renderCard(entry);
  state.queue.unshift(entry);
  pump();
}

function setServerDown(down) {
  if (down === state.serverDown) return;
  state.serverDown = down;
  renderBanner();
}

function renderBanner() {
  const banner = $("banner");
  const paused = Date.now() < state.pausedUntil;
  banner.hidden = !(state.serverDown || state.dailyOut || paused);
  if (state.serverDown) {
    banner.innerHTML =
      "Can't reach the MenuLens server at <code></code>. Start it from <code>estimator</code> with " +
      "<code>python -m uvicorn server:app --port 8000</code>, then retry.";
    banner.querySelector("code").textContent = API_BASE;
  } else if (state.dailyOut) {
    banner.textContent = "Today's free estimate limit is used up. Dishes already estimated stay here; try the rest tomorrow.";
  } else if (paused) {
    const seconds = Math.ceil((state.pausedUntil - Date.now()) / 1000);
    banner.textContent = `Reached the free limit for the moment. Continuing by itself in about ${seconds}s.`;
  }
}

// ---------- rendering ----------

function renderHeader() {
  const { name, cuisine, price_tier } = state.restaurant;
  const count = state.items.size;
  $("restaurant").textContent = count ? name || "This restaurant" : "No restaurant yet";
  const tier = [price_tier ? "$".repeat(price_tier) : "", cuisine].filter(Boolean).join(" · ");
  $("tier").textContent = count ? tier || "Restaurant" : "Browsing";
  $("count-text").textContent = `${count} ${count === 1 ? "item" : "items"} detected`;
  $("count").classList.toggle("busy", state.running > 0 || state.queue.length > 0);
  $("empty").hidden = count > 0;
}

function addSectionLabel(text) {
  const label = document.createElement("p");
  label.className = "section-label";
  label.textContent = text;
  $("list").append(label);
}

const confidenceLabel = (band) => band.charAt(0).toUpperCase() + band.slice(1);

function renderCard(entry) {
  if (!entry.el) {
    entry.el = $("card-template").content.firstElementChild.cloneNode(true);
    entry.el.querySelector(".card-button").addEventListener("click", () => toggle(entry));
    $("list").append(entry.el);
  }
  const el = entry.el;
  const { item, status, result } = entry;
  const open = state.opened.has(entry.key) && status === "done";
  el.classList.toggle("open", open);
  el.querySelector(".card-button").setAttribute("aria-expanded", String(open));
  el.querySelector(".card-name").textContent = item.name;
  el.querySelector(".card-desc").textContent = item.description;
  el.querySelector(".card-flag").textContent = entry.isNew && !open ? "New" : "";

  const row = el.querySelector(".card-row");
  row.replaceChildren();
  if (status === "pending") {
    row.innerHTML = '<span class="skeleton" style="width:80px"></span><span class="skeleton" style="width:56px"></span>';
  } else if (status === "done") {
    const { low, high } = result.calories;
    const band = result.confidence.band;
    const range = document.createElement("span");
    range.className = "range";
    range.textContent = `${low}–${high} cal`;
    const badge = document.createElement("span");
    badge.className = `badge ${band}`;
    badge.textContent = confidenceLabel(band);
    row.append(range, badge);
    if (SPICY.test(`${item.name} ${item.description}`)) row.insertAdjacentHTML("beforeend", `<span class="tag" style="color:var(--primary)">${FLAME}</span>`);
    if (high <= LIGHT_MAX_KCAL) row.insertAdjacentHTML("beforeend", `<span class="tag" style="color:var(--confidence-high)">${LEAF}</span>`);
  } else {
    const note = document.createElement("span");
    note.className = `status${status === "error" ? " error" : ""}`;
    note.textContent = entry.error;
    row.append(note);
    if (status === "error") {
      const again = document.createElement("button");
      again.type = "button";
      again.className = "link-button";
      again.textContent = "Retry";
      again.addEventListener("click", (event) => {
        event.stopPropagation();
        retry(entry);
      });
      row.append(again);
    }
  }

  const detail = el.querySelector(".card-detail");
  detail.hidden = !open;
  if (open) {
    const { low, high } = result.calories;
    detail.querySelector(".detail-head").textContent =
      `${low}–${high} cal · ${confidenceLabel(result.confidence.band)} confidence`;
    detail.querySelector(".detail-body").textContent = result.rationale;
  }
}

function toggle(entry) {
  if (entry.status !== "done") return;
  if (state.opened.has(entry.key)) state.opened.delete(entry.key);
  else state.opened.add(entry.key);
  renderCard(entry);
}

function showNoAccess() {
  reset(state.tabId);
  state.restaurant = { name: "", cuisine: "", price_tier: null };
  renderHeader();
  $("empty-title").textContent = "Click the MenuLens icon to read this page";
  $("empty-body").textContent =
    "MenuLens only reads a page when you ask. Open a restaurant menu, then click the MenuLens icon in the toolbar.";
  $("rescan").hidden = true;
}

function showEmptyCopy() {
  $("empty-title").textContent = "No menu detected on this page yet";
  $("empty-body").textContent = "Keep browsing. We'll light up as soon as dishes show up.";
  $("rescan").hidden = false;
}

// ---------- wiring ----------

async function followActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) return;
  reset(tab.id);
  showEmptyCopy();
  renderHeader();
  scan(tab.id);
}

chrome.runtime.onMessage.addListener((message, sender) => {
  if (message?.type === "menulens:items" && sender.tab?.id === state.tabId) {
    showEmptyCopy();
    accept(message);
  }
  if (message?.type === "menulens:rescan" && message.tabId === state.tabId) followActiveTab();
});

chrome.tabs.onActivated.addListener(() => followActiveTab());
chrome.tabs.onUpdated.addListener((tabId, change) => {
  if (tabId === state.tabId && change.status === "complete") followActiveTab();
});

$("rescan").addEventListener("click", () => followActiveTab());

followActiveTab();
