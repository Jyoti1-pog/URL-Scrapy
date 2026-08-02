"""Currency conversion for `--price-strategy convert` and `markup:N`.

Config-only by design: no live rate provider. That is a deliberate trade -- a
fetcher means a new outbound dependency and a new failure mode on a tool whose
default price strategy is `blank` anyway. Rates live in `config.yaml` with an
explicit `as_of` date, and `config-check` warns when they go stale.

Every converted price records the rate and the date it came from. A price
nobody can trace back to a rate is not a price, it is a rumour.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import FxConfig, PriceConfig
from ..models import Confidence, FieldSource, FieldValue, IntField, PriceStrategy


@dataclass
class FxResult:
    price_inr: IntField = field(default_factory=FieldValue)
    rate_used: float | None = None
    rate_as_of: str | None = None
    notes: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


def rate_for(currency: str, cfg: FxConfig) -> float | None:
    if currency.upper() == "INR":
        return 1.0
    return cfg.rates_to_inr.get(currency.upper())


def convert(
    amount: float | None,
    currency: str | None,
    price_cfg: PriceConfig,
    fx_cfg: FxConfig,
) -> FxResult:
    """Apply `convert` or `markup:N`. Never guesses a rate it does not have."""
    result = FxResult()

    if price_cfg.strategy not in (PriceStrategy.CONVERT, PriceStrategy.MARKUP):
        return result

    if amount is None or currency is None:
        result.flags.append(
            f"--price-strategy {price_cfg.strategy.value} was requested but the page gave no "
            f"{'amount' if amount is None else 'currency'}; price_inr is blank."
        )
        return result

    rate = rate_for(currency, fx_cfg)
    if rate is None:
        result.flags.append(
            f"No FX rate configured for {currency}. price_inr left blank rather than converted "
            "at a guessed rate. Add it to config.yaml -> fx.rates_to_inr."
        )
        return result

    inr = amount * rate
    note = f"Converted {amount} {currency} at {rate} (as of {fx_cfg.as_of or 'unknown date'})."

    if price_cfg.strategy is PriceStrategy.MARKUP:
        percent = price_cfg.markup_percent
        if percent is None:
            result.flags.append(
                "--price-strategy markup was requested without a percentage; price_inr is blank."
            )
            return result
        inr *= 1 + percent / 100
        note += f" Marked up {percent}%."

    result.price_inr = FieldValue.found(
        int(round(inr)), FieldSource.FX_CONVERTED, Confidence.MEDIUM, note
    )
    result.rate_used = rate
    result.rate_as_of = str(fx_cfg.as_of) if fx_cfg.as_of else None

    result.flags.append(
        f"price_inr was CONVERTED, not set by the maker. {note} haat asks for the maker's own "
        "INR price -- confirm this is the number you want to sell at."
    )
    if fx_cfg.as_of is None:
        result.flags.append("The FX rate has no as_of date, so its age cannot be judged.")

    return result
