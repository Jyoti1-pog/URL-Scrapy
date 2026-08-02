"""Weight and dimensions.

Two rules from the spec drive everything here:

  - Never fabricate from category averages. A missing dimension is blank and
    flagged; haat needs these for duties, and a plausible invented number is a
    customs problem rather than a convenience.
  - Prefer product weight over shipping weight. Shipping weight includes
    packaging, so using it is a real answer but a worse one, and the row says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from selectolax.parser import HTMLParser

from ..config import ExtractionConfig
from ..models import Confidence, FieldSource, FieldValue, IntField
from ..utils.units import (
    Measurement,
    normalise_unit,
    parse_dimension_triple,
    parse_measurement,
    to_cm,
    to_grams,
)
from .specs import find_by_labels, spec_pairs
from .structured import StructuredData, scalar

# Exported so `pipeline` can register these as retractable gap notes rather
# than matching on duplicated literals. See ProductRecord.note_gap.
NO_WEIGHT_NOTE = "No weight found. haat requires weight_g, and duties depend on it."

AXES = ("length_cm", "width_cm", "height_cm")


def no_dimensions_note(missing: list[str]) -> str:
    return (
        f"No value found for {', '.join(missing)}. haat requires all three; they are left "
        "blank rather than estimated from similar products."
    )


@dataclass
class DimensionResult:
    weight_g: IntField = field(default_factory=FieldValue)
    length_cm: IntField = field(default_factory=FieldValue)
    width_cm: IntField = field(default_factory=FieldValue)
    height_cm: IntField = field(default_factory=FieldValue)
    # `notes` are expected gaps; `flags` are judgement calls to overturn.
    notes: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


def _quantitative(value: object) -> Measurement | None:
    """schema.org QuantitativeValue: {"value": 350, "unitCode": "GRM"}."""
    if isinstance(value, dict):
        raw = scalar(value.get("value") or value.get("@value"))
        unit = value.get("unitCode") or value.get("unitText") or ""
        if raw is None:
            return None
        try:
            return Measurement(float(raw), normalise_unit(str(unit)))
        except ValueError:
            return None
    if text := scalar(value):
        return parse_measurement(text)
    return None


def _weight(
    sd: StructuredData, pairs: dict[str, str], cfg: ExtractionConfig
) -> tuple[IntField, list[str], list[str]]:
    """Returns (field, notes, flags)."""
    notes: list[str] = []
    flags: list[str] = []

    if sd.product and sd.product_source:
        if grams := to_grams(_quantitative(sd.product.get("weight"))):
            return FieldValue.found(grams, sd.product_source, Confidence.HIGH), notes, flags

    # Shipping-weight labels are excluded here so that product weight wins
    # whenever a page carries both.
    if found := find_by_labels(pairs, cfg.weight_labels):
        label, value = found
        if not any(s.lower() in label for s in cfg.shipping_weight_labels):
            if grams := to_grams(parse_measurement(value)):
                return (
                    FieldValue.found(grams, FieldSource.SPEC_TABLE, Confidence.HIGH),
                    notes,
                    flags,
                )

    if found := find_by_labels(pairs, cfg.shipping_weight_labels):
        _, value = found
        if grams := to_grams(parse_measurement(value)):
            flags.append(
                "weight_g came from the shipping weight, which includes packaging. "
                "Replace it with the product weight if you have one."
            )
            return (
                FieldValue.found(
                    grams,
                    FieldSource.SPEC_TABLE,
                    Confidence.LOW,
                    "Shipping weight, not product weight.",
                ),
                notes,
                flags,
            )

    notes.append(NO_WEIGHT_NOTE)
    return FieldValue.missing("No weight on the page."), notes, flags


def _explicit_axes(
    sd: StructuredData, pairs: dict[str, str], cfg: ExtractionConfig
) -> dict[str, IntField]:
    """Individually labelled length/width/height, which need no order guessing."""
    out: dict[str, IntField] = {}

    schema_keys = {"length_cm": "length", "width_cm": "width", "height_cm": "height"}
    if sd.product and sd.product_source:
        for target, key in schema_keys.items():
            if cm := to_cm(_quantitative(sd.product.get(key))):
                out[target] = FieldValue.found(cm, sd.product_source, Confidence.HIGH)
        # schema.org calls the third axis `depth`.
        if "height_cm" not in out and (cm := to_cm(_quantitative(sd.product.get("depth")))):
            out["height_cm"] = FieldValue.found(cm, sd.product_source, Confidence.MEDIUM)

    label_sets = {
        "length_cm": cfg.length_labels,
        "width_cm": cfg.width_labels,
        "height_cm": cfg.height_labels,
    }
    for target, labels in label_sets.items():
        if target in out:
            continue
        if found := find_by_labels(pairs, labels):
            if cm := to_cm(parse_measurement(found[1])):
                out[target] = FieldValue.found(cm, FieldSource.SPEC_TABLE, Confidence.HIGH)

    return out


def extract_dimensions(
    sd: StructuredData, dom: HTMLParser, cfg: ExtractionConfig
) -> DimensionResult:
    pairs = spec_pairs(dom)
    result = DimensionResult()

    result.weight_g, weight_notes, weight_flags = _weight(sd, pairs, cfg)
    result.notes.extend(weight_notes)
    result.flags.extend(weight_flags)

    axes = _explicit_axes(sd, pairs, cfg)
    for name, value in axes.items():
        setattr(result, name, value)

    if len(axes) < 3 and (found := find_by_labels(pairs, cfg.dimension_labels)):
        _, raw = found
        parsed = parse_dimension_triple(raw)
        if parsed:
            values, stated_axes = parsed
            order = ["length_cm", "width_cm", "height_cm"]

            if stated_axes:
                # The source told us which axis is which, e.g. "H70 x W50 x D2".
                mapping = dict(zip(stated_axes, values, strict=False))
                normalised = [mapping.get(a.replace("_cm", ""), None) for a in order]
                if stated_axes != ["length", "width", "height"]:
                    result.flags.append(
                        f"Source stated dimensions as {' x '.join(stated_axes)}; "
                        "normalised to length x width x height. Check the mapping."
                    )
                confidence = Confidence.MEDIUM
            else:
                normalised = [*values, None, None][:3]
                result.flags.append(
                    f"Dimensions '{raw}' were unlabelled; mapped in source order to "
                    "length x width x height. Check the mapping."
                )
                confidence = Confidence.LOW

            for name, centimetres in zip(order, normalised, strict=True):
                if name not in axes and centimetres is not None:
                    setattr(
                        result,
                        name,
                        FieldValue.found(centimetres, FieldSource.SPEC_TABLE, confidence),
                    )

    missing = [
        name
        for name in ("length_cm", "width_cm", "height_cm")
        if not getattr(result, name).is_present
    ]
    if missing:
        result.notes.append(no_dimensions_note(missing))

    return result
