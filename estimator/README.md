# Estimator

Calorie ranges for menu items from a pre-trained LLM. No model training: one API
call per item. The model is swappable, and the default is Gemini's free tier.

```
providers.py   the model behind MenuLens: gemini (free tier), anthropic (paid), mock (fake, free)
estimator.py   the prompt, the answer format and the range rules, plus a command line
server.py      POST /v1/estimate and GET /health, for the extension and the web app
bench.py       runs the estimator over ../benchmark/items.csv for scoring
```

## Setup

```powershell
pip install -r requirements.txt
$env:GEMINI_API_KEY = "paste-your-key-here"    # free key from aistudio.google.com
```

No key yet? Everything below also runs with `--provider mock` (or
`$env:MENULENS_PROVIDER = "mock"` for the server). Mock answers are fake, so use them to
test the plumbing, never to judge accuracy.

## Use it

```powershell
python estimator.py "Chicken Alfredo" --restaurant "Olive Garden" --cuisine italian --tier 2
python estimator.py "Chicken Alfredo" --provider mock

$env:MENULENS_PROVIDER = "gemini"               # or mock / anthropic
$env:MENULENS_ALLOWED_ORIGINS = "https://your-lovable-preview.lovable.app"   # web app only
uvicorn server:app --port 8000
```

## Choosing a model

| Provider | Cost | Key | Notes |
|---|---|---|---|
| `gemini` (default: `gemini-3.6-flash`) | Free tier | `GEMINI_API_KEY` | Per-minute and per-day limits apply per Google project. On the free tier, Google may use prompts to improve its products. |
| `anthropic` (default: `claude-opus-5`) | Paid | `ANTHROPIC_API_KEY` | No free tier. |
| `mock` | Free | none | Fake, repeatable numbers for testing only. |

Override the model with `--model` or `MENULENS_MODEL`. Check which Gemini models your
project can use for free, and its limits, at https://aistudio.google.com/rate-limit.

## What the code guarantees, whatever the model says

| Rule | Why |
|---|---|
| Answer must match a JSON schema, with every field present and the right type | Every estimate has a range, a confidence band, a portion and a rationale |
| Minimum range width per band: high ±12%, medium ±20%, low ±33%, never under 80 kcal | A low-confidence answer can't pose as precise |
| Wider than ±60% → no estimate (HTTP 422) | "300–2,000 calories" helps nobody |
| Rate limits are retried with backoff (Gemini); quota still exhausted → HTTP 429 | Free-tier limits are per minute |
| A rejected key → HTTP 503 with no key details | The user isn't told server secrets |

## Measuring accuracy (free with Gemini)

```powershell
python bench.py --hide-names --limit 2 -o smoke.jsonl        # 2 requests: check the key works
python bench.py --hide-names -o preds-hidden.jsonl
python bench.py -o preds-named.jsonl
python ..\benchmark\score.py preds-hidden.jsonl ..\benchmark\items.csv
python ..\benchmark\score.py preds-named.jsonl  ..\benchmark\items.csv
```

`bench.py` runs one item at a time, spaced to 8 requests per minute for Gemini, so each
30-item run takes about 4 minutes or more. Change the pace with `--rpm`. Rate-limit waits
and retries are printed. Each result is saved as it arrives: Ctrl+C stops safely, and
re-running the same command with `--resume` continues where it left off. The hidden-name score is the headline number (see
`../benchmark/README.md`). Scores only count rows with verified labels (58 of 60).
