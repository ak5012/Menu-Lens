# MenuLens build log

A running record of what is being built, why, how it was tested, and what is left.
Newest entries at the top.

**Goal:** a free-to-run calorie estimator, published on the Microsoft Edge Add-ons store.

**Plan:**

| Step | What | Status |
|---|---|---|
| 1 | Swap the paid AI model for a free one, and keep the benchmark able to test it | **Done** (waiting on a free Gemini key for a live test) |
| 2 | Build the Edge extension: content script, service worker, side panel | Next |
| 3 | Deploy the backend as a small proxy that holds the key, with caching and rate limits | Planned |
| 4 | Connect the Lovable frontend to the real API | Planned |
| 5 | Privacy policy, store listing, submit to Edge Add-ons | Planned |

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
