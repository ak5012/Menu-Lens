# Estimator

Calorie ranges for menu items from a pre-trained LLM (Claude). No model training:
one API call per item.

```
estimator.py   the API call, plus a command line
server.py      POST /v1/estimate for the browser extension
bench.py       runs the estimator over ../benchmark/items.csv for scoring
```

## Setup

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...      # PowerShell: $env:ANTHROPIC_API_KEY="sk-ant-..."
```

## Use it

```bash
python estimator.py "Chicken Alfredo" --description "Grilled chicken over fettuccine alfredo" \
    --restaurant "Olive Garden" --cuisine italian --tier 2

uvicorn server:app --port 8000
```

## What the code guarantees, whatever the model says

| Rule | Why |
|---|---|
| Response must match a JSON schema | Every answer has a range, a confidence band, and a rationale |
| Minimum range width per band: high ±12%, medium ±20%, low ±33%, never under 80 kcal | A low-confidence answer can't pose as precise |
| Wider than ±60% → no estimate (HTTP 422) | "300–2,000 calories" helps nobody |
| Refusals re-run on Anthropic's recommended fallback model | `fallbacks: "default"`; the `model` field reports which model served it |

## Which model is best?

Measure it instead of guessing. Use `--hide-names` so the model sees generic dish
names and no restaurant name — these chains publish their counts, and the named
run can measure memory instead of estimation:

```bash
python bench.py --model claude-opus-5   --hide-names -o preds-opus5.jsonl
python bench.py --model claude-sonnet-5 --hide-names -o preds-sonnet5.jsonl
python ../benchmark/score.py preds-opus5.jsonl   ../benchmark/items.csv
python ../benchmark/score.py preds-sonnet5.jsonl ../benchmark/items.csv
```

Scores only count benchmark rows with verified labels (58 of 60).

To compare a non-Claude model (Gemini, GPT, Grok), add an estimator for that
provider that returns the same `Estimate` shape. `bench.py` and `score.py` work unchanged.
