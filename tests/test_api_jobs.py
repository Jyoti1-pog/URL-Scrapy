"""v2 Phase 3: create, preflight, run, watch, cancel, resume -- over HTTP.

Two claims here are the reason SSE was chosen at all, and both are about what
happens when the connection is *not* perfect:

  A page refresh mid-job reconstructs the exact view, because every response is
  built from the ledger rather than from the runner's memory.

  A dropped connection resumes from `Last-Event-ID` rather than replaying, so a
  200-row job does not turn into 400 rows after one blip on the wifi.

The rest is the ordinary shape: provenance cannot be defaulted, one job runs at
a time, and cancel keeps what it finished.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from haat_lister.api.app import create_app
from haat_lister.api.events import BUFFER, EventBroker
from haat_lister.config import Settings
from haat_lister.jobs import is_job_id, job_paths
from haat_lister.models import (
    Confidence,
    FieldSource,
    FieldValue,
    ImageMethod,
    Provenance,
)
from haat_lister.pipeline import new_record

DEMO = "http://demo.invalid"


@pytest.fixture
def job_settings(settings: Settings, tmp_path: Path) -> Settings:
    tuned = settings.model_copy(deep=True, update={"root": tmp_path})
    tuned.config.fetch.per_domain_delay_s = 0.0
    tuned.config.fetch.per_domain_delay_jitter_s = 0.0
    tuned.config.fetch.respect_robots = False
    return tuned


class FakeProcessor:
    """Stands in for `process_url` so these tests are about the API.

    Extraction has four hundred tests of its own; running it again inside a
    stream test would only make the suite slow. It reports the same stages the
    real coroutine does, because the console renders them.
    """

    def __init__(self, fail_for: set[str] | None = None, work_s: float = 0.0) -> None:
        self.calls: list[str] = []
        self._fail_for = fail_for or set()
        self._work_s = work_s

    async def __call__(
        self,
        url: str,
        provenance: Provenance = Provenance.OWN,
        *_: object,
        on_stage=None,
        **__: object,
    ):
        self.calls.append(url)
        for name in ("fetching", "extracting", "enriching", "images"):
            if on_stage:
                on_stage(name)
        await asyncio.sleep(self._work_s)

        record = new_record(url, provenance)
        if url in self._fail_for:
            record.fail("http_503")
            return record
        record.title = FieldValue.found(
            f"Product {url.rsplit('/', 1)[-1]}", FieldSource.JSONLD, Confidence.HIGH
        )
        record.description = FieldValue.found(
            "Hand-embroidered in Kutch.", FieldSource.JSONLD, Confidence.HIGH
        )
        record.category_slug = FieldValue.found("apparel", FieldSource.INFERRED, Confidence.HIGH)
        record.subcategory_slug = FieldValue.found(
            "womens-fashion", FieldSource.INFERRED, Confidence.HIGH
        )
        record.image.method = ImageMethod.DIRECT
        record.image.reason = "direct_ok"
        if on_stage:
            on_stage("written")
        return record


@pytest.fixture
def processor() -> FakeProcessor:
    return FakeProcessor(work_s=0.004)


@pytest.fixture
def client(job_settings: Settings, processor: FakeProcessor) -> TestClient:
    with TestClient(create_app(job_settings, process=processor)) as test_client:
        yield test_client


def urls(n: int) -> list[str]:
    return [f"https://shop{i}.example/p/{i}" for i in range(n)]


def create(client: TestClient, url_list: list[str], **settings: object) -> dict:
    body = {
        "urls": url_list,
        "settings": {"provenance": "own", "concurrency": 4, **settings},
    }
    response = client.post("/api/jobs", json=body)
    assert response.status_code == 202, response.text
    return response.json()


def wait_for(client: TestClient, job_id: str, states=("done", "cancelled", "error")) -> dict:
    """Drain the SSE stream, which ends when the job does."""
    with client.stream("GET", f"/api/jobs/{job_id}/events") as stream:
        for _ in stream.iter_lines():
            pass
    state = client.get(f"/api/jobs/{job_id}").json()
    assert state["state"] in states, state["state"]
    return state


def sse_events(raw: str) -> list[tuple[int, str, dict]]:
    """Parse an SSE body into (id, event, data)."""
    out = []
    for block in raw.split("\n\n"):
        if not block.strip() or block.lstrip().startswith(":"):
            continue
        fields: dict[str, str] = {}
        for line in block.splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                fields[key.strip()] = value.strip()
        if "event" in fields:
            out.append((int(fields.get("id", 0)), fields["event"], json.loads(fields["data"])))
    return out


# --------------------------------------------------------------------------
# Creating
# --------------------------------------------------------------------------


def test_provenance_cannot_be_defaulted(client: TestClient) -> None:
    """The web equivalent of the CLI's panel: the run cannot start until a human
    says who made the content."""
    response = client.post("/api/jobs", json={"urls": urls(2), "settings": {}})
    assert response.status_code == 422
    assert "provenance" in response.text


def test_an_unknown_provenance_is_refused(client: TestClient) -> None:
    response = client.post(
        "/api/jobs", json={"urls": urls(1), "settings": {"provenance": "probably-fine"}}
    )
    assert response.status_code == 422


def test_create_reports_the_collapse_rather_than_hiding_it(client: TestClient) -> None:
    body = create(
        client,
        [
            "https://shop.example/p/1",
            "https://shop.example/p/1?utm_source=x",
            "not-a-url",
            "https://shop.example/p/2",
        ],
    )
    assert is_job_id(body["job_id"])
    assert body["accepted"] == 2
    assert body["duplicates_removed"] == 1
    assert [i["raw"] for i in body["invalid"]] == ["not-a-url"]


def test_a_list_with_nothing_usable_is_refused(client: TestClient) -> None:
    response = client.post(
        "/api/jobs", json={"urls": ["nope", "also nope"], "settings": {"provenance": "own"}}
    )
    assert response.status_code == 422
    assert "No usable product links" in response.text


def test_the_upload_cap_is_a_message_not_a_500(client: TestClient) -> None:
    response = client.post(
        "/api/jobs",
        json={"urls": [f"https://s{i}.example/p" for i in range(10_001)],
              "settings": {"provenance": "own"}},
    )
    assert response.status_code == 413
    assert "10,000-line limit" in response.text

    huge = ["https://s.example/" + "x" * 3000 for _ in range(800)]
    response = client.post("/api/jobs", json={"urls": huge, "settings": {"provenance": "own"}})
    assert response.status_code == 413


# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------


def test_preflight_fetches_no_product_page(client: TestClient, job_settings: Settings) -> None:
    """A preflight that fetched product pages would be the job."""
    body = {
        "urls": [*urls(5), urls(5)[0], "bad"],
        "settings": {"provenance": "own", "concurrency": 5},
    }
    data = client.post("/api/jobs/preflight", json=body).json()

    assert data["pasted"] == 7
    assert data["unique"] == 5
    assert data["duplicates"] == 1
    assert len(data["invalid"]) == 1
    assert len(data["domains"]) == 5
    assert data["estimate_high_s"] >= data["estimate_low_s"]
    # Nothing was created.
    assert client.get("/api/jobs").json() == []


def test_preflight_estimate_respects_the_per_domain_floor(
    client: TestClient, job_settings: Settings
) -> None:
    job_settings.config.fetch.per_domain_delay_s = 2.0
    one_domain = [f"https://oneshop.example/p/{i}" for i in range(100)]
    data = client.post(
        "/api/jobs/preflight",
        json={"urls": one_domain, "settings": {"provenance": "own", "concurrency": 20}},
    ).json()

    assert data["estimate_low_s"] >= 100 * 2.0, "concurrency cannot beat one host's spacing"


# --------------------------------------------------------------------------
# Running, from curl's point of view
# --------------------------------------------------------------------------


def test_a_job_runs_and_the_stream_reports_it(client: TestClient) -> None:
    body = create(client, urls(4))
    job_id = body["job_id"]

    with client.stream("GET", f"/api/jobs/{job_id}/events") as stream:
        raw = "".join(stream.iter_text())

    events = sse_events(raw)
    names = [name for _, name, _ in events]

    assert "job_started" in names
    assert names[-1] == "job_done"
    assert names.count("row_done") + names.count("row_failed") == 4
    # Real stages, not a spinner.
    stages = {data["stage"] for _, name, data in events if name == "row_stage"}
    assert {"fetching", "written"} <= stages

    done = next(data for _, name, data in events if name == "job_done")
    assert done["state"] == "done"
    assert Path(done["directory"]).name == job_id


def test_row_events_carry_the_image_tier(client: TestClient) -> None:
    """Section 7: Rule 1's ratio should be visible while it happens."""
    job_id = create(client, urls(2))["job_id"]
    with client.stream("GET", f"/api/jobs/{job_id}/events") as stream:
        raw = "".join(stream.iter_text())

    rows = [d for _, n, d in sse_events(raw) if n in ("row_done", "row_failed")]
    assert rows
    assert all("image_tier" in r for r in rows)


def test_event_ids_are_monotonic(client: TestClient) -> None:
    job_id = create(client, urls(3))["job_id"]
    with client.stream("GET", f"/api/jobs/{job_id}/events") as stream:
        raw = "".join(stream.iter_text())

    ids = [i for i, _, _ in sse_events(raw)]
    assert ids == sorted(ids)
    assert len(ids) == len(set(ids))


# --------------------------------------------------------------------------
# Refresh and reconnect
# --------------------------------------------------------------------------


def test_page_refresh_reconstructs_job_state(client: TestClient) -> None:
    """The client's whole recovery strategy is "refetch and start listening
    again", which only works if this is authoritative."""
    job_id = create(client, urls(6))["job_id"]
    wait_for(client, job_id)

    # A brand-new client: nothing carried over in memory.
    fresh = TestClient(client.app)
    state = fresh.get(f"/api/jobs/{job_id}").json()

    assert state["job_id"] == job_id
    assert state["state"] == "done"
    assert state["total"] == 6
    # `in_listings`, not `written`: the question here is how many rows the file
    # has, and those two differ by exactly the rows that need a human. `written`
    # is one of §1.1's four disjoint terminal states.
    assert state["in_listings"] == 6
    assert state["written"] + state["needs_human"] == state["in_listings"]
    assert len(state["rows"]) == 6
    assert [r["input_index"] for r in state["rows"]] == list(range(6))
    assert all(r["outcome"] for r in state["rows"])
    assert {a["name"] for a in state["artifacts"]} >= {"listings", "review", "failed"}
    assert state["settings"]["provenance"] == "own"


def test_sse_reconnect_no_duplicate_rows(client: TestClient) -> None:
    """A reconnect must resume, not replay. Otherwise one dropped connection
    turns a 200-row job into 400 rows on screen."""
    job_id = create(client, urls(8))["job_id"]
    wait_for(client, job_id)

    first = sse_events(client.get(f"/api/jobs/{job_id}/events").text)
    assert first
    cut = first[len(first) // 2][0]

    # What EventSource sends when it comes back.
    second = sse_events(
        client.get(f"/api/jobs/{job_id}/events", headers={"Last-Event-ID": str(cut)}).text
    )

    assert all(event_id > cut for event_id, _, _ in second)

    # What the client had already rendered when the connection dropped, versus
    # what it got when it came back. No row may appear in both.
    delivered = {d.get("row_key") for i, n, d in first if n == "row_done" and i <= cut}
    redelivered = {d.get("row_key") for _, n, d in second if n == "row_done"}
    assert delivered, "the cut was too early for the test to mean anything"
    assert not (delivered & redelivered), "a row arrived twice across the reconnect"

    # And between them they cover the job exactly once.
    kept = [i for i, _, _ in first if i <= cut] + [i for i, _, _ in second]
    assert kept == sorted(set(kept))


def test_a_gap_bigger_than_the_buffer_asks_for_a_resync(client: TestClient) -> None:
    """The honest answer to "you have been asleep for an hour" is refetch, not a
    partial replay the client cannot tell is partial."""
    broker = EventBroker()
    for i in range(BUFFER + 50):
        broker.publish("j_abcd1234", "row_done", index=i)

    async def first_frame() -> str:
        stream = broker.subscribe("j_abcd1234", last_event_id=1)
        try:
            return await anext(stream)
        finally:
            await stream.aclose()

    frame = asyncio.run(first_frame())
    assert "resync" in frame
    assert "/api/jobs/j_abcd1234" in frame, "the resync must say what to refetch"


def test_a_reconnect_inside_the_buffer_replays_only_the_gap() -> None:
    broker = EventBroker()
    for i in range(10):
        broker.publish("j_abcd1234", "row_done", index=i)

    async def frames() -> list[str]:
        out = []
        stream = broker.subscribe("j_abcd1234", last_event_id=6)
        try:
            for _ in range(4):
                out.append(await anext(stream))
        finally:
            await stream.aclose()
        return out

    got = asyncio.run(frames())
    ids = [int(frame.splitlines()[0].removeprefix("id: ")) for frame in got]
    assert ids == [7, 8, 9, 10]


# --------------------------------------------------------------------------
# Cancel and resume
# --------------------------------------------------------------------------


def test_cancel_preserves_completed_rows(client: TestClient, job_settings: Settings) -> None:
    job_id = create(client, urls(40), concurrency=2)["job_id"]

    # Cancel as soon as the stream shows work happening.
    with client.stream("GET", f"/api/jobs/{job_id}/events") as stream:
        seen = 0
        for line in stream.iter_lines():
            if line.startswith("event: row_done"):
                seen += 1
                if seen >= 3:
                    client.post(f"/api/jobs/{job_id}/cancel")
                    break

    state = wait_for(client, job_id, states=("cancelled", "done"))
    listings = job_paths(job_settings, job_id).listings
    assert listings.exists()

    rows = listings.read_text(encoding="utf-8").splitlines()
    assert len(rows) - 1 == state["in_listings"] >= 1, "a cancel threw away finished rows"


def test_cancelling_a_finished_job_is_a_409(client: TestClient) -> None:
    job_id = create(client, urls(2))["job_id"]
    wait_for(client, job_id)
    assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 409


def test_resume_processes_only_remainder(client: TestClient, job_settings: Settings) -> None:
    job_id = create(client, urls(30), concurrency=2)["job_id"]

    with client.stream("GET", f"/api/jobs/{job_id}/events") as stream:
        seen = 0
        for line in stream.iter_lines():
            if line.startswith("event: row_done"):
                seen += 1
                if seen >= 3:
                    client.post(f"/api/jobs/{job_id}/cancel")
                    break
    wait_for(client, job_id, states=("cancelled", "done"))

    before = client.get(f"/api/jobs/{job_id}").json()
    if before["state"] == "done":
        pytest.skip("the job finished before the cancel landed")

    response = client.post(f"/api/jobs/{job_id}/resume")
    assert response.status_code == 200
    wait_for(client, job_id)

    after = client.get(f"/api/jobs/{job_id}").json()
    assert after["written"] == 30
    assert after["state"] == "done"
    assert all(r["outcome"] for r in after["rows"])


def test_resuming_a_complete_job_says_so(client: TestClient) -> None:
    job_id = create(client, urls(2))["job_id"]
    wait_for(client, job_id)

    response = client.post(f"/api/jobs/{job_id}/resume")
    assert response.status_code == 409
    assert "Nothing left to do" in response.text


# --------------------------------------------------------------------------
# Shape and safety
# --------------------------------------------------------------------------


def test_a_bad_job_id_is_a_404_not_a_path(client: TestClient) -> None:
    """The only path component a client supplies; its shape is a control."""
    for bad in ("j_UPPER123", "j_short", "nonsense", "j_" + "a" * 9, "j_abc-1234"):
        assert client.get(f"/api/jobs/{bad}").status_code == 404, bad
        assert client.post(f"/api/jobs/{bad}/cancel").status_code == 404, bad
        assert client.post(f"/api/jobs/{bad}/resume").status_code == 404, bad

    # A traversal never reaches a file. `../..` is normalised out of the path
    # before the request is even sent, and the encoded form lands on the SPA
    # fallback -- so what comes back is the console, never something off disk.
    for attempt in ("../../.env", "%2e%2e%2f%2e%2e%2f.env", "..%2f..%2fconfig.yaml"):
        response = client.get(f"/api/jobs/{attempt}")
        assert "ANTHROPIC" not in response.text
        assert "user_agent_template" not in response.text
        assert "job_id" not in response.text, f"{attempt} reached job data"


def test_history_is_newest_first(client: TestClient) -> None:
    first = create(client, urls(1))["job_id"]
    wait_for(client, first)
    second = create(client, urls(2))["job_id"]
    wait_for(client, second)

    history = client.get("/api/jobs").json()
    assert [h["job_id"] for h in history][:2] == [second, first]
    assert all("counts" in h for h in history)


def test_one_job_at_a_time(client: TestClient) -> None:
    """Section 10.6: the rate limiter is per-domain, and three concurrent jobs
    would quietly triple the load on someone's server."""
    first = create(client, urls(6), concurrency=1)["job_id"]
    second = create(client, urls(2))

    assert second["queued_behind"] >= 0
    wait_for(client, first)
    wait_for(client, second["job_id"])

    for job_id in (first, second["job_id"]):
        assert client.get(f"/api/jobs/{job_id}").json()["state"] == "done"


def test_opening_a_finished_jobs_stream_is_instant(client: TestClient) -> None:
    """Caught by a test suite that took six and a half minutes for twenty-six
    tests.

    A finished job has its terminal event in the replay buffer, so subscribing
    delivers it immediately -- but the old code then fell into the wait loop and
    sat there for a full heartbeat before noticing the stream was closed. A
    browser landing on a completed job did exactly that.
    """
    import time

    from haat_lister.api.events import HEARTBEAT_S

    job_id = create(client, urls(2))["job_id"]
    wait_for(client, job_id)

    started = time.monotonic()
    events = sse_events(client.get(f"/api/jobs/{job_id}/events").text)
    elapsed = time.monotonic() - started

    assert any(name == "job_done" for _, name, _ in events)
    assert elapsed < HEARTBEAT_S / 3, f"took {elapsed:.1f}s to replay a finished job"
