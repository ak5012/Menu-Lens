# MenuLens build log

A running record of what is being built, why, how it was tested, and what is left.
Newest entries at the top.

**Goal:** a free-to-run calorie estimator, published on the Microsoft Edge Add-ons store.

**Plan:**

| Step | What | Status |
|---|---|---|
| 1 | Swap the paid AI model for a free one, and keep the benchmark able to test it | **Done** (waiting on a free Gemini key for a live test) |
| 2 | Build the Edge extension: content script, service worker, side panel | **Done** (tested with mock answers) |
| 3 | Deploy the backend as a small proxy that holds the key, with caching and rate limits | Next |
| 4 | Connect the Lovable frontend to the real API | **Done** for local testing (needs the deployed URL later) |
| 5 | Privacy policy, store listing, submit to Edge Add-ons | Planned |

---

## 2026-09-26 - Making the free Gemini quota last: group requests, fallback models, cache

**Problem:** the extension kept saying "busy". There were two causes:
- `gemini-3.6-flash` allows **20 free requests a day**. Google's error said so:
  `GenerateRequestsPerDayPerProjectPerModel-FreeTier`, limit 20.
- The extension sent **one request per dish**. Bamboo House has 97 dishes, so it hit the
  per-minute limit within seconds.

A new key wouldn't help: limits are per Google project, not per key. Making extra
projects to multiply the free quota would break Google's terms, so it isn't used.

### Benchmark first: does estimating several dishes per request hurt accuracy?

`gemini-3.1-flash-lite`, hidden names, `train`+`val` (29 verified dishes), each mode run
twice. Groups hold one restaurant's dishes: 6 to 12 per request here, since the
benchmark has at most 12 per restaurant.

| Run | Requests | Coverage | High | Medium | Width | Typical miss | Answered |
|---|---|---|---|---|---|---|---|
| One per request, run 1 | 30 | 52% | 71% | 45% | 0.40 | 15% | 100% |
| One per request, run 2 | 30 | 48% | 71% | 41% | 0.42 | 22% | 100% |
| Groups, run 1 | 4 | 52% | 70% | 44% | 0.44 | 25% | 100% |
| Groups, run 2 | 4 | 55% | 67% | 46% | 0.44 | 21% | 100% |

- **Decision:** groups are **as accurate as single requests**. Repeating the same mode varies
  about as much as switching modes does. Ranges are slightly wider (0.44 vs 0.41) and still
  under the 0.45 bar. Requests drop by about 7.5x here, and by up to 20x on real menus.
- **Not tested:** groups of 20 on a real menu. The benchmark's groups were at most 12 dishes.
- **Bigger finding:** `gemini-3.1-flash-lite` misses the accuracy bar in **both** modes:
  about 50% coverage against the 80% target. The model is the accuracy problem, not the
  grouping. Choose a model with `compare.py` before launch.
- Raw predictions: `estimator/runs/batchcmp-*-3.1-flash-lite.jsonl`.

### What changed

**`estimator/estimator.py`**
- New `Estimator.estimate_batch(items)`: 1 to 20 dishes from **one restaurant** in one
  request. The restaurant is stated once, and the dishes are numbered.
  - New `BATCH_SYSTEM` prompt: "estimate each dish on its own, as if it were the only dish".
  - New `BATCH_SCHEMA`: a list of the usual answer fields, plus `index`.
  - Returns one result per dish. A skipped, garbled or too-vague dish fails **alone**
    (`ProviderError` or `Suppressed`). A rate limit or other whole-request error is raised.
- The per-answer rules (fields, minimum width per band, ±60% ceiling) moved into
  `_checked()`, so single and group answers pass through identical code.
- `MenuItem.to_prompt()` is split into `dish_lines()` and `restaurant_lines()`. The
  single-dish prompt text is **byte-for-byte unchanged** (tested), so old cache entries and
  benchmark results stay valid.

**`estimator/providers.py`**
- `.env` is loaded automatically (python-dotenv).
- New `FallbackProvider`, used when `MENULENS_MODEL` lists several models, comma-separated.
  - A model that is rate limited rests for the wait Google asks for (30s if none).
  - A model whose daily quota is gone rests for 1 hour.
  - A busy or slow model rests for 30s.
  - Meanwhile the next model answers. A "daily" error reaches the user only when every
    model is out for the day.
- `MockProvider` answers group requests too, for free testing.

**`estimator/cache.py`** (new): saves every answer (estimates and "too vague") in SQLite
(`estimator/cache.db`) for 30 days. The key is the provider plus the normalized dish
prompt, so mock answers never stand in for real ones. `cache.db` is in `.gitignore`.

**`estimator/server.py`**
- New `POST /v1/estimate/batch`:
  - Request: `{items: [up to 20], restaurant}`.
  - Response: `{results: [...], model_requests: 0 | 1}`. Each result's `status` is `ok`,
    `insufficient_signal` or `error`.
  - Cached dishes skip the model. If every dish is cached, no model request is made.
- `/v1/estimate` and the group endpoint share one cache and one error mapping (`model_errors_as_http`).
- 429 errors now say which limit was hit:
  - `rate_limited`, with `retry_after_s`: wait and try again.
  - `daily_quota`: "Today's free estimate limit is used up".
  - Before, both said "try again in a minute".

**`estimator/bench.py`**
- New `--batch N` option: groups the benchmark's dishes by restaurant, N per request.
- Retry handling moved into `call_with_retries()`, shared by both modes. In group mode,
  `seconds` is the whole request, and the record includes `batch_size`.

**`estimator/.env`**
- `MENULENS_MODEL=gemini-3.1-flash-lite,gemini-3.5-flash-lite,gemini-3.5-flash,gemini-3-flash-preview`.
- `gemini-3.6-flash` was dropped from the list because of its 20-a-day limit.

**`extension/`**
- `sidepanel.js`:
  - The whole menu is sent to `/v1/estimate/batch` in groups of 20, 2 groups at a time,
    in page order.
  - Rate limited: the group keeps its place, and a banner says "Continuing by itself in
    about Ns".
  - Daily limit: the panel stops and says so.
  - Retry re-asks for one dish.
  - The on-screen-only estimation added earlier today was removed. With groups, the
    whole menu costs only a few requests.
- `content.js`:
  - The restaurant name skips title parts about the menu itself, so "Menu @ Austin -
    Bamboo House" gives "Bamboo House".
  - Menu codes are removed ("N3. Szechuan Beef" becomes "Szechuan Beef").
  - A section heading inside a card is no longer taken as the dish name.
  - Price lines ("$21.95 SP") and headings are no longer used as descriptions.

**Lovable MenuLens:** the upload page uses `estimateBatch()`, with groups of 20, 2 at a
time. Rate limits wait and resend automatically. The daily limit stops the run with a
message. Retry sends one dish. (2.6 credits.)

### Tested

- Unit tests:
  - `estimate_batch`: missing, out-of-order, duplicate and garbled answers; a too-wide
    range; the size and one-restaurant rules.
  - `FallbackProvider`: rests and skips a limited model; tells daily limits from per-minute ones.
  - Cache: reused across both endpoints, still served while rate limited.
  - Group endpoint: partial failures, 429 detail, 0 or 21 items rejected.
- **Real Gemini benchmark:** the table above.
- **Extension, headless Chrome, 60-dish test page, stand-in API on port 8090:**
  - The whole menu took 3 group requests, plus 1 re-send after a simulated rate limit.
  - 38 dishes showed within 1.5s.
  - Skipped dishes showed Retry, and Retry fixed them.
  - No page errors.
- **Not tested:**
  - The Lovable page against the server (its preview needs a Lovable login).
  - A real 97-dish menu through the real server.

**Testing mistake:** an earlier extension test used port 8000 while the real server was
running there. About 15-20 test dishes went to real Gemini, and the test's cleanup
stopped the server. Tests now use port 8090.

---

## 2026-09-25 — Edge extension, and the website connected to the API

**Backend (`estimator/`):**
- Keys and settings now load from `estimator/.env` automatically (`python-dotenv`).
  Anything set in the shell still wins.
- New `POST /v1/menu/parse`: splits pasted menu text into dishes (name, description,
  section). Plain rules, no model call, so it's free and instant. It handles section
  headings, prices, "name - description" lines, and descriptions on the line below.
- Input limits on `/v1/estimate` (name 120 characters, description 500), so a page
  can't send a huge prompt.
- `.env` allows the Lovable preview addresses in `MENULENS_ALLOWED_ORIGINS`.

**Extension (`extension/`, Manifest V3):** see `extension/README.md`.
- Uses the smallest set of permissions: `activeTab` (read a page only after the icon is
  clicked), `scripting`, `sidePanel`, and the local API. No "read all websites"
  permission, which makes store review easier.
- Reads dishes from schema.org menu data first, then from prices on the page.
- The side panel matches the Lovable panel design. It sends 2 requests at a time and
  shows a loading card per dish, "too uncertain" for dishes the estimator declines
  (422), Retry on errors, and a banner when the server can't be reached.

**Website (Lovable MenuLens):** the upload page now calls `/v1/menu/parse` and then
`/v1/estimate` for each dish, 2 at a time, instead of using sample data. PDF and .docx
files are read in the browser. The API address comes from `VITE_MENULENS_API_URL`, and
defaults to `http://localhost:8000`.

**Tested:**
- API with `mock`: parsing (sections, prices, duplicates, name-only dishes), input
  limits (422), and CORS (listed site allowed, other sites refused).
- Extension in headless Chrome. Automated Edge exits on launch on this machine, and the
  extension APIs are the same Chromium ones.
  - A normal menu page: 6 dishes and the $$ tier found. Navigation and footer prices
    ignored. A dish added 2.5 seconds later appeared as "New".
  - A schema.org page: name, "$ · Mexican" and 3 dishes.
  - A news article with one price: no menu found.
  - A dish name containing HTML was shown as text, not run as code.
  - Server stopped: a banner and a Retry button per dish. After restarting the server,
    Retry filled in every card.
  - Fixed a bug found here: the "no menu" message showed even when dishes were found.
- **Not tested:**
  - The real toolbar-icon click (a script can't click the toolbar). The test granted
    access another way.
  - The Lovable page against the API, because its preview requires a Lovable login.
    Only a code review was done.
  - Real Gemini answers (daily quota used up).

**To try it:** start the API, load `extension/` in `edge://extensions` (see its README),
and open the Lovable preview while logged in. The first time the website calls
`localhost`, the browser may ask to allow access to devices on your local network.
Choose Allow.

---

## 2026-09-15 — Fix: run crashed on "model busy" (503)

**What happened:** the real Gemini run estimated two items in 3.9s and 2.3s, then
crashed on item 3. Google answered `503 This model is currently experiencing high demand`.

**Why:** the rewritten `bench.py` retried rate limits and timeouts, but left out plain
model errors, so this one escaped and ended the run. The two finished items were still
saved, because results are written as they arrive.

**What changed:**
- `providers.py`: new error type **`ProviderBusy`** for temporary server-side failures
  (any 5xx from Gemini; 5xx, including 529 "overloaded", from Claude).
- `bench.py`:
  - A busy model is retried with a visible wait of 20s, then 40s, then 60s.
  - Any other model error, such as an unreadable answer, **fails that one item and the
    run continues**. The reason is printed. `--resume` retries failed items later.
  - A bad key or unavailable model still stops the whole run, since it affects every item.
- The server already turns these into 503 "model unavailable", so no server change.

**Tested (simulated):** Google's exact 503 body is read as `ProviderBusy`. Busy on item 3
then success: waits, completes all 4. Busy 4 times on one item: that item fails, the next
item runs. Unreadable answer: that item fails with the reason shown, and the run continues.

**Early latency signal:** 3.9s and 2.3s per item on `gemini-3.6-flash`, close to the PRD's
3-second target. The full run will give a better picture.

---

## 2026-09-15 — Fix: benchmark run appeared frozen

**What happened:** with a real Gemini key, the first item estimated fine (Chicken Burrito
Bowl 630-820, high confidence), then the run went silent until it was stopped with Ctrl+C,
which crashed with a long error.

**Why:** two things could make it go quiet, and the old code couldn't tell which:
- **Hidden retries.** Google's SDK was set to wait and retry rate-limit errors on its own,
  for up to about 2.5 minutes, printing nothing.
- **No time limit.** A slow answer could wait indefinitely.

Ctrl+C crashed because requests ran on background threads that Python waits for before
exiting.

**What changed:**
- `providers.py`
  - Gemini requests now have a **90-second time limit**.
  - The SDK's hidden retries are **off**. Retrying is now the caller's job, so it can be
    visible.
  - A rate-limit error now carries **how long Google says to wait** and **whether it's the
    daily quota**. Retrying a daily quota the same day is pointless, so that stops the run.
  - New error type `ProviderTimeout`.
- `bench.py` rewritten again, for visibility and safety:
  - Items run **one at a time** by default. At 8 requests a minute, parallel requests
    gained nothing and caused the Ctrl+C crash.
  - Each item shows before its request starts, then the result and **how many seconds
    it took**.
  - Rate limits: prints "rate limited; waiting 33s, then retrying" using Google's suggested
    wait, up to 4 attempts. Timeouts retry visibly too.
  - Daily quota: stops and says to resume tomorrow or switch model.
  - **Every result is saved the moment it arrives.** Ctrl+C now stops cleanly and keeps
    everything done so far.
  - **New `--resume`:** re-run the same command and it skips items already saved. Failed
    items are retried.
- The server is unaffected in behaviour. With no hidden retries it now answers "too many
  estimates" (HTTP 429) straight away, instead of making a user wait minutes.

**Tested (no cost, simulated errors):**

| Scenario | Result |
|---|---|
| Rate limited once, then succeeds | Wait message shown, item completes, 3/3 saved |
| Times out once, then succeeds | Retry message shown, item completes |
| Daily quota on item 3 of 5 | Stops, first 2 saved, advice to resume tomorrow |
| `--resume` after that | Runs only the remaining 3; file ends with 5 unique items |
| Ctrl+C during item 2 | Clean stop, item 1 kept, exit code 130 |
| Google-format rate-limit error, per minute | Read as "wait 33s", not daily |
| Google-format rate-limit error, per day | Read as daily quota |
| Network read timeout | Becomes `ProviderTimeout` |

**Still unknown:** why the real second request went quiet. The next real run will show it
on screen: either a rate-limit wait, a timeout retry, or a slow answer with its time in seconds.

---

## 2026-09-15 — Fix: Gemini default model changed

**What happened:** the first live smoke test with a real Gemini key failed with
`404 This model models/gemini-2.5-flash is no longer available to new users`.
The key itself was accepted.

**What changed:**
- `providers.py`: default Gemini model is now **`gemini-3.6-flash`**, the model Google's
  error message recommends, which is on Google's list of stable models.
- New error type `ProviderModelUnavailable` for "that model doesn't exist or isn't
  offered to you" (HTTP 404), for both Gemini and Claude.
- `bench.py` treats it like a bad key: it stops after the first request and says to pick
  another model with `--model`, instead of printing the same failure for every item.
- `server.py` returns 503 "The estimation service isn't configured correctly" for it,
  because it's a server setup problem, not the user's fault.
- `estimator.py` command line prints a plain message for it.

**Tested:** simulated a 404 from the model. `bench.py` stopped after one item with
exit code 1 and wrote no predictions file. The server answered 503 `upstream_unavailable`.

**Not changed:** Google's error message also recommends its newer "Interactions API".
MenuLens still uses `generate_content`, which works and is documented in the SDK. Moving
to the Interactions API isn't needed for anything MenuLens does today.

---

## 2026-09-15 — Step 1: free model support

### Why this step came first

Everything else (the extension, the Lovable site, the accuracy test) needs a model
that answers calorie questions without costing money. Until now the only model was
Claude, which has no free tier. It was also the biggest unknown: if a free model
estimates badly, the plan changes before any extension work is done.

### The idea: a "provider" layer

Before, `estimator.py` called Claude directly. Now the model sits behind one small
interface: give it instructions, a question and the required answer format, and get
back an answer. Three interchangeable implementations plug into it:

| Provider | What it is | Cost |
|---|---|---|
| `gemini` | Google's Gemini API, model `gemini-3.6-flash`. **The new default.** | Free tier |
| `anthropic` | Claude, exactly as before | Paid |
| `mock` | No AI at all: fake but repeatable numbers | Free |

The MenuLens-specific rules (the prompt, the answer format, minimum range widths,
suppressing vague answers) did **not** move. They stay in `estimator.py` and apply
identically to every provider, so switching model can't weaken what the product
promises.

`mock` exists so the server, extension and website can be built and tested without
any key or cost. Its numbers are random-looking and **must never be used to judge
accuracy**. The benchmark runner prints a warning whenever it's used.

### What changed, file by file

**`estimator/providers.py` — new**
- One class per provider, all with the same `generate()` method.
- Gemini is called through Google's official `google-genai` SDK. The key comes from
  `GEMINI_API_KEY`, and the answer is forced into our JSON format with
  `response_json_schema`.
- **Automatic retries:** Gemini's free tier limits requests per minute. When Google
  answers "too many requests" (HTTP 429), the SDK waits and retries up to 4 times, with
  the delay growing from 5 seconds up to 60, instead of failing straight away.
- **Plain error types.** Each provider's errors are translated into four shared ones,
  so the rest of the code handles every model the same way:
  - `ProviderAuthError`: key missing, wrong, or not allowed.
  - `ProviderRateLimited`: the quota is still used up after the retries.
  - `ProviderModelUnavailable`: the model doesn't exist or isn't offered to this account.
  - `ProviderError`: the model is down, or returned something unreadable.
  - `Suppressed`: the model declined, or gave no answer.
- A `MENULENS_PROVIDER` setting chooses the provider; `MENULENS_MODEL` overrides the model.

**`estimator/estimator.py` — changed**
- `Estimator` now takes a provider instead of calling Claude itself.
- New check: if a model returns an answer with missing fields, wrong types or an unknown
  confidence level, it's rejected as an error instead of crashing or reaching users.
  Free models are more likely to do this than Claude, so it matters more now.
- The command line gained `--provider`. The old `--effort` option was Claude-only and
  was removed.

**`estimator/bench.py` — rewritten**
- `--provider` chooses the model (default `gemini`).
- **Pacing:** requests are spaced to 8 per minute for Gemini, so a full benchmark run
  stays under the free limit instead of hitting errors. Change it with `--rpm`.
- A bad or missing key stops the run after one request, with setup steps for that
  provider.
- Items that still fail are listed and counted. Re-running fills them in.
- Each prediction now records which model produced it.

**`estimator/server.py` — changed**
- The model comes from `MENULENS_PROVIDER`, not a hard-coded Claude.
- **New `GET /health`** reports which provider and model the server is using.
- **Browser access (CORS):** web pages can only call the server if their address is
  listed in `MENULENS_ALLOWED_ORIGINS`. The Lovable site will need this. The extension
  won't: extensions call servers they have permission for directly.
- **What users see when things go wrong:**

  | Situation | HTTP | Message |
  |---|---|---|
  | Too vague, or the model declined | 422 | `insufficient_signal` |
  | Free quota used up | 429 | "Too many estimates right now. Try again in a minute." |
  | Server key wrong or missing | 503 | "The estimation service isn't configured correctly." (never reveals key details) |
  | Model down or garbled answer | 503 | "The estimation model is unavailable. Try again." |

**`estimator/requirements.txt` — changed**: added `google-genai` (Google's SDK) and
`httpx` (lets the server be tested without starting it).

**READMEs**: `estimator/README.md` rewritten for the three providers.
`benchmark/README.md` now points at `estimator/bench.py` as the runner.
`benchmark/baseline.py`, the original Claude-only runner, is kept for reference.

### How it was tested (all at zero cost)

| Test | Result |
|---|---|
| All four files compile | Pass |
| Every SDK name used exists in the installed `google-genai` 2.23.0 | Pass |
| Command line with `mock` | Returns a range, confidence, portion and rationale |
| `bench.py` with `mock`, all 30 test items, names hidden | 30/30 written |
| Scorer on the mock file | Fails 5 of 6 accuracy measures, which is correct for fake numbers; only the answered-rate check passes |
| Server `GET /health` | 200, reports `mock` |
| Server valid request | 200 with the full response shape |
| Server bad input (too-short name, price tier 9) | 422 |
| Server when the model declines / quota used up / key wrong / model down / garbled answer | 422 / 429 / 503 / 503 / 503, with no key details leaked |
| Browser access from an allowed site / any other site | Allowed / blocked |
| Gemini with no key | Stops with setup steps |
| Gemini with a deliberately invalid key (a real request to Google) | Google rejected only the key, so the request format is accepted |
| Claude with a deliberately invalid key | Rejected with the Claude setup steps |

### Not tested yet (needs your free Gemini key)

- A real answer from Gemini: whether it follows the answer format every time, and how
  fast it is.
- Accuracy: the full benchmark run.
- Whether `gemini-3.6-flash` is free for **your** Google project. Free models and limits
  vary; check at https://aistudio.google.com/rate-limit and switch with `--model` if needed.

### Decisions and trade-offs

- **`gemini-3.6-flash` as the default** (originally `gemini-2.5-flash`; see the fix
  above). The benchmark is how to compare it with other Flash models.
- **Free-tier privacy:** on the free tier, Google may use prompts to improve its
  products. MenuLens sends only menu text, not personal data, but the privacy policy in
  Step 5 must say this.
- **Claude stays available** (`--provider anthropic`), in case you ever want to compare
  accuracy or pay for quality.

### What you need to do

1. Get a free key: https://aistudio.google.com, then "Get API key".
2. In PowerShell, from `menu-estimator\estimator`:
   ```powershell
   $env:GEMINI_API_KEY = "paste-your-key-here"
   python bench.py --hide-names --limit 2 -o smoke.jsonl
   ```
3. If that prints two ranges, run the full test (about 4 minutes per run):
   ```powershell
   python bench.py --hide-names -o preds-hidden.jsonl
   python bench.py -o preds-named.jsonl
   python ..\benchmark\score.py preds-hidden.jsonl ..\benchmark\items.csv
   python ..\benchmark\score.py preds-named.jsonl ..\benchmark\items.csv
   ```

---

## 2026-09-15 — Benchmark accuracy audit

- Re-extracted all 58 verified calorie figures directly from the five official
  nutrition guides, using a lookup independent of `items.csv`. **58 of 58 match exactly.**
- Fixed three places where the text the model reads didn't match the figure it's scored
  against:
  - Three Cheesecake Factory appetizers are whole sharing plates, so their descriptions
    now say "Serves 2-4".
  - Three Panera replacement items had descriptions written from memory. The Ciabatta
    Cheesesteak's was even copied from a discontinued sandwich. All three were cleared.
- **Remaining limit:** other menu descriptions were written from memory of the menus, not
  copied from them. They're model input, not answers, so the calorie figures are unaffected.
