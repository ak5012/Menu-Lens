"""Run the estimator over the benchmark and write predictions for score.py.

    python bench.py --split test --hide-names -o preds-hidden.jsonl
    python bench.py --split test               -o preds-named.jsonl
    python ../benchmark/score.py preds-hidden.jsonl ../benchmark/items.csv --split test
    python ../benchmark/score.py preds-named.jsonl  ../benchmark/items.csv --split test

--hide-names replaces the chain and its branded dish names with generic ones
("Cheeseburger with Special Sauce" at "a fast-casual american restaurant"). These
chains publish their calorie counts, so the model may have memorised them; the
hidden-name score is the one that predicts accuracy at independent restaurants.
A large gap between the two scores means the named run is measuring recall.

Run it once per model and score each file to compare models.
"""

from __future__ import annotations

import argparse
import csv
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anthropic

from estimator import DEFAULT_MODEL, Estimator, MenuItem, Suppressed

BENCHMARK = Path(__file__).resolve().parent.parent / "benchmark" / "items.csv"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--items", type=Path, default=BENCHMARK)
    ap.add_argument("-o", "--out", type=Path, default=Path("predictions.jsonl"))
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--split", default="test", help="'all' for every split")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--hide-names", action="store_true",
                    help="use generic dish names and no restaurant name")
    args = ap.parse_args()

    with args.items.open(newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh) if args.split == "all" or r["split"] == args.split]
    rows = rows[: args.limit] if args.limit else rows

    est = Estimator(args.model, args.effort)

    def run(row: dict) -> dict:
        # Menu-visible fields only: true_kcal must never reach the model.
        if args.hide_names:
            item = MenuItem(
                name=row["generic_name"],
                description=row["generic_description"],
                restaurant=f"a {row['format']} {row['cuisine']} restaurant",
                cuisine=row["cuisine"],
                price_tier=int(row["price_tier"]),
            )
        else:
            item = MenuItem(
                name=row["item_name"],
                description=row["menu_description"],
                restaurant=row["chain"].replace("-", " ").title(),
                cuisine=row["cuisine"],
                price_tier=int(row["price_tier"]),
            )
        try:
            e = est.estimate(item)
        except Suppressed:
            return {"id": row["id"], "suppressed": True}
        except (anthropic.APIStatusError, anthropic.APIConnectionError, RuntimeError) as exc:
            print(f"  failed {row['id']}: {exc}")
            return {}
        print(f"  {row['item_name'][:40]:<40} {e.low}-{e.high}  {e.band}")
        return {"id": row["id"], "low": e.low, "high": e.high, "midpoint": e.midpoint,
                "band": e.band, "widened": e.widened}

    with ThreadPoolExecutor(args.concurrency) as pool:
        results = [r for r in pool.map(run, rows) if r]

    args.out.write_text("\n".join(json.dumps(r) for r in results) + "\n", encoding="utf-8")
    print(f"\n  {len(results)}/{len(rows)} written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
