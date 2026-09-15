"""Calorie estimates for menu items from a pre-trained LLM.

No training, no retrieval corpus: one API call per item. Which model answers
(Gemini, Claude, or a free mock) is chosen in providers.py. The model supplies the
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
    python estimator.py "Chicken Alfredo" --provider mock      # free, fake numbers
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field

from providers import (PROVIDERS, Provider, ProviderAuthError, ProviderError,
                       ProviderModelUnavailable, ProviderRateLimited, Suppressed, get_provider)

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
    def __init__(self, provider: Provider | str | None = None, model: str | None = None):
        self.provider = provider if isinstance(provider, Provider) else get_provider(provider, model)

    def estimate(self, item: MenuItem) -> Estimate:
        data, served_by = self.provider.generate(SYSTEM, item.to_prompt(), SCHEMA)
        try:
            low, high, midpoint, band = (int(data["low"]), int(data["high"]),
                                         int(data["midpoint"]), data["band"])
            portion, rationale = int(data["portion_assumption_g"]), str(data["rationale"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError(f"model answer is missing or has malformed fields: {data!r}") from exc
        if band not in MIN_HALF_WIDTH:
            raise ProviderError(f"model returned an unknown confidence band {band!r}")

        low, high, widened = enforce_width(low, high, midpoint, band)
        return Estimate(
            low=low,
            high=high,
            midpoint=min(max(midpoint, low), high),
            band=band,
            portion_assumption_g=portion,
            rationale=rationale,
            model=served_by,
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
    ap.add_argument("--provider", choices=sorted(PROVIDERS),
                    help="gemini (free tier, default), anthropic (paid), mock (fake, free)")
    ap.add_argument("--model", help="override the provider's default model")
    ap.add_argument("--json", action="store_true", help="print raw JSON")
    args = ap.parse_args()

    item = MenuItem(args.name, args.description, args.restaurant, args.cuisine,
                    args.tier, args.section, args.sourcing)
    try:
        est = Estimator(args.provider, args.model).estimate(item)
    except Suppressed as exc:
        print(f"No estimate: {exc}")
        return 2
    except ProviderAuthError as exc:
        print(f"The API key was rejected or is missing: {exc}")
        return 1
    except ProviderModelUnavailable as exc:
        print(f"That model isn't available to this account; choose another with --model: {exc}")
        return 1
    except ProviderRateLimited as exc:
        print(f"Rate limit or free quota used up; try again later: {exc}")
        return 1

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
