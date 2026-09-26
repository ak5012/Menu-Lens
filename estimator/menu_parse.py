"""Turn pasted menu text into a list of dishes, without calling a model.

Menus pasted from a website or a PDF usually look like one of these:

    STARTERS
    Burrata - heirloom tomato, basil oil  $14
    Cacio e Pepe: tonnarelli, pecorino, black pepper ... 19
    Osso Buco
    Braised veal shank, saffron risotto, gremolata

So each line is a section heading, a dish ("name - description"), a dish name alone,
or the description of the dish just above it. Prices are dropped: they carry no
calorie information beyond the restaurant's price tier.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_ITEMS = 100

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
        if not raw.strip() or PRICE_ONLY.match(raw):
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
            continue
        else:
            name, description = line, ""

        if not 2 <= len(name) <= 80 or name.lower() in seen:
            continue
        seen.add(name.lower())
        items.append(ParsedItem(name=name, description=description[:300], section=section))
        if len(items) >= MAX_ITEMS:
            break
    return items
