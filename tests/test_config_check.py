"""Phase 1 gate tests: config-check must fail helpfully, and pass once fixed.

Also pins two invariants early, before any extractor exists that could violate
them: `gi_region` is not a field on the model at all, and `--provenance` has no
default.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from haat_lister.cli import app
from haat_lister.config import ConfigError, Secrets, Settings, assert_runnable, collect_findings
from haat_lister.models import ImageMode, ProductRecord, Provenance

runner = CliRunner()


# ---------------------------------------------------------------------------
# Taxonomy gate
# ---------------------------------------------------------------------------


def test_incomplete_taxonomy_blocks_a_run(app_config, incomplete_taxonomy, good_secrets, tmp_path):
    """An unfinished taxonomy must be a hard stop, not a warning."""
    s = Settings(
        config=app_config,
        secrets=good_secrets,
        taxonomy=incomplete_taxonomy,
        root=tmp_path,
        config_path=tmp_path / "config.yaml",
        taxonomy_path=tmp_path / "taxonomy.yaml",
    )
    fails = [f for f in collect_findings(s, ImageMode.MANIFEST) if f.is_fail]
    assert any("taxonomy" in f.title.lower() for f in fails)

    with pytest.raises(ConfigError) as exc:
        assert_runnable(s, ImageMode.MANIFEST)
    # The message has to tell the operator what to do, not just that it broke.
    assert "seller dashboard" in str(exc.value)
    assert "complete: true" in str(exc.value)


def test_shipped_taxonomy_matches_the_listing_creator(shipped_taxonomy):
    """The five categories and their shelves, as the seller dashboard lists them."""
    assert shipped_taxonomy.complete
    assert set(shipped_taxonomy.categories) == {
        "handwoven-textiles",
        "apparel",
        "jewellery",
        "leather-bags",
        "more-crafts",
    }
    # Confirmed against the bulk-listing template's two sample rows.
    assert shipped_taxonomy.has_subcategory("apparel", "womens-fashion")
    assert shipped_taxonomy.has_subcategory("jewellery", "earrings")
    # "Other -- my craft isn't listed" has no shelves; rows there get a blank
    # subcategory plus a flag rather than an invented slug.
    assert shipped_taxonomy.categories["more-crafts"].subcategories == {}


def test_confirmed_slugs_are_not_marked_derived(shipped_taxonomy):
    derived = set(shipped_taxonomy.derived_slugs)
    assert "apparel/womens-fashion" not in derived
    assert "jewellery/earrings" not in derived
    # Everything else is inferred from haat's slug convention and must say so.
    assert "apparel/mens-fashion" in derived
    assert "leather-bags/wallets-small-goods" in derived


def test_derived_slugs_are_warned_about_but_do_not_block(settings, shipped_taxonomy):
    settings.taxonomy = shipped_taxonomy
    findings = collect_findings(settings, ImageMode.MANIFEST)
    assert any("derived" in f.title for f in findings if f.level == "warn")
    assert_runnable(settings, ImageMode.MANIFEST)


def test_completed_taxonomy_is_runnable(settings):
    assert_runnable(settings, ImageMode.MANIFEST)
    assert not [f for f in collect_findings(settings, ImageMode.MANIFEST) if f.is_fail]


def test_fallback_category_must_exist(settings):
    settings.taxonomy.categories.pop("more-crafts")
    fails = [f for f in collect_findings(settings, ImageMode.MANIFEST) if f.is_fail]
    assert any("fallback_category" in f.title for f in fails)


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("contact", ["", "you@example.com", "ops@yourdomain.com"])
def test_placeholder_contact_blocks_a_run(settings, contact):
    """We identify honestly in the User-Agent. A placeholder is a hard stop."""
    settings.secrets = Secrets(haat_contact=contact)
    fails = [f for f in collect_findings(settings, ImageMode.MANIFEST) if f.is_fail]
    assert any("HAAT_CONTACT" in f.title for f in fails)


# ---------------------------------------------------------------------------
# Image mode
# ---------------------------------------------------------------------------


def test_manifest_mode_needs_no_host_credentials(settings):
    """The default mode never contacts a host, so missing creds cannot block it."""
    assert not settings.secrets.has_host_credentials("cloudinary")
    assert_runnable(settings, ImageMode.MANIFEST)


def test_url_mode_warns_that_importer_support_is_unconfirmed(settings):
    titles = [f.title for f in collect_findings(settings, ImageMode.URL_COLUMNS)]
    assert any("image URL columns" in t for t in titles)


def test_url_mode_without_any_host_credentials_blocks(settings):
    """url_columns with no usable host means Tier-1 failures have nowhere to go."""
    fails = [f for f in collect_findings(settings, ImageMode.URL_COLUMNS) if f.is_fail]
    assert any("credentials missing" in f.title for f in fails)


@pytest.mark.parametrize(
    ("mode", "need_url", "need_file"),
    [
        (ImageMode.MANIFEST, False, True),
        (ImageMode.URL_COLUMNS, True, False),
        (ImageMode.BOTH, True, True),
    ],
)
def test_mode_need_flags(mode, need_url, need_file):
    """These two flags are the entire Rule-1 gate; wrong values would open the
    expensive path silently."""
    assert mode.need_url is need_url
    assert mode.need_file is need_file


# ---------------------------------------------------------------------------
# Invariants pinned early
# ---------------------------------------------------------------------------


def test_gi_region_is_not_a_field_on_the_model():
    """Structural, not behavioural: there is no code path that could set it."""
    assert "gi_region" not in ProductRecord.model_fields
    assert "gi_mention_found" in ProductRecord.model_fields


def test_provenance_has_no_default():
    result = runner.invoke(app, ["single", "https://example.com/p/1"])
    assert result.exit_code == 2
    assert "provenance" in result.output.lower()


@pytest.mark.parametrize("value", ["own", "authorised", "third-party"])
def test_provenance_accepts_the_three_values(value):
    assert Provenance(value)


def test_config_check_blocks_while_haat_contact_is_unset(monkeypatch):
    """A fresh clone must not look ready when it isn't."""
    monkeypatch.setenv("HAAT_CONTACT", "")
    result = runner.invoke(app, ["config-check"])
    assert result.exit_code == 1


def test_config_check_passes_once_the_operator_has_filled_it_in(monkeypatch):
    monkeypatch.setenv("HAAT_CONTACT", "ops@a-real-domain.in")
    result = runner.invoke(app, ["config-check"])
    assert result.exit_code == 0
