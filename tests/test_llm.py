"""Phase 12: the optional `--llm` layer.

Most of this file is about what a model is not allowed to do. The layer's whole
justification is that a rewritten description and a category chosen from a closed
list are low-stakes, while a model-written price or HS code is not — so the tests
that matter are the ones proving the second kind cannot happen, and that the
first kind is visible in review.csv when it does.

No network. The client is a Protocol with one method, and every test injects a
fake that returns canned text.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from haat_lister.config import Settings
from haat_lister.enrich.rewrite import (
    LlmClient,
    LlmEnricher,
    LlmUnavailable,
    RewriteResult,
    apply,
    build_client,
    build_prompt,
    interpret,
    llm_tasks,
    parse_response,
    rewrite_wanted_for,
)
from haat_lister.fetch.static import build_client as build_http_client
from haat_lister.models import (
    Confidence,
    DescriptionMode,
    FieldSource,
    FieldValue,
    Provenance,
    RowStatus,
)
from haat_lister.pipeline import new_record, process_url
from haat_lister.policy.screen import load_vocabulary
from haat_lister.store.ledger import Ledger

PAGE_URL = "https://shop.example/products/kurta"

SOURCE_HTML = """<!doctype html><html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product",
 "name":"Handwoven cotton stole",
 "description":"ELEVATE YOUR WARDROBE with this must-have handwoven cotton stole,
 woven on a pit loom by our team and finished with a hand-knotted fringe."}
</script></head><body><h1>Stole</h1></body></html>"""


class FakeLlm:
    """One method, because that is the whole Protocol."""

    def __init__(self, reply: str | dict | None = None, raises: Exception | None = None) -> None:
        self.reply = json.dumps(reply) if isinstance(reply, dict) else (reply or "{}")
        self.raises = raises
        self.prompts: list[str] = []

    async def complete(self, system: str, prompt: str, cfg: object) -> str:
        self.prompts.append(prompt)
        if self.raises is not None:
            raise self.raises
        return self.reply


@pytest.fixture
def vocabulary(settings: Settings):
    return load_vocabulary(
        settings.root / settings.config.policy.keywords_file,
        settings.root / settings.config.policy.brands_file,
    )


def record_with(
    settings: Settings,
    *,
    description: str = "A stole.",
    category: str | None = None,
):
    record = new_record(PAGE_URL, Provenance.OWN)
    record.title = FieldValue.found("Handwoven cotton stole", FieldSource.JSONLD, Confidence.HIGH)
    record.description = FieldValue.found(description, FieldSource.JSONLD, Confidence.HIGH)
    if category is not None:
        record.category_slug = FieldValue.found(category, FieldSource.INFERRED, Confidence.LOW)
    return record


# --------------------------------------------------------------------------
# What the layer structurally cannot do
# --------------------------------------------------------------------------


def test_the_result_has_nowhere_to_put_a_price_or_a_customs_code() -> None:
    """The prompt forbids these too, but the prompt is the second line of
    defence. This is the first: there is no field."""
    fields = set(RewriteResult.__dataclass_fields__)
    forbidden = {
        "price_inr",
        "source_price",
        "hs_code",
        "weight_g",
        "length_cm",
        "width_cm",
        "height_cm",
        "gi_region",
        "availability",
        "stock_qty",
    }
    assert fields & forbidden == set()


def test_a_model_returning_a_price_is_ignored_without_ceremony(
    settings: Settings,
) -> None:
    """`interpret` only ever reads the keys it knows about, so extra keys need no
    rejection logic -- they simply have nowhere to go."""
    record = record_with(settings)
    payload = {
        "description": "A handwoven cotton stole.",
        "price_inr": 2499,
        "hs_code": "6214",
        "weight_g": 200,
        "gi_region": "Kutch",
    }

    result = interpret(payload, record, settings, ["description"])

    assert result.description == "A handwoven cotton stole."
    assert not hasattr(result, "price_inr")
    assert not record.price_inr.is_present
    assert not record.hs_code.is_present


def test_a_category_outside_the_taxonomy_is_discarded(settings: Settings) -> None:
    """The closed vocabulary is the point. A slug the taxonomy does not have
    either rejects the import or buries the listing."""
    record = record_with(settings, category=settings.taxonomy.fallback_category)

    result = interpret({"category_slug": "artisanal-luxury"}, record, settings, ["category"])

    assert result.category_slug is None
    assert any("not in taxonomy.yaml" in note for note in result.notes)


def test_a_subcategory_under_the_wrong_parent_is_dropped(settings: Settings) -> None:
    record = record_with(settings, category=settings.taxonomy.fallback_category)
    payload = {"category_slug": "apparel", "subcategory_slug": "earrings"}

    result = interpret(payload, record, settings, ["category"])

    assert result.category_slug == "apparel"
    assert result.subcategory_slug is None
    assert any("not under" in note for note in result.notes)


# --------------------------------------------------------------------------
# The cost gate
# --------------------------------------------------------------------------


async def test_no_model_call_when_there_is_nothing_for_it_to_do(
    settings: Settings, vocabulary
) -> None:
    """A model call is money. Same shape as the Stage B gate: nothing
    speculative, nothing 'while we're here'."""
    record = record_with(settings, category="apparel")  # classified fine
    fake = FakeLlm({"description": "should never be asked for"})
    enricher = LlmEnricher(settings, fake, vocabulary)

    await enricher.enhance(record, rewrite_wanted=False)

    assert fake.prompts == []
    assert enricher.calls == 0


def test_the_gate_asks_only_for_the_tasks_that_apply(settings: Settings) -> None:
    classified = record_with(settings, category="apparel")
    assert llm_tasks(classified, settings, rewrite_wanted=False) == []
    assert llm_tasks(classified, settings, rewrite_wanted=True) == ["description"]

    fell_through = record_with(settings, category=settings.taxonomy.fallback_category)
    assert llm_tasks(fell_through, settings, rewrite_wanted=False) == ["category"]
    assert llm_tasks(fell_through, settings, rewrite_wanted=True) == ["description", "category"]


def test_a_row_with_no_description_is_not_sent_for_a_rewrite(settings: Settings) -> None:
    record = new_record(PAGE_URL, Provenance.OWN)
    record.category_slug = FieldValue.found("apparel", FieldSource.INFERRED, Confidence.HIGH)
    assert llm_tasks(record, settings, rewrite_wanted=True) == []


async def test_no_client_means_no_path_to_a_model(settings: Settings, vocabulary) -> None:
    enricher = LlmEnricher(settings, None, vocabulary)
    assert not enricher.enabled

    record = record_with(settings, category=settings.taxonomy.fallback_category)
    await enricher.enhance(record, rewrite_wanted=True)

    assert record.category_slug.value == settings.taxonomy.fallback_category
    assert build_client(settings, enabled=False) is None


def test_llm_without_a_key_fails_at_startup(settings: Settings) -> None:
    """Before the batch, not 300 rows into it."""
    with pytest.raises(LlmUnavailable, match="ANTHROPIC_API_KEY"):
        build_client(settings, enabled=True)


def test_an_empty_key_counts_as_no_key(settings: Settings) -> None:
    """Copying .env.example produces `ANTHROPIC_API_KEY=`, which parses to an
    empty SecretStr: present as an object, useless as a key. Caught in a live
    run -- a None check let it through the gate and it failed on the first API
    call instead."""
    from pydantic import SecretStr

    tuned = settings.model_copy(deep=True)
    tuned.secrets.anthropic_api_key = SecretStr("")

    assert not tuned.secrets.has_llm_credentials
    with pytest.raises(LlmUnavailable):
        build_client(tuned, enabled=True)


# --------------------------------------------------------------------------
# Output guards
# --------------------------------------------------------------------------


def test_a_rewrite_that_invents_a_gi_claim_is_flagged(
    settings: Settings, vocabulary
) -> None:
    """The failure mode this layer is most likely to produce and a human least
    likely to catch, because the embellished version reads better."""
    record = record_with(settings, description="A handwoven cotton stole from our workshop.")
    result = RewriteResult(
        description="An authentic GI-tagged Kanchipuram silk stole, woven in Kanchipuram."
    )

    apply(record, result, settings, vocabulary)

    assert record.status is RowStatus.NEEDS_REVIEW
    invented = [n for n in record.notes if "which the source text" in n]
    assert invented and "authentic" in invented[0]


def test_a_faithful_rewrite_is_not_flagged_as_an_invention(
    settings: Settings, vocabulary
) -> None:
    record = record_with(
        settings, description="A Kanchipuram silk stole, handwoven on a pit loom."
    )
    apply(
        record,
        RewriteResult(description="A Kanchipuram silk stole, woven by hand on a pit loom."),
        settings,
        vocabulary,
    )

    invented = [n for n in record.notes if "which the source text" in n]
    assert invented == []


def test_a_rewritten_description_is_stamped_and_marked_low_confidence(
    settings: Settings, vocabulary
) -> None:
    """review.csv's low-confidence column is how an operator finds every row a
    model touched."""
    record = record_with(settings)
    apply(record, RewriteResult(description="A handwoven cotton stole."), settings, vocabulary)

    assert record.description.source is FieldSource.LLM
    assert record.description.confidence is Confidence.MEDIUM
    assert any("rewritten by a language model" in note.lower() for note in record.notes)


def test_a_long_rewrite_is_cut_to_the_configured_length(settings: Settings) -> None:
    record = record_with(settings)
    tuned = settings.model_copy(deep=True)
    tuned.config.llm.max_description_words = 10

    result = interpret({"description": "word " * 200}, record, tuned, ["description"])

    assert result.description is not None
    assert len(result.description.split()) <= 11  # 10 words plus the ellipsis


def test_a_chosen_category_clears_the_made_up_custom_name(
    settings: Settings, vocabulary
) -> None:
    """Keyword matching files a mystery under the fallback with a custom name
    derived from the title. A real slug replaces both."""
    record = record_with(settings, category=settings.taxonomy.fallback_category)
    record.custom_category = FieldValue.found(
        "Handwoven Cotton Stole", FieldSource.INFERRED, Confidence.LOW
    )

    apply(
        record,
        RewriteResult(category_slug="apparel", subcategory_slug="womens-fashion"),
        settings,
        vocabulary,
    )

    assert record.category_slug.value == "apparel"
    assert record.subcategory_slug.value == "womens-fashion"
    assert not record.custom_category.is_present


# --------------------------------------------------------------------------
# Robustness
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        '{"description": "A stole."}',
        '```json\n{"description": "A stole."}\n```',
        'Here you go:\n{"description": "A stole."}\nHope that helps.',
    ],
)
def test_json_is_found_however_the_model_wrapped_it(raw: str) -> None:
    assert parse_response(raw)["description"] == "A stole."


@pytest.mark.parametrize("raw", ["not json at all", "", "[1, 2, 3]"])
def test_an_unusable_reply_raises_rather_than_returning_junk(raw: str) -> None:
    with pytest.raises((ValueError, json.JSONDecodeError)):
        parse_response(raw)


async def test_an_unparseable_reply_leaves_the_row_untouched(
    settings: Settings, vocabulary
) -> None:
    record = record_with(settings, description="The original text.")
    enricher = LlmEnricher(settings, FakeLlm("I'm afraid I can't do that"), vocabulary)

    await enricher.enhance(record, rewrite_wanted=True)

    assert record.description.value == "The original text."
    assert record.description.source is FieldSource.JSONLD
    assert any("could not be read as JSON" in note for note in record.notes)


async def test_a_model_outage_is_not_a_row_failure(settings: Settings, vocabulary) -> None:
    record = record_with(settings, description="The original text.")
    enricher = LlmEnricher(settings, FakeLlm(raises=RuntimeError("503 overloaded")), vocabulary)

    await enricher.enhance(record, rewrite_wanted=True)

    assert record.status is not RowStatus.FAILED
    assert record.description.value == "The original text."
    assert any("failed" in note for note in record.notes)


# --------------------------------------------------------------------------
# Cost control
# --------------------------------------------------------------------------


async def test_an_identical_prompt_is_answered_from_the_ledger(
    settings: Settings, vocabulary, tmp_path
) -> None:
    """A re-run or a --resume over the same catalogue costs nothing."""
    fake = FakeLlm({"description": "A handwoven cotton stole."})

    with Ledger(tmp_path / "ledger.db") as ledger:
        enricher = LlmEnricher(settings, fake, vocabulary, ledger)
        for _ in range(3):
            await enricher.enhance(record_with(settings), rewrite_wanted=True)

        assert len(fake.prompts) == 1
        assert enricher.calls == 1
        assert enricher.cache_hits == 2
        assert ledger.llm_count() == 1


async def test_a_cached_answer_for_another_model_is_not_reused(
    settings: Settings, vocabulary, tmp_path
) -> None:
    """A different model is a different answer."""
    fake = FakeLlm({"description": "A handwoven cotton stole."})

    with Ledger(tmp_path / "ledger.db") as ledger:
        first = settings.model_copy(deep=True)
        await LlmEnricher(first, fake, vocabulary, ledger).enhance(
            record_with(first), rewrite_wanted=True
        )

        second = settings.model_copy(deep=True)
        second.config.llm.model = "claude-haiku-4-5-20251001"
        await LlmEnricher(second, fake, vocabulary, ledger).enhance(
            record_with(second), rewrite_wanted=True
        )

    assert len(fake.prompts) == 2


async def test_the_budget_stops_and_says_so(settings: Settings, vocabulary) -> None:
    """A silent cap would look like the layer not working. Rows past the budget
    name it."""
    tuned = settings.model_copy(deep=True)
    tuned.config.llm.max_calls_per_run = 1
    tuned.config.llm.cache = False

    fake = FakeLlm({"description": "A handwoven cotton stole."})
    enricher = LlmEnricher(tuned, fake, vocabulary)

    first = record_with(tuned, description="First product.")
    second = record_with(tuned, description="Second product.")
    await enricher.enhance(first, rewrite_wanted=True)
    await enricher.enhance(second, rewrite_wanted=True)

    assert enricher.calls == 1
    assert second.description.source is FieldSource.JSONLD
    assert any("budget of 1 call(s)" in note for note in second.notes)


# --------------------------------------------------------------------------
# The prompt
# --------------------------------------------------------------------------


def test_the_prompt_hands_over_the_taxonomy_verbatim(settings: Settings) -> None:
    """The model chooses from a list; it does not recall one."""
    record = record_with(settings, category=settings.taxonomy.fallback_category)
    prompt = build_prompt(record, settings, ["category"])

    for slug in settings.taxonomy.categories:
        assert slug in prompt
    assert "inventing a slug" in prompt


def test_the_prompt_does_not_ask_for_a_price_or_a_measurement(settings: Settings) -> None:
    prompt = build_prompt(record_with(settings), settings, ["description", "category"])
    assert "leave it out" in prompt
    assert "price" in prompt  # named, in order to be excluded


# --------------------------------------------------------------------------
# Through the pipeline
# --------------------------------------------------------------------------


@respx.mock
async def test_third_party_provenance_rewrites_without_being_asked(
    settings: Settings, vocabulary
) -> None:
    """Rule 2.2: someone else's marketing copy is never passed through verbatim."""
    respx.get(PAGE_URL).mock(
        return_value=httpx.Response(200, html=SOURCE_HTML, headers={"content-type": "text/html"})
    )
    fake = FakeLlm({"description": "A stole of handwoven cotton."})

    async with build_http_client(settings) as client:
        record = await process_url(
            PAGE_URL,
            Provenance.THIRD_PARTY,
            settings,
            client,
            description_mode=DescriptionMode.RAW,
            enricher=LlmEnricher(settings, fake, vocabulary),
        )

    assert len(fake.prompts) == 1
    assert record.description.source is FieldSource.LLM
    assert "ELEVATE" not in (record.description.value or "")
    assert record.status is RowStatus.NEEDS_REVIEW


def test_third_party_provenance_forces_a_rewrite_regardless_of_the_flag(
    settings: Settings,
) -> None:
    own = new_record(PAGE_URL, Provenance.OWN)
    third = new_record(PAGE_URL, Provenance.THIRD_PARTY)

    assert not rewrite_wanted_for(own, DescriptionMode.RAW)
    assert rewrite_wanted_for(own, DescriptionMode.REWRITE)
    assert rewrite_wanted_for(third, DescriptionMode.RAW)


@respx.mock
async def test_a_run_without_llm_never_constructs_a_client(settings: Settings) -> None:
    respx.get(PAGE_URL).mock(
        return_value=httpx.Response(200, html=SOURCE_HTML, headers={"content-type": "text/html"})
    )
    async with build_http_client(settings) as client:
        record = await process_url(PAGE_URL, Provenance.OWN, settings, client)

    assert record.description.source is FieldSource.JSONLD


def test_the_fake_satisfies_the_client_protocol() -> None:
    assert isinstance(FakeLlm(), LlmClient)
