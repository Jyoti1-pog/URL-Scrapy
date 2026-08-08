"""Phase 0: the report that makes every later phase diagnosable.

The bar these tests hold: `diagnose` must never be the thing that is wrong.
An operator uses it to decide whether the extractor or the page is at fault, so
a report that disagrees with the pipeline is worse than no report at all. Hence
the first test, which is the important one -- the candidate list in the report
is asserted to be the list the pipeline actually walks, not a lookalike.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from selectolax.parser import HTMLParser

from haat_lister.config import Settings
from haat_lister.diagnose import diagnose_url, human_bytes, steps_for
from haat_lister.extract.images import RULES, CollectionTrace, collect_candidates
from haat_lister.extract.structured import extract_structured
from haat_lister.fetch.shape import inspect
from haat_lister.images.reasons import REASONS, NoImageReason, explain
from haat_lister.images.validator import EVALUATION_ORDER
from haat_lister.models import ValidationResult

PAGE = """
<html><head>
  <title>Blue Kurta | Handloom Co</title>
  <meta property="og:image" content="https://cdn.example/hero.jpg">
  <meta property="og:type" content="product">
</head><body>
  <h1>Indigo Handloom Kurta</h1>
  <p>Price Rs 2,400</p>
  <button>Add to cart</button>
  <img src="/img/gallery-1.jpg" srcset="/img/gallery-1.jpg 1200w">
  <img src="/img/icon-cart.svg">
</body></html>
"""


def _dom(html: str) -> HTMLParser:
    return HTMLParser(html)


def png_bytes(width: int = 1200, height: int = 1200) -> bytes:
    """A real PNG, and a noisy one.

    Two things this fixture had to learn the hard way. A hand-rolled header with
    no CRC is undecodable, so predicate 6 rejects it. And a solid-colour PNG of
    any size compresses to a couple of KB, so predicate 5 rejects it for being
    below the 10 KB floor -- correctly, which is how the test found out.
    """
    import io
    import os

    from PIL import Image

    noise = Image.frombytes("RGB", (width, height), os.urandom(width * height * 3))
    buffer = io.BytesIO()
    noise.save(buffer, format="PNG")
    return buffer.getvalue()


# --------------------------------------------------------------------------
# The report describes the real run
# --------------------------------------------------------------------------


def test_the_traced_collection_is_the_same_call_the_pipeline_makes(settings: Settings) -> None:
    """The trace must not become a second implementation.

    `collect_candidates` fills a trace when handed one and is expected to behave
    identically otherwise. If passing a trace could change the ranking, every
    report would describe a run that never happened.
    """
    cfg = settings.config
    dom = _dom(PAGE)
    structured = extract_structured(PAGE, "https://shop.example/p/1", dom)

    plain = collect_candidates(
        structured, dom, "https://shop.example/p/1", cfg.images, cfg.validator
    )
    trace = CollectionTrace()
    traced = collect_candidates(
        structured, dom, "https://shop.example/p/1", cfg.images, cfg.validator, trace
    )

    assert [c.url for c in plain] == [c.url for c in traced]
    assert [c.url for c in trace.kept] == [c.url for c in plain]


def test_every_raw_reference_is_either_kept_or_has_a_stated_reason(settings: Settings) -> None:
    """No reference disappears without a word. That silence is the bug."""
    cfg = settings.config
    dom = _dom(PAGE)
    structured = extract_structured(PAGE, "https://shop.example/p/1", dom)

    trace = CollectionTrace()
    collect_candidates(
        structured, dom, "https://shop.example/p/1", cfg.images, cfg.validator, trace
    )

    assert trace.raw, "the fixture has images; collection found none"
    assert all(why for _, why in trace.dropped), "a reference was dropped with a blank reason"
    # The icon is rejected by substring, and says so.
    assert any("reject_url_substrings" in why for _, why in trace.dropped)


def test_every_candidate_carries_the_rule_that_found_it(settings: Settings) -> None:
    cfg = settings.config
    dom = _dom(PAGE)
    structured = extract_structured(PAGE, "https://shop.example/p/1", dom)

    trace = CollectionTrace()
    collect_candidates(
        structured, dom, "https://shop.example/p/1", cfg.images, cfg.validator, trace
    )

    assert all(c.rule in RULES for c in trace.raw), "a rule label is missing or misspelled"
    counts = trace.by_rule()
    assert set(counts) == set(RULES), "by_rule() must name every rule, including the empty ones"
    assert counts["og:image"] == 1
    assert counts["background-image"] == 0


# --------------------------------------------------------------------------
# Predicate reconstruction
# --------------------------------------------------------------------------


def test_the_stated_evaluation_order_matches_the_validator(settings: Settings) -> None:
    """EVALUATION_ORDER is a claim about `validate`. Read the source and check it.

    Without this, the constant and the function drift and every report starts
    telling operators that predicates passed which were never run.
    """
    import inspect as py_inspect

    from haat_lister.images.validator import Tier1Validator

    # Matched on the CHECK, not on the result it builds. Predicate 5 is decided
    # twice -- once on Content-Length and once, deferred, on bytes actually read
    # after predicate 6 has opened the file -- so the order the results are
    # constructed in is not the order the checks run in.
    checks = {
        1: "check_syntax(",
        8: "check_not_signed(",
        9: "check_host_reputation(",
        2: "self._probe(",
        3: "check_redirect_chain(",
        4: "check_content_type(",
        5: "check_size_floor(",
        6: "check_dimensions(",
        7: "self._hotlink_ok(",
    }

    source = py_inspect.getsource(Tier1Validator.validate)
    positions = []
    for number, _ in EVALUATION_ORDER:
        marker = checks[number]
        assert marker in source, f"predicate {number} is no longer evaluated in validate()"
        positions.append(source.index(marker))

    assert positions == sorted(positions), (
        "EVALUATION_ORDER no longer matches the order validate() evaluates in; "
        "every diagnose report is now claiming predicates passed that never ran"
    )


def test_a_failure_shows_what_passed_before_it() -> None:
    """"failed 6" alone hides that it got a 200, the right content-type and 48 KB."""
    result = ValidationResult(
        url="https://cdn.example/a.jpg",
        ok=False,
        reason="below_min_dimensions",
        predicate=6,
        content_type="image/jpeg",
        content_length=49152,
        width=679,
        height=679,
    )
    steps = steps_for(result, hotlink_test=True)

    by_number = {s.predicate: s for s in steps}
    assert by_number[4].outcome == "ok"
    assert by_number[4].detail == "image/jpeg"
    assert by_number[5].detail == "48 KB"
    assert by_number[6].outcome == "fail"
    assert "679x679" in by_number[6].detail
    # Predicate 7 runs after 6, so it never happened.
    assert by_number[7].outcome == "not reached"


def test_a_pass_reports_all_nine_as_passed() -> None:
    result = ValidationResult(
        url="https://cdn.example/a.jpg", ok=True, reason="direct_ok", width=1200, height=1200
    )
    steps = steps_for(result, hotlink_test=True)
    assert len(steps) == len(EVALUATION_ORDER)
    assert all(s.outcome == "ok" for s in steps)


def test_a_skipped_hotlink_test_is_labelled_not_passed() -> None:
    """An optimistic result must not read as a clean one."""
    result = ValidationResult(url="https://cdn.example/a.jpg", ok=True, reason="direct_ok")
    seven = next(s for s in steps_for(result, hotlink_test=False) if s.predicate == 7)
    assert seven.outcome == "skipped"
    assert "optimistic" in seven.detail


# --------------------------------------------------------------------------
# Page shape
# --------------------------------------------------------------------------


def test_a_captcha_page_is_named_as_one() -> None:
    html = """<html><body><h4>Enter the characters you see below</h4>
    <p>Sorry, we just need to make sure you're not a robot.</p></body></html>"""
    shape = inspect(html, "https://www.amazon.in/errors/validateCaptcha")
    assert shape.verdict is NoImageReason.BOT_CHALLENGE
    assert shape.captcha
    assert shape.evidence, "a verdict with no evidence cannot be argued with"


def test_a_real_product_page_that_says_currently_unavailable_is_not_condemned() -> None:
    """Caught on the very first real run.

    Amazon's live product page carries "currently unavailable" in its furniture
    while selling the product perfectly well. A shape check that fired on the
    string alone would fail good rows, which is worse than the silence it
    replaces -- so the weak signals need corroboration.
    """
    html = """<html><head><meta property="og:type" content="product"></head>
    <body><h1>Kurta</h1><p>Rs 2,400</p><button>Add to cart</button>
    <span>Some sizes currently unavailable</span></body></html>"""
    shape = inspect(html, "https://shop.example/p/1")
    assert shape.unavailable, "the marker should still be observed"
    assert shape.verdict is None, "...but not acted on, because this is clearly a product page"
    assert shape.looks_like_product


def test_an_unavailable_page_with_no_product_signal_is_condemned() -> None:
    html = "<html><body><h1>Sorry</h1><p>This item is not available.</p></body></html>"
    shape = inspect(html, "https://shop.example/p/1")
    assert shape.verdict is NoImageReason.NOT_A_PRODUCT_PAGE


def test_a_login_wall_is_only_a_wall_when_the_product_is_absent() -> None:
    wall = "<html><body><form>Sign in to continue</form></body></html>"
    assert inspect(wall, "https://shop.example/ap/signin").verdict is NoImageReason.SIGN_IN_REQUIRED

    shop = """<html><body><h1>Kurta</h1><p>Rs 999</p><button>Add to cart</button>
    <a href="/login">Sign in</a></body></html>"""
    assert inspect(shop, "https://shop.example/p/1").verdict is None


def test_shape_never_raises_on_junk() -> None:
    for html in ("", "<html>", "not html at all", "<html><body></body></html>"):
        assert inspect(html, "https://shop.example/p/1") is not None


# --------------------------------------------------------------------------
# The reason enum
# --------------------------------------------------------------------------


def test_every_reason_has_a_sentence_a_person_can_act_on() -> None:
    """The enum exists so `none` is never bare. A member with no explanation
    would put us straight back where we started."""
    for reason in NoImageReason:
        assert reason in REASONS, f"{reason} has no explanation"
        words = REASONS[reason].what_to_do.split()
        assert len(words) >= 8, f"{reason} has no usable next action"


def test_an_unknown_reason_is_returned_verbatim_not_swallowed() -> None:
    assert explain("something_invented") == "something_invented"


def test_the_enum_covers_the_reasons_the_spec_names() -> None:
    required = {
        "no_image_candidates",
        "bot_challenge",
        "all_candidates_rejected",
                        "robots_disallowed",
    }
    assert required <= {r.value for r in NoImageReason}


# --------------------------------------------------------------------------
# End to end, against a served page
# --------------------------------------------------------------------------


@respx.mock
async def test_a_page_with_no_images_says_so_by_name(settings: Settings) -> None:
    respx.get("https://shop.example/p/1").mock(
        return_value=httpx.Response(
            200,
            html="<html><body><h1>Kurta</h1><p>Rs 900</p></body></html>",
            headers={"content-type": "text/html"},
        )
    )
    settings.config.render.enabled = False
    settings.config.fetch.respect_robots = False

    report = await diagnose_url("https://shop.example/p/1", settings)

    assert report.images.method == "none"
    assert report.images.reason == NoImageReason.NO_IMAGE_CANDIDATES.value
    assert report.images.explanation, "a bare reason is the defect this command exists to fix"
    assert report.fetch.ok
    assert report.title.value == "Kurta"
    # Every rule is listed, including the six that found nothing.
    assert {r.rule for r in report.images.rules} == set(RULES)


@respx.mock
async def test_a_blocked_page_is_reported_as_blocked_not_as_no_photo(settings: Settings) -> None:
    """The conflation that made the original bug invisible."""
    respx.get("https://shop.example/p/1").mock(
        return_value=httpx.Response(
            200,
            html="<html><body><h4>Enter the characters you see below</h4></body></html>",
            headers={"content-type": "text/html"},
        )
    )
    settings.config.render.enabled = False
    settings.config.fetch.respect_robots = False

    report = await diagnose_url("https://shop.example/p/1", settings)

    assert report.shape.captcha
    assert report.images.reason == NoImageReason.BOT_CHALLENGE.value
    assert "not try to defeat" in report.images.explanation
    # Phase 0 only observed this; Phase 3 acts on it. The report says which,
    # because a report that quietly changed meaning between versions is the kind
    # of thing that gets believed for a year.
    assert report.shape_enforced


@respx.mock
async def test_a_failed_fetch_is_an_answer_not_an_exception(settings: Settings) -> None:
    respx.get("https://shop.example/p/1").mock(return_value=httpx.Response(503))
    settings.config.render.enabled = False
    settings.config.fetch.respect_robots = False

    report = await diagnose_url("https://shop.example/p/1", settings)

    assert not report.fetch.ok
    assert report.fetch.error_reason == "http_503"
    # v4 §11: the wire-level cause, not the bucket. `http_503` tells an
    # operator the shop is down; `page_fetch_failed` told them nothing.
    assert report.images.reason == "http_error_5xx"


@respx.mock
async def test_diagnose_writes_nothing_and_contacts_no_image_host(
    settings: Settings, tmp_path
) -> None:
    """§4.1's promise, asserted rather than described.

    Run against an empty root so the claim can be checked by listing it: the
    ledger is the only thing allowed to appear. No CSV, no downloaded bytes, no
    normalised deliverable, and no host adapter ever constructed -- Tier 2 is
    not on this path at all.
    """
    respx.get("https://shop.example/p/1").mock(
        return_value=httpx.Response(
            200,
            html='<html><body><h1>Kurta</h1><img src="https://cdn.example/a.jpg"></body></html>',
            headers={"content-type": "text/html"},
        )
    )
    respx.head("https://cdn.example/a.jpg").mock(return_value=httpx.Response(404))
    respx.get("https://cdn.example/a.jpg").mock(return_value=httpx.Response(404))

    empty = settings.model_copy(deep=True, update={"root": tmp_path})
    empty.config.render.enabled = False
    empty.config.fetch.respect_robots = False

    with pytest.MonkeyPatch.context() as patch:
        def refuse(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("diagnose built an image host")

        patch.setattr("haat_lister.images.hosts.build_hosts", refuse)
        report = await diagnose_url("https://shop.example/p/1", empty)

    assert report.images.method == "none"

    ledger_dir = (tmp_path / empty.config.paths.ledger).parent
    written = {
        path.relative_to(tmp_path).as_posix()
        for path in tmp_path.rglob("*")
        if path.is_file() and ledger_dir not in path.parents
    }
    assert not written, f"diagnose wrote files it should not have: {sorted(written)}"


@respx.mock
async def test_a_winner_stops_the_walk_and_the_rest_say_why_they_were_skipped(
    settings: Settings,
) -> None:
    """The report must not read as a survey of the whole gallery when it isn't."""
    png = png_bytes(1200, 1200)

    html = (
        "<html><body><h1>Kurta</h1>"
        '<img src="https://cdn.example/a.png">'
        '<img src="https://cdn.example/b.png">'
        "</body></html>"
    )
    respx.get("https://shop.example/p/1").mock(
        return_value=httpx.Response(200, html=html, headers={"content-type": "text/html"})
    )
    for name in ("a", "b"):
        respx.head(f"https://cdn.example/{name}.png").mock(
            return_value=httpx.Response(
                200, headers={"content-type": "image/png", "content-length": str(len(png))}
            )
        )
        respx.get(f"https://cdn.example/{name}.png").mock(
            return_value=httpx.Response(200, content=png, headers={"content-type": "image/png"})
        )

    settings.config.render.enabled = False
    settings.config.fetch.respect_robots = False
    settings.config.validator.hotlink_test = False

    report = await diagnose_url("https://shop.example/p/1", settings)

    assert report.images.method == "direct"
    assert report.images.candidates[0].checked and report.images.candidates[0].ok
    assert [c.checked for c in report.images.candidates[1:]] == [False] * (
        len(report.images.candidates) - 1
    )


def test_human_bytes_is_readable_at_every_scale() -> None:
    assert human_bytes(None) == "?"
    assert human_bytes(867) == "867 B"
    assert human_bytes(49152) == "48 KB"
    assert human_bytes(2_500_000) == "2.4 MB"
