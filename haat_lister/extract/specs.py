"""Label/value pairs from a product page's spec area.

Storefronts express the same table three ways -- a real <table>, a <dl>, or
"Label: value" lines in a list -- so all three are collected into one flat
mapping. Shared by dimensions.py and variants.py; keeping it here stops those
two growing near-identical parsers.
"""

from __future__ import annotations

import re

from selectolax.parser import HTMLParser

from .title import normalise_text

_LABEL_VALUE_LINE = re.compile(r"^\s*([A-Za-z][\w\s/()-]{1,40}?)\s*[:：]\s*(.+?)\s*$")


def _clean_label(text: str) -> str:
    return normalise_text(text).rstrip(":：").strip().lower()


def spec_pairs(dom: HTMLParser) -> dict[str, str]:
    """Every label -> value pair we can find, lowercased labels.

    First occurrence of a label wins. Later duplicates are usually a related-
    products block repeating the layout.
    """
    pairs: dict[str, str] = {}

    def add(label: str, value: str) -> None:
        key = _clean_label(label)
        value = normalise_text(value)
        if key and value and key not in pairs:
            pairs[key] = value

    # <table><tr><th>Weight</th><td>350 g</td></tr>
    for row in dom.css("tr"):
        cells = row.css("th, td")
        if len(cells) >= 2:
            add(cells[0].text(strip=True), cells[1].text(separator=" ", strip=True))

    # <dl><dt>Weight</dt><dd>350 g</dd>
    for definition_list in dom.css("dl"):
        terms = definition_list.css("dt")
        values = definition_list.css("dd")
        for term, value in zip(terms, values, strict=False):
            add(term.text(strip=True), value.text(separator=" ", strip=True))

    # "Weight: 350 g" as a line inside a list item or paragraph.
    for node in dom.css("li, p, span, div"):
        text = node.text(separator="\n", strip=True)
        if not text or len(text) > 200:
            continue
        for line in text.split("\n"):
            if match := _LABEL_VALUE_LINE.match(line):
                add(match.group(1), match.group(2))

    return pairs


def find_by_labels(pairs: dict[str, str], labels: list[str]) -> tuple[str, str] | None:
    """First matching label, and its value.

    Exact matches are preferred over substring ones so that a page carrying both
    "Weight" and "Shipping weight" resolves each to the right cell.
    """
    lowered = [label.lower() for label in labels]

    for label in lowered:
        if label in pairs:
            return label, pairs[label]

    for label in lowered:
        for key, value in pairs.items():
            if label in key:
                return key, value
    return None
