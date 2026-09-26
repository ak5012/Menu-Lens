# MenuLens for Microsoft Edge

Click the MenuLens icon on a restaurant's menu page. A side panel lists each dish with a
calorie range, a confidence level and a short reason.

```
manifest.json    permissions: activeTab + scripting + sidePanel, and the local API only
background.js    icon click -> open the side panel, grant access to that one tab
content.js       reads the dishes on the page (injected only after the click)
sidepanel.*      the panel: sends the whole menu in groups of 20 dishes, and shows results
icons/           toolbar and store icons
```

## Try it

1. Start the API from `estimator` (keys and settings come from `estimator/.env`):
   ```powershell
   python -m uvicorn server:app --port 8000
   ```
   To test without using Gemini quota, set `MENULENS_PROVIDER=mock` in `.env` first.
   Mock numbers are fake.
2. In Edge, go to `edge://extensions`, turn on **Developer mode**, click **Load unpacked**
   and pick this `extension` folder.
3. Pin MenuLens in the toolbar (puzzle-piece icon, then the eye icon).
4. Open a restaurant menu page and click the MenuLens icon.

After editing any file here, click **Reload** on the MenuLens card in `edge://extensions`.

## How it finds dishes

1. **schema.org menu data** (JSON-LD). Many restaurant sites publish it for search
   engines. It is exact: dish names, descriptions, sections, cuisine and price range.
2. **The page itself.** It finds each price and the dish name printed next to it, and
   groups dishes under the nearest heading. Prices in navigation bars and footers are
   ignored. A page with fewer than 3 priced items isn't treated as a menu.

Dishes that load later (scrolling, clicking a menu tab) show up under "Found further
down the page".

## Privacy

- MenuLens reads a page only after you click its icon on that page.
- It sends only the dish name, description, section, restaurant name, cuisine and
  price tier to the MenuLens API.
- It does not collect browsing history or personal data.

## How it uses the free quota

- The whole menu is estimated when the panel opens, in groups of up to 20 dishes: one
  request per group, 2 groups at a time, in page order, so the top of the panel fills first.
- **Rate limited:** the panel pauses by itself, says so in a banner, and continues when
  the server says it can.
- **Daily limit gone:** the panel stops and says so. Dishes already estimated stay.
- **Retry** on a card re-asks for just that dish.

## Known limits

- Menus shown only as images or PDFs can't be read. The upload page on the website
  handles PDFs.
- After the page navigates, click the icon again. Access is granted per click, per page.
- The API address is `http://localhost:8000` for now. When the backend is deployed,
  change `API_BASE` in `sidepanel.js` and `host_permissions` in `manifest.json`.
