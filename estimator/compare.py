"""Run the same benchmark through several models and put the scores side by side.

    python compare.py --list-models                       # what this key can use
    python compare.py --models gemini-3.6-flash,gemini-3.5-flash-lite
    python compare.py --models auto --split val           # a quick, cheap sweep

Picking a model is a tuning decision, so the sweep runs on the train and val
splits and leaves `test` untouched. Once a winner is chosen, confirm it on the
held-out split with one run:

    python bench.py --hide-names --split test -o preds-hidden.jsonl --resume
    python ..\\benchmark\\score.py preds-hidden.jsonl ..\\benchmark\\items.csv

Each model writes its own predictions file under runs/, so nothing is mixed and
an interrupted sweep continues where it stopped: re-run the same command.
"""

from __future__ import annotations

import argparse
import re
import statistics
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BENCHMARK_DIR = HERE.parent / "benchmark"
ITEMS = BENCHMARK_DIR / "items.csv"
RUNS = HERE / "runs"

sys.path.insert(0, str(BENCHMARK_DIR))
import score  # noqa: E402  (the scorer is the single source of truth for metrics)

# Models that answer text but can't do what MenuLens needs, or aren't worth benchmarking.
SKIP_PATTERNS = re.compile(
    r"embedding|aqa|imagen|veo|image|vision|tts|audio|live|learnlm|gemma|"
    r"thinking-exp|robotics|computer-use",
    re.I,
)


def slug(model: str) -> str:
    return re.sub(r"[^a-z0-9.+-]+", "-", model.lower()).strip("-")


def list_models(provider: str) -> list[str]:
    """Ask the provider which models this key can actually use."""
    if provider != "gemini":
        sys.exit("  --list-models / --models auto currently only works for gemini.")
    from google import genai

    client = genai.Client()
    names = []
    for m in client.models.list():
        name = (m.name or "").removeprefix("models/")
        actions = getattr(m, "supported_actions", None) or []
        if actions and "generateContent" not in actions:
            continue
        if not name.startswith("gemini") or SKIP_PATTERNS.search(name):
            continue
        names.append(name)
    return sorted(set(names))


def run_one(model: str, args, out: Path) -> int:
    """Run the benchmark for one model. Returns bench.py's exit code."""
    cmd = [sys.executable, str(HERE / "bench.py"),
           "--provider", args.provider, "--model", model,
           "--split", args.split, "-o", str(out), "--resume"]
    if args.hide_names:
        cmd.append("--hide-names")
    if args.limit:
        cmd += ["--limit", str(args.limit)]
    if args.rpm is not None:
        cmd += ["--rpm", str(args.rpm)]
    print("\n" + "=" * 78)
    print(f"  MODEL: {model}")
    print("=" * 78)
    return subprocess.run(cmd, cwd=HERE).returncode


def score_one(out: Path, split: str) -> dict | None:
    if not out.exists() or not out.read_text(encoding="utf-8").strip():
        return None
    bench = score.load_benchmark(ITEMS, None if split == "all" else split)
    preds = score.load_predictions(out)
    m = score.compute(bench, preds)
    if not m["results"]:
        return None
    seconds = [p["seconds"] for p in preds.values() if p.get("seconds")]
    m["median_seconds"] = statistics.median(seconds) if seconds else None
    m["n_scored"] = len(m["results"])
    m["n_bench"] = len(bench)
    m["failures"] = sum(
        1 for _, key, comparison, threshold, _ in score.EXIT_BAR
        if m.get(key) is not None
        and not (m[key] >= threshold if comparison == ">=" else m[key] <= threshold)
    )
    return m


def pct(v: float | None) -> str:
    return "  n/a" if v is None else f"{v:>4.0%}"


def table(rows: list[tuple[str, dict]], split: str, hide_names: bool) -> str:
    """The comparison, as a markdown table (also readable in a terminal)."""
    lines = ["| Model | Scored | Covered | High | Med | Width | Answered | Typical miss | Sec |",
             "|---|---|---|---|---|---|---|---|---|"]
    for model, m in rows:
        width = "n/a" if m["median_width"] is None else f"{m['median_width']:.2f}"
        secs = "n/a" if m["median_seconds"] is None else f"{m['median_seconds']:.1f}"
        lines.append(
            f"| `{model}` | {m['n_scored']}/{m['n_bench']} | {pct(m['coverage'])} | "
            f"{pct(m['coverage_high'])} | {pct(m['coverage_medium'])} | {width} | "
            f"{pct(m['answered_rate'])} | {pct(m['median_midpoint_err'])} | {secs} |")
    lines.append("| **Target** | - | >=80% | >=90% | >=75% | <=0.45 | >=85% | - | - |")
    label = "hidden names" if hide_names else "named dishes"
    return f"**Split: {split} - {label}**\n\n" + "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="auto",
                    help="comma-separated model ids, or 'auto' to sweep everything the key allows")
    ap.add_argument("--provider", default="gemini")
    ap.add_argument("--split", default="train,val",
                    help="default train,val - keeps the test split clean for the final number")
    ap.add_argument("--hide-names", action="store_true", default=True)
    ap.add_argument("--named", dest="hide_names", action="store_false",
                    help="use real chain and dish names (measures memorisation, not estimation)")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--rpm", type=int)
    ap.add_argument("--list-models", action="store_true")
    ap.add_argument("--report", type=Path, default=HERE.parent / "MODEL_COMPARISON.md")
    args = ap.parse_args()

    if args.list_models:
        for name in list_models(args.provider):
            print(" ", name)
        return 0

    if args.models == "auto":
        models = list_models(args.provider)
        print(f"\n  Sweeping {len(models)} model(s): {', '.join(models)}")
    else:
        models = [m.strip() for m in args.models.split(",") if m.strip()]

    RUNS.mkdir(exist_ok=True)
    tag = "hidden" if args.hide_names else "named"
    scored: list[tuple[str, dict]] = []
    skipped: list[tuple[str, str]] = []

    for model in models:
        out = RUNS / f"{slug(args.split)}-{tag}-{slug(model)}.jsonl"
        code = run_one(model, args, out)
        m = score_one(out, args.split)
        if m is None:
            skipped.append((model, f"no usable results (bench.py exit {code})"))
            continue
        scored.append((model, m))

    if not scored:
        print("\n  Nothing could be scored. Check the messages above.\n")
        return 1

    # Best first: coverage decides, then the typical miss.
    scored.sort(key=lambda r: (-(r[1]["coverage"] or 0), r[1]["median_midpoint_err"] or 1))
    report = table(scored, args.split, args.hide_names)
    print("\n\n  COMPARISON\n")
    print(report)

    best, m = scored[0]
    print(f"\n  Most accurate: {best} - {m['coverage']:.0%} covered, "
          f"typical miss {m['median_midpoint_err']:.0%}, "
          f"{m['failures']} metric(s) below bar.")
    if skipped:
        print("\n  Not scored:")
        for model, why in skipped:
            print(f"    {model}: {why}")
    print(f"\n  Confirm the winner on the held-out split:\n"
          f"    python bench.py --hide-names --split test --model {best} "
          f"-o preds-hidden.jsonl --resume\n")

    args.report.write_text(
        "# Model comparison\n\nGenerated by `estimator/compare.py`.\n\n" + report + "\n",
        encoding="utf-8")
    print(f"  Written to {args.report}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
