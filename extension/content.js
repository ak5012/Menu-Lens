// Reads the dishes on the current page. Injected only when the user clicks the MenuLens
// icon. Two sources, best first:
//   1. schema.org Menu data (JSON-LD), which many restaurant sites publish for search engines;
//   2. the page itself: each price on the page, and the dish name printed next to it.
// It then watches the page, so dishes that load later (scrolling, tabs) are reported too.

(() => {
  if (window.__menulens) return;

  const MAX_ITEMS = 150;
  const MIN_DOM_ITEMS = 3; // fewer priced things than this is a shop or article, not a menu
  const PRICE = /(?:^|[\s(])(?:[$€£]\s?\d{1,3}(?:[.,]\d{2})?|\d{1,3}[.,]\d{2})(?=$|[\s)])/g;
  const PRICE_ONLY = /^\s*(?:[$€£]\s?)?\d{1,3}(?:[.,]\d{2})?\s*$/;
  const NOT_A_DISH = /^(add|add to (cart|order|bag)|order( now)?|sold out|menu|view|select|customi[sz]e|popular|new)$/i;
  const SKIP = "script,style,noscript,template,svg,nav,footer,[aria-hidden='true']";

  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
  const types = (node) => [].concat(node?.["@type"] || []).map(String);

  // ---------- 1. schema.org JSON-LD ----------
  function fromJsonLd() {
    const items = [];
    const restaurant = {};
    const walk = (node, section) => {
      if (Array.isArray(node)) return node.forEach((n) => walk(n, section));
      if (!node || typeof node !== "object") return;
      const t = types(node);
      if (t.some((x) => /Restaurant|FoodEstablishment|CafeOrCoffeeShop|FastFoodRestaurant|Bakery|BarOrPub/.test(x))) {
        restaurant.name ||= clean(node.name);
        const cuisine = [].concat(node.servesCuisine || [])[0];
        restaurant.cuisine ||= clean(cuisine);
        restaurant.priceRange ||= clean(node.priceRange);
      }
      if (t.includes("MenuSection")) section = clean(node.name) || section;
      if (t.includes("MenuItem") && node.name) {
        items.push({ name: clean(node.name), description: clean(node.description), section: section || "" });
      }
      for (const [key, value] of Object.entries(node)) {
        if (key !== "@context" && value && typeof value === "object") walk(value, section);
      }
    };
    for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
      try {
        walk(JSON.parse(script.textContent), "");
      } catch {
        /* a malformed block on someone else's page: ignore it */
      }
    }
    return { items, restaurant };
  }

  // ---------- 2. the visible page ----------
  const countPrices = (text) => (text.match(PRICE) || []).length;

  function priceElements() {
    const found = new Set();
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const text = node.nodeValue;
      if (!text || text.length > 200 || !countPrices(" " + text + " ")) continue;
      const el = node.parentElement;
      if (el && !el.closest(SKIP)) found.add(el);
    }
    return [...found];
  }

  // The largest ancestor that still holds exactly one price: one dish's "card".
  function cardFor(priceEl) {
    let card = priceEl;
    for (let el = priceEl.parentElement, depth = 0; el && el !== document.body && depth < 6; el = el.parentElement, depth++) {
      const text = el.innerText || "";
      if (countPrices(text) > 1 || text.length > 600) break;
      card = el;
    }
    return card;
  }

  // The dish name is the last title-like text before the price. Taking the last one skips
  // a section heading that ended up inside the same card.
  function nameFor(card, lines, priceEl) {
    const candidates = [...card.querySelectorAll(
      "h1,h2,h3,h4,h5,h6,[class*='name' i],[class*='title' i],[data-testid*='name' i],strong,b")]
      .map((el) => ({ el, text: clean(el.innerText) }))
      .filter(({ text }) => text.length >= 2 && text.length <= 80 && /[a-z]/i.test(text) && !countPrices(" " + text + " "));
    const before = candidates.filter(({ el }) => el.compareDocumentPosition(priceEl) & Node.DOCUMENT_POSITION_FOLLOWING);
    const pick = before.at(-1) || candidates[0];
    if (pick) return pick.text;
    return lines.find((l) => l.length >= 2 && l.length <= 80 && /[a-z]/i.test(l) && !PRICE_ONLY.test(l)) || "";
  }

  // "N3. Szechuan Beef" -> "Szechuan Beef": menu numbering only confuses the model.
  const stripCode = (name) => {
    const stripped = name.replace(/^[A-Z]{0,3}\d{1,3}[a-z]?[.)]\s*/, "");
    return /[a-z]/i.test(stripped) ? stripped : name;
  };

  function sectionFor(card, headings) {
    let section = "";
    for (const h of headings) {
      if (h.el.compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING) section = h.text;
      else break;
    }
    return section;
  }

  function fromPage() {
    const headings = [...document.querySelectorAll("h1,h2,h3,h4")]
      .filter((h) => !h.closest(SKIP))
      .map((el) => ({ el, text: clean(el.innerText) }))
      .filter((h) => h.text && h.text.length <= 60);
    const items = [];
    const prices = [];
    const cards = new Set();
    for (const priceEl of priceElements()) {
      const card = cardFor(priceEl);
      if (cards.has(card)) continue;
      cards.add(card);
      const lines = (card.innerText || "").split("\n").map(clean).filter(Boolean);
      const name = stripCode(nameFor(card, lines, priceEl).replace(PRICE, "").trim());
      if (!name || NOT_A_DISH.test(name)) continue;
      const section = sectionFor(card, headings);
      const headingTexts = new Set(headings.map((h) => h.text));
      // A description is any other line that isn't a price, a badge, or a section heading.
      const description = lines.find((l) => l !== name && stripCode(l) !== name && l.length > 3
        && !countPrices(" " + l + " ") && !headingTexts.has(l) && !NOT_A_DISH.test(l)) || "";
      const price = (priceEl.innerText.match(/\d{1,3}(?:[.,]\d{2})?/) || [])[0];
      if (price) prices.push(parseFloat(price.replace(",", ".")));
      items.push({ name, description: description.slice(0, 300), section });
    }
    return { items: items.length >= MIN_DOM_ITEMS ? items : [], prices };
  }

  // ---------- restaurant context ----------
  function tierFrom(priceRange, prices) {
    const dollars = (priceRange || "").match(/^\s*([$€£]{1,4})\s*$/);
    if (dollars) return dollars[1].length;
    if (!prices.length) return null;
    const sorted = [...prices].sort((a, b) => a - b);
    const median = sorted[Math.floor(sorted.length / 2)];
    return median < 12 ? 1 : median < 25 ? 2 : median < 45 ? 3 : 4;
  }

  function siteName() {
    const og = document.querySelector('meta[property="og:site_name"]')?.content;
    if (clean(og)) return clean(og);
    // Titles look like "Menu @ Austin - Bamboo House": skip the parts about the menu itself.
    const parts = document.title.split(/\s[|–—:\/-]\s/).map(clean).filter(Boolean);
    return parts.find((part) => !/\b(menu|order|online|delivery|pickup|takeout)\b/i.test(part)) || parts[0] || "";
  }

  function extract() {
    const ld = fromJsonLd();
    const page = ld.items.length ? { items: [], prices: [] } : fromPage();
    const seen = new Set();
    const items = [];
    for (const item of ld.items.length ? ld.items : page.items) {
      const key = item.name.toLowerCase();
      if (item.name.length < 2 || seen.has(key)) continue;
      seen.add(key);
      items.push(item);
      if (items.length >= MAX_ITEMS) break;
    }
    return {
      url: location.href,
      source: ld.items.length ? "schema.org" : "page",
      restaurant: {
        name: (ld.restaurant.name || siteName()).slice(0, 120),
        cuisine: (ld.restaurant.cuisine || "").slice(0, 60),
        price_tier: tierFrom(ld.restaurant.priceRange, page.prices),
      },
      items,
    };
  }

  // ---------- watch for dishes that load later ----------
  let lastCount = -1;
  let timer = null;
  const report = () => {
    const result = extract();
    if (result.items.length === lastCount) return;
    lastCount = result.items.length;
    chrome.runtime.sendMessage({ type: "menulens:items", ...result }).catch(() => {});
  };
  new MutationObserver(() => {
    clearTimeout(timer);
    timer = setTimeout(report, 800);
  }).observe(document.body, { childList: true, subtree: true });

  window.__menulens = {
    extract: () => {
      const result = extract();
      lastCount = result.items.length;
      return result;
    },
  };
})();
