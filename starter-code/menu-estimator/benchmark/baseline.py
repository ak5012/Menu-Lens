"""The ungrounded baseline - Phase 1, step 2.

One Claude call per item. No retrieval, no corpus, no deterministic scorer, no
validators. Just the menu text and the restaurant context, exactly what a user
would see.

This exists to produce the number that the rest of the product has to beat. If
an ungrounded call already clears the section 10 exit bar, the ~1,400 hand-curated
Tier B rows have to justify themselves against a baseline that cost a day. If it
lands well short, the retrieval thesis is proven and the corpus spend is
de-risked. Either result is worth more than the day it takes.

Note what this deliberately does NOT do: the model picks its own confidence
band here. That is the design the estimation layer rejects - so the band
separation this run produces is also the measurement of how much the
deterministic scorer needs to buy.

Run:  export ANTHROPIC_API_KEY=...        (or: ant auth login)
      python baseline.py --split test -o predictions-baseline.jsonl
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Literal

try:
    import anthropic
    from pydantic import BaseModel, Field
except ImportError:
    sys.exit("Missing dependencies. Run: pip install -r requirements.txt")


# USD per million tokens, for the run-cost report.
PRICING = {
    "claude-opus-5":    (5.00, 25.00),
    "claude-opus-4-8":  (5.00, 25.00),
    "claude-sonnet-5":  (2.00, 10.00),
    "claude-haiku-4-5": (1.00,  5.00),
    "claude-fable-5-1": (10.00, 50.00),
}

SYSTEM = """You estimate calorie counts for restaurant menu items.

You are given only what a diner would see: the item name, whatever description \
the menu prints, and basic context about the restaurant. You have no nutrition \
database and no reference values. Estimate from what you know about how this \
kind of dish is prepared and portioned at this kind of restaurant.

Rules:
- Return a range, never a single figure. A range that is honestly wide beats a \
narrow one that is wrong.
- State the portion you assumed, in grams.
- Choose the confidence band that reflects how much the menu text and context \
actually told you: high when the dish is unambiguous and standard, low when \
portion or preparation is genuinely unclear.
- The rationale is two sentences at most, plain language, naming the specific \
things that drove the estimate."""


class Estimate(BaseModel):
    low: int = Field(description="Lower bound of the calorie range, kcal")
    high: int = Field(description="Upper bound of the calorie range, kcal")
    midpoint: int = Field(description="Best single estimate within the range, kcal")
    portion_assumption_g: int = Field(description="Assumed served portion, grams")
    band: Literal["high", "medium", "low"]
    rationale: str


def build_prompt(row: dict[str, str]) -> str:
    """Menu-visible fields only.

    The label lives in `true_kcal` and the recalled hint in
    `candidate_kcal_UNVERIFIED`; neither may reach the model. Leaking either
    turns coverage into a measurement of nothing.
    """
    tier = "$" * int(row["price_tier"])
    lines = [
        f"Item: {row['item_name']}",
        f"Description: {row['menu_description'] or '(none printed on the menu)'}",
        f"Restaurant: {row['chain'].replace('-', ' ').title()}",
        f"Cuisine: {row['cuisine']}",
        f"Price tier: {tier}",
    ]
    if row["listed_price_usd"].strip():
        lines.append(f"Listed price: ${row['listed_price_usd']}")
    return "\n".join(lines)


def estimate_one(client, model: str, row: dict[str, str], max_tokens: int) -> dict:
    response = client.messages.parse(
        model=model,
        max_tokens=max_tokens,
        system=SYSTEM,
        messages=[{"role": "user", "content": build_prompt(row)}],
        output_format=Estimate,
    )

    if response.stop_reason == "refusal":
        return {"id": row["id"], "suppressed": True, "reason": "refusal"}

    est = response.parsed_output
    if est is None:
        return {"id": row["id"], "suppressed": True, "reason": "unparsed"}

    return {
        "id": row["id"],
        "low": est.low,
        "high": est.high,
        "midpoint": est.midpoint,
        "band": est.band,
        "portion_assumption_g": est.portion_assumption_g,
        "rationale": est.rationale,
        "_usage": {
            "input": response.usage.input_tokens,
            "output": response.usage.output_tokens,
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--items", type=Path, default=Path("items.csv"))
    ap.add_argument("-o", "--out", type=Path, default=Path("predictions-baseline.jsonl"))
    ap.add_argument("--model", default="claude-opus-5",
                    help="default claude-opus-5 - the strongest ungrounded case, "
                         "which is the honest thing for retrieval to have to beat")
    ap.add_argument("--split", default="test", help="'all' to run every split")
    ap.add_argument("--limit", type=int, help="stop after N items (smoke test)")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--max-tokens", type=int, default=2000)
    args = ap.parse_args()

    with args.items.open(newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh)
                if args.split == "all" or r["split"] == args.split]
    if args.limit:
        rows = rows[:args.limit]
    if not rows:
        sys.exit(f"No rows in split {args.split!r}")

    unverified = sum(1 for r in rows if r["verified"] != "yes")
    print(f"\n  {len(rows)} item(s), model {args.model}, concurrency {args.concurrency}")
    if unverified:
        print(f"  {unverified} of them still lack a verified label - this run will")
        print("  produce predictions, but score.py will ignore those rows.")

    client = anthropic.Anthropic()
    lock = threading.Lock()
    done = 0
    predictions: list[dict] = []
    failures: list[tuple[str, str]] = []

    def run(row: dict[str, str]) -> None:
        nonlocal done
        try:
            result = estimate_one(client, args.model, row, args.max_tokens)
        except anthropic.APIStatusError as exc:
            with lock:
                failures.append((row["id"], f"{exc.status_code} {exc.__class__.__name__}"))
                done += 1
            return
        except anthropic.APIConnectionError as exc:
            with lock:
                failures.append((row["id"], f"connection: {exc}"))
                done += 1
            return
        with lock:
            predictions.append(result)
            done += 1
            print(f"  [{done:>3}/{len(rows)}] {row['item_name'][:42]:<42} "
                  f"{result.get('low', '-')}-{result.get('high', '-')}"
                  f"  {result.get('band', 'suppressed')}")

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(run, row) for row in rows]
        for future in as_completed(futures):
            future.result()

    in_tok = sum(p.get("_usage", {}).get("input", 0) for p in predictions)
    out_tok = sum(p.get("_usage", {}).get("output", 0) for p in predictions)

    order = {r["id"]: i for i, r in enumerate(rows)}
    predictions.sort(key=lambda p: order.get(p["id"], 0))
    with args.out.open("w", encoding="utf-8") as fh:
        for pred in predictions:
            fh.write(json.dumps({k: v for k, v in pred.items()
                                 if not k.startswith("_")}) + "\n")

    print(f"\n  Wrote {len(predictions)} prediction(s) to {args.out}")
    if failures:
        print(f"  {len(failures)} failed:")
        for item_id, why in failures[:10]:
            print(f"    {item_id}: {why}")

    print(f"  Tokens: {in_tok:,} in / {out_tok:,} out")
    if args.model in PRICING:
        rate_in, rate_out = PRICING[args.model]
        cost = in_tok / 1e6 * rate_in + out_tok / 1e6 * rate_out
        per_item = cost / len(predictions) if predictions else 0
        print(f"  Cost:   ${cost:.3f} total, ${per_item:.4f} per item")

    print(f"\n  Next:  python score.py {args.out} --split {args.split}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
