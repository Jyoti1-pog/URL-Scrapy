"""SQLite ledger. In v2 it is the SOURCE OF TRUTH, not a cache.

Every output file -- listings.csv, review.csv, the manifest, failed.csv -- is a
projection of this database, regenerable at any moment. That resolves the direct
conflict between "rows come out in input order" and "write incrementally, never
hold the batch in memory": rows commit here the instant they complete, tagged
with the position they were pasted at, and the CSV is rendered from here in
index order. It also means the web console's every response, a mid-run download,
and a re-export after edits are all the same operation rather than three.

Calls are synchronous. SQLite writes here take microseconds against network
operations measured in hundreds of milliseconds, so wrapping them in threads
would cost more than it saves.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..utils.logging import get_logger

log = get_logger(__name__)

# Bumped when the shape changes. `_migrate` walks a ledger up from any older
# version rather than asking an operator to delete their run history.
SCHEMA_VERSION = 2

_SCHEMA = """
-- One per run, CLI or web. `settings` is the job.json payload verbatim, so
-- "why is this CSV different from the one six weeks ago" has an answer.
CREATE TABLE IF NOT EXISTS jobs (
    job_id      TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    finished_at TEXT,
    state       TEXT NOT NULL,
    settings    TEXT NOT NULL,
    input_count INTEGER NOT NULL DEFAULT 0
);

-- The accounting ledger, and the reason "every input URL is in exactly one
-- output file" is checkable rather than aspirational. A row lands here for
-- every pasted line at job creation, with outcome NULL; the job cannot be
-- finalised while any outcome is still NULL.
CREATE TABLE IF NOT EXISTS job_urls (
    job_id      TEXT NOT NULL,
    input_index INTEGER NOT NULL,
    source_url  TEXT NOT NULL,
    canonical   TEXT NOT NULL,
    outcome     TEXT,
    row_key     TEXT,
    reason      TEXT,
    PRIMARY KEY (job_id, input_index)
);

-- Section 4 edits. Kept apart from `rows` so the original extraction is never
-- destructively overwritten: a re-export applies these on top of what was
-- scraped, and removing an edit restores what the page actually said.
CREATE TABLE IF NOT EXISTS row_edits (
    job_id    TEXT NOT NULL,
    row_key   TEXT NOT NULL,
    field     TEXT NOT NULL,
    value     TEXT NOT NULL,
    edited_at TEXT NOT NULL,
    PRIMARY KEY (job_id, row_key, field)
);

CREATE TABLE IF NOT EXISTS rows (
    job_id        TEXT NOT NULL DEFAULT '',
    input_index   INTEGER NOT NULL DEFAULT 0,
    row_key       TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    status        TEXT NOT NULL,
    completed_at  TEXT,
    payload       TEXT,
    PRIMARY KEY (job_id, row_key)
);

CREATE INDEX IF NOT EXISTS rows_by_url ON rows (canonical_url);
CREATE INDEX IF NOT EXISTS rows_by_order ON rows (job_id, input_index);

CREATE TABLE IF NOT EXISTS uploads (
    content_hash TEXT PRIMARY KEY,
    host         TEXT NOT NULL,
    url          TEXT NOT NULL,
    delete_url   TEXT,
    created_at   TEXT NOT NULL
);

-- The --llm layer, keyed by a hash of (system prompt, user prompt, model). A
-- re-run, a --resume, or a second pass over the same catalogue therefore costs
-- nothing. Keyed on the prompt rather than the row so that two products with
-- identical copy are one call.
CREATE TABLE IF NOT EXISTS llm_cache (
    prompt_hash TEXT PRIMARY KEY,
    model       TEXT NOT NULL,
    response    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- Predicate 9. A host earns a place here only through evidence that it blocks
-- third-party fetches: hotlink failures and hard 403s. Timeouts and undersized
-- images are properties of one image, not of a host, and never count -- letting
-- them in would quietly push whole CDNs onto the expensive path.
CREATE TABLE IF NOT EXISTS bad_hotlink_hosts (
    host        TEXT PRIMARY KEY,
    failures    INTEGER NOT NULL DEFAULT 0,
    last_seen   TEXT NOT NULL
);
"""


LEGACY_JOB = "j_legacy00"


class Ledger:
    def __init__(self, path: Path | str = ":memory:") -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._migrate()
        self._conn.executescript(_SCHEMA)
        self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _migrate(self) -> None:
        """Walk an older ledger up to the current shape, in place.

        The alternative -- telling an operator to delete store/ledger.db -- also
        deletes their upload dedupe and their bad-host cache, which would mean
        paying to re-upload photos that are already hosted. Not worth saving
        thirty lines.
        """
        version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if version >= SCHEMA_VERSION:
            return

        existing = {
            r["name"]
            for r in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "rows" not in existing:
            return  # A fresh file. _SCHEMA below creates everything.

        columns = {r["name"] for r in self._conn.execute("PRAGMA table_info(rows)").fetchall()}
        if "job_id" in columns:
            return

        log.info("Upgrading the ledger to schema v%d (adding job identity)", SCHEMA_VERSION)
        self._conn.executescript(
            f"""
            BEGIN;
            ALTER TABLE rows RENAME TO rows_v1;
            CREATE TABLE rows (
                job_id        TEXT NOT NULL DEFAULT '',
                input_index   INTEGER NOT NULL DEFAULT 0,
                row_key       TEXT NOT NULL,
                canonical_url TEXT NOT NULL,
                status        TEXT NOT NULL,
                completed_at  TEXT,
                payload       TEXT,
                PRIMARY KEY (job_id, row_key)
            );
            INSERT INTO rows (job_id, input_index, row_key, canonical_url,
                              status, completed_at, payload)
                SELECT '{LEGACY_JOB}', rowid, row_key, canonical_url,
                       status, completed_at, payload
                FROM rows_v1;
            DROP TABLE rows_v1;
            COMMIT;
            """
        )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- rows: dedupe now, resume in Phase 9 -------------------------------

    def has_row(self, canonical_url: str) -> bool:
        """Has this URL already produced a row in listings.csv?

        Failed rows are excluded, and the distinction is load-bearing: a failure
        is recorded here so `review` can show it and so resume knows the URL was
        attempted, but it never reached the CSV -- it has no title to import. If
        this matched them too, a retry that finally succeeded would be discarded
        as a duplicate of a row that does not exist.
        """
        row = self._conn.execute(
            "SELECT 1 FROM rows WHERE canonical_url = ? AND status != 'failed'",
            (canonical_url,),
        ).fetchone()
        return row is not None

    def record_row(
        self, record: object, job_id: str = LEGACY_JOB, input_index: int = 0
    ) -> None:
        """Store the full record. `input_index` is what makes ordering possible.

        Keyed on (job_id, row_key) rather than on canonical_url: two jobs over
        the same catalogue must both be able to store their own row, or the
        second one produces an empty CSV.
        """
        from ..models import ProductRecord

        assert isinstance(record, ProductRecord)
        self._conn.execute(
            """
            INSERT INTO rows (job_id, input_index, row_key, canonical_url,
                              status, completed_at, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id, row_key) DO UPDATE SET
                input_index = excluded.input_index,
                status = excluded.status,
                completed_at = excluded.completed_at,
                payload = excluded.payload
            """,
            (
                job_id,
                input_index,
                record.row_key,
                record.canonical_url,
                record.status.value,
                datetime.now(UTC).isoformat(),
                record.model_dump_json(),
            ),
        )

    def iter_payloads(self, job_id: str | None = None) -> Iterator[str]:
        """Stored record payloads, one at a time.

        In **input order** when a job is named -- that ordering is the whole
        reason `input_index` exists -- and by completion time otherwise. A full
        record is a large object and 5,000 at once is the memory profile the
        batch path exists to avoid, so this streams. Do not write while iterating.
        """
        if job_id is None:
            cursor = self._conn.execute(
                "SELECT payload FROM rows WHERE payload IS NOT NULL ORDER BY completed_at"
            )
        else:
            cursor = self._conn.execute(
                "SELECT payload FROM rows WHERE job_id = ? AND payload IS NOT NULL "
                "ORDER BY input_index",
                (job_id,),
            )
        for row in cursor:
            yield row["payload"]

    def all_rows(self, job_id: str | None = None) -> list[str]:
        return list(self.iter_payloads(job_id))

    def completed_urls(self, job_id: str | None = None) -> set[str]:
        """What `--resume` may skip.

        Failed rows are deliberately absent. A row that failed on a timeout
        failed because of the network, not because of the page, and a resume
        that permanently wrote those off would quietly turn one bad minute into
        a hole in the catalogue. Successes are skipped; failures are retried.
        """
        if job_id is None:
            rows = self._conn.execute(
                "SELECT canonical_url FROM rows WHERE status != 'failed'"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT canonical_url FROM rows WHERE job_id = ? AND status != 'failed'",
                (job_id,),
            ).fetchall()
        return {r["canonical_url"] for r in rows}

    # -- jobs --------------------------------------------------------------

    def create_job(self, job_id: str, settings_json: str, urls: list[tuple[int, str, str]]) -> None:
        """Register a job and every URL it will account for.

        Each input line gets a `job_urls` row here, with `outcome` NULL. That is
        what turns "every input URL ends up in exactly one output file" from an
        intention into something `finalise_job` can assert.
        """
        now = datetime.now(UTC).isoformat()
        self._conn.execute(
            "INSERT INTO jobs (job_id, created_at, state, settings, input_count) "
            "VALUES (?, ?, 'running', ?, ?)",
            (job_id, now, settings_json, len(urls)),
        )
        self._conn.executemany(
            "INSERT INTO job_urls (job_id, input_index, source_url, canonical) "
            "VALUES (?, ?, ?, ?)",
            [(job_id, index, source, canonical) for index, source, canonical in urls],
        )

    def set_outcome(
        self,
        job_id: str,
        input_index: int,
        outcome: str,
        row_key: str | None = None,
        reason: str | None = None,
    ) -> None:
        self._conn.execute(
            "UPDATE job_urls SET outcome = ?, row_key = ?, reason = ? "
            "WHERE job_id = ? AND input_index = ?",
            (outcome, row_key, reason, job_id, input_index),
        )

    def job_inputs(self, job_id: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM job_urls WHERE job_id = ? ORDER BY input_index", (job_id,)
        ).fetchall()

    def unaccounted(self, job_id: str) -> list[int]:
        """Input indices that reached the end of a job with no outcome."""
        rows = self._conn.execute(
            "SELECT input_index FROM job_urls WHERE job_id = ? AND outcome IS NULL "
            "ORDER BY input_index",
            (job_id,),
        ).fetchall()
        return [int(r["input_index"]) for r in rows]

    def resumable(self, job_id: str) -> list[int]:
        """Input indices a resume should pick up.

        Wider than `unaccounted` on purpose, and the difference matters. A
        cancelled job's un-started URLs are given `failed / not_started` so that
        "every URL is in exactly one output file" still holds at the end -- they
        go to failed.csv, which is the file an operator re-runs. But that same
        act would make them look finished to a resume. So resume asks for
        anything that has no outcome *or* failed: a 503 failed because of the
        network rather than the page, and a URL that never started did not fail
        at all.
        """
        rows = self._conn.execute(
            "SELECT input_index FROM job_urls WHERE job_id = ? "
            "AND (outcome IS NULL OR outcome = 'failed') ORDER BY input_index",
            (job_id,),
        ).fetchall()
        return [int(r["input_index"]) for r in rows]

    def outcome_counts(self, job_id: str) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT COALESCE(outcome, 'pending') AS outcome, COUNT(*) AS n "
            "FROM job_urls WHERE job_id = ? GROUP BY 1",
            (job_id,),
        ).fetchall()
        return {r["outcome"]: int(r["n"]) for r in rows}

    def set_job_state(self, job_id: str, state: str) -> None:
        terminal = state in ("done", "cancelled", "error")
        finished = datetime.now(UTC).isoformat() if terminal else None
        self._conn.execute(
            "UPDATE jobs SET state = ?, finished_at = COALESCE(?, finished_at) WHERE job_id = ?",
            (state, finished, job_id),
        )

    def job(self, job_id: str) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()

    def jobs(self, limit: int = 50) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()

    # -- edits (section 4; the API uses these in Phase 7) -------------------

    def record_edit(self, job_id: str, row_key: str, field: str, value: str) -> None:
        """Stored beside the extraction, never over it."""
        self._conn.execute(
            "INSERT INTO row_edits (job_id, row_key, field, value, edited_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(job_id, row_key, field) DO UPDATE SET "
            "value = excluded.value, edited_at = excluded.edited_at",
            (job_id, row_key, field, value, datetime.now(UTC).isoformat()),
        )

    def row_payload(self, job_id: str, row_key: str) -> str | None:
        row = self._conn.execute(
            "SELECT payload FROM rows WHERE job_id = ? AND row_key = ?", (job_id, row_key)
        ).fetchone()
        return str(row["payload"]) if row and row["payload"] else None

    def delete_edit(self, job_id: str, row_key: str, field: str) -> None:
        """Undo. The stored extraction was never touched, so removing the edit
        is all it takes to get back what the page said."""
        self._conn.execute(
            "DELETE FROM row_edits WHERE job_id = ? AND row_key = ? AND field = ?",
            (job_id, row_key, field),
        )

    def edits_for(self, job_id: str) -> dict[str, dict[str, str]]:
        rows = self._conn.execute(
            "SELECT row_key, field, value FROM row_edits WHERE job_id = ?", (job_id,)
        ).fetchall()
        out: dict[str, dict[str, str]] = {}
        for r in rows:
            out.setdefault(r["row_key"], {})[r["field"]] = r["value"]
        return out

    # -- upload dedupe -----------------------------------------------------

    def find_upload(self, content_hash: str) -> tuple[str, str, str | None] | None:
        """Identical bytes already hosted? Returns (url, host, delete_url).

        Content-addressed rather than URL-addressed on purpose: the same photo
        often appears under several source URLs across a catalogue, and paying
        to upload it twice helps nobody.
        """
        row = self._conn.execute(
            "SELECT url, host, delete_url FROM uploads WHERE content_hash = ?", (content_hash,)
        ).fetchone()
        return (row["url"], row["host"], row["delete_url"]) if row else None

    def record_upload(
        self, content_hash: str, host: str, url: str, delete_url: str | None
    ) -> None:
        """Every delete_url is kept: someone will want to clean these up."""
        self._conn.execute(
            """
            INSERT INTO uploads (content_hash, host, url, delete_url, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(content_hash) DO NOTHING
            """,
            (content_hash, host, url, delete_url, datetime.now(UTC).isoformat()),
        )

    def upload_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM uploads").fetchone()
        return int(row["n"])

    # -- --llm cache -------------------------------------------------------

    def find_llm(self, prompt_hash: str, model: str) -> str | None:
        """Model is part of the lookup: a cached answer from a different model
        is a different answer."""
        row = self._conn.execute(
            "SELECT response FROM llm_cache WHERE prompt_hash = ? AND model = ?",
            (prompt_hash, model),
        ).fetchone()
        return str(row["response"]) if row else None

    def record_llm(self, prompt_hash: str, model: str, response: str) -> None:
        self._conn.execute(
            """
            INSERT INTO llm_cache (prompt_hash, model, response, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(prompt_hash) DO NOTHING
            """,
            (prompt_hash, model, response, datetime.now(UTC).isoformat()),
        )

    def llm_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM llm_cache").fetchone()
        return int(row["n"])

    # -- predicate 9 -------------------------------------------------------

    def is_bad_hotlink_host(self, host: str, threshold: int, ttl_days: int) -> bool:
        row = self._conn.execute(
            "SELECT failures, last_seen FROM bad_hotlink_hosts WHERE host = ?", (host,)
        ).fetchone()
        if row is None or row["failures"] < threshold:
            return False

        last_seen = datetime.fromisoformat(row["last_seen"])
        if datetime.now(UTC) - last_seen > timedelta(days=ttl_days):
            # Expired. A CDN that changed its policy deserves another look.
            self._conn.execute("DELETE FROM bad_hotlink_hosts WHERE host = ?", (host,))
            return False
        return True

    def record_hotlink_failure(self, host: str) -> int:
        """Only ever called for confirmed third-party blocks. Returns the new count."""
        now = datetime.now(UTC).isoformat()
        self._conn.execute(
            """
            INSERT INTO bad_hotlink_hosts (host, failures, last_seen) VALUES (?, 1, ?)
            ON CONFLICT(host) DO UPDATE SET failures = failures + 1, last_seen = excluded.last_seen
            """,
            (host, now),
        )
        row = self._conn.execute(
            "SELECT failures FROM bad_hotlink_hosts WHERE host = ?", (host,)
        ).fetchone()
        return int(row["failures"])

    def clear_bad_host(self, host: str) -> None:
        """A host that serves us a clean hotlink is no longer suspect."""
        self._conn.execute("DELETE FROM bad_hotlink_hosts WHERE host = ?", (host,))

    def bad_hosts(self) -> list[tuple[str, int]]:
        rows = self._conn.execute(
            "SELECT host, failures FROM bad_hotlink_hosts ORDER BY failures DESC"
        ).fetchall()
        return [(r["host"], r["failures"]) for r in rows]
