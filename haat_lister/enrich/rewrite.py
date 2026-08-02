"""The optional `--llm` layer: rewriting descriptions, choosing a category.

Two jobs, and only two.

  **Rewrite a description** into the seller's own words. Required when
  provenance is third-party, where passing marketing copy through verbatim would
  be reproducing someone else's writing; useful anywhere a source description is
  a wall of SEO.

  **Choose a category** from `taxonomy.yaml` when keyword matching fell through
  to the fallback. A *choice from a closed list*, never an invention -- the model
  is given the exact slugs and anything else it returns is discarded.

What this layer may NEVER do, and cannot, because `RewriteResult` has nowhere to
put it: a price, a weight, a dimension, an HS code, a GI region, an availability
or a stock count. Those are money, customs and legal declarations. A plausible
number in one of them is strictly worse than a blank cell, because a blank cell
is visibly blank and a wrong number is not. The prompt says so too, but the
prompt is the second line of defence; the first is that there is no field.

Three further guards on what comes back:

  - The description is re-screened against the policy vocabulary. A rewrite that
    introduces a GI claim the source never made is flagged loudly, because that
    is exactly the kind of confident-sounding embellishment a model produces and
    a human skims past.
  - Slugs are validated against the taxonomy and dropped if unknown.
  - Everything is stamped `source=llm` at `medium` confidence, so `review.csv`'s
    low-confidence column lists every row a model touched.

Calls are cached in the ledger by prompt hash, so a re-run, a `--resume`, or a
second pass over the same catalogue costs nothing.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..config import LlmConfig, Settings
from ..models import (
    Confidence,
    DescriptionMode,
    FieldSource,
    FieldValue,
    ProductRecord,
    Provenance,
)
from ..policy.screen import Vocabulary, gi_mentions, screen_text
from ..store.ledger import Ledger
from ..utils.logging import get_logger

log = get_logger(__name__)

INSTALL_HINT = (
    'The --llm layer needs the optional extra:\n'
    '    pip install "haat-lister[llm]"\n'
    "and ANTHROPIC_API_KEY set in .env"
)


class LlmUnavailable(Exception):
    """No SDK, or no key. Never a row failure -- the run continues without it."""


SYSTEM_PROMPT = """\
You are helping a craft seller migrate their own product catalogue onto haat, an \
Indian marketplace for handmade goods.

Rules, in order of importance:

1. Never state a fact that is not in the source text. No invented materials, \
origins, techniques, dyes, artisan names, certifications, awards or history. If \
the source does not say where something was made, neither do you.
2. Never mention a price, a weight, a measurement, an HS code, a customs \
classification, or a Geographical Indication (GI) tag -- even if the source \
text does. Those are handled elsewhere in this system and anything you write \
would be read as a fact by a customs form.
3. No superlatives and no marketing filler. Not "elevate your wardrobe", not \
"a must-have", not "exquisite". Plain, specific, concrete sentences about what \
the object is and what it is made of.
4. Indian English.

Reply with a single JSON object and nothing else."""


@runtime_checkable
class LlmClient(Protocol):
    """Just enough of the Anthropic SDK to be swapped for a fake in tests."""

    async def complete(self, system: str, prompt: str, cfg: LlmConfig) -> str: ...


@dataclass
class RewriteResult:
    """Note the fields that are absent. That absence is the safety property."""

    description: str | None = None
    category_slug: str | None = None
    subcategory_slug: str | None = None
    omitted: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


class AnthropicClient:
    """Lazy import, so an install without the `[llm]` extra pays nothing."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._client: Any = None

    async def complete(self, system: str, prompt: str, cfg: LlmConfig) -> str:
        if self._client is None:
            try:
                from anthropic import AsyncAnthropic
            except ImportError as exc:
                raise LlmUnavailable(f"{INSTALL_HINT}\n({exc})") from exc
            self._client = AsyncAnthropic(api_key=self._api_key, timeout=cfg.timeout_s)

        message = await self._client.messages.create(
            model=cfg.model,
            max_tokens=cfg.max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(block.text for block in message.content if block.type == "text")

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None


def build_client(settings: Settings, enabled: bool) -> AnthropicClient | None:
    """None means the layer is off and there is no path to a model call.

    Same shape as `build_renderer`: absence of an object rather than a flag
    somebody could forget to check.
    """
    if not enabled:
        return None
    if not settings.secrets.has_llm_credentials:
        raise LlmUnavailable(
            "--llm was requested but ANTHROPIC_API_KEY is not set (or is empty).\n\n"
            + INSTALL_HINT
        )
    assert settings.secrets.anthropic_api_key is not None
    return AnthropicClient(settings.secrets.anthropic_api_key.get_secret_value())


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def llm_tasks(record: ProductRecord, settings: Settings, rewrite_wanted: bool) -> list[str]:
    """What a model would actually be for on this row. Empty means don't call.

    A model call is money and a second or two, so this is the same shape as the
    Stage B gate: nothing speculative, nothing "while we're here".
    """
    cfg = settings.config.llm
    tasks: list[str] = []

    if cfg.rewrite_descriptions and rewrite_wanted and record.description.is_present:
        tasks.append("description")

    if cfg.suggest_categories and _category_fell_through(record, settings):
        tasks.append("category")

    return tasks


def _category_fell_through(record: ProductRecord, settings: Settings) -> bool:
    """Keyword matching gave up and used the fallback bucket."""
    slug = record.category_slug.value
    return bool(slug) and slug == settings.taxonomy.fallback_category


# ---------------------------------------------------------------------------
# Prompt and parsing
# ---------------------------------------------------------------------------


def taxonomy_lines(settings: Settings) -> list[str]:
    """The exact closed vocabulary, given to the model verbatim."""
    lines = []
    for slug, category in sorted(settings.taxonomy.categories.items()):
        subs = ", ".join(sorted(category.subcategories)) or "(no subcategories)"
        lines.append(f"- {slug} ({category.label or slug}): {subs}")
    return lines


def build_prompt(record: ProductRecord, settings: Settings, tasks: list[str]) -> str:
    cfg = settings.config.llm
    parts = [
        "SOURCE PRODUCT",
        f"Title: {record.title.value or '(none)'}",
        f"Description: {record.description.value or '(none)'}",
        "",
    ]

    if "description" in tasks:
        parts += [
            f"TASK: rewrite the description in at most {cfg.max_description_words} words, in "
            "the seller's own voice. Keep every concrete fact the source states; drop "
            "everything it does not. If the source states a price, a measurement, or a GI "
            "tag, leave it out and list it under `omitted`.",
            "",
        ]

    if "category" in tasks:
        parts += [
            "TASK: choose the single best category and subcategory from this list. Use the "
            "slugs exactly as written. If nothing fits, return null for both rather than "
            "inventing a slug.",
            *taxonomy_lines(settings),
            "",
        ]

    schema = {
        "description": "string or null",
        "category_slug": "string or null",
        "subcategory_slug": "string or null",
        "omitted": ["short phrases you deliberately left out"],
    }
    parts += ["Reply with this JSON shape and nothing else:", json.dumps(schema, indent=2)]
    return "\n".join(parts)


def parse_response(raw: str) -> dict[str, Any]:
    """Tolerant of a model wrapping JSON in prose or a fence, strict about the rest."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text[3:]
        text = text.removeprefix("json").strip()

    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in the response")

    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("response JSON was not an object")
    return parsed


def _clean_description(value: object, cfg: LlmConfig, max_chars: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text:
        return None
    words = text.split(" ")
    if len(words) > cfg.max_description_words:
        text = " ".join(words[: cfg.max_description_words]).rstrip(",;:") + "…"
    return text[:max_chars]


def interpret(
    payload: dict[str, Any], record: ProductRecord, settings: Settings, tasks: list[str]
) -> RewriteResult:
    """Turn a parsed response into something the record may safely absorb.

    Everything the model returned that we did not ask for is dropped on the
    floor here, silently and by construction: this function only ever reads the
    keys it knows about.
    """
    result = RewriteResult()
    cfg = settings.config.llm

    if "description" in tasks:
        result.description = _clean_description(
            payload.get("description"), cfg, settings.config.csv.max_description_length
        )

    if "category" in tasks:
        parent = payload.get("category_slug")
        child = payload.get("subcategory_slug")
        taxonomy = settings.taxonomy

        if isinstance(parent, str) and taxonomy.has_category(parent):
            result.category_slug = parent
            if isinstance(child, str) and taxonomy.has_subcategory(parent, child):
                result.subcategory_slug = child
            elif child:
                result.notes.append(
                    f"The model suggested subcategory '{child}', which is not under "
                    f"'{parent}' in taxonomy.yaml. It was discarded; the parent was kept."
                )
        elif parent:
            # The one thing a closed vocabulary exists to prevent.
            result.notes.append(
                f"The model suggested category '{parent}', which is not in taxonomy.yaml. "
                "It was discarded and the keyword-matched category stands."
            )

    omitted = payload.get("omitted")
    if isinstance(omitted, list):
        result.omitted = [str(item) for item in omitted if str(item).strip()][:10]

    return result


# ---------------------------------------------------------------------------
# Applying, with the output guards
# ---------------------------------------------------------------------------


def _check_rewrite(
    record: ProductRecord, rewritten: str, vocabulary: Vocabulary
) -> list[str]:
    """Did the rewrite invent a claim the source never made?

    A model embellishing "handwoven cotton" into "GI-tagged Kanchipuram silk" is
    the failure mode this layer is most likely to produce and the one a human is
    least likely to catch, because it reads better than the original.
    """
    flags: list[str] = []
    original = f"{record.title.value or ''} {record.description.value or ''}"

    before = {hit.term for hit in gi_mentions(screen_text("", original, vocabulary), vocabulary)}
    after_hits = screen_text("", rewritten, vocabulary)
    after = {hit.term for hit in gi_mentions(after_hits, vocabulary)}

    if invented := sorted(after - before):
        flags.append(
            f"The rewritten description mentions {', '.join(invented)}, which the source text "
            "did not. A GI claim this tool invented is not a claim the seller can stand behind "
            "-- read the new description before importing."
        )

    new_policy = {hit.flag for hit in after_hits} - {
        hit.flag for hit in screen_text("", original, vocabulary)
    }
    if new_policy:
        flags.append(
            f"The rewritten description introduced policy-screened wording "
            f"({', '.join(sorted(new_policy))}) that the source did not contain."
        )
    return flags


def apply(
    record: ProductRecord, result: RewriteResult, settings: Settings, vocabulary: Vocabulary
) -> None:
    """Fold a model's output onto the record, at medium confidence, stamped llm."""
    if result.description:
        for flag in _check_rewrite(record, result.description, vocabulary):
            record.flag(flag)
        record.description = FieldValue.found(
            result.description,
            FieldSource.LLM,
            Confidence.MEDIUM,
            note="Rewritten by a language model from the source description.",
        )
        record.note(
            "The description was rewritten by a language model and is not the seller's own "
            "words yet. Read it before importing; it is listed as low-confidence for that "
            "reason."
        )

    if result.category_slug:
        record.category_slug = FieldValue.found(
            result.category_slug, FieldSource.LLM, Confidence.MEDIUM
        )
        record.subcategory_slug = (
            FieldValue.found(result.subcategory_slug, FieldSource.LLM, Confidence.MEDIUM)
            if result.subcategory_slug
            else FieldValue()
        )
        # Keyword matching had given up and filed this under the fallback with a
        # made-up custom name; a real slug replaces both.
        record.custom_category = FieldValue()
        record.note(
            f"Category '{result.category_slug}"
            f"{'/' + result.subcategory_slug if result.subcategory_slug else ''}' was chosen by "
            "a language model from taxonomy.yaml, because keyword matching found nothing. "
            "Confirm it before importing."
        )

    if result.omitted:
        record.note(
            "The rewrite deliberately left out: "
            + "; ".join(result.omitted)
            + ". Those are handled by other columns, or are claims this tool will not make."
        )

    for note in result.notes:
        record.note(note)
    for flag in result.flags:
        record.flag(flag)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _prompt_hash(system: str, prompt: str, model: str) -> str:
    digest = hashlib.sha256()
    for part in (system, prompt, model):
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


class LlmEnricher:
    """Holds the client, the cache and the per-run budget."""

    def __init__(
        self,
        settings: Settings,
        client: LlmClient | None,
        vocabulary: Vocabulary,
        ledger: Ledger | None = None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._vocabulary = vocabulary
        self._ledger = ledger
        self.calls = 0
        self.cache_hits = 0
        self._budget_announced = False

    @property
    def enabled(self) -> bool:
        return self._client is not None

    async def enhance(self, record: ProductRecord, rewrite_wanted: bool) -> None:
        """Never raises. A model that is down is not a reason to lose a row."""
        if self._client is None:
            return

        tasks = llm_tasks(record, self._settings, rewrite_wanted)
        if not tasks:
            return

        cfg = self._settings.config.llm
        if cfg.max_calls_per_run and self.calls >= cfg.max_calls_per_run:
            if not self._budget_announced:
                log.warning(
                    "llm.max_calls_per_run (%d) reached; the rest of this run is unassisted",
                    cfg.max_calls_per_run,
                )
                self._budget_announced = True
            record.note(
                f"The --llm budget of {cfg.max_calls_per_run} call(s) was already spent, so "
                f"this row was not {' or '.join(tasks)}-assisted. Raise "
                "llm.max_calls_per_run to cover more rows."
            )
            return

        prompt = build_prompt(record, self._settings, tasks)
        key = _prompt_hash(SYSTEM_PROMPT, prompt, cfg.model)

        raw = self._cached(key) if cfg.cache else None
        if raw is None:
            try:
                raw = await self._client.complete(SYSTEM_PROMPT, prompt, cfg)
            except LlmUnavailable as exc:
                log.warning("--llm unavailable: %s", exc)
                record.note(f"This row was not language-model assisted: {exc}")
                return
            except Exception as exc:  # noqa: BLE001 -- a model outage is not a row failure
                log.exception("Model call failed for %s", record.source_url)
                record.note(
                    f"The language model call for this row failed ({exc!r}); the extracted "
                    "values stand unchanged."
                )
                return
            self.calls += 1
            if cfg.cache and self._ledger is not None:
                self._ledger.record_llm(key, cfg.model, raw)

        try:
            payload = parse_response(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            log.warning("Unparseable model response for %s: %s", record.source_url, exc)
            record.note(
                "The language model's reply could not be read as JSON, so it was ignored and "
                "the extracted values stand unchanged."
            )
            return

        result = interpret(payload, record, self._settings, tasks)
        apply(record, result, self._settings, self._vocabulary)

    def _cached(self, key: str) -> str | None:
        if self._ledger is None:
            return None
        hit = self._ledger.find_llm(key, self._settings.config.llm.model)
        if hit is not None:
            self.cache_hits += 1
        return hit


def rewrite_wanted_for(record: ProductRecord, mode: DescriptionMode) -> bool:
    """Third-party copy is always rewritten; anyone else's is on request.

    `effective_description_mode` already forces REWRITE for third-party rows, so
    the second clause is belt and braces -- but this is the function that decides
    whether someone else's marketing copy gets copied verbatim into a listing,
    and that is worth two conditions.
    """
    return mode is DescriptionMode.REWRITE or record.provenance is Provenance.THIRD_PARTY
