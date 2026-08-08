"""Which rung a domain answers on, remembered.

The ladder is cheap when it succeeds on the first rung and expensive when it
does not. A catalogue is almost always one shop, so a host that needed HTTP/1.1
for its first URL will need it for the other 199 -- and paying the HTTP/2
failure 200 times is the whole cost of having a ladder at all.

So: when a domain succeeds on a rung other than A1, write that down and start
there next time.

PERSISTED, WITH A TTL. An earlier version kept this in memory only, on the
argument that a hint should not outlive the reason for it. That was wrong twice
over: a resumed batch is a new process and would re-learn everything, and
`profiles --list` -- which §2.6 asks for so the mechanism is visible -- would
always have printed nothing. A per-host rung is stable enough to be worth
keeping for thirty days.

TWO PROPERTIES MAKE THE PERSISTENCE SAFE:

  * a stale hint can never make a working site fail. Starting at A2 only SKIPS
    rungs; if A2 fails the climb continues to A3 exactly as it would have. The
    worst case is wasted time, not a lost row.
  * it ages out. A shop that fixes its HTTP/2 is back on the fast path within
    thirty days rather than never.

The in-process layer stays in front as a cache, so a 200-URL batch does one
lookup per host rather than 200.

`profiles --list` and `--clear` exist for the same reason the SSRF allowlist is
visible: a mechanism that silently changes what the tool does has to be one an
operator can look at.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Settings
from ..store.ledger import Ledger
from ..utils.logging import get_logger
from ..utils.urls import host_of
from .ladder import Rung

log = get_logger(__name__)


# How many whole-ladder failures on one host before later URLs stop climbing.
# More than one, because a single failure is a bad moment and a run of them on
# one host is a decision that host has made.
#
# The default when nobody says otherwise. `FetchConfig.refusals_before_fast_fail`
# is what actually decides, and v5 §5 sets it to 5 -- this constant remains for
# a store built without a config, which is only ever a test.
REFUSALS_BEFORE_FAST_FAIL = 5


@dataclass
class ProfileStore:
    """host -> the rung that worked. Keyed by host, not by origin: a shop's
    edge behaves the same on http and https."""

    rungs: dict[str, Rung] = field(default_factory=dict)
    # host -> consecutive climbs where every rung failed. The other half of the
    # same economy: a catalogue is one shop, so a host that refuses the whole
    # ladder refuses it 200 times, and each of those costs the full per-rung
    # budget three times over. Measured on the hosts that motivated the ladder:
    # ~18 seconds per URL, which is an hour of waiting for a 200-link job to
    # tell an operator what the first link already told them.
    refusals: dict[str, int] = field(default_factory=dict)
    # §5's circuit breaker, as a number rather than a second mechanism. The
    # ladder already counted consecutive whole-climb failures per host and
    # already fast-failed on them; making the threshold configurable is the
    # entire change, because building a breaker beside one would be two things
    # deciding the same question.
    threshold: int = REFUSALS_BEFORE_FAST_FAIL
    # Hosts already looked up in the ledger this run, so a host with no stored
    # profile costs one query rather than one per URL.
    checked: set[str] = field(default_factory=set)

    def get(self, host: str) -> Rung | None:
        return self.rungs.get(host)

    def remember(self, host: str, rung: Rung) -> None:
        # A1 is the default start, so recording it would be noise -- and would
        # stop a later A2 result from being visible in `--list`.
        if rung is Rung.A1:
            self.rungs.pop(host, None)
            return
        if self.rungs.get(host) is not rung:
            log.info("%s answers on %s; later URLs on this host will start there", host, rung.value)
        self.rungs[host] = rung

    def refused(self, host: str) -> int:
        """Record a climb where every rung failed, and return the running count."""
        self.refusals[host] = self.refusals.get(host, 0) + 1
        if self.refusals[host] == self.threshold:
            log.warning(
                "%s has refused every fetch rung %d times; later URLs on this host will fail "
                "immediately rather than re-climbing. Clear it with: haat-lister profiles "
                "--clear %s",
                host,
                self.threshold,
                host,
            )
        return self.refusals[host]

    def answered(self, host: str) -> None:
        """Any success clears the count. A host that comes back is not on a
        blacklist -- the count is about a run, not about a reputation."""
        self.refusals.pop(host, None)

    def is_refusing(self, host: str) -> bool:
        return self.refusals.get(host, 0) >= self.threshold

    def clear(self, host: str | None = None) -> None:
        if host is None:
            self.rungs.clear()
            self.refusals.clear()
        else:
            self.rungs.pop(host, None)
            self.refusals.pop(host, None)


# One store per process. Attached to Settings lazily rather than passed through
# every call: it is a cache, and threading a cache through six signatures buys
# nothing that a module-level lifetime does not already give.
_STORES: dict[int, ProfileStore] = {}


def store_for(settings: Settings) -> ProfileStore:
    key = id(settings)
    if key not in _STORES:
        _STORES[key] = ProfileStore()
    # Refreshed on every access rather than frozen at construction. The store
    # outlives any one config read, and a threshold captured once means a
    # `--url-timeout`-style override set after the first fetch is silently
    # ignored -- which is the kind of bug that only shows up as "the number in
    # the config does nothing".
    _STORES[key].threshold = settings.config.fetch.refusals_before_fast_fail
    return _STORES[key]


def _ledger(settings: Settings) -> Ledger:
    return Ledger(settings.root / settings.config.paths.ledger)


def get_profile(settings: Settings, url: str) -> Rung | None:
    host = host_of(url)
    if not host:
        return None
    store = store_for(settings)
    if (cached := store.get(host)) is not None:
        return cached
    # Not seen this process. Ask the ledger once, then remember the answer --
    # including "nothing", so a host with no profile costs one lookup per run
    # rather than one per URL.
    if host in store.checked:
        return None
    store.checked.add(host)
    try:
        with _ledger(settings) as ledger:
            stored = ledger.rung_for(host)
    except Exception:  # noqa: BLE001 -- a cache miss is not a run-ending event
        return None
    if stored is None:
        return None
    try:
        rung = Rung(stored)
    except ValueError:
        return None
    store.rungs[host] = rung
    return rung


def remember_profile(settings: Settings, url: str, rung: Rung) -> None:
    if not (host := host_of(url)):
        return
    store = store_for(settings)
    before = store.get(host)
    store.remember(host, rung)
    store.answered(host)
    if before is rung:
        return
    try:
        with _ledger(settings) as ledger:
            if rung is Rung.A1:
                ledger.forget_rung(host)
            else:
                ledger.remember_rung(host, rung.value)
    except Exception:  # noqa: BLE001 -- a hint that fails to save is not a failure
        log.debug("could not persist the fetch profile for %s", host)


def record_refusal(settings: Settings, url: str) -> int:
    """One whole-ladder failure. Returns how many in a row this host has had."""
    host = host_of(url)
    return store_for(settings).refused(host) if host else 0


def is_refusing(settings: Settings, url: str) -> bool:
    """True when every rung has failed on this host enough times that climbing
    again is just making the operator wait for the same answer."""
    host = host_of(url)
    return store_for(settings).is_refusing(host) if host else False


def clear_profiles(settings: Settings, host: str | None = None) -> int:
    """Forget what we learned, in memory and on disk. Returns rows removed."""
    store = store_for(settings)
    store.clear(host)
    if host is None:
        store.checked.clear()
    else:
        store.checked.discard(host)
    try:
        with _ledger(settings) as ledger:
            return ledger.forget_rung(host)
    except Exception:  # noqa: BLE001
        return 0


def all_profiles(settings: Settings) -> dict[str, str]:
    """What has been learned, from the ledger -- which is what `--list` means
    across processes."""
    try:
        with _ledger(settings) as ledger:
            stored = {host: rung for host, rung, _ in ledger.all_rungs()}
    except Exception:  # noqa: BLE001
        stored = {}
    stored.update({h: r.value for h, r in store_for(settings).rungs.items()})
    return dict(sorted(stored.items()))
