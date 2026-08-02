"""Unit parsing and conversion.

haat wants integer grams and integer centimetres. Sources give pounds, ounces,
inches, millimetres and every spelling of each.

Rounding happens once, at the end, using banker's-rounding-free `round()` on a
positive value -- so 349.5 g becomes 350 g rather than 349. Converting then
rounding (never rounding then converting) keeps a 2.5 inch item at 6 cm rather
than 5 cm.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Multipliers to grams.
WEIGHT_TO_GRAMS: dict[str, float] = {
    "g": 1.0,
    "gm": 1.0,
    "gms": 1.0,
    "gram": 1.0,
    "grams": 1.0,
    "kg": 1000.0,
    "kgs": 1000.0,
    "kilo": 1000.0,
    "kilos": 1000.0,
    "kilogram": 1000.0,
    "kilograms": 1000.0,
    "mg": 0.001,
    "lb": 453.592,
    "lbs": 453.592,
    "pound": 453.592,
    "pounds": 453.592,
    "oz": 28.3495,
    "ounce": 28.3495,
    "ounces": 28.3495,
}

# Multipliers to centimetres.
LENGTH_TO_CM: dict[str, float] = {
    "cm": 1.0,
    "cms": 1.0,
    "centimetre": 1.0,
    "centimetres": 1.0,
    "centimeter": 1.0,
    "centimeters": 1.0,
    "mm": 0.1,
    "millimetre": 0.1,
    "millimetres": 0.1,
    "millimeter": 0.1,
    "millimeters": 0.1,
    "m": 100.0,
    "metre": 100.0,
    "metres": 100.0,
    "meter": 100.0,
    "meters": 100.0,
    "in": 2.54,
    "inch": 2.54,
    "inches": 2.54,
    '"': 2.54,
    "ft": 30.48,
    "foot": 30.48,
    "feet": 30.48,
    "'": 30.48,
}

# schema.org QuantitativeValue unitCode (UN/CEFACT), which JSON-LD uses.
UNIT_CODES: dict[str, str] = {
    "GRM": "g",
    "KGM": "kg",
    "MGM": "mg",
    "LBR": "lb",
    "ONZ": "oz",
    "CMT": "cm",
    "MMT": "mm",
    "MTR": "m",
    "INH": "in",
    "FOT": "ft",
}

_NUMBER = re.compile(r"(\d+(?:[.,]\d+)?)")
_UNIT = re.compile(r"([a-zA-Z]+|\"|')")


@dataclass(frozen=True)
class Measurement:
    value: float
    unit: str


def normalise_unit(unit: str) -> str:
    """Accept `KGM`, `Kg`, `kilograms` and friends as the same thing."""
    cleaned = unit.strip().rstrip(".")
    if cleaned.upper() in UNIT_CODES:
        return UNIT_CODES[cleaned.upper()]
    return cleaned.lower()


def parse_number(text: str) -> float | None:
    """First number in a string, tolerating `1,5` and `1.5`."""
    match = _NUMBER.search(text)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", "."))
    except ValueError:
        return None


def parse_measurement(text: str, default_unit: str | None = None) -> Measurement | None:
    """`"350 g"`, `"1.2kg"`, `"12 inches"` -> value plus a normalised unit."""
    if not text:
        return None
    number_match = _NUMBER.search(text)
    if number_match is None:
        return None
    try:
        value = float(number_match.group(1).replace(",", "."))
    except ValueError:
        return None

    unit_match = _UNIT.search(text[number_match.end() :])
    unit = normalise_unit(unit_match.group(1)) if unit_match else (default_unit or "")
    return Measurement(value=value, unit=unit)


def to_grams(measurement: Measurement | None) -> int | None:
    if measurement is None:
        return None
    factor = WEIGHT_TO_GRAMS.get(normalise_unit(measurement.unit))
    if factor is None or measurement.value <= 0:
        return None
    return int(round(measurement.value * factor))


def to_cm(measurement: Measurement | None) -> int | None:
    if measurement is None:
        return None
    factor = LENGTH_TO_CM.get(normalise_unit(measurement.unit))
    if factor is None or measurement.value <= 0:
        return None
    return int(round(measurement.value * factor))


# ---------------------------------------------------------------------------
# Dimension triples
# ---------------------------------------------------------------------------

# Splitting on the separator beats one big regex here: "x" is a letter, so any
# pattern that also matches a unit will happily swallow the separator itself.
_SEPARATOR = re.compile(r"\s*[x×✕*]\s*", re.IGNORECASE)

# Axis letters some sources prefix onto each number: "L70 x W50 x H2".
_AXIS_PREFIX = re.compile(r"\b([LWHD])\s*[:=]?\s*(?=\d)", re.IGNORECASE)

# `depth` is the front-to-back measure, which is what haat calls length -- so
# "H x W x D" maps cleanly onto height, width, length.
_AXIS_NAMES = {"l": "length", "w": "width", "h": "height", "d": "length"}


def parse_dimension_triple(text: str) -> tuple[list[int], list[str] | None] | None:
    """`"70 x 50 x 2 cm"` -> ([70, 50, 2], None).

    Returns the values in cm, plus the axis order the SOURCE stated if it
    labelled its numbers. A stated order that is not length, width, height is
    reported so the caller can normalise and flag rather than trusting position.

    A stated order with a repeated axis (e.g. "L x W x D", where both map to
    length) is reported as unstated: we would rather admit we cannot tell than
    silently drop one of the numbers.
    """
    if not text:
        return None

    parts = [part for part in _SEPARATOR.split(text) if part.strip()]
    if len(parts) < 2:
        return None

    measurements = [parse_measurement(part) for part in parts[:3]]
    if any(m is None for m in measurements):
        return None

    # A trailing unit applies to every number: "70 x 50 x 2 cm".
    known = [m.unit for m in measurements if m is not None and m.unit in LENGTH_TO_CM]
    fallback = known[-1] if known else "cm"

    values: list[int] = []
    for measurement in measurements:
        assert measurement is not None
        unit = measurement.unit if measurement.unit in LENGTH_TO_CM else fallback
        converted = to_cm(Measurement(measurement.value, unit))
        if converted is None:
            return None
        values.append(converted)

    axes = [_AXIS_NAMES[m.group(1).lower()] for m in _AXIS_PREFIX.finditer(text)]
    stated = axes if len(axes) == len(values) and len(set(axes)) == len(axes) else None
    return values, stated
