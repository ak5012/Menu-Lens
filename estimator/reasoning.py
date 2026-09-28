"""The reasoned pipeline: understand the restaurant first, then reason through each dish.

Two model requests per menu instead of one per step per dish, because free tiers count
requests, not how much the model thinks:

  1. Restaurant profile, once per restaurant and cached: what kind of place this is
     (fast food, diner, fine dining...), its cuisine and cooking style, where it is and
     what that means for portion sizes, its price level, and whether it's likely a chain.
  2. Dishes, up to 20 per request, with the profile as context. For each dish the
     model must write down its likely ingredients, cooking method, portion basis and
     main calorie drivers *before* the numbers, so the range follows from the reasoning.

The same range rules as the direct pipeline apply afterwards (estimator.py).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from estimator import SCHEMA, SYSTEM, MenuItem
from providers import Provider, ProviderError, ProviderRateLimited

VENUE_TYPES = ["fast_food", "fast_casual", "cafe_bakery", "diner", "family_style",
               "casual_dining", "fine_dining", "bar_pub", "buffet", "food_truck",
               "dessert_shop", "unknown"]
PORTION_NORMS = ["small", "standard", "large", "very_large"]
PRICE_LEVELS = ["budget", "moderate", "upscale", "luxury", "unknown"]

PROFILE_SYSTEM = """You profile restaurants so a nutrition estimator can size their dishes.

From the restaurant's name, any cuisine, location and price information, and a sample \
of its menu, describe:
- venue_type: the kind of place. Use "unknown" only when nothing points either way.
- cuisine: the specific cuisine or regional style (e.g. "Sichuan Chinese", not just "Chinese").
- cooking_style: one sentence on the fats, sauces and methods typical of this kind of \
place and cuisine (e.g. "wok-fried with generous oil; sauces thickened with starch").
- region: the country and region if known from the input, else "unknown".
- portion_norm: how large servings here typically are compared with a standard US \
restaurant serving. Consider the venue type, price level and region (US and chain \
portions run large; many European and East Asian portions run smaller; family-style \
dishes are for sharing).
- price_level: from the price tier or menu prices.
- likely_chain: true if this is probably a chain with many locations.
- notes: at most two sentences on anything else that affects calories here.
Base everything on the input and general knowledge of this kind of restaurant. Do not \
invent specific facts about this particular restaurant."""

PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "venue_type": {"type": "string", "enum": VENUE_TYPES},
        "cuisine": {"type": "string"},
        "cooking_style": {"type": "string"},
        "region": {"type": "string"},
        "portion_norm": {"type": "string", "enum": PORTION_NORMS},
        "price_level": {"type": "string", "enum": PRICE_LEVELS},
        "likely_chain": {"type": "boolean"},
        "notes": {"type": "string"},
    },
    "required": ["venue_type", "cuisine", "cooking_style", "region", "portion_norm",
                 "price_level", "likely_chain", "notes"],
    "additionalProperties": False,
}

REASONED_SYSTEM = SYSTEM + """

You are given a profile of the restaurant, then several numbered dishes from its menu. \
For each dish, work through these steps in order, then give the range:
1. likely_ingredients: the main components, from the description; where the menu is \
silent, what this dish usually contains at this kind of restaurant.
2. cooking_method: how it is most likely cooked (fried, grilled, braised, cream-based...).
3. portion_basis: the serving size you assume and why, using the profile's portion norm, \
the menu section, and any size words or price.
4. calorie_drivers: the few components that contribute most calories, including oils, \
butter, sauces and included sides.
Estimate each dish on its own. Ingredients you inferred rather than read on the menu \
are assumptions: keep them in the reasoning fields and widen the range for them, but \
don't present them as facts in the rationale. Return one entry for every dish, with \
its number in "index"."""

REASONING_FIELDS = ["likely_ingredients", "cooking_method", "portion_basis", "calorie_drivers"]

# Reasoning fields come first so the numbers are written after, and follow from, them.
REASONED_SCHEMA = {
    "type": "object",
    "properties": {
        "estimates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    **{f: {"type": "string"} for f in REASONING_FIELDS},
                    **SCHEMA["properties"],
                },
                "required": ["index", *REASONING_FIELDS, *SCHEMA["required"]],
                "additionalProperties": False,
            },
        },
    },
    "required": ["estimates"],
    "additionalProperties": False,
}


@dataclass
class RestaurantProfile:
    venue_type: str = "unknown"
    cuisine: str = ""
    cooking_style: str = ""
    region: str = "unknown"
    portion_norm: str = "standard"
    price_level: str = "unknown"
    likely_chain: bool = False
    notes: str = ""

    @classmethod
    def from_answer(cls, data: dict) -> "RestaurantProfile":
        profile = cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})
        if profile.venue_type not in VENUE_TYPES:
            profile.venue_type = "unknown"
        if profile.portion_norm not in PORTION_NORMS:
            profile.portion_norm = "standard"
        if profile.price_level not in PRICE_LEVELS:
            profile.price_level = "unknown"
        profile.likely_chain = bool(profile.likely_chain)
        return profile

    def to_lines(self) -> list[str]:
        return [
            "Restaurant profile:",
            f"  Venue type: {self.venue_type.replace('_', ' ')}",
            f"  Cuisine: {self.cuisine or 'unknown'}",
            f"  Cooking style: {self.cooking_style or 'unknown'}",
            f"  Region: {self.region or 'unknown'}",
            f"  Typical portions: {self.portion_norm.replace('_', ' ')}",
            f"  Price level: {self.price_level}",
            f"  Likely a chain: {'yes' if self.likely_chain else 'no'}",
        ] + ([f"  Notes: {self.notes}"] if self.notes else [])

    def to_dict(self) -> dict:
        return asdict(self)


def profile_key(item: MenuItem) -> str:
    return json.dumps(item.restaurant_key())


def profile_prompt(items: list[MenuItem]) -> str:
    sample = [f"- {i.name}" + (f" (${i.price:.2f})" if i.price else "")
              + (f": {i.description}" if i.description else "") for i in items[:20]]
    return "\n".join(items[0].restaurant_lines()) + "\n\nMenu sample:\n" + "\n".join(sample)


def build_profile(provider: Provider, items: list[MenuItem]) -> RestaurantProfile:
    """Step 1. A garbled answer gives an 'unknown' profile rather than failing the dishes;
    rate limits and outages still raise, since step 2 would hit them too."""
    try:
        data, _ = provider.generate(PROFILE_SYSTEM, profile_prompt(items), PROFILE_SCHEMA)
    except ProviderRateLimited:
        raise
    except ProviderError as exc:
        if "invalid JSON" not in str(exc):
            raise
        return RestaurantProfile()
    return RestaurantProfile.from_answer(data if isinstance(data, dict) else {})


def dishes_prompt(profile: RestaurantProfile, items: list[MenuItem]) -> str:
    dishes = []
    for n, item in enumerate(items, 1):
        first, *rest = item.dish_lines()
        dishes.append(f"{n}. {first}" + "".join(f"\n   {line}" for line in rest))
    return ("\n".join(profile.to_lines() + items[0].restaurant_lines())
            + "\n\nDishes:\n" + "\n".join(dishes))
