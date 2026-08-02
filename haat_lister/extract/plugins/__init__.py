"""Site-specific extraction, for the shops the generic path gets wrong.

The generic extractors read JSON-LD, microdata, OpenGraph and then the DOM, and
on a well-built storefront that is enough. It is not always enough: a shop may
put its real price in a JavaScript object, its gallery behind a slider that
renders one `<img>`, or its size chart in a table nobody could guess the shape
of. A plugin is where the operator encodes what they know about one such shop.

    A plugin runs LAST and its values win.

That is deliberate. A plugin exists precisely because the generic path got
something wrong, so deferring to the generic answer would defeat the point. The
safeguard is not restraint, it is accountability: every field a plugin supplies
is re-stamped `source=plugin` no matter what the plugin claims, so `review.csv`
names exactly which cells came from which plugin, and one line of grep finds
every row a plugin touched.

What a plugin CANNOT do, enforced in `apply_result` rather than documented and
hoped for:

  - write `gi_region`. There is no such field on the record, and a plugin naming
    it gets an error that says why.
  - claim a value came from JSON-LD. Sources are overwritten, not trusted.
  - clear the row's status, its notes, or its provenance. A plugin adds; it
    cannot launder.
  - bypass Tier 1. Image candidates a plugin supplies are validated by the same
    nine predicates as any other URL.

Loading operator plugins is loading operator Python, which is why it happens
only when `extraction.plugins_dir` is set, and why every file loaded is logged
by full path.
"""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from selectolax.parser import HTMLParser

from ...config import AppConfig
from ...models import FieldSource, FieldValue, ProductRecord
from ...utils.logging import get_logger
from ..structured import StructuredData

log = get_logger(__name__)


@dataclass
class PluginContext:
    """Everything a plugin gets. Read-only in practice: nothing here is the record.

    A plugin sees the page, not the row. It cannot inspect what other stages
    decided, which keeps plugins from developing opinions about each other.
    """

    url: str
    final_url: str
    html: str
    dom: HTMLParser
    structured: StructuredData
    config: AppConfig


@dataclass
class PluginResult:
    """What a plugin found. Every part optional; an empty result is fine."""

    fields: dict[str, FieldValue[Any]] = field(default_factory=dict)
    image_candidates: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    # The price the page states, in the shop's own currency. Separate from
    # `fields` because it is not a CSV column: it feeds `price.strategy`, so a
    # plugin recovers the fact and the operator's policy still decides what
    # reaches price_inr. A plugin cannot set an INR price directly.
    source_price: float | None = None
    source_currency: str | None = None

    @property
    def is_empty(self) -> bool:
        return not (
            self.fields
            or self.image_candidates
            or self.notes
            or self.flags
            or self.source_price is not None
        )


@runtime_checkable
class Plugin(Protocol):
    """Two methods. `matches` must be cheap -- it runs on every page."""

    name: str

    def matches(self, url: str, html: str) -> bool: ...

    def extract(self, ctx: PluginContext) -> PluginResult: ...


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

# A channel, not a store. `register()` is called at module import time, which is
# the only moment a plugin module can announce itself, so something global has
# to catch it -- but it is drained by whoever triggered the import. A permanent
# global list would mean an operator plugin loaded during one run reappearing as
# a built-in in the next, which is exactly the kind of spooky action this file
# is meant not to have.
_PENDING: list[Plugin] = []
_BUILTINS: list[Plugin] | None = None


def register(plugin: Plugin) -> Plugin:
    """Decorator or plain call. Idempotent by name, so a module imported twice
    (which `plugins_dir` loading can cause) does not double-apply."""
    _PENDING[:] = [p for p in _PENDING if p.name != plugin.name]
    _PENDING.append(plugin)
    return plugin


def _drain() -> list[Plugin]:
    found = list(_PENDING)
    _PENDING.clear()
    return found


class PluginRegistry:
    """The set of plugins for one run. Built once, then asked per page."""

    def __init__(self, plugins: Iterable[Plugin] = ()) -> None:
        self.plugins: list[Plugin] = list(plugins)

    def __len__(self) -> int:
        return len(self.plugins)

    def match(self, url: str, html: str = "") -> Plugin | None:
        """First match wins. Operator plugins are ordered ahead of built-ins, so
        a shop-specific plugin beats the generic Shopify one for that shop."""
        for plugin in self.plugins:
            try:
                if plugin.matches(url, html):
                    return plugin
            except Exception:  # noqa: BLE001 -- a broken matcher is not a dead run
                log.exception("Plugin %s raised while matching %s; skipping it", plugin.name, url)
        return None


def _load_builtins() -> list[Plugin]:
    """Import every module in this package so its `register` calls run.

    Cached: Python imports each module once, so a second call would drain an
    empty channel and conclude there are no built-ins.
    """
    global _BUILTINS
    if _BUILTINS is None:
        # Not cleared first: a built-in module may already have been imported
        # (a test importing the example directly does exactly this), in which
        # case its registration is already sitting in the channel and
        # `import_module` below will be a no-op.
        for module in pkgutil.iter_modules(__path__):
            if not module.name.startswith("_"):
                importlib.import_module(f"{__name__}.{module.name}")
        _BUILTINS = _drain()
    return list(_BUILTINS)


def _load_directory(directory: Path) -> list[Plugin]:
    """Import operator plugins from a directory, loudly.

    This executes the operator's own Python, which is the point -- but it is
    also why it never happens implicitly. Every file is logged by full path so a
    surprising extraction can be traced to a file on disk.
    """
    if not directory.exists():
        log.warning("extraction.plugins_dir %s does not exist; no plugins loaded", directory)
        return []

    _PENDING.clear()
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        log.info("Loading plugin from %s", path)
        spec = importlib.util.spec_from_file_location(f"haat_lister_plugin_{path.stem}", path)
        if spec is None or spec.loader is None:
            log.warning("Could not load %s as a plugin; skipping", path)
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:  # noqa: BLE001 -- one bad plugin file is not a dead run
            log.exception("Plugin file %s failed to import; skipping it", path)
            continue

    return _drain()


def build_registry(config: AppConfig, root: Path) -> PluginRegistry:
    """Operator plugins first, then built-ins."""
    builtins = _load_builtins()
    operator: list[Plugin] = []
    if config.extraction.plugins_dir:
        operator = _load_directory(root / config.extraction.plugins_dir)

    registry = PluginRegistry([*operator, *builtins])
    log.debug(
        "Plugin registry: %s", ", ".join(p.name for p in registry.plugins) or "none"
    )
    return registry


# ---------------------------------------------------------------------------
# Applying a result
# ---------------------------------------------------------------------------


class PluginError(Exception):
    """A plugin asked for something it is not allowed to have."""


def apply_result(record: ProductRecord, result: PluginResult, plugin_name: str) -> list[str]:
    """Fold a plugin's findings onto the record. Returns the field names it set.

    Raises rather than ignoring an unknown field name: a typo that silently did
    nothing would leave a plugin author debugging their selectors when the
    problem is a misspelt key.
    """
    writable = record.field_values()
    applied: list[str] = []

    for name, value in result.fields.items():
        if name == "gi_region":
            raise PluginError(
                "A plugin may not set gi_region. A GI tag is an Indian government "
                "certification and haat makes it a seller declaration, so it is left blank on "
                "every automated row without exception. Put what you found in a note instead."
            )
        if name not in writable:
            raise PluginError(
                f"Plugin {plugin_name!r} tried to set unknown field {name!r}. "
                f"Writable fields: {', '.join(sorted(writable))}."
            )
        if not isinstance(value, FieldValue):
            raise PluginError(
                f"Plugin {plugin_name!r} returned a bare value for {name!r}. Wrap it: "
                f"FieldValue.found(value, FieldSource.PLUGIN, Confidence.HIGH)."
            )
        if not value.is_present:
            continue

        # Stamped, not trusted. A plugin cannot dress its guess up as JSON-LD.
        stamped = value.model_copy(update={"source": FieldSource.PLUGIN})
        setattr(record, name, stamped)
        applied.append(name)

    if result.source_price is not None:
        record.source_price = result.source_price
        if result.source_currency:
            record.source_currency = result.source_currency
        applied.append("source_price")

    if result.image_candidates:
        # Prepended: the plugin knows this shop's gallery better than the
        # generic ranker does. They still face Tier 1 like every other URL.
        existing = [u for u in record.image_candidates if u not in result.image_candidates]
        record.image_candidates = [*result.image_candidates, *existing]

    for note in result.notes:
        record.note(note)
    for flag in result.flags:
        record.flag(flag)

    if applied or result.image_candidates:
        record.note(
            f"Plugin {plugin_name!r} supplied: "
            f"{', '.join(applied) or 'no fields'}"
            + (
                f", and {len(result.image_candidates)} image candidate(s)"
                if result.image_candidates
                else ""
            )
            + ". Those cells show source=plugin in review.csv."
        )

    return applied
