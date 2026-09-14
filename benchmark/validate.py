"""Validate the benchmark set and report its stratification.

The benchmark is a ruler. A ruler with wrong markings is worse than no ruler, so
this script's main job is refusing to let unverified numbers become labels:

  * `true_kcal` may only be filled in from a published source, and a row that has
    one must also carry `source_url` and `retrieved_on`.
  * `candidate_kcal_UNVERIFIED` is a tripwire for whoever does the verification
    pass, never a label. If the published figure lands far from it, you probably
    matched the wrong menu item.

Run:  python validate.py [items.csv]
Exit code is non-zero if any row is malformed.
"""

from __future__ import annotations

import csv
import sys
from collections import Counter
from datetime import date
from pathlib import Path

REQUIRED_COLUMNS = [
    "id", "split", "chain", "cuisine", "price_tier", "archetype",
    "item_name", "menu_description", "listed_price_usd",
    "candidate_kcal_UNVERIFIED", "true_kcal", "source_row", "source_url",
    "retrieved_on",
    "verified",
]

SPLITS = {"train", "val", "test"}
VERIFIED = {"yes", "no"}
ARCHETYPES = {
    "bowl", "burrito", "taco", "salad", "soup", "pasta", "sandwich", "burger",
    "fried", "grilled", "braised", "side", "bread", "dessert", "shake",
    "shareable", "breakfast",
}

# How far a published value may sit from the recalled candidate before we assume
# the wrong item was matched rather than that the candidate was simply off.
TRIPWIRE_RATIO = 0.25


def load(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing = set(REQUIRED_COLUMNS) - set(reader.fieldnames or [])
        if missing:
            sys.exit(f"FATAL: items.csv is missing columns: {sorted(missing)}")
        return list(reader)


def check(rows: list[dict[str, str]]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    seen_ids: set[str] = set()

    for n, row in enumerate(rows, start=2):  # header is line 1
        rid = row["id"].strip()
        where = f"line {n} ({rid or 'MISSING ID'})"

        if not rid:
            errors.append(f"{where}: empty id")
        elif rid in seen_ids:
            errors.append(f"{where}: duplicate id")
        seen_ids.add(rid)

        if row["split"] not in SPLITS:
            errors.append(f"{where}: split must be one of {sorted(SPLITS)}")
        if row["archetype"] not in ARCHETYPES:
            errors.append(f"{where}: unknown archetype {row['archetype']!r}")
        if row["verified"] not in VERIFIED:
            errors.append(f"{where}: verified must be yes or no")
        if not row["item_name"].strip():
            errors.append(f"{where}: empty item_name")

        tier = row["price_tier"].strip()
        if tier not in {"1", "2", "3", "4"}:
            errors.append(f"{where}: price_tier must be 1-4, got {tier!r}")

        true_kcal = row["true_kcal"].strip()
        verified = row["verified"].strip()

        # The core invariant, in both directions.
        if verified == "yes":
            if not true_kcal:
                errors.append(f"{where}: marked verified but true_kcal is empty")
            if not row["source_url"].strip():
                errors.append(f"{where}: marked verified but has no source_url")
            if not row["source_row"].strip():
                errors.append(
                    f"{where}: marked verified but has no source_row - record the "
                    "exact line the figure came from, size variant included"
                )
            if not row["retrieved_on"].strip():
                errors.append(f"{where}: marked verified but has no retrieved_on")
        elif true_kcal:
            errors.append(
                f"{where}: has a true_kcal but is not marked verified - "
                "a label must come from a checked source"
            )

        if true_kcal:
            try:
                actual = int(true_kcal)
            except ValueError:
                errors.append(f"{where}: true_kcal {true_kcal!r} is not an integer")
            else:
                if actual <= 0:
                    errors.append(f"{where}: true_kcal must be positive")
                candidate = row["candidate_kcal_UNVERIFIED"].strip()
                if candidate.isdigit() and int(candidate) > 0:
                    drift = abs(actual - int(candidate)) / int(candidate)
                    if drift > TRIPWIRE_RATIO:
                        warnings.append(
                            f"{where}: published {actual} kcal is {drift:.0%} from the "
                            f"candidate {candidate} - confirm you matched the right item"
                        )

        if row["retrieved_on"].strip():
            try:
                date.fromisoformat(row["retrieved_on"].strip())
            except ValueError:
                errors.append(f"{where}: retrieved_on must be ISO date (YYYY-MM-DD)")

    return errors, warnings


def report(rows: list[dict[str, str]]) -> None:
    verified = [r for r in rows if r["verified"] == "yes"]

    print(f"\n  {len(rows)} rows, {len(verified)} verified "
          f"({len(verified) / len(rows):.0%})")

    def table(title: str, counts: Counter, width: int = 26) -> None:
        print(f"\n  {title}")
        for key, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
            bar = "#" * count
            print(f"    {str(key):<{width}} {count:>3}  {bar}")

    table("by split", Counter(r["split"] for r in rows), 10)
    table("by chain", Counter(r["chain"] for r in rows), 20)
    table("by archetype", Counter(r["archetype"] for r in rows), 12)

    has_desc = sum(1 for r in rows if r["menu_description"].strip())
    print(f"\n  with a menu description  {has_desc:>3} / {len(rows)}")
    print("    (description richness feeds the confidence score, so both")
    print("     populated and bare rows have to be represented)")

    if not verified:
        print("\n  Calorie spread unavailable - no verified labels yet.")
        print("  Until rows are verified this set cannot score anything.")
        return

    values = sorted(int(r["true_kcal"]) for r in verified)
    bands = Counter()
    for v in values:
        lo = (v // 250) * 250
        bands[f"{lo}-{lo + 249}"] += 1
    table("verified calorie spread", bands, 12)
    print(f"\n    min {values[0]}   median {values[len(values) // 2]}   max {values[-1]}")


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "items.csv")
    if not path.exists():
        sys.exit(f"FATAL: {path} not found")

    rows = load(path)
    if not rows:
        sys.exit("FATAL: benchmark is empty")

    errors, warnings = check(rows)

    for w in warnings:
        print(f"  WARN  {w}")
    for e in errors:
        print(f"  ERROR {e}")

    report(rows)

    if errors:
        print(f"\n  {len(errors)} error(s). Benchmark is not usable as-is.\n")
        return 1

    unverified = sum(1 for r in rows if r["verified"] != "yes")
    if unverified:
        print(f"\n  Structurally valid, but {unverified} row(s) still need a")
        print("  published source before they can be scored. See README.md.\n")
    else:
        print("\n  Valid, and every row carries a verified label.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
