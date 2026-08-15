"""Per-domain canonical forms: two links to one product become one row.

The generic pass in `urls.canonicalise` drops fragments, default ports and a
fixed list of tracking parameters. That is not enough for a marketplace. A
single Amazon product link pasted out of a browser carries `_encoding`,
`pd_rd_w`, `content-id`, `pf_rd_p`, `pf_rd_r`, `pd_rd_wg`, `pd_rd_r`, `ref_` and
`th` -- none of which identify the product, all of which differ between two
copies of the same link. Deduping on the generic form means fetching the same
ASIN four times and writing it four times.

RULES ARE DATA. Each one is a row in a table applied by one generic engine, so
adding a marketplace is an edit to `DEFAULT_RULES` or to `config.yaml`, not a
new branch in a chain. That matters because the alternative -- an if/elif per
site -- is exactly the thing that rots into nobody knowing which rule won.

CONSERVATIVE ABOUT WHAT IT THROWS AWAY. A canonical form that drops something
load-bearing silently sends the operator a different product. So a rule only
rewrites a path when the pattern matched, and `keep_query` is spelled out per
site rather than inferred.

WHAT IS DELIBERATELY NOT HERE: `?variant=`. Shopify variants are very often
genuinely separate listings -- a different size at a different price is a
different row on haat -- so collapsing them is opt-in via `--merge-variants` and
never a default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query parameters that select a variant rather than a product. Dropped only
# when the operator says so, because the safe assumption is that a variant is
# its own listing.
VARIANT_PARAMS: tuple[str, ...] = ("variant",)


@dataclass(frozen=True)
class CanonicalRule:
    """One marketplace's idea of "the same product"."""

    name: str
    # Full-match against the lowercased host.
    host_pattern: str
    # THE GUARD, and the only thing standing between this rule and a search
    # page. When set, the whole rule applies only if this matches the path: a
    # category URL on the same host is not a product, and applying `drop_query`
    # to `/s?k=wireless+earbuds` deletes the search itself. Caught by a test,
    # which is the only reason it is a guard rather than just a rewrite.
    path_pattern: str = ""
    # Optional. With a template the path is rebuilt from the captured groups;
    # without one the path is left exactly as found and only the query is
    # filtered -- which is what a site wants when its slug is load-bearing.
    path_template: str = ""
    # ASINs and listing ids are case-sensitive identities; two spellings of one
    # would defeat the dedupe this whole module exists for.
    group_case: str = ""  # "" | "upper" | "lower"
    # None: keep whatever the generic pass left. A tuple: keep only these.
    keep_query: tuple[str, ...] | None = None
    # ("*",) drops the query entirely.
    drop_query: tuple[str, ...] = ()
    why: str = ""


@lru_cache(maxsize=256)
def _compiled(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


DEFAULT_RULES: tuple[CanonicalRule, ...] = (
    CanonicalRule(
        name="amazon",
        # Every Amazon TLD, and the mobile and smile hosts.
        host_pattern=r"(?:.+\.)?amazon\.(?:[a-z]{2,3})(?:\.[a-z]{2})?",
        # The ASIN is the product. Everything before and after it in the path is
        # an SEO slug that changes with the title.
        path_pattern=r"/(?:dp|gp/product|gp/aw/d)/([A-Za-z0-9]{10})",
        path_template="/dp/{0}",
        group_case="upper",
        drop_query=("*",),
        why="the ASIN is the product; the slug and every parameter are decoration",
    ),
    CanonicalRule(
        name="flipkart",
        host_pattern=r"(?:.+\.)?flipkart\.com",
        # Guard only: the slug in a Flipkart product path is left alone, because
        # `pid` in the query is what identifies the product and the path is not
        # ours to second-guess.
        path_pattern=r"/p/itm",
        # `pid` is the product; `lid` is the listing (which seller), and the
        # rest is where the click came from.
        keep_query=("pid",),
        why="pid identifies the product; lid, srno, otracker and friends do not",
    ),
    CanonicalRule(
        name="etsy",
        host_pattern=r"(?:.+\.)?etsy\.com",
        path_pattern=r"/listing/(\d+)",
        path_template="/listing/{0}",
        drop_query=("*",),
        why="the listing id is the product",
    ),
)


@dataclass(frozen=True)
class Identity:
    """Everything that decides whether two links are the same product.

    One object rather than two parameters because it has to be threaded through
    the planner, the record and the batch, and a call site that passes the rules
    but forgets the variant flag would dedupe differently from the one next to
    it -- silently, and only on Shopify shops.
    """

    rules: tuple[CanonicalRule, ...] = DEFAULT_RULES
    merge_variants: bool = False


DEFAULT_IDENTITY = Identity()


def rule_for(host: str, rules: tuple[CanonicalRule, ...] = DEFAULT_RULES) -> CanonicalRule | None:
    """First rule whose host pattern matches. Order is precedence."""
    for rule in rules:
        if _compiled(rule.host_pattern).fullmatch(host):
            return rule
    return None


def _rebuild_path(path: str, rule: CanonicalRule) -> str:
    """The path this rule wants, given that its guard has already matched."""
    if not rule.path_pattern or not rule.path_template:
        return path
    match = _compiled(rule.path_pattern).search(path)
    if match is None:
        return path
    groups = [g or "" for g in match.groups()]
    if rule.group_case == "upper":
        groups = [g.upper() for g in groups]
    elif rule.group_case == "lower":
        groups = [g.lower() for g in groups]
    return rule.path_template.format(*groups)


def _applies(rule: CanonicalRule, path: str) -> bool:
    """A rule with a path guard is only for the URLs that guard describes."""
    return not rule.path_pattern or _compiled(rule.path_pattern).search(path) is not None


def _filter_query(query: str, rule: CanonicalRule | None, merge_variants: bool) -> str:
    pairs = parse_qsl(query, keep_blank_values=True)
    if not pairs:
        return ""

    if rule is not None:
        if rule.keep_query is not None:
            keep = {k.lower() for k in rule.keep_query}
            pairs = [(k, v) for k, v in pairs if k.lower() in keep]
        elif "*" in rule.drop_query:
            pairs = []
        elif rule.drop_query:
            drop = {k.lower() for k in rule.drop_query}
            pairs = [(k, v) for k, v in pairs if k.lower() not in drop]

    if merge_variants:
        variants = {p.lower() for p in VARIANT_PARAMS}
        pairs = [(k, v) for k, v in pairs if k.lower() not in variants]

    return urlencode(pairs)


def apply_rules(url: str, identity: Identity = DEFAULT_IDENTITY) -> str:
    """The domain-specific half of canonicalisation.

    Expects a URL the generic pass has already been through. Returns it
    unchanged when no rule matches its host, which is the common case.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    original_path = parts.path or "/"

    rule = rule_for(host, identity.rules) if host else None
    if rule is not None and not _applies(rule, original_path):
        rule = None

    if rule is None and not identity.merge_variants:
        return url

    path = _rebuild_path(original_path, rule) if rule else original_path
    query = _filter_query(parts.query, rule, identity.merge_variants)
    return urlunsplit((parts.scheme, parts.netloc, path, query, ""))
