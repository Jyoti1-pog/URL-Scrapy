"""§5 -- one clock per URL, and an honest account of where it went.

THE DEFECT. Every timeout in this program was per-attempt, and the attempts
nest: three A-rungs at 8s, a browser at 30s, a retry on top of each. Nothing
anywhere held the total. A single URL could occupy the better part of a minute
while every individual limit was being respected, and the operator watching a
200-link job had no way to know which URL was doing it or why.

So the budget is a DEADLINE, not a sum of limits. It is set once when the row
starts and every subsequent wait is clipped to what is left. A rung that would
take 8 seconds gets 3 if 3 is all there is, and gets skipped if there is none.

WHY IT REPORTS ITS SPEND. `21s` on its own is a complaint. `21s -- fetch 19.8s,
parse 0.2s, idle 1.0s` is a diagnosis: the shop is slow, we are not, and no
amount of tuning concurrency will help. The three-way split is the smallest one
that distinguishes "their fault", "our fault" and "our politeness".

WHAT `idle` IS, and it is not padding. It is time this row spent deliberately
waiting: the jittered per-domain delay, a rung backoff, a `Retry-After` we were
asked to honour. It belongs in the total because the operator waited through it,
and it belongs apart from `fetch` because it is the one part we chose.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum


class Spend(StrEnum):
    """The three ways a row's time can go. Deliberately only three.

    A finer breakdown (dns, tls, first byte, decode) is available from the
    attempt ledger for one URL and is noise across two hundred. This split is
    the one an operator can act on: slow shop, slow us, or our own politeness.
    """

    FETCH = "fetch"
    PARSE = "parse"
    IDLE = "idle"


class BudgetExhausted(Exception):
    """The row ran out of time. Carries the account, because the number alone
    does not tell anybody what to change."""

    def __init__(self, budget: UrlBudget) -> None:
        super().__init__(budget.report())
        self.budget = budget


@dataclass
class UrlBudget:
    """One row's clock. Created per URL, consulted before every wait.

    Not a context manager over the whole row: the row's phases are not nested
    and the interesting question is always "how much is left", which a manager
    would hide behind its own lifetime.
    """

    limit_s: float
    started: float = field(default_factory=time.monotonic)
    spent: dict[str, float] = field(default_factory=lambda: {s.value: 0.0 for s in Spend})
    # Set when a `Retry-After` was honoured, so the report can say that the wait
    # was asked for rather than chosen -- a 30-second idle looks like a bug
    # until you know the site requested it.
    retry_after_s: float = 0.0

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    @property
    def remaining(self) -> float:
        """Never negative. A caller asking for a timeout wants a number it can
        pass to httpx, and a negative one is an error there rather than here."""
        return max(0.0, self.limit_s - self.elapsed)

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0.0

    def check(self) -> None:
        """Raise if there is nothing left. Called before starting a new attempt,
        never in the middle of one -- abandoning a response we have already paid
        for wastes the request AND the row."""
        if self.exhausted:
            raise BudgetExhausted(self)

    def clip(self, wanted_s: float) -> float:
        """The smaller of what this step wants and what the row has left.

        This is the whole mechanism. A rung configured for 8 seconds gets 8 when
        the row is fresh and 1.4 when it is nearly out, so the LAST rung is the
        one that gets squeezed rather than the row overrunning.
        """
        return min(wanted_s, self.remaining)

    @contextmanager
    def spending(self, on: Spend):
        """Time a phase and attribute it. Attributes even when it raises.

        The `finally` matters more than the happy path: a timeout is exactly the
        case where an operator wants to know how long we waited, and a naive
        implementation loses precisely that number.

        Deliberate waiting recorded INSIDE the block is subtracted from it. A
        rung backoff happens between two requests and is therefore inside the
        fetch phase by the clock and outside it by the meaning; counting it in
        both made the parts sum to more than the whole, which is the one thing
        a breakdown must never do.
        """
        at = time.monotonic()
        idle_before = self.spent[Spend.IDLE.value]
        try:
            yield self
        finally:
            elapsed = time.monotonic() - at
            if on is not Spend.IDLE:
                elapsed -= self.spent[Spend.IDLE.value] - idle_before
            self.spent[on.value] += max(0.0, elapsed)

    def waited(self, seconds: float, *, requested: bool = False) -> None:
        """Record a deliberate wait. `requested` marks a `Retry-After`."""
        self.spent[Spend.IDLE.value] += seconds
        if requested:
            self.retry_after_s += seconds

    def report(self) -> str:
        """`21s - fetch 19.8s, parse 0.2s, idle 1.0s`.

        Total first because that is the number somebody is angry about, and the
        split immediately after because it is the answer.
        """
        parts = ", ".join(f"{name} {self.spent[name]:.1f}s" for name in (s.value for s in Spend))
        line = f"{self.elapsed:.0f}s - {parts}"
        if self.retry_after_s:
            line += f" (of which {self.retry_after_s:.0f}s was a Retry-After we honoured)"
        return line


def budget_for(settings, override_s: float | None = None) -> UrlBudget:  # noqa: ANN001
    """One place that knows the default, so CLI, API and batch cannot disagree."""
    return UrlBudget(limit_s=override_s or settings.config.fetch.url_timeout_s)
