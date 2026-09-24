"""Score a set of predictions against the benchmark - the section 10 exit bar.

Reads predictions as JSONL, one object per line:

    {"id": "shk-shackburger", "low": 480, "high": 640,
     "midpoint": 560, "band": "high"}

A suppressed item (the service declined to answer) is recorded as:

    {"id": "og-breadstick", "suppressed": true}

Three metrics here exist only to close gaming vectors, and none of them are
optional:

  * median relative width, because coverage goes to 100% the moment ranges are
    wide enough to be useless;
  * answered rate, because coverage on the remainder climbs every time the
    system suppresses an item it was unsure about;
  * band separation, because a confidence label that doesn't predict accuracy
    is decoration.

Run:  python score.py predictions.jsonl [items.csv] [--split test]
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from pathlib import Path

# 10 exit bar. Each entry: (label, key, comparison, threshold, format)
EXIT_BAR = [
    ("Overall coverage",      "coverage",        ">=", 0.80, "pct"),
    ("High-band coverage",    "coverage_high",   ">=", 0.90, "pct"),
    ("Medium-band coverage",  "coverage_medium", ">=", 0.75, "pct"),
    ("Median relative width", "median_width",    "<=", 0.45, "num"),
    ("Band separation",       "band_separation", ">=", 0.15, "pts"),
    ("Answered rate",         "answered_rate",   ">=", 0.85, "pct"),
]


def load_benchmark(path: Path, split: str | None) -> dict[str, dict]:
    # split may be one name, several comma-separated ("train,val"), a set, or None for all.
    if isinstance(split, str):
        wanted = {s.strip() for s in split.split(",") if s.strip()}
    else:
        wanted = set(split) if split else set()

    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    usable: dict[str, dict] = {}
    skipped_unverified = 0
    for row in rows:
        if row["verified"] != "yes" or not row["true_kcal"].strip():
            skipped_unverified += 1
            continue
        if wanted and row["split"] not in wanted:
            continue
        usable[row["id"]] = {
            "true_kcal": int(row["true_kcal"]),
            "chain": row["chain"],
            "archetype": row["archetype"],
            "item_name": row["item_name"],
        }

    if skipped_unverified:
        print(f"  note: {skipped_unverified} row(s) ignored - no verified label yet")
    return usable


def load_predictions(path: Path) -> dict[str, dict]:
    preds: dict[str, dict] = {}
    with path.open(encoding="utf-8") as fh:
        for n, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                sys.exit(f"FATAL: {path}:{n} is not valid JSON - {exc}")
            if "id" not in obj:
                sys.exit(f"FATAL: {path}:{n} has no id")
            preds[obj["id"]] = obj

    # A score describes one model. Mixing two in a file makes it describe neither.
    models = {p["model"] for p in preds.values() if p.get("model")}
    if len(models) > 1:
        print(f"  WARNING: predictions come from {len(models)} different models: "
              f"{', '.join(sorted(models))}")
        print("  The scores below are a mix and describe no single model. Give each model")
        print("  its own output file.")
    elif models:
        print(f"  model: {next(iter(models))}")
    return preds


def compute(bench: dict[str, dict], preds: dict[str, dict]) -> dict:
    answered, suppressed, missing = [], [], []

    for item_id, row in bench.items():
        pred = preds.get(item_id)
        if pred is None:
            missing.append(item_id)
        elif pred.get("suppressed"):
            suppressed.append(item_id)
        else:
            answered.append((item_id, row, pred))

    results = []
    for item_id, row, pred in answered:
        low, high = int(pred["low"]), int(pred["high"])
        midpoint = int(pred.get("midpoint") or round((low + high) / 2))
        truth = row["true_kcal"]
        results.append({
            "id": item_id,
            "chain": row["chain"],
            "archetype": row["archetype"],
            "item_name": row["item_name"],
            "band": pred.get("band", "unknown"),
            "covered": low <= truth <= high,
            "rel_width": (high - low) / midpoint if midpoint else float("inf"),
            "midpoint_err": abs(midpoint - truth) / truth,
            "truth": truth,
            "low": low,
            "high": high,
            "midpoint": midpoint,
        })

    def coverage(subset: list[dict]) -> float | None:
        return sum(r["covered"] for r in subset) / len(subset) if subset else None

    by_band = {b: [r for r in results if r["band"] == b]
               for b in ("high", "medium", "low")}

    cov_high, cov_low = coverage(by_band["high"]), coverage(by_band["low"])
    separation = (cov_high - cov_low) if (cov_high is not None and cov_low is not None) else None

    total_scoreable = len(answered) + len(suppressed)

    return {
        "results": results,
        "by_band": by_band,
        "missing": missing,
        "suppressed": suppressed,
        "coverage": coverage(results),
        "coverage_high": cov_high,
        "coverage_medium": coverage(by_band["medium"]),
        "coverage_low": cov_low,
        "median_width": statistics.median(r["rel_width"] for r in results) if results else None,
        "median_midpoint_err": statistics.median(r["midpoint_err"] for r in results) if results else None,
        "band_separation": separation,
        "answered_rate": len(answered) / total_scoreable if total_scoreable else None,
        "over_wide": sum(1 for r in results if r["rel_width"] > 1.2),
    }


def fmt(value: float | None, kind: str) -> str:
    if value is None:
        return "  n/a"
    if kind == "pct":
        return f"{value:>5.0%}"
    if kind == "pts":
        return f"{value * 100:>4.0f}p"
    return f"{value:>5.2f}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("predictions", type=Path)
    ap.add_argument("items", type=Path, nargs="?", default=Path("items.csv"))
    ap.add_argument("--split", default="test",
                    help="splits to score: one name, several separated by commas "
                         "(train,val), or 'all' (default: test)")
    ap.add_argument("--worst", type=int, default=8,
                    help="how many worst-miss rows to list (default: 8)")
    args = ap.parse_args()

    split = None if args.split == "all" else args.split
    bench = load_benchmark(args.items, split)
    if not bench:
        print("\n  Nothing to score: no verified rows in this split.")
        print("  Run the verification pass first - see README.md.\n")
        return 1

    preds = load_predictions(args.predictions)
    m = compute(bench, preds)

    label = args.split if split else "all splits"
    print(f"\n  Benchmark: {len(bench)} verified item(s) in {label}")
    print(f"  Answered {len(m['results'])}   suppressed {len(m['suppressed'])}"
          f"   no prediction {len(m['missing'])}")

    if m["missing"]:
        shown = ", ".join(m["missing"][:5])
        more = f" (+{len(m['missing']) - 5} more)" if len(m["missing"]) > 5 else ""
        print(f"  missing: {shown}{more}")

    if not m["results"]:
        print("\n  No answered items - nothing to measure.\n")
        return 1

    print("\n  EXIT BAR")
    print(f"    {'metric':<24}{'value':>8}{'bar':>10}   result")
    failures = 0
    for label_, key, comparison, threshold, kind in EXIT_BAR:
        value = m[key]
        if value is None:
            verdict = "n/a"
        else:
            ok = value >= threshold if comparison == ">=" else value <= threshold
            verdict = "PASS" if ok else "FAIL"
            failures += 0 if ok else 1
        bar = f"{comparison} {fmt(threshold, kind).strip()}"
        print(f"    {label_:<24}{fmt(value, kind):>8}{bar:>10}   {verdict}")

    print("\n  DIAGNOSTICS")
    print(f"    {'median midpoint error':<24}{fmt(m['median_midpoint_err'], 'pct'):>8}")
    print(f"    {'low-band coverage':<24}{fmt(m['coverage_low'], 'pct'):>8}")
    print(f"    {'ranges wider than +/-60%':<24}{m['over_wide']:>8}")
    for band in ("high", "medium", "low"):
        rows = m["by_band"][band]
        if rows:
            width = statistics.median(r["rel_width"] for r in rows)
            print(f"    {'  ' + band + ' band':<24}{len(rows):>5} items"
                  f"   median width {width:.2f}")

    misses = sorted((r for r in m["results"] if not r["covered"]),
                    key=lambda r: -r["midpoint_err"])[:args.worst]
    if misses:
        print(f"\n  WORST MISSES (of {sum(1 for r in m['results'] if not r['covered'])})")
        for r in misses:
            print(f"    {r['item_name'][:38]:<38} {r['band']:<7}"
                  f" said {r['low']}-{r['high']}   actual {r['truth']}"
                  f"   off {r['midpoint_err']:.0%}")

    print(f"\n  {'ALL METRICS PASS' if failures == 0 else f'{failures} metric(s) below bar'}\n")
    return 0 if failures == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
