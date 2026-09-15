"""Calorie estimates for menu items from a pre-trained LLM (Claude).

No training, no retrieval corpus: one API call per item. The model supplies the
food knowledge; this module supplies the parts a model can't be trusted to do on
its own:

  * a strict JSON schema, so every response is a range + band + rationale;
  * a minimum range width per confidence band, enforced in code, so a "low"
    confidence answer can't ship a suspiciously tight range;
  * a ceiling, past which the item is suppressed instead of answered.

Usage as a library:

    from estimator import Estimator, MenuItem
    est = Estimator().estimate(MenuItem(name="Chicken Alfredo", restaurant="Olive Garden",
                                        cuisine="italian", price_tier=2))

Usage from the shell:

    python estimator.py "Chicken Alfredo" --restaurant "Olive Garden" --cuisine italian --tier 2
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field

DEFAULT_MODEL = "claude-opus-5"

# Minimum half-width of the range, as a fraction of the midpoint, per band.
MIN_HALF_WIDTH = {"high": 0.12, "medium": 0.20, "low": 0.33}
MIN_TOTAL_WIDTH_KCAL = 80
# Past this half-width the estimate is too vague to act on; suppress it.
MAX_HALF_WIDTH = 0.60

SYSTEM = """You estimate calorie counts for restaurant menu items.

You are given what a diner would see: the item name, any description the menu \
prints, and context about the restaurant. Estimate from how this kind of dish is \
typically prepared and portioned at this kind of restaurant.

- Give a range, never a single figure. An honestly wide range beats a narrow wrong one.
- State the served portion you assumed, in grams.
- Band: "high" when the dish is standard and unambiguous, "medium" when preparation \
or portion is somewhat uncertain, "low" when the menu text leaves a lot unknown.
- Rationale: at most two plain sentences naming what drove the estimate. Mention only \
details that appear in the input; do not invent ingredients or restaurant facts."""

SCHEMA = {
    "type": "object",
    "properties": {
        "low": {"type": "integer"},
        "high": {"type": "integer"},
        "midpoint": {"type": "integer"},
        "portion_assumption_g": {"type": "integer"},
        "band": {"type": "string", "enum": ["high", "medium", "low"]},
        "rationale": {"type": "string"},
    },
    "required": ["low", "high", "midpoint", "portion_assumption_g", "band", "rationale"],
    "additionalProperties": False,
}


@dataclass
class MenuItem:
    name: str
    description: str = ""
    restaurant: str = ""
    cuisine: str = ""
    price_tier: int | None = None
    section: str = ""
    sourcing_signals: list[str] = field(default_factory=list)

    def to_prompt(self) -> str:
        lines = [f"Item: {self.name}",
                 f"Description: {self.description or '(none printed on the menu)'}"]
        if self.section:
            lines.append(f"Menu section: {self.section}")
        lines.append(f"Restaurant: {self.restaurant or 'unknown'}")
        lines.append(f"Cuisine: {self.cuisine or 'unknown'}")
        lines.append(f"Price tier: {'$' * self.price_tier if self.price_tier else 'unknown'}")
        if self.sourcing_signals:
            lines.append(f"Sourcing language on the menu: {', '.join(self.sourcing_signals)}")
        return "\n".join(lines)


@dataclass
class Estimate:
    low: int
    high: int
    midpoint: int
    band: str
    portion_assumption_g: int
    rationale: str
    model: str
    widened: bool = False  # True when the model's range was narrower than its band allows


class Suppressed(Exception):
    """The item can't be given a range worth acting on."""


def enforce_width(low: int, high: int, midpoint: int, band: str) -> tuple[int, int, bool]:
    """Widen a range to its band's minimum; raise Suppressed past the ceiling."""
    if low > high:
        low, high = high, low
    if midpoint <= 0:
        raise Suppressed("model returned a non-positive midpoint")
    midpoint = min(max(midpoint, low), high)

    required = max(MIN_HALF_WIDTH[band] * midpoint, MIN_TOTAL_WIDTH_KCAL / 2)
    new_low = max(0, min(low, round(midpoint - required)))
    new_high = max(high, round(midpoint + required))

    if (new_high - new_low) / 2 / midpoint > MAX_HALF_WIDTH:
        raise Suppressed("range wider than +/-60% of the midpoint")
    return new_low, new_high, (new_low, new_high) != (low, high)


class Estimator:
    def __init__(self, model: str = DEFAULT_MODEL, effort: str | None = None, client=None):
        import anthropic  # imported here so enforce_width is usable without the SDK

        self.model = model
        self.effort = effort
        self.client = client or anthropic.Anthropic()

    def estimate(self, item: MenuItem) -> Estimate:
        output_config: dict = {"format": {"type": "json_schema", "schema": SCHEMA}}
        if self.effort:
            output_config["effort"] = self.effort

        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            system=SYSTEM,
            messages=[{"role": "user", "content": item.to_prompt()}],
            output_config=output_config,
            # If a safety classifier declines, re-run on Anthropic's recommended fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )

        if response.stop_reason == "refusal":
            raise Suppressed("the model declined this request")
        if response.stop_reason == "max_tokens":
            raise RuntimeError("response was cut off at max_tokens")

        text = "".join(block.text for block in response.content if block.type == "text")
        data = json.loads(text)
        low, high, widened = enforce_width(data["low"], data["high"], data["midpoint"], data["band"])

        return Estimate(
            low=low,
            high=high,
            midpoint=min(max(data["midpoint"], low), high),
            band=data["band"],
            portion_assumption_g=data["portion_assumption_g"],
            rationale=data["rationale"],
            model=response.model,  # the model that actually served it, fallback included
            widened=widened,
        )


def main() -> int:
    ap = argparse.ArgumentParser(description="Estimate calories for one menu item.")
    ap.add_argument("name")
    ap.add_argument("--description", default="")
    ap.add_argument("--restaurant", default="")
    ap.add_argument("--cuisine", default="")
    ap.add_argument("--tier", type=int, choices=[1, 2, 3, 4])
    ap.add_argument("--section", default="")
    ap.add_argument("--sourcing", nargs="*", default=[])
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--json", action="store_true", help="print raw JSON")
    args = ap.parse_args()

    item = MenuItem(args.name, args.description, args.restaurant, args.cuisine,
                    args.tier, args.section, args.sourcing)
    try:
        est = Estimator(args.model, args.effort).estimate(item)
    except Suppressed as exc:
        print(f"No estimate: {exc}")
        return 2

    if args.json:
        print(json.dumps(asdict(est), indent=2))
    else:
        print(f"\n  {item.name}")
        print(f"  {est.low}-{est.high} kcal   ({est.band} confidence, ~{est.portion_assumption_g} g)")
        print(f"  {est.rationale}")
        if est.widened:
            print("  (range widened to the minimum for this confidence band)")
        print(f"  Estimated range, not a nutrition label. Model: {est.model}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
