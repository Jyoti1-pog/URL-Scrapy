"""Configuration: `config.yaml` for behaviour, `.env` for secrets, `taxonomy.yaml`
for the category vocabulary.

Kept as two separate objects on purpose. `AppConfig` is plain data loaded from
YAML and is trivially constructible in tests; `Secrets` is the only thing that
reads the environment. Nothing in this module ever logs a secret value.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import ImageMode, PriceStrategy
from .utils.canonical import DEFAULT_RULES, CanonicalRule, Identity

PLACEHOLDER_CONTACT_MARKERS = ("example.com", "yourdomain.com", "you@")


# ---------------------------------------------------------------------------
# config.yaml
# ---------------------------------------------------------------------------


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FetchConfig(_Section):
    user_agent_template: str
    download_user_agent: str
    timeout_s: float = 20.0
    connect_timeout_s: float = 10.0
    max_retries: int = 3
    respect_robots: bool = True
    per_domain_concurrency: int = 1
    per_domain_delay_s: float = 2.0
    per_domain_delay_jitter_s: float = 0.75
    max_redirects: int = 5
    max_html_bytes: int = 5_000_000

    # --- the fetch ladder (v4 §2.2) ------------------------------------
    #
    # Each rung gets its own short budget rather than the whole `timeout_s`.
    # Measured, not guessed: a host that black-holes HTTP/1.1 holds the
    # connection for the full timeout, so three rungs at 20s each turn a
    # 0.7-second failure into a 60-second one on exactly the sites the ladder
    # was added to help.
    rung_timeout_s: float = 8.0
    rung_backoff_s: float = 0.75
    ladder_enabled: bool = True

    # --- the per-URL budget (v5 §5) --------------------------------------
    #
    # ONE clock for everything a row does: every rung, the browser, and every
    # retry. Not a sum of the limits above -- a deadline. Without it a single
    # URL could take the better part of a minute while every individual limit
    # was being respected, because the limits nest and nothing held the total.
    url_timeout_s: float = 20.0

    # Consecutive whole-ladder failures before later URLs on the same host fail
    # immediately. A run-scoped count, cleared by any success: the host is not
    # on a blacklist, it is having a bad afternoon. §5 names 5; the ladder shipped
    # with 3, and 5 is the more forgiving of the two on a host that is merely
    # flaky rather than refusing.
    refusals_before_fast_fail: int = 5

    # Retries per URL, on top of the ladder and only for retryable reasons.
    # Small because the ladder is already three attempts: this is for the
    # `Retry-After` case, not for grinding.
    max_url_retries: int = 2
    retry_base_s: float = 1.0

    # The header set, in config because "what a browser sends" changes and
    # should not need a release. See fetch/ladder.py for why these are honesty
    # rather than disguise.
    accept_header: str = (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
    )
    accept_language: str = "en-IN,en;q=0.9"
    extra_headers: dict[str, str] = Field(default_factory=dict)

    # SSRF escape hatch. An allowlist of HOSTS, not a boolean, so switching the
    # guard off wholesale is not the easy path. Config-file only -- the API
    # never accepts it from a request body.
    allow_private_hosts: list[str] = Field(default_factory=list)


class RenderConfig(_Section):
    """Stage B. See config.yaml for why `retry_when_missing` is so short."""

    enabled: bool = True
    retry_when_missing: list[str] = Field(
        default_factory=lambda: ["title", "description", "images"]
    )
    timeout_ms: int = 20000
    wait_until: str = "networkidle"
    settle_ms: int = 500
    block_resource_types: list[str] = Field(default_factory=lambda: ["image", "media", "font"])
    viewport_width: int = 1280
    viewport_height: int = 2000

    # Scroll once before reading the DOM. Most lazy-load galleries are wired to
    # IntersectionObserver, so a page that is never scrolled shows exactly one
    # photo no matter how long it is left to settle -- `networkidle` is
    # satisfied because nothing was ever asked for.
    scroll_before_parse: bool = True

    # Wait for one of these to appear before reading the DOM, rather than for
    # the network to fall quiet. Best-effort: a page with no gallery is a fact
    # about the page, not a failure, so a miss costs `gallery_wait_ms` and the
    # render proceeds regardless.
    gallery_selectors: list[str] = Field(
        default_factory=lambda: [
            "#altImages img",
            "[data-gallery] img",
            ".product__media img",
            ".product-gallery img",
            ".woocommerce-product-gallery img",
            "[class*='gallery'] img",
            "main img[srcset]",
        ]
    )
    gallery_wait_ms: int = 3000

    @field_validator("retry_when_missing")
    @classmethod
    def _known_triggers(cls, value: list[str]) -> list[str]:
        # Loudly, at startup. A typo here would silently mean "never render",
        # and the symptom -- rows quietly missing titles -- looks like an
        # extraction bug rather than a config one.
        from .pipeline import RENDER_TRIGGERS

        unknown = [name for name in value if name not in RENDER_TRIGGERS]
        if unknown:
            raise ValueError(
                f"render.retry_when_missing has unknown entries {unknown}. "
                f"Allowed: {', '.join(sorted(RENDER_TRIGGERS))}."
            )
        return value

    @field_validator("wait_until")
    @classmethod
    def _known_wait(cls, value: str) -> str:
        allowed = {"load", "domcontentloaded", "networkidle", "commit"}
        if value not in allowed:
            raise ValueError(f"render.wait_until must be one of {sorted(allowed)}, not {value!r}")
        return value


class ExtractionConfig(_Section):
    title_suffix_separators: list[str] = Field(default_factory=list)
    title_suffix_max_words: int = 6
    allcaps_ratio_threshold: float = 0.6
    description_min_length: int = 40
    description_selectors: list[str] = Field(default_factory=list)
    description_boilerplate: list[str] = Field(default_factory=list)
    weight_labels: list[str] = Field(default_factory=list)
    shipping_weight_labels: list[str] = Field(default_factory=list)
    dimension_labels: list[str] = Field(default_factory=list)
    length_labels: list[str] = Field(default_factory=list)
    width_labels: list[str] = Field(default_factory=list)
    height_labels: list[str] = Field(default_factory=list)
    size_selectors: list[str] = Field(default_factory=list)
    size_noise: list[str] = Field(default_factory=list)
    vague_stock_phrases: list[str] = Field(default_factory=list)

    # Loading this is loading your Python. Left unset, no directory is read.
    plugins_dir: str = ""


class CurrencyConfig(_Section):
    symbols: dict[str, str] = Field(default_factory=dict)
    prefixes: dict[str, str] = Field(default_factory=dict)
    ambiguous_dollar_default: str = "USD"


class ValidatorConfig(_Section):
    min_bytes: int = 10_240
    # The listable standard. haat is a premium marketplace and this is the size
    # a photo should be.
    min_width: int = 800
    min_height: int = 800
    # The floor below which the answer is still "no photo". Between the two, a
    # photo ships with `image_method=*_low_res` and a flag carrying its actual
    # dimensions -- because "reject everything, ship nothing" is the wrong
    # failure mode for a standard. An operator with a 679x679 photo and a flag
    # can decide; an operator with nothing cannot.
    hard_min_width: int = 400
    hard_min_height: int = 400
    max_redirect_hops: int = 5
    header_probe_bytes: int = 65_536
    max_probe_bytes: int = 5_242_880
    hotlink_test: bool = True
    hotlink_neutral_user_agent: str
    signed_url_tokens: list[str]
    bad_host_failures_before_caching: int = 3
    bad_host_cache_ttl_days: int = 30


class ImagesConfig(_Section):
    default_mode: ImageMode = ImageMode.MANIFEST
    max_images_per_product: int = 10
    max_download_mb: int = 15
    accepted_formats: list[str] = Field(default_factory=lambda: ["jpeg", "png", "webp"])
    max_file_mb: int = 8
    max_edge_px: int = 2000
    jpeg_quality: int = 88
    jpeg_quality_steps: list[int] = Field(default_factory=lambda: [78, 68])
    keep_webp: bool = False
    strip_exif: bool = True
    reject_url_substrings: list[str] = Field(default_factory=list)
    strip_query_params: list[str] = Field(default_factory=list)
    lazy_attributes: list[str] = Field(default_factory=list)


class HostsConfig(_Section):
    chain: list[str] = Field(default_factory=lambda: ["cloudinary", "imgbb", "imgur"])
    max_attempts_per_host: int = 3
    backoff_initial_s: float = 1.0
    backoff_max_s: float = 30.0
    cloudinary_folder: str = "haat-listings"


class CsvConfig(_Section):
    quote_all: bool = True
    line_terminator: str = "\r\n"
    excel_bom: bool = False
    max_title_length: int = 200
    max_description_length: int = 5000
    injection_prefixes: list[str] = Field(default_factory=lambda: ["=", "+", "-", "@", "\t", "\r"])


class FieldsConfig(_Section):
    availability_values: list[str] = Field(default_factory=list)
    availability_in_stock_value: str | None = None
    availability_made_to_order_value: str | None = None
    yes_value: str = "yes"
    blank_value: str = ""
    rfq_default: str = ""
    rfq_min_qty: int | None = None
    bulk_only_default: str = ""
    required_by_haat: list[str] = Field(default_factory=list)


class PriceConfig(_Section):
    strategy: PriceStrategy = PriceStrategy.BLANK
    # Only meaningful with `markup`. Parsed from `--price-strategy markup:15`.
    markup_percent: float | None = None


class FxConfig(_Section):
    as_of: date | None = None
    stale_after_days: int = 30
    rates_to_inr: dict[str, float] = Field(default_factory=dict)


class HsCodesConfig(_Section):
    by_category: dict[str, str] = Field(default_factory=dict)
    by_material_keyword: dict[str, str] = Field(default_factory=dict)


class PolicyConfig(_Section):
    brands_file: str = "haat_lister/policy/brands.txt"
    keywords_file: str = "haat_lister/policy/keywords.yaml"


class LlmConfig(_Section):
    """The `--llm` layer. Off unless asked for; see enrich/rewrite.py for what
    it is structurally incapable of writing."""

    model: str = "claude-sonnet-5"
    max_tokens: int = 1500
    timeout_s: float = 60.0

    rewrite_descriptions: bool = True
    suggest_categories: bool = True
    max_description_words: int = 120

    # Responses are cached in the ledger by prompt hash, so a re-run or a
    # --resume costs nothing.
    cache: bool = True

    # 0 means no ceiling. A non-zero value stops and says so rather than
    # degrading quietly: rows past the budget carry a note naming it.
    max_calls_per_run: int = 0


class CanonicalRuleConfig(_Section):
    """One per-domain canonical rule, from config.yaml.

    Mirrors `utils.canonical.CanonicalRule`. A rule whose `name` matches a
    built-in replaces it outright rather than merging field by field: a partial
    override would leave an operator reading their own config and still not
    knowing what the effective rule is.
    """

    name: str
    host_pattern: str
    path_pattern: str = ""
    path_template: str = ""
    group_case: str = ""
    keep_query: list[str] | None = None
    drop_query: list[str] = Field(default_factory=list)
    why: str = ""

    def to_rule(self) -> CanonicalRule:
        return CanonicalRule(
            name=self.name,
            host_pattern=self.host_pattern,
            path_pattern=self.path_pattern,
            path_template=self.path_template,
            group_case=self.group_case,
            keep_query=tuple(self.keep_query) if self.keep_query is not None else None,
            drop_query=tuple(self.drop_query),
            why=self.why,
        )


class CanonicalConfig(_Section):
    """Which links count as the same product.

    Empty by default: the built-in rules for Amazon, Flipkart and Etsy cover the
    marketplaces this tool is pointed at most, and an operator only needs this
    section to add their own.
    """

    rules: list[CanonicalRuleConfig] = Field(default_factory=list)
    # Off, and it stays off unless asked for on the command line. A size that
    # costs a different amount is a different haat listing, so collapsing
    # variants is a claim about the catalogue that only the seller can make.
    merge_variants: bool = False

    def resolved(self) -> tuple[CanonicalRule, ...]:
        overrides = {r.name: r.to_rule() for r in self.rules}
        merged = [overrides.pop(rule.name, rule) for rule in DEFAULT_RULES]
        # Anything not overriding a built-in is appended, in config order.
        return tuple(merged + [overrides[r.name] for r in self.rules if r.name in overrides])


class PathsConfig(_Section):
    taxonomy: str = "taxonomy.yaml"
    # The accumulating sheet, relative to runs_dir. What happens when a URL is
    # already in it: skip (report and move on), replace (update in place,
    # keeping its position), append (allow the duplicate).
    master_csv: str = "master.csv"
    master_on_duplicate: str = "skip"

    @field_validator("master_on_duplicate")
    @classmethod
    def _known_duplicate_policy(cls, value: str) -> str:
        from .output.master import ON_DUPLICATE

        if value not in ON_DUPLICATE:
            raise ValueError(
                f"paths.master_on_duplicate must be one of {list(ON_DUPLICATE)}, not {value!r}"
            )
        return value
    ledger: str = "store/ledger.db"
    # Every batch run gets runs/<job_id>/ with all four files and its images.
    runs_dir: str = "runs"
    downloads_dir: str = "downloads"
    images_dir: str = "images"
    out_csv: str = "listings.csv"
    review_csv: str = "review.csv"
    manifest_csv: str = "image_manifest.csv"


class AppConfig(_Section):
    fetch: FetchConfig
    render: RenderConfig = Field(default_factory=RenderConfig)
    extraction: ExtractionConfig
    currency: CurrencyConfig
    validator: ValidatorConfig
    images: ImagesConfig
    hosts: HostsConfig
    csv: CsvConfig
    fields: FieldsConfig
    price: PriceConfig
    fx: FxConfig
    hs_codes: HsCodesConfig
    policy: PolicyConfig
    llm: LlmConfig
    canonical: CanonicalConfig = Field(default_factory=CanonicalConfig)
    paths: PathsConfig

    @classmethod
    def load(cls, path: Path) -> AppConfig:
        if not path.exists():
            raise ConfigError(
                f"config.yaml not found at {path}. Copy the one from the repo root, "
                f"or pass --config."
            )
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.model_validate(raw)


# ---------------------------------------------------------------------------
# .env
# ---------------------------------------------------------------------------


class Secrets(BaseSettings):
    """The only object that reads the environment. Values are SecretStr so an
    accidental repr in a log line cannot leak them."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    haat_contact: str = ""

    cloudinary_url: SecretStr | None = None
    cloudinary_cloud_name: str = ""
    cloudinary_api_key: SecretStr | None = None
    cloudinary_api_secret: SecretStr | None = None

    imgbb_api_key: SecretStr | None = None
    imgur_client_id: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None

    # Optional Google Sheets export. Both halves are required for it to work,
    # and with neither set the feature is entirely absent -- no flag, no
    # warning, no nag.
    google_credentials_file: str = ""
    google_sheet_id: str = ""

    def has_host_credentials(self, host: str) -> bool:
        match host:
            case "cloudinary":
                three_part = bool(
                    self.cloudinary_cloud_name
                    and self.cloudinary_api_key
                    and self.cloudinary_api_secret
                )
                return bool(self.cloudinary_url) or three_part
            case "imgbb":
                return bool(self.imgbb_api_key)
            case "imgur":
                return bool(self.imgur_client_id)
            case _:
                return False

    @property
    def has_llm_credentials(self) -> bool:
        """Truthiness, not `is not None`.

        Copying .env.example produces `ANTHROPIC_API_KEY=`, which parses to an
        empty SecretStr -- present as an object, useless as a key. Checking for
        None would let that through the startup gate and fail on the first API
        call instead, which is the whole thing the gate exists to prevent.
        """
        return bool(self.anthropic_api_key)

    @property
    def contact_is_placeholder(self) -> bool:
        c = self.haat_contact.strip().lower()
        if not c:
            return True
        return any(marker in c for marker in PLACEHOLDER_CONTACT_MARKERS)


# ---------------------------------------------------------------------------
# taxonomy.yaml
# ---------------------------------------------------------------------------


class Subcategory(BaseModel):
    model_config = ConfigDict(extra="ignore")
    label: str = ""
    keywords: list[str] = Field(default_factory=list)
    # True when the slug was inferred from haat's slug convention rather than
    # read from real haat data. Surfaced by config-check so an operator knows
    # which ones to verify before a large run.
    derived: bool = False


class Category(BaseModel):
    model_config = ConfigDict(extra="ignore")
    label: str = ""
    keywords: list[str] = Field(default_factory=list)
    subcategories: dict[str, Subcategory] = Field(default_factory=dict)


class Taxonomy(BaseModel):
    """The category vocabulary. Every emitted slug is checked against this.

    An unrecognised slug is a hard row failure, not a warning: a bad slug either
    rejects the import or buries the listing in the wrong aisle.
    """

    model_config = ConfigDict(extra="ignore")

    complete: bool = False
    fallback_category: str = "more-crafts"
    categories: dict[str, Category] = Field(default_factory=dict)
    keyword_hints: dict[str, list[str]] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Taxonomy:
        if not path.exists():
            raise ConfigError(
                f"taxonomy.yaml not found at {path}. It defines the only category slugs "
                f"this tool is allowed to emit; without it every row would fail."
            )
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.model_validate(raw)

    def has_category(self, slug: str) -> bool:
        return slug in self.categories

    def has_subcategory(self, parent: str, slug: str) -> bool:
        cat = self.categories.get(parent)
        return bool(cat and slug in cat.subcategories)

    @property
    def categories_without_subcategories(self) -> list[str]:
        return sorted(s for s, c in self.categories.items() if not c.subcategories)

    @property
    def subcategory_count(self) -> int:
        return sum(len(c.subcategories) for c in self.categories.values())

    @property
    def derived_slugs(self) -> list[str]:
        return sorted(
            f"{parent}/{slug}"
            for parent, cat in self.categories.items()
            for slug, sub in cat.subcategories.items()
            if sub.derived
        )


# ---------------------------------------------------------------------------
# Bundle
# ---------------------------------------------------------------------------


class ConfigError(Exception):
    """Raised for an unusable configuration. Always carries an actionable message."""


class Settings(BaseModel):
    """Everything the run needs, resolved once at startup."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    config: AppConfig
    secrets: Secrets
    taxonomy: Taxonomy
    root: Path
    config_path: Path
    taxonomy_path: Path

    @classmethod
    def load(cls, root: Path | None = None, config_path: Path | None = None) -> Settings:
        root = (root or Path.cwd()).resolve()
        config_path = config_path or root / "config.yaml"
        app = AppConfig.load(config_path)
        taxonomy_path = root / app.paths.taxonomy
        return cls(
            config=app,
            secrets=Secrets(),
            taxonomy=Taxonomy.load(taxonomy_path),
            root=root,
            config_path=config_path,
            taxonomy_path=taxonomy_path,
        )

    @property
    def user_agent(self) -> str:
        return self.config.fetch.user_agent_template.format(
            contact=self.secrets.haat_contact or "UNSET"
        )

    @property
    def identity(self) -> Identity:
        """What counts as the same product: the built-in per-domain rules with
        config.yaml's on top, plus the variant decision.

        Resolved here rather than at each call site so that every place which
        computes a URL identity -- the planner, the record, the ledger -- is
        looking at one table. Two of them disagreeing does not raise anything;
        it just quietly stops deduping, which is the worst kind of bug to have.
        """
        return Identity(
            rules=self.config.canonical.resolved(),
            merge_variants=self.config.canonical.merge_variants,
        )


# ---------------------------------------------------------------------------
# config-check
# ---------------------------------------------------------------------------


class Finding(BaseModel):
    level: str          # "fail" | "warn" | "info"
    title: str
    detail: str
    fix: str = ""

    @property
    def is_fail(self) -> bool:
        return self.level == "fail"


def _taxonomy_findings(s: Settings) -> list[Finding]:
    tx = s.taxonomy
    out: list[Finding] = []

    if not tx.categories:
        out.append(
            Finding(
                level="fail",
                title="taxonomy.yaml has no categories",
                detail="Every category_slug the tool emits is validated against this file.",
                fix=f"Fill in `categories:` in {s.taxonomy_path}.",
            )
        )
        return out

    if not tx.complete:
        missing = tx.categories_without_subcategories
        out.append(
            Finding(
                level="fail",
                title="taxonomy.yaml is marked incomplete",
                detail=(
                    f"{len(tx.categories)} parent categories, "
                    f"{tx.subcategory_count} subcategories. "
                    + (
                        f"No subcategories at all for: {', '.join(missing)}. "
                        if missing
                        else ""
                    )
                    + "An unrecognised slug is a hard row failure, so the tool will not run on "
                    "a taxonomy it cannot trust."
                ),
                fix=(
                    "Open the haat seller dashboard -> listing creator -> category picker, paste "
                    f"the authoritative parent and child slugs into {s.taxonomy_path}, then set "
                    "`complete: true` at the top of that file."
                ),
            )
        )

    if derived := tx.derived_slugs:
        out.append(
            Finding(
                level="warn",
                title=f"{len(derived)} subcategory slugs are derived, not confirmed",
                detail=(
                    "These follow haat's slug convention but have not been seen in real haat "
                    "data: " + ", ".join(derived) + ". A slug that is wrong in both this file "
                    "and the CSV passes our validation and is then rejected by the importer."
                ),
                fix=(
                    "Verify with one manual import, or read the slug from a live category page "
                    "URL, then remove `derived: true` in taxonomy.yaml."
                ),
            )
        )

    if tx.fallback_category not in tx.categories:
        out.append(
            Finding(
                level="fail",
                title=f"fallback_category '{tx.fallback_category}' is not a defined category",
                detail="Ambiguous classifications route here, so it must exist.",
                fix=f"Add '{tx.fallback_category}' to `categories:` or change `fallback_category`.",
            )
        )

    return out


def _identity_findings(s: Settings) -> list[Finding]:
    if s.secrets.contact_is_placeholder:
        return [
            Finding(
                level="fail",
                title="HAAT_CONTACT is unset or still a placeholder",
                detail=(
                    "It goes into the User-Agent of every outgoing request so a site operator "
                    "who wants us to stop can reach a human. We identify honestly; that is not "
                    "optional."
                ),
                fix="Set HAAT_CONTACT in .env to a real, monitored address.",
            )
        ]
    return []


def _mode_findings(s: Settings, mode: ImageMode) -> list[Finding]:
    out = [
        Finding(
            level="info",
            title=f"Image mode: {mode.value}",
            detail=(
                "manifest -- the 19-column CSV is emitted untouched, images land in "
                "images/<row_key>/, and NO third-party image host is ever contacted."
                if mode is ImageMode.MANIFEST
                else f"need_url={mode.need_url}  need_file={mode.need_file}"
            ),
        )
    ]

    if mode.need_url:
        out.append(
            Finding(
                level="warn",
                title=f"'{mode.value}' appends image URL columns to the CSV",
                detail=(
                    "The supplied haat template has no image column. This mode assumes you have "
                    "confirmed the importer accepts extra image URL columns -- if it does not, the "
                    "import will reject. It is also the only mode in which an image host can ever "
                    "be contacted."
                ),
                fix="Confirm with the haat platform team, or use the default --images manifest.",
            )
        )

        configured = [h for h in s.config.hosts.chain if s.secrets.has_host_credentials(h)]
        missing = [h for h in s.config.hosts.chain if not s.secrets.has_host_credentials(h)]
        if missing:
            out.append(
                Finding(
                    level="fail" if not configured else "warn",
                    title=f"Image host credentials missing: {', '.join(missing)}",
                    detail=(
                        "These hosts are skipped for the whole run. "
                        + (
                            f"Usable chain: {', '.join(configured)}."
                            if configured
                            else "That leaves NO usable host, so any row whose direct URL fails "
                            "Tier 1 will end at image_method=none."
                        )
                    ),
                    fix="Fill the relevant keys in .env (see .env.example).",
                )
            )
    return out


def _field_policy_findings(s: Settings) -> list[Finding]:
    out: list[Finding] = []
    c = s.config

    if not c.fields.availability_made_to_order_value:
        out.append(
            Finding(
                level="warn",
                title="made-to-order availability value is unknown",
                detail=(
                    "haat offers two availability states, 'Ready stock' and 'Made to order'. "
                    f"The ready-stock wire value is confirmed as "
                    f"'{c.fields.availability_in_stock_value}'; the made-to-order one is not. "
                    "Made-to-order products therefore get a blank availability plus a review "
                    "flag, because a guessed enum would be rejected at import."
                ),
                fix="Read the availability cell from a made-to-order listing and set "
                "config.yaml -> fields.availability_made_to_order_value.",
            )
        )

    mapped = len(c.hs_codes.by_category) + len(c.hs_codes.by_material_keyword)
    out.append(
        Finding(
            level="warn",
            title=f"HS code map has {mapped} entries",
            detail=(
                "HS classification is a customs declaration and a wrong code is a legal and "
                "financial problem for the seller, so this map ships nearly empty rather than "
                "plausible. Unmapped categories yield a blank cell plus a review flag. Every "
                "hs_code appears in review.csv even when populated."
            ),
            fix="Extend config.yaml -> hs_codes with an authoritative list when you have one.",
        )
    )

    if c.price.strategy is PriceStrategy.BLANK:
        out.append(
            Finding(
                level="info",
                title="price_inr strategy: blank (default)",
                detail=(
                    "price_inr is left empty and the source amount, currency and rate go to "
                    "review.csv. haat wants the maker's INR price, which is a business decision -- "
                    "not the scraped retail price of some other shop."
                ),
            )
        )
    else:
        stale = "unset" if not c.fx.as_of else str(c.fx.as_of)
        level = "fail" if not c.fx.rates_to_inr else "warn"
        if c.price.strategy in (PriceStrategy.CONVERT, PriceStrategy.MARKUP):
            out.append(
                Finding(
                    level=level,
                    title=f"price strategy '{c.price.strategy.value}' needs FX rates",
                    detail=(
                        f"fx.rates_to_inr has {len(c.fx.rates_to_inr)} entries; "
                        f"fx.as_of is {stale}."
                    ),
                    fix="Fill fx.rates_to_inr and fx.as_of in config.yaml. Every converted price "
                    "records the rate and date in review.csv.",
                )
            )

    if c.fx.as_of and c.fx.rates_to_inr:
        age = (date.today() - c.fx.as_of).days
        if age > c.fx.stale_after_days:
            out.append(
                Finding(
                    level="warn",
                    title=f"FX rates are {age} days old",
                    detail=f"fx.as_of is {c.fx.as_of}, threshold is {c.fx.stale_after_days} days.",
                    fix="Refresh fx.rates_to_inr and fx.as_of in config.yaml.",
                )
            )

    return out


def _invariant_findings() -> list[Finding]:
    return [
        Finding(
            level="info",
            title="gi_region is always blank",
            detail=(
                "A GI tag is an Indian government certification. The extractor's model has no "
                "gi_region field at all, so no code path can populate it. GI mentions found in "
                "source text surface in review.csv as a question for a human."
            ),
        )
    ]


def collect_findings(s: Settings, mode: ImageMode | None = None) -> list[Finding]:
    """Everything `config-check` reports, in display order."""
    mode = mode or s.config.images.default_mode
    return [
        *_identity_findings(s),
        *_codec_findings(),
        *_taxonomy_findings(s),
        *_mode_findings(s, mode),
        *_field_policy_findings(s),
        *_plugin_findings(s),
        *_llm_findings(s),
        *_invariant_findings(),
    ]


def _codec_findings() -> list[Finding]:
    """A missing compression codec, said out loud.

    Without this it presents as a shop with no photographs: the fetch returns
    200, the body is undecodable binary, and the row blames the page for having
    no title and no images. It cost an entire catalogue's worth of rows before
    anyone thought to look at the bytes. One line here, one line to fix.
    """
    from .fetch.ladder import missing_codecs

    if not (missing := missing_codecs()):
        return []
    return [
        Finding(
            level="warn",
            title=f"Cannot decode {', '.join(missing)} responses",
            detail=(
                "Sites that compress with these will come back as unreadable bytes, and the "
                "row will report no title and no images -- blaming the page for something "
                "that is ours. We do not advertise what we cannot read, so nothing breaks "
                "silently; those sites just serve us the slower encoding."
            ),
            fix="pip install brotli zstandard",
        )
    ]


def _llm_findings(s: Settings) -> list[Finding]:
    """Informational only: `--llm` is opt-in per run, so a missing key is not a
    problem until somebody asks for it -- at which point they are told at
    startup rather than mid-batch."""
    if s.secrets.has_llm_credentials:
        return [
            Finding(
                level="info",
                title="--llm is available",
                detail=f"ANTHROPIC_API_KEY is set; --llm would use {s.config.llm.model} to "
                "rewrite descriptions and choose a category from taxonomy.yaml. It is never "
                "used for price, weight, dimensions, HS code or GI region -- the result "
                "object has no field for any of them.",
                fix="",
            )
        ]
    return [
        Finding(
            level="info",
            title="--llm is not configured",
            detail="ANTHROPIC_API_KEY is unset or empty. Runs without --llm are unaffected; "
            "a run WITH --llm will refuse to start rather than fail partway.",
            fix='Set ANTHROPIC_API_KEY in .env and pip install "haat-lister[llm]".',
        )
    ]


def _plugin_findings(s: Settings) -> list[Finding]:
    """Loading a plugin directory executes the operator's own Python, so
    config-check names what would run before a batch does it."""
    directory = s.config.extraction.plugins_dir
    if not directory:
        return []

    path = s.root / directory
    if not path.exists():
        return [
            Finding(
                level="warn",
                title="plugins_dir does not exist",
                detail=f"extraction.plugins_dir points at {path}, which is not there. "
                "No plugins will be loaded.",
                fix=f"Create {path}, or clear extraction.plugins_dir in config.yaml.",
            )
        ]

    files = sorted(p.name for p in path.glob("*.py") if not p.name.startswith("_"))
    if not files:
        return []
    return [
        Finding(
            level="info",
            title="Operator plugins will be executed",
            detail=f"Every run imports {len(files)} file(s) from {path}: {', '.join(files)}. "
            "A plugin's values override the generic extractors, though every field it "
            "supplies is stamped source=plugin in review.csv.",
            fix="",
        )
    ]


def assert_runnable(s: Settings, mode: ImageMode) -> None:
    """Raise ConfigError if the configuration cannot support a real run.

    Called at the top of `single` and `batch` so an operator never discovers a
    broken taxonomy 300 rows into a batch.
    """
    fails = [f for f in collect_findings(s, mode) if f.is_fail]
    if not fails:
        return
    lines = [f"  - {f.title}\n      {f.detail}\n      Fix: {f.fix}" for f in fails]
    raise ConfigError(
        "Configuration is not runnable:\n"
        + "\n".join(lines)
        + "\n\nRun `haat-lister config-check` for the full report."
    )
