"""Phase 2: the live parse the Compose screen shows before anything runs.

The route exists because the browser used to do this itself, with its own URL
parser that split on newlines. A comma-separated paste of twelve links was
counted as one bad line and the operator was told so in confident red text --
§5's "no second implementation" rule, broken where it was most visible.

So the assertions here are mostly about agreement: this route, `preflight` and
`POST /api/jobs` must all say the same thing about the same paste, because they
are all the same function.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from haat_lister.api.app import create_app
from haat_lister.api.schemas import MAX_URLS, PREVIEW_LIMIT
from haat_lister.config import Settings

TWELVE = ", ".join(f"https://shop.example/p/{i}" for i in range(12))

AMAZON_TRACKED = (
    "https://www.amazon.in/Mivi-Marathon-Playtime-Wireless/dp/B0FTFMNYBV/"
    "?_encoding=UTF8&pd_rd_w=Xk9Lm&ref_=pd_hp_d_atf&th=1"
)


@pytest.fixture
def client(settings: Settings, tmp_path: Path) -> TestClient:
    return TestClient(create_app(settings.model_copy(deep=True, update={"root": tmp_path})))


def parse(client: TestClient, *lines: str) -> dict:
    response = client.post("/api/jobs/parse", json={"urls": list(lines)})
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------
# The headline case
# --------------------------------------------------------------------------


def test_a_comma_separated_paste_of_twelve_shows_twelve(client: TestClient) -> None:
    """§7 phase 2's done-when, asserted on the wire the console reads."""
    body = parse(client, TWELVE)

    assert body["unique"] == 12
    assert body["invalid"] == 0
    assert len(body["links"]) == 12
    assert [link["canonical"] for link in body["links"]] == [
        f"https://shop.example/p/{i}" for i in range(12)
    ]


def test_a_comma_inside_a_url_is_not_a_delimiter(client: TestClient) -> None:
    body = parse(client, "https://shop.example/p?ids=1,2,3")
    assert body["unique"] == 1
    assert body["links"][0]["canonical"] == "https://shop.example/p?ids=1,2,3"


# --------------------------------------------------------------------------
# What the preview has to show
# --------------------------------------------------------------------------


def test_both_forms_of_a_link_are_sent(client: TestClient) -> None:
    """The canonical is what runs; the original is the only one an operator
    recognises once nine tracking parameters have come off."""
    link = parse(client, AMAZON_TRACKED)["links"][0]
    assert link["canonical"] == "https://amazon.in/dp/B0FTFMNYBV"
    assert link["original"] == AMAZON_TRACKED
    assert link["host"] == "amazon.in"


def test_a_duplicate_is_listed_rather_than_removed(client: TestClient) -> None:
    """Struck through in the UI, not absent. A link that vanishes from the list
    looks like one that was lost."""
    body = parse(client, AMAZON_TRACKED, "https://www.amazon.in/dp/B0FTFMNYBV")

    assert body["unique"] == 1
    assert body["duplicates"] == 1
    assert [link["status"] for link in body["links"]] == ["ok", "duplicate"]
    assert body["links"][1]["note"] == "same product as link 1"


def test_an_assumed_scheme_is_marked(client: TestClient) -> None:
    body = parse(client, "amazon.in/dp/B0FTFMNYBV")
    assert body["links"][0]["assumed_scheme"] is True

    typed = parse(client, "https://amazon.in/dp/B0FTFMNYBV")
    assert typed["links"][0]["assumed_scheme"] is False


def test_unparsed_fragments_come_back_verbatim_with_a_line(client: TestClient) -> None:
    body = parse(client, "https://shop.example/p/1", "utter nonsense", "htps://typo.example/x")

    assert body["invalid"] == 2
    assert [f["raw"] for f in body["unparsed"]] == ["utter nonsense", "htps://typo.example/x"]
    assert [f["line"] for f in body["unparsed"]] == [2, 3]


def test_the_line_number_is_where_to_look_not_the_output_row(client: TestClient) -> None:
    """Once one line holds twelve links, an output index is not a place."""
    body = parse(client, "", "", "not a link at all")
    assert body["unparsed"][0]["line"] == 3


# --------------------------------------------------------------------------
# It is a preview, so it must be cheap and quiet
# --------------------------------------------------------------------------


def test_the_parse_touches_no_network(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Called on a keystroke. A robots.txt fetch per character would be a
    denial-of-service against the shops we are trying to be polite to."""
    import haat_lister.fetch.static as static

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("the parse preview opened a client")

    monkeypatch.setattr(static, "build_client", forbidden)
    body = parse(client, TWELVE)
    assert body["unique"] == 12


def test_it_needs_no_provenance(client: TestClient) -> None:
    """Asking for one to count links would teach an operator that the field is
    decorative. It is the one thing this tool refuses to assume."""
    response = client.post("/api/jobs/parse", json={"urls": ["https://shop.example/p/1"]})
    assert response.status_code == 200


def test_a_long_paste_is_capped_but_counted(client: TestClient) -> None:
    lines = [f"https://shop.example/p/{i}" for i in range(PREVIEW_LIMIT + 40)]
    body = parse(client, *lines)

    assert body["unique"] == PREVIEW_LIMIT + 40
    assert len(body["links"]) == PREVIEW_LIMIT
    assert body["truncated"] is True


def test_the_upload_cap_applies_here_too(client: TestClient) -> None:
    """§10.4. A message, not a 500 -- and not an unbounded regex either."""
    response = client.post(
        "/api/jobs/parse",
        json={"urls": [f"https://s{i}.example/p" for i in range(MAX_URLS + 1)]},
    )
    assert response.status_code == 413
    assert "line limit" in response.text


# --------------------------------------------------------------------------
# One parser, three callers
# --------------------------------------------------------------------------


def test_parse_preflight_and_create_agree_about_one_paste(client: TestClient) -> None:
    """The property the whole route exists for. A console showing a count the
    run does not honour is worse than showing no count."""
    lines = [TWELVE, "rubbish", AMAZON_TRACKED, "https://www.amazon.in/dp/B0FTFMNYBV"]

    preview = parse(client, *lines)
    flight = client.post(
        "/api/jobs/preflight",
        json={"urls": lines, "settings": {"provenance": "own"}},
    ).json()
    created = client.post(
        "/api/jobs",
        json={"urls": lines, "settings": {"provenance": "own"}},
    ).json()

    assert preview["unique"] == flight["unique"] == created["accepted"] == 13
    assert preview["duplicates"] == flight["duplicates"] == created["duplicates_removed"] == 1
    assert preview["invalid"] == len(flight["invalid"]) == len(created["invalid"]) == 1


def test_the_console_has_no_url_parser_of_its_own() -> None:
    """§5: one implementation of URL parsing, shared.

    The browser's copy split on newlines, which is precisely the defect this
    phase exists to fix -- so its absence is asserted rather than assumed.
    """
    web = Path(__file__).resolve().parents[1] / "web" / "src"
    for path in web.rglob("*.ts*"):
        source = path.read_text(encoding="utf-8")
        assert "countLines" not in source or path.name == "urls.ts", (
            f"{path.name} still counts links in the browser"
        )
    assert "canonical(" not in (web / "lib" / "urls.ts").read_text(encoding="utf-8")
