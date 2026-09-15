"""Run the estimator over the benchmark and write predictions for score.py.

    python bench.py --hide-names -o preds-hidden.jsonl
    python bench.py -o preds-named.jsonl
    python ..\\benchmark\\score.py preds-hidden.jsonl ..\\benchmark\\items.csv

--provider picks the model: gemini (free tier, the default), anthropic (paid), or
mock (fake answers, free, for testing the pipeline only - its score means nothing).

--hide-names replaces the chain and its branded dish names with generic ones
("Cheeseburger with Special Sauce" at "a fast-casual american restaurant"). These
chains publish their calorie counts, so the model may have memorised them; the
hidden-name score is the one that predicts accuracy at independent restaurants.

Every result is saved to the output file as soon as it arrives. If a run stops
(Ctrl+C, daily quota, network), re-run the same command with --resume and it picks up
where it left off.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from estimator import Estimator, MenuItem
from providers import (DEFAULT_MODELS, PROVIDERS, ProviderAuthError, ProviderError,
                       ProviderModelUnavailable, ProviderRateLimited, ProviderTimeout, Suppressed)

BENCHMARK = Path(__file__).resolve().parent.parent / "benchmark" / "items.csv"

# Requests per minute to stay under by default. Gemini's free tier is limited per
# minute (check your own limits at https://aistudio.google.com/rate-limit).
DEFAULT_RPM = {"gemini": 8, "anthropic": 0, "mock": 0}
MAX_ATTEMPTS = 4

KEY_HELP = {
    "gemini": """
  1. Create a free API key in Google AI Studio (aistudio.google.com -> Get API key).
  2. Set it in this PowerShell window:
        $env:GEMINI_API_KEY = "paste-your-key-here"
  3. Try two items first:
        python bench.py --hide-names --limit 2 -o smoke.jsonl
""",
    "anthropic": """
  1. Create an API key in the Claude Console (platform.claude.com -> API keys). This is paid.
  2. $env:ANTHROPIC_API_KEY = "sk-ant-api03-...your key..."
""",
}


class Stop(Exception):
    """End the whole run early, with a message for the user."""


def say(text: str = "", end: str = "\n") -> None:
    print(text, end=end, flush=True)


def wait_visibly(seconds: float, reason: str) -> None:
    seconds = max(1, round(seconds))
    for left in range(seconds, 0, -1):
        say(f"\r      {reason}: waiting {left:>3}s ", end="")
        time.sleep(1)
    say("\r" + " " * 60 + "\r", end="")


def build_item(row: dict, hide_names: bool) -> MenuItem:
    # Menu-visible fields only: true_kcal must never reach the model.
    if hide_names:
        return MenuItem(name=row["generic_name"], description=row["generic_description"],
                        restaurant=f"a {row['format']} {row['cuisine']} restaurant",
                        cuisine=row["cuisine"], price_tier=int(row["price_tier"]))
    return MenuItem(name=row["item_name"], description=row["menu_description"],
                    restaurant=row["chain"].replace("-", " ").title(),
                    cuisine=row["cuisine"], price_tier=int(row["price_tier"]))


def estimate_with_retries(est: Estimator, row: dict, hide_names: bool, quiet: bool) -> dict | None:
    """One item, retrying rate limits and timeouts in the open. None means it failed."""
    item = build_item(row, hide_names)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        started = time.monotonic()
        try:
            e = est.estimate(item)
        except Suppressed:
            return {"id": row["id"], "suppressed": True}
        except ProviderRateLimited as exc:
            if exc.daily:
                raise Stop("Daily free quota is used up for this model. Re-run tomorrow with "
                           "--resume, or try another model with --model.") from exc
            if attempt == MAX_ATTEMPTS:
                break
            delay = exc.retry_after or 15 * attempt
            if not quiet:
                wait_visibly(delay, f"rate limited (attempt {attempt}/{MAX_ATTEMPTS})")
            else:
                time.sleep(delay)
            continue
        except ProviderTimeout:
            if attempt == MAX_ATTEMPTS:
                break
            if not quiet:
                say(f"\r      no answer after {time.monotonic() - started:.0f}s, retrying "
                    f"(attempt {attempt + 1}/{MAX_ATTEMPTS})")
            continue
        e_secs = time.monotonic() - started
        return {"id": row["id"], "low": e.low, "high": e.high, "midpoint": e.midpoint,
                "band": e.band, "widened": e.widened, "model": e.model, "seconds": round(e_secs, 1)}
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--items", type=Path, default=BENCHMARK)
    ap.add_argument("-o", "--out", type=Path, default=Path("predictions.jsonl"))
    ap.add_argument("--provider", choices=sorted(PROVIDERS), default="gemini")
    ap.add_argument("--model", help="override the provider's default model")
    ap.add_argument("--split", default="test", help="'all' for every split")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--resume", action="store_true",
                    help="keep results already in the output file and only run the rest")
    ap.add_argument("--concurrency", type=int, default=1,
                    help="parallel requests (keep at 1 for free tiers)")
    ap.add_argument("--rpm", type=int, help="max requests per minute (0 = unlimited)")
    ap.add_argument("--hide-names", action="store_true",
                    help="use generic dish names and no restaurant name")
    args = ap.parse_args()

    with args.items.open(newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh) if args.split == "all" or r["split"] == args.split]
    rows = rows[: args.limit] if args.limit else rows

    done: dict[str, dict] = {}
    if args.resume and args.out.exists():
        for line in args.out.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                done[rec["id"]] = rec
    todo = [r for r in rows if r["id"] not in done]

    try:
        est = Estimator(args.provider, args.model)
    except ProviderAuthError as exc:
        say(f"\n  {exc}.{KEY_HELP.get(args.provider, '')}")
        return 1

    model = args.model or DEFAULT_MODELS[args.provider]
    rpm = DEFAULT_RPM[args.provider] if args.rpm is None else args.rpm
    interval = 60.0 / rpm if rpm > 0 else 0.0
    say(f"\n  {args.provider} / {model}: {len(todo)} item(s) to run"
        + (f", {len(done)} already done" if done else "")
        + (f", one request every {interval:.1f}s" if interval else ""))
    if args.provider == "mock":
        say("  MOCK PROVIDER: these are fake numbers. Any score from this file is meaningless.")
    say("  Results are saved as they arrive. Ctrl+C stops safely; re-run with --resume to continue.\n")

    # Rewrite the file with only the kept results, then append each new one as it lands.
    with args.out.open("w", encoding="utf-8") as out:
        for rec in done.values():
            out.write(json.dumps(rec) + "\n")

    saved, failed, next_start = 0, [], 0.0
    stop_message = None

    def record(rec: dict) -> None:
        nonlocal saved
        with args.out.open("a", encoding="utf-8") as out:
            out.write(json.dumps(rec) + "\n")
        saved += 1

    try:
        if args.concurrency <= 1:
            for i, row in enumerate(todo, 1):
                pause = next_start - time.monotonic()
                if pause > 0:
                    time.sleep(pause)
                next_start = time.monotonic() + interval
                say(f"  [{i:>2}/{len(todo)}] {row['item_name'][:38]:<38} ", end="")
                rec = estimate_with_retries(est, row, args.hide_names, quiet=False)
                if rec is None:
                    failed.append(row["id"]); say("failed after retries")
                elif rec.get("suppressed"):
                    record(rec); say("no estimate (suppressed)")
                else:
                    record(rec); say(f"{rec['low']}-{rec['high']}  {rec['band']:<6} ({rec['seconds']}s)")
        else:
            with ThreadPoolExecutor(args.concurrency) as pool:
                futures = {pool.submit(estimate_with_retries, est, r, args.hide_names, True): r for r in todo}
                for i, fut in enumerate(as_completed(futures), 1):
                    row, rec = futures[fut], fut.result()
                    if rec is None:
                        failed.append(row["id"]); say(f"  [{i:>2}/{len(todo)}] {row['item_name'][:38]:<38} failed")
                    else:
                        record(rec)
                        say(f"  [{i:>2}/{len(todo)}] {row['item_name'][:38]:<38} "
                            + ("no estimate" if rec.get("suppressed") else f"{rec['low']}-{rec['high']}  {rec['band']}"))
    except ProviderAuthError as exc:
        say(f"\n\n  The API key was rejected: {exc}{KEY_HELP.get(args.provider, '')}")
        return 1
    except ProviderModelUnavailable as exc:
        say(f"\n\n  The model {model!r} isn't available to this account, so the run stopped.")
        say(f"  Provider said: {exc}")
        say("  Choose another with --model, e.g.  python bench.py --model gemini-3.6-flash ...")
        return 1
    except Stop as exc:
        stop_message = str(exc)
    except KeyboardInterrupt:
        stop_message = "Stopped by you."

    total = len(done) + saved
    say(f"\n  {total}/{len(rows)} saved in {args.out}"
        + (f" ({saved} new this run)" if done else ""))
    if failed:
        say(f"  {len(failed)} failed: {', '.join(failed[:6])}{' ...' if len(failed) > 6 else ''}")
    if stop_message or failed:
        say(f"  {stop_message + ' ' if stop_message else ''}Re-run the same command with --resume to finish.")
    return 130 if stop_message == "Stopped by you." else 0


if __name__ == "__main__":
    sys.exit(main())
