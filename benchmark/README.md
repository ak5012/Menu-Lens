# Menu Calorie Estimator — benchmark set

The ruler for Phase 1. Everything downstream — whether retrieval beats an
ungrounded call, how many Tier B rows to curate, whether the confidence score
means anything — is measured against this file.

A ruler with wrong markings is worse than no ruler, so the harness is built to
make it structurally impossible to score against a number nobody checked.

```
items.csv      60 rows; 58 verified against official nutrition guides, 2 pending
validate.py    schema + provenance + brand-leak checks, stratification report
baseline.py    original Claude-only runner (use ../estimator/bench.py instead)
score.py       the §10 exit bar
```

---

## Verification status

| Chain | Verified | Source |
|---|---|---|
| Panera | 12 / 12 | US Nutrition Guide, effective 6/17/2026 |
| Olive Garden | 12 / 12 | US Nutrition Information PDF, revision US_083126, dinner portions |
| Cheesecake Factory | 12 / 12 | Nutritional Guide (c)2026 TCF Co., full-size portions |
| Shake Shack | 12 / 12 | Nutrition & Allergen Information, 1.6.26, standalone items |
| Chipotle | 10 / 12 | US Nutrition Facts, 3-2025 — per-ingredient, summed (see `source_row`) |

**Still pending:** both Chipotle quesadillas. The chart has no adult quesadilla
tortilla or cheese portion, so there is nothing published to sum.

Verification changed the set as well as filling it. Four items were discontinued
and replaced with current ones (three at Panera; Cheesecake Factory's Factory
Burrito Grande became its Breakfast Burrito). Recalled figures were often far
off — Panera's Greek Salad is 630 kcal, not 380; Cheesecake Factory's Avocado
Eggrolls are 930, not 1490 — which is exactly why none of them were used as labels.

Two traps worth knowing if you add rows: the Cheesecake Factory and Olive Garden
guides list lunch and dinner sizes of the same dish, and Shake Shack's guide
lists meal ranges (burger + fries + drink, marked `*`) next to the standalone
items. Search snippets quote the meal range as if it were the burger.

Unverified rows carry a `candidate_kcal_UNVERIFIED` ballpark figure. It is
**not a label and must never be used as one** — chains reformulate, portions
change, and being 80 kcal off on a benchmark item silently corrupts every
coverage number you compute afterwards. Its one job is catching a wrong-item
match: if the published figure lands more than 25% away, `validate.py` warns.
Once a row is verified, clear the candidate.

`validate.py` refuses any row that has a `true_kcal` without `source_url`,
`source_row`, and `retrieved_on`. `score.py` ignores every row that isn't
verified. You cannot accidentally measure against a guess.

---

## Named vs hidden-name runs

These chains publish their calorie counts, so a model may have memorised them
during training. A benchmark that shows it "Zuppa Toscana at Olive Garden" can
end up measuring recall instead of estimation — and the product is for
restaurants whose numbers nobody has published.

Every row therefore carries three extra columns:

| Column | Example | Purpose |
|---|---|---|
| `format` | `casual-dining` | Kept in both runs: the extension will know this about a real restaurant |
| `generic_name` | `Sausage, Potato and Kale Soup` | Replaces branded dish names |
| `generic_description` | `ShackSauce` → `special sauce` | Replaces branded ingredient terms |

Run both and compare:

```powershell
cd ..\estimator
python bench.py --split test --hide-names -o preds-hidden.jsonl
python bench.py --split test              -o preds-named.jsonl
```

**The hidden-name score is the headline.** The named score is only there for
contrast: a large gap means the named run is measuring memory. `validate.py`
rejects any generic field containing a chain or brand term, so a leak can't
slip back in when rows are added.

---

## Verification pass

For each row, open the chain's published nutrition information, find the item,
and fill five columns: `true_kcal`, `source_row` (the exact line read, size
included), `source_url`, `retrieved_on`, `verified=yes`.

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

`../estimator/bench.py` and `baseline.py` build their prompts from menu-visible fields only: `item_name`,
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
the gaps worth filling first are breakfast items (one so far), shareable
appetizers, and tier-4 restaurants (none yet).

---

## Running it

```powershell
pip install -r ..\estimator\requirements.txt
python validate.py                                   # always start here

$env:GEMINI_API_KEY = "..."                          # free key from aistudio.google.com
cd ..\estimator
python bench.py --hide-names --limit 2 -o smoke.jsonl
python bench.py --hide-names -o preds-hidden.jsonl
python ..\benchmark\score.py preds-hidden.jsonl ..\benchmark\items.csv
```

`../estimator/bench.py` is the runner to use: it tests the same code path the product
serves, and switches model with `--provider` (gemini, anthropic, mock). `baseline.py` in
this folder is the original Claude-only runner, kept for reference.

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
