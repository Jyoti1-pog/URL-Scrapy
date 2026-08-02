from __future__ import annotations

from pathlib import Path

import pytest

from haat_lister.config import AppConfig, Secrets, Settings, Taxonomy

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def app_config() -> AppConfig:
    """The shipped config.yaml. Tests run against what the operator actually gets."""
    return AppConfig.load(REPO_ROOT / "config.yaml")


@pytest.fixture
def shipped_taxonomy() -> Taxonomy:
    """The real taxonomy.yaml an operator gets."""
    return Taxonomy.load(REPO_ROOT / "taxonomy.yaml")


@pytest.fixture
def incomplete_taxonomy() -> Taxonomy:
    """A taxonomy an operator has not finished filling in."""
    return Taxonomy.model_validate(
        {
            "complete": False,
            "fallback_category": "more-crafts",
            "categories": {
                "apparel": {"label": "Apparel", "subcategories": {}},
                "more-crafts": {"label": "More Crafts", "subcategories": {}},
            },
        }
    )


@pytest.fixture
def complete_taxonomy() -> Taxonomy:
    """What a taxonomy looks like after the operator has filled it in."""
    return Taxonomy.model_validate(
        {
            "complete": True,
            "fallback_category": "more-crafts",
            "categories": {
                "handwoven-textiles": {
                    "label": "Handwoven Textiles",
                    "subcategories": {"sarees": {"label": "Sarees"}},
                },
                "apparel": {
                    "label": "Apparel",
                    "subcategories": {"womens-fashion": {"label": "Women's Fashion"}},
                },
                "jewellery": {
                    "label": "Jewellery",
                    "subcategories": {"earrings": {"label": "Earrings"}},
                },
                "leather-bags": {
                    "label": "Leather Bags",
                    "subcategories": {"totes": {"label": "Totes"}},
                },
                "more-crafts": {
                    "label": "More Crafts",
                    "subcategories": {"other": {"label": "Other"}},
                },
            },
            "keyword_hints": {},
        }
    )


@pytest.fixture
def good_secrets() -> Secrets:
    return Secrets(haat_contact="ops@a-real-domain.in")


@pytest.fixture
def settings(app_config: AppConfig, shipped_taxonomy: Taxonomy, good_secrets: Secrets) -> Settings:
    """What an operator actually runs with: the real config.yaml and the real
    taxonomy.yaml, plus a contact address.

    Deliberately not a synthetic taxonomy -- one with no keywords would let
    every category-mapping test pass by falling back to more-crafts.
    """
    return Settings(
        config=app_config,
        secrets=good_secrets,
        taxonomy=shipped_taxonomy,
        root=REPO_ROOT,
        config_path=REPO_ROOT / "config.yaml",
        taxonomy_path=REPO_ROOT / "taxonomy.yaml",
    )
