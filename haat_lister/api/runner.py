"""The in-process job queue: one job at a time, cancellable, resumable.

**One at a time by default, and the reason is not tidiness.** The rate limiter
is per-domain and lives inside a run, so three concurrent jobs over the same
shop would quietly triple the load on someone's server -- the operator would see
three tidy progress bars and the shop would see a spike it never agreed to. A
queue keeps the politeness budget meaningful.

Cancellation sets the same `StopSignal` Ctrl-C sets in the CLI. It is a flag,
not an exception: an interrupt that unwound through the writers would abandon
the `.tmp` and lose the run's CSV, which is the opposite of what someone
pressing Cancel wants. Finished rows stay finished, and `Resume` picks up the
indices that have no outcome yet.

Everything this class knows is also in the ledger. It holds no row, no record,
and no state a refresh could not rebuild -- which is what makes
`GET /api/jobs/{id}` authoritative rather than a second opinion.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..batch import BatchOptions, BatchRunner, StopSignal
from ..config import Settings
from ..jobs import JobPlan, job_paths, plan_from_ledger, settings_for_job
from ..pipeline import process_url
from ..store.ledger import Ledger
from ..utils.logging import get_logger
from .events import EventBroker

log = get_logger(__name__)


def _forward(publish: Callable[..., Any], job_id: str) -> Callable[..., None]:
    """Adapt the runner's (name, **fields) callback onto the broker.

    A lambda would do, but this keeps the return type honest: BatchRunner wants
    a callback that returns nothing, and `publish` hands back the Event it made.
    """

    def emit(name: str, **data: Any) -> None:
        publish(job_id, name, **data)

    return emit


@dataclass
class QueuedJob:
    job_id: str
    plan: JobPlan
    options: BatchOptions
    ignore_robots: bool = False
    render: bool | None = None
    llm: bool = False
    stop: StopSignal = field(default_factory=StopSignal)


class JobRunner:
    """Owns the queue and the one worker that drains it."""

    def __init__(
        self,
        settings: Settings,
        broker: EventBroker,
        process: Callable[..., Any] | None = None,
    ) -> None:
        self._settings = settings
        self._broker = broker
        # The same seam BatchRunner has, for the same reason: the API tests are
        # about create/stream/cancel/resume, and re-running extraction inside
        # them would be re-testing four hundred other tests slowly.
        self._process = process
        self._queue: asyncio.Queue[QueuedJob] = asyncio.Queue()
        self._jobs: dict[str, QueuedJob] = {}
        self._worker: asyncio.Task[None] | None = None
        self.current: str | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._drain(), name="job-runner")

    async def stop(self) -> None:
        for job in self._jobs.values():
            job.stop.set()
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None

    # -- submission --------------------------------------------------------

    def submit(self, job: QueuedJob) -> str:
        self._jobs[job.job_id] = job
        self._queue.put_nowait(job)
        self._broker.publish(
            job.job_id,
            "job_queued",
            position=self._queue.qsize(),
            total=len(job.plan.accepted),
        )
        self.start()
        return job.job_id

    def cancel(self, job_id: str) -> bool:
        """Ask the job to wind down. Returns False if it was never here.

        Rows already finished stay in listings.csv and in the ledger; the ones
        never begun land in failed.csv with `not_started`, which is exactly the
        column an operator pastes into the next job.
        """
        job = self._jobs.get(job_id)
        if job is None:
            return False
        job.stop.set()
        self._broker.publish(job_id, "job_cancelling")
        return True

    def is_running(self, job_id: str) -> bool:
        return self.current == job_id

    def is_queued(self, job_id: str) -> bool:
        return job_id in self._jobs and self.current != job_id

    def resume(self, ledger: Ledger, job_id: str, **overrides: Any) -> QueuedJob | None:
        """Re-queue a job for the indices that never got an outcome.

        The plan comes back from the ledger rather than from the caller: the tab
        that submitted the URLs may be gone, and the process may have restarted
        since. Nothing about a resume depends on the browser still being open.
        """
        row = ledger.job(job_id)
        if row is None:
            return None

        import json

        stored = json.loads(row["settings"])
        from ..models import DescriptionMode, ImageMode, Provenance

        options = BatchOptions(
            provenance=Provenance(stored["provenance"]),
            image_mode=ImageMode(stored["images"]),
            job_id=job_id,
            concurrency=int(overrides.get("concurrency", stored.get("concurrency", 5))),
            resume=True,
            seller_note=stored.get("seller_note"),
            description_mode=DescriptionMode(stored["description_mode"]),
        )
        job = QueuedJob(
            job_id=job_id,
            plan=plan_from_ledger(ledger, job_id),
            options=options,
            render=stored.get("render_enabled"),
            llm=bool(stored.get("llm")),
        )
        ledger.set_job_state(job_id, "queued")
        self.submit(job)
        return job

    # -- the worker --------------------------------------------------------

    async def _drain(self) -> None:
        while True:
            job = await self._queue.get()
            self.current = job.job_id
            try:
                await self._run(job)
            except Exception as exc:  # noqa: BLE001 -- one bad job must not kill the queue
                log.exception("Job %s failed", job.job_id)
                self._broker.publish(job.job_id, "job_error", error=repr(exc))
                with Ledger(self._ledger_path()) as ledger:
                    ledger.set_job_state(job.job_id, "error")
            finally:
                self.current = None
                self._jobs.pop(job.job_id, None)
                self._broker.close(job.job_id)

    def _ledger_path(self) -> Path:
        return self._settings.root / self._settings.config.paths.ledger

    async def _run(self, job: QueuedJob) -> None:
        from ..enrich.rewrite import LlmEnricher, build_client
        from ..extract.plugins import build_registry
        from ..fetch.rendered import build_renderer
        from ..fetch.static import build_client as build_http
        from ..images.hosts import build_hosts
        from ..images.pipeline import ImageResolver
        from ..policy.screen import load_vocabulary
        from ..utils.robots import RobotsCache

        settings = self._settings
        cfg = settings.config
        publish = self._broker.publish

        publish(job.job_id, "job_started", total=len(job.plan.accepted))

        with Ledger(self._ledger_path()) as ledger:
            ledger.set_job_state(job.job_id, "running")
            async with build_http(settings) as client:
                robots = (
                    None
                    if job.ignore_robots or not cfg.fetch.respect_robots
                    else RobotsCache(client, settings.user_agent)
                )
                hosts, _skipped = build_hosts(settings, client, job.options.image_mode)
                resolver = ImageResolver(
                    settings_for_job(settings, job.job_id),
                    client,
                    job.options.image_mode,
                    hosts=hosts,
                    ledger=ledger,
                )
                renderer = build_renderer(settings, job.render)
                vocabulary = load_vocabulary(
                    settings.root / cfg.policy.keywords_file,
                    settings.root / cfg.policy.brands_file,
                )
                enricher = LlmEnricher(
                    settings, build_client(settings, job.llm), vocabulary, ledger
                )

                runner = BatchRunner(
                    settings,
                    job.options,
                    ledger=ledger,
                    client=client,
                    resolver=resolver,
                    renderer=renderer,
                    plugins=build_registry(cfg, settings.root),
                    enricher=enricher,
                    robots=robots,
                    stop=job.stop,
                    on_event=_forward(publish, job.job_id),
                    process=self._process or process_url,
                )
                try:
                    stats = await runner.run(job.plan)
                finally:
                    if renderer is not None and renderer.started:
                        await renderer.close()

        paths = job_paths(settings, job.job_id)
        publish(
            job.job_id,
            "job_done",
            state="cancelled" if stats.stopped_early else "done",
            written=stats.written,
            needs_review=stats.needs_review,
            failed=stats.failed_written,
            processed=stats.processed,
            not_started=stats.not_started,
            pages_rendered=stats.pages_rendered,
            directory=str(paths.root),
        )
