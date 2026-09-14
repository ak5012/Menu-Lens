# Menu Calorie Estimator — benchmark set

The ruler for Phase 1. Everything downstream — whether retrieval beats an
ungrounded call, how many Tier B rows to curate, whether the confidence score
means anything — is measured against this file.

A ruler with wrong markings is worse than no ruler, so the harness is built to
make it structurally impossible to score against a number nobody checked.

```
items.csv      60 candidate rows, none verified yet
validate.py    schema + provenance checks, stratification report
baseline.py    the ungrounded Claude baseline (Phase 1, step 2)
score.py       the §10 exit bar
```

---

## The state this ships in

**Every row has `true_kcal` empty and `verified=no`.** That is deliberate, not
unfinished work.

The 60 rows carry real chain menu items, real menu descriptions, and a
`candidate_kcal_UNVERIFIED` column holding a recalled ballpark figure. Those
recalled figures are **not labels and must never be used as labels** — chains
reformulate, portions change, and being 80 kcal off on a benchmark item
silently corrupts every coverage number you compute afterwards.

`validate.py` refuses any row that has a `true_kcal` without a `source_url` and
a `retrieved_on` date. `score.py` ignores every row that isn't verified. You
cannot accidentally measure against a guess.

The candidate column has exactly one job: when you pull the published figure and
it lands more than 25% away from the candidate, you have probably matched the
wrong menu item. `validate.py` warns on that.

---

## Verification pass

For each row, open the chain's published nutrition information, find the item,
and fill four columns: `true_kcal`, `source_url`, `retrieved_on`, `verified=yes`.

Start points (confirm these still resolve — chains move these pages):

| Chain | Where the published figures live |
|---|---|
| Chipotle | Nutrition calculator on chipotle.com — per-ingredient, so a bowl is the sum of what the `menu_description` lists |
| Cheesecake Factory | Nutrition PDF linked from thecheesecakefactory.com |
| Olive Garden | Nutrition pages on olivegarden.com |
| Panera | Nutrition information on panerabread.com — per item, with size variants |
| Shake Shack | Nutrition PDF on shakeshack.com |

Three things that will bite:

- **Size variants.** "Bowl" vs "cup" of soup, single vs double patty. The
  `item_name` and `menu_description` in this file define which variant is meant;
  match that, don't take the first row you find.
- **Chipotle is per-ingredient.** Sum the ingredients named in the
  `menu_description` and nothing else. Record which ingredients you summed in a
  note if it isn't obvious.
- **Regional menus.** Where a figure differs by market, take the US national
  value and say so.

Run after every batch:

```bash
python validate.py
```

---

## The label-leakage rule

`baseline.py` builds its prompt from menu-visible fields only: `item_name`,
`menu_description`, `chain`, `cuisine`, `price_tier`, `listed_price_usd`.

`true_kcal` and `candidate_kcal_UNVERIFIED` must never reach a model. This also
means: do not paste an item name copied off the *nutrition* page into
`item_name` if it differs from the menu wording. The input has to be what a
diner sees; the label comes from the nutrition page. Blur that and coverage
becomes a measurement of nothing.

---

## Stratification

The set is built to make failures diagnosable rather than just counted.

- **5 chains**, spanning price tiers 1–3 and four cuisines.
- **17 archetypes** (burger, pasta, soup, salad, fried, side, dessert, shake…),
  so a systematic failure on fried items is visible instead of averaged away.
- **Both description states** — Chipotle rows print almost nothing, Cheesecake
  Factory rows print a paragraph. Description richness feeds the confidence
  score, so both have to be represented or that part of the scorer is untested.
- **Wide calorie spread** by design, from a single breadstick to a
  2,000-plus-calorie pasta. Without both tails the median-width metric is
  meaningless.
- **Splits**: `test` (30) is the headline, `val` (15) for tuning decisions,
  `train` (15) for prompt iteration. Tune against `train`/`val`; touch `test`
  only to report. An eval you tuned against is a training set wearing a
  benchmark's clothes.

To reach the 120 rows the design doc calls for, extend along the same axes —
the gaps worth filling first are breakfast items, shareable appetizers, and
tier-4 restaurants, none of which are represented yet.

---

## Running it

```bash
pip install -r requirements.txt
python validate.py                                   # always start here

export ANTHROPIC_API_KEY=...                         # or: ant auth login
python baseline.py --split test --limit 5            # smoke test, ~5 calls
python baseline.py --split test                      # full run
python score.py predictions-baseline.jsonl --split test
```

`baseline.py` defaults to `claude-opus-5` — the strongest ungrounded case,
which is the honest thing for the retrieval pipeline to have to beat. Sweep with
`--model claude-sonnet-5` to see what the cheaper production candidate gives up.
Both print measured cost per item at the end.

---

## What the exit bar is actually guarding

| Metric | Bar | The failure it prevents |
|---|---|---|
| Overall coverage | ≥ 80% | The obvious one: is the true value in the range |
| High-band coverage | ≥ 90% | High confidence has to mean something |
| Medium-band coverage | ≥ 75% | — |
| Median relative width | ≤ 0.45 | Coverage hits 100% the moment ranges are useless |
| Band separation | ≥ 15 pts | If high and low bands are equally accurate, the confidence score is decoration |
| Answered rate | ≥ 85% | Coverage climbs every time the system suppresses an item it was unsure about |

The last three exist only because the first three are gameable. Report all six
together or none of them.

---

## Expected outcome

Nobody knows yet — that is the point of running it.

A strong ungrounded baseline is good news, not disappointing news: it means
fewer curated rows and an earlier ship. A weak one proves the retrieval thesis
and de-risks the corpus spend. The result either way is what sizes Tier B, and
that decision is currently the largest uncosted item in the project.
