"""/api/health and /api/config.

Both are read-only and both are derived from the same functions the CLI uses --
`collect_findings` for health, the loaded `Settings` for config. A second
implementation of "is this configured" would drift within a phase.

The rule that shapes this file: **whether**, never **what**. An operator needs
to know Cloudinary is usable; nothing in the browser needs the key, so nothing
in the browser gets a chance to leak it.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ... import __version__
from ...config import Settings, collect_findings
from ...enrich.rewrite import SYSTEM_PROMPT  # noqa: F401 -- import proves the layer loads
from ...models import DescriptionMode, ImageMode, PriceStrategy, Provenance
from ..schemas import (
    LOCKED_FIELDS,
    CategoryOut,
    ConfigOut,
    DefaultsOut,
    FindingOut,
    HealthOut,
    HostOut,
    LlmOut,
    SubcategoryOut,
)

router = APIRouter(tags=["config"])

# Matches `hosts.chain` order in config.yaml; the chain itself is the authority.
KNOWN_HOSTS = ("cloudinary", "imgbb", "imgur")


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


@router.get("/api/health", response_model=HealthOut)
def health(request: Request) -> HealthOut:
    """What `config-check` says, for a browser.

    Surfaces an incomplete taxonomy.yaml in particular: it is the one piece of
    configuration that silently produces a bad CSV rather than an error, so the
    console shows it before the first job rather than after.
    """
    settings = _settings(request)
    findings = collect_findings(settings)
    blocking = sum(1 for f in findings if f.level == "fail")

    return HealthOut(
        ok=blocking == 0,
        version=__version__,
        config_path=str(settings.config_path),
        taxonomy_path=str(settings.taxonomy_path),
        user_agent=settings.user_agent,
        blocking=blocking,
        warnings=sum(1 for f in findings if f.level == "warn"),
        findings=[
            FindingOut(level=f.level, title=f.title, detail=f.detail, fix=f.fix or "")
            for f in findings
        ],
    )


@router.get("/api/config", response_model=ConfigOut)
def config(request: Request) -> ConfigOut:
    settings = _settings(request)
    cfg = settings.config
    taxonomy = settings.taxonomy

    categories = [
        CategoryOut(
            slug=slug,
            label=category.label or slug,
            subcategories=[
                SubcategoryOut(slug=sub_slug, label=sub.label or sub_slug, derived=sub.derived)
                for sub_slug, sub in sorted(category.subcategories.items())
            ],
        )
        for slug, category in sorted(taxonomy.categories.items())
    ]

    return ConfigOut(
        version=__version__,
        taxonomy_complete=taxonomy.complete,
        fallback_category=taxonomy.fallback_category,
        categories=categories,
        enums={
            "provenance": [p.value for p in Provenance],
            "image_mode": [m.value for m in ImageMode],
            "price_strategy": [s.value for s in PriceStrategy],
            "description_mode": [d.value for d in DescriptionMode],
            "availability": list(cfg.fields.availability_values),
        },
        image_hosts=[
            HostOut(name=name, configured=settings.secrets.has_host_credentials(name))
            for name in KNOWN_HOSTS
        ],
        llm=LlmOut(
            configured=settings.secrets.has_llm_credentials,
            model=cfg.llm.model,
            used_for=["description rewrite", "category choice from taxonomy.yaml"],
            never_used_for=[
                "price_inr",
                "hs_code",
                "weight_g",
                "length_cm",
                "width_cm",
                "height_cm",
                "gi_region",
                "availability",
                "stock_qty",
            ],
        ),
        defaults=DefaultsOut(
            image_mode=cfg.images.default_mode.value,
            price_strategy=cfg.price.strategy.value,
            description_mode=DescriptionMode.RAW.value,
            concurrency=5,
            render_enabled=cfg.render.enabled,
            per_domain_delay_s=cfg.fetch.per_domain_delay_s,
        ),
        locked_fields=list(LOCKED_FIELDS),
        # Shown so an operator can see the SSRF escape hatch is in effect
        # without opening config.yaml. Hostnames only -- and it is config-file
        # only, so nothing here can be changed through the API.
        allow_private_hosts=list(cfg.fetch.allow_private_hosts),
    )
