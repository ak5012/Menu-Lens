"""Turn pasted menu text into a list of dishes, without calling a model.

Menus pasted from a website or a PDF usually look like one of these:

    STARTERS
    Burrata - heirloom tomato, basil oil  $14
    Cacio e Pepe: tonnarelli, pecorino, black pepper ... 19
    Osso Buco
    Braised veal shank, saffron risotto, gremolata

So each line is a section heading, a dish ("name - description"), a dish name alone,
or the description of the dish just above it. Each dish keeps its price (a hint at
portion size), and any calories the menu prints ("650 Cal"), which are shown as listed
instead of estimated. A price or calorie count on a line of its own belongs to the dish
above it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_ITEMS = 100

# "650 Cal", "450-600 cal", "1,050 calories", "320 kcal"
CALORIES = re.compile(r"(\d{1,2},?\d{3}|\d{2,4})\s*(?:[-–]\s*(\d{1,2},?\d{3}|\d{2,4})\s*)?"
                      r"(?:k?cals?|calories)\b\.?", re.I)
PRICE = re.compile(r"(?:[.…·\s]*)(?:[$€£]\s*)?\d{1,3}(?:[.,]\d{2})?\s*$")
BULLET = re.compile(r"^\s*(?:[-*•·]|\d{1,2}[.)])\s+")
# The first separator between a dish name and its description.
SEPARATOR = re.compile(r"\s+[-–—|]\s+|:\s+|\s*—\s*")
PRICE_ONLY = re.compile(r"^\s*(?:[$€£]\s*)?\d{1,3}(?:[.,]\d{2})?\s*$")


@dataclass
class ParsedItem:
    name: str
    description: str = ""
    section: str = ""
    price: float | None = None
    listed_calories: dict | None = None  # {"low": 450, "high": 600}, as printed on the menu


def _calories(line: str) -> tuple[dict | None, str]:
    """Calories printed in the line, and the line without them."""
    m = CALORIES.search(line)
    if not m:
        return None, line
    low = int(m.group(1).replace(",", ""))
    high = int(m.group(2).replace(",", "")) if m.group(2) else low
    rest = re.sub(r"\(\s*\)|\[\s*\]", "", line[:m.start()] + " " + line[m.end():]).strip(" ,|·()")
    return ({"low": low, "high": high} if 10 <= low <= high <= 5000 else None), rest


def _price(line: str) -> float | None:
    m = PRICE.search(line.strip())
    if not m:
        return None
    number = re.search(r"\d{1,3}(?:[.,]\d{2})?", m.group())
    value = float(number.group().replace(",", ".")) if number else 0
    return value if 0 < value <= 10_000 else None


def _clean(line: str) -> str:
    line = BULLET.sub("", line.strip())
    return PRICE.sub("", line).strip(" .…·-–—|:")


def _is_heading(line: str, raw: str) -> bool:
    words = line.split()
    return (0 < len(words) <= 4 and not PRICE.search(raw.strip())
            and (line.isupper() or raw.strip().endswith(":")))


def _looks_like_description(line: str) -> bool:
    return "," in line or line[:1].islower() or len(line.split()) > 6


def parse_menu_text(text: str) -> list[ParsedItem]:
    items: list[ParsedItem] = []
    section = ""
    seen: set[str] = set()

    for raw in text.splitlines():
        calories, raw = _calories(raw)
        price = _price(raw)
        if not raw.strip() or PRICE_ONLY.match(raw):
            # A line holding only a price or a calorie count belongs to the dish above.
            if items:
                items[-1].price = items[-1].price or price
                items[-1].listed_calories = items[-1].listed_calories or calories
            continue
        line = _clean(raw)
        if not re.search(r"[A-Za-z]", line):
            continue
        if _is_heading(line, raw):
            section = line.rstrip(":").title()
            continue

        parts = SEPARATOR.split(line, maxsplit=1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            name, description = parts[0].strip(), parts[1].strip()
        elif items and not items[-1].description and _looks_like_description(line):
            items[-1].description = line  # the description line under a name-only dish
            items[-1].price = items[-1].price or price
            items[-1].listed_calories = items[-1].listed_calories or calories
            continue
        else:
            name, description = line, ""

        if not 2 <= len(name) <= 80 or name.lower() in seen:
            continue
        seen.add(name.lower())
        items.append(ParsedItem(name=name, description=description[:300], section=section,
                                price=price, listed_calories=calories))
        if len(items) >= MAX_ITEMS:
            break
    return items
