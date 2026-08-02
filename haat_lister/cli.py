"""Typer entrypoint. Argument parsing, wiring, and reporting only -- no logic.

Everything a command actually does lives in the modules it calls. The one thing
this file does own is the provenance gate (Rule 2.1), because that is a property
of being invoked, not of any particular pipeline stage.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .config import ConfigError, Finding, Settings, collect_findings
from .models import DescriptionMode, ImageMode, ProductRecord, Provenance, RowStatus

if TYPE_CHECKING:  # heavy imports stay out of `--help`
    from collections.abc import Iterator

    from .batch import BatchOptions, BatchStats, StopSignal

console = Console()

app = typer.Typer(
    name="haat-lister",
    help="Turn arbitrary product pages into a haat bulk-listing CSV, honestly.",
    no_args_is_help=True,
    add_completion=False,
)

# --------------------------------------------------------------------------
# Shared options
# --------------------------------------------------------------------------

ConfigOpt = Annotated[
    Path | None, typer.Option("--config", help="Path to config.yaml (default: ./config.yaml).")
]
ProvenanceOpt = Annotated[
    Provenance | None,
    typer.Option(
        "--provenance",
        help="REQUIRED. own | authorised | third-party. Who owns the source content.",
    ),
]
ImagesOpt = Annotated[
    ImageMode | None,
    typer.Option("--images", help="manifest (default) | url_columns | both."),
]
RenderOpt = Annotated[
    bool | None,
    typer.Option(
        "--render/--no-render",
        help="Stage B browser fallback for pages Stage A left incomplete. "
        "Defaults to config.yaml's render.enabled.",
    ),
]
LlmOpt = Annotated[
    bool,
    typer.Option(
        "--llm",
        help="Use a language model to rewrite descriptions and choose a category from "
        "taxonomy.yaml. Never used for price, weight, dimensions, HS code or GI region.",
    ),
]
VerboseOpt = Annotated[int, typer.Option("-v", "--verbose", count=True, help="-v info, -vv debug.")]
LogFileOpt = Annotated[
    Path | None, typer.Option("--log-file", help="Write one JSON line per URL here.")
]


PROVENANCE_INTRO = (
    "[bold]--provenance is required. There is no default.[/bold]\n\n"
    "This tool exists so a seller can migrate [i]their own[/i] catalogue onto haat. "
    "Product photographs and marketing copy belong to whoever created them, and haat's "
    "seller rules prohibit resold or dropshipped goods, counterfeits, and unverified GI "
    "claims. Which of those applies is a fact only you know, so the tool will not assume it."
)

PROVENANCE_CHOICES = [
    ("own", "green", "You made or own this content."),
    ("authorised", "green", "You have the rights holder's permission."),
    (
        "third-party",
        "yellow",
        "Neither. The run still proceeds, but every row is forced to needs_review, "
        "images are never re-hosted, and descriptions are forced through a rewrite.",
    ),
]


def _provenance_help() -> Table:
    """Built as a table so rich wraps the descriptions instead of us guessing a width."""
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold", no_wrap=True)
    grid.add_column(ratio=1)
    for value, style, description in PROVENANCE_CHOICES:
        grid.add_row(f"[{style}]--provenance {value}[/{style}]", description)
    return grid


def _resolve_provenance(value: Provenance | None) -> Provenance:
    if value is None:
        console.print(
            Panel(
                Group(PROVENANCE_INTRO, "", _provenance_help()),
                title="Missing --provenance",
                border_style="red",
            )
        )
        raise typer.Exit(code=2)
    if value is Provenance.THIRD_PARTY:
        console.print(
            Panel(
                "Running with [yellow]--provenance third-party[/yellow].\n\n"
                "Every row will be marked [yellow]needs_review[/yellow]. Images will not be "
                "re-hosted, because re-uploading photographs you do not own creates a copyright "
                "exposure for the seller. Descriptions will be rewritten rather than copied.\n\n"
                "Before importing, check haat's rules on resale, dropshipping, counterfeits, and "
                "GI claims. Listing goods you do not have the right to sell is a delisting risk "
                "regardless of what this tool produces.",
                title="Provenance warning",
                border_style="yellow",
            )
        )
    return value


def _error_panel(text: str, title: str) -> Panel:
    """Exception text is data. Unescaped, a message containing `[llm]` or
    `[render]` loses it -- rich reads square brackets as style tags -- and the
    operator is handed an install command that does not work."""
    from rich.markup import escape

    return Panel(escape(text), title=title, border_style="red")


def _load_settings(config_path: Path | None) -> Settings:
    try:
        return Settings.load(config_path=config_path)
    except ConfigError as exc:
        console.print(_error_panel(str(exc), "Configuration error"))
        raise typer.Exit(code=2) from exc


# --------------------------------------------------------------------------
# config-check
# --------------------------------------------------------------------------

_LEVEL_STYLE = {"fail": ("FAIL", "red"), "warn": ("WARN", "yellow"), "info": ("INFO", "cyan")}


def _render_findings(findings: list[Finding]) -> None:
    table = Table(show_header=True, header_style="bold", show_lines=True, expand=True)
    table.add_column("", width=4)
    table.add_column("Check", style="bold", ratio=2)
    table.add_column("Detail", ratio=5)

    from rich.markup import escape

    for f in findings:
        label, style = _LEVEL_STYLE[f.level]
        # Finding text is data, not markup. Unescaped, a fix line reading
        # `pip install "haat-lister[llm]"` loses the extra -- rich reads [llm]
        # as a style tag -- and prints an instruction that does not work.
        detail = escape(f.detail)
        if f.fix:
            detail += f"\n[bold]Fix:[/bold] {escape(f.fix)}"
        table.add_row(f"[{style}]{label}[/{style}]", escape(f.title), detail)

    console.print(table)


@app.command("config-check")
def config_check(
    config: ConfigOpt = None,
    images: ImagesOpt = None,
    verbose: VerboseOpt = 0,
) -> None:
    """Validate configuration, taxonomy and credentials before spending a request."""
    from .utils.logging import setup_logging

    setup_logging(verbose)
    settings = _load_settings(config)
    mode = images or settings.config.images.default_mode

    console.print(
        Panel(
            f"haat-lister {__version__}\n"
            f"config:   {settings.config_path}\n"
            f"taxonomy: {settings.taxonomy_path}\n"
            f"UA:       {settings.user_agent}",
            title="config-check",
            border_style="blue",
        )
    )

    findings = collect_findings(settings, mode)
    _render_findings(findings)

    fails = sum(1 for f in findings if f.level == "fail")
    warns = sum(1 for f in findings if f.level == "warn")

    if fails:
        console.print(
            f"\n[red bold]{fails} blocking problem(s), {warns} warning(s).[/red bold] "
            "Fix the FAIL rows above before running single or batch."
        )
        raise typer.Exit(code=1)

    console.print(f"\n[green bold]Ready.[/green bold] {warns} warning(s), 0 blocking problems.")


# --------------------------------------------------------------------------
# Stubs -- each names the phase that fills it in
# --------------------------------------------------------------------------


@app.command()
def single(
    url: Annotated[str, typer.Argument(help="One product page URL.")],
    provenance: ProvenanceOpt = None,
    images: ImagesOpt = None,
    description_mode: Annotated[
        DescriptionMode, typer.Option("--description-mode")
    ] = DescriptionMode.RAW,
    ignore_robots: Annotated[
        bool,
        typer.Option("--ignore-robots", help="Skip robots.txt. Only for sites you own."),
    ] = False,
    render: RenderOpt = None,
    llm: LlmOpt = False,
    json_only: Annotated[
        bool, typer.Option("--json", help="Print the record JSON and write nothing.")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Extract and report, but write no files.")
    ] = False,
    out: Annotated[Path | None, typer.Option("--out", help="listings.csv path.")] = None,
    seller_note: Annotated[
        str | None, typer.Option("--seller-note", help="Applied to every row.")
    ] = None,
    excel_bom: Annotated[bool, typer.Option("--excel-bom")] = False,
    config: ConfigOpt = None,
    verbose: VerboseOpt = 0,
    log_file: LogFileOpt = None,
) -> None:
    """Process one product page interactively."""
    import asyncio

    from .config import assert_runnable
    from .utils.logging import setup_logging

    setup_logging(verbose, log_file)
    prov = _resolve_provenance(provenance)
    settings = _load_settings(config)
    mode = images or settings.config.images.default_mode

    try:
        assert_runnable(settings, mode)
    except ConfigError as exc:
        console.print(_error_panel(str(exc), "Configuration error"))
        raise typer.Exit(code=2) from exc

    if excel_bom:
        settings.config.csv.excel_bom = True

    if ignore_robots:
        console.print(
            "[yellow]--ignore-robots is set: robots.txt will not be consulted. "
            "Only do this on sites you own.[/yellow]"
        )

    from .policy.provenance import effective_description_mode

    mode_used = effective_description_mode(description_mode, prov)
    record = asyncio.run(
        _run_single(
            url, prov, settings, ignore_robots, seller_note, mode_used, mode, render, llm
        )
    )

    if json_only:
        print(record.model_dump_json(indent=2, exclude_none=False))
        raise typer.Exit(code=0 if record.status is not RowStatus.FAILED else 1)

    _render_record(record)

    if not dry_run:
        _write_outputs([record], settings, mode, out)
    else:
        console.print("[dim]--dry-run: no files written.[/dim]")

    raise typer.Exit(code=0 if record.status is not RowStatus.FAILED else 1)


async def _run_single(
    url: str,
    prov: Provenance,
    settings: Settings,
    ignore_robots: bool,
    seller_note: str | None = None,
    description_mode: DescriptionMode = DescriptionMode.RAW,
    image_mode: ImageMode = ImageMode.MANIFEST,
    render: bool | None = None,
    llm: bool = False,
):
    from .enrich.rewrite import LlmEnricher
    from .extract.plugins import build_registry
    from .fetch.rendered import build_renderer
    from .fetch.static import build_client
    from .images.hosts import build_hosts
    from .images.pipeline import ImageResolver
    from .pipeline import process_url
    from .store.ledger import Ledger
    from .utils.robots import RobotsCache

    cfg = settings.config
    with Ledger(settings.root / cfg.paths.ledger) as ledger:
        async with build_client(settings) as client:
            robots = (
                None
                if ignore_robots or not cfg.fetch.respect_robots
                else RobotsCache(client, settings.user_agent)
            )
            hosts, skipped = build_hosts(settings, client, image_mode)
            _warn_about_hosts(hosts, skipped, image_mode)

            resolver = ImageResolver(settings, client, image_mode, hosts=hosts, ledger=ledger)
            # Built, not started. The browser launches only if a row turns out
            # to need it, so a page Stage A handles costs nothing here.
            renderer = build_renderer(settings, render)
            plugins = build_registry(cfg, settings.root)
            _report_plugins(plugins)
            enricher = LlmEnricher(
                settings, _llm_client(settings, llm), _vocabulary(settings), ledger
            )
            try:
                return await process_url(
                    url,
                    prov,
                    settings,
                    client,
                    robots,
                    seller_note,
                    description_mode,
                    resolver,
                    renderer,
                    plugins,
                    enricher,
                )
            finally:
                if renderer is not None and renderer.started:
                    await renderer.close()


def _llm_client(settings: Settings, enabled: bool):
    """At STARTUP, like the image hosts: an operator should learn the key is
    missing before a batch, not 300 rows into one."""
    from .enrich.rewrite import LlmUnavailable, build_client

    try:
        client = build_client(settings, enabled)
    except LlmUnavailable as exc:
        console.print(_error_panel(str(exc), "--llm is not usable"))
        raise typer.Exit(code=2) from exc

    if client is not None:
        console.print(
            f"[dim]--llm on ({settings.config.llm.model}): descriptions and category "
            "choice only. Price, weight, dimensions, HS code and GI region are never "
            "model-written.[/dim]"
        )
    return client


def _vocabulary(settings: Settings):
    """The policy screen, reused to check what a rewrite introduced."""
    from .policy.screen import load_vocabulary

    return load_vocabulary(
        settings.root / settings.config.policy.keywords_file,
        settings.root / settings.config.policy.brands_file,
    )


def _report_plugins(registry) -> None:
    """Named at startup. Operator plugins execute their own Python, so an
    operator should see which ones are live before a run, not deduce it from a
    surprising row afterwards."""
    if len(registry):
        console.print(
            f"[dim]Plugins: {', '.join(p.name for p in registry.plugins)}[/dim]"
        )


def _warn_about_hosts(hosts, skipped: list[str], mode: ImageMode) -> None:
    """At STARTUP, not mid-run: an operator should learn a host is unusable
    before spending a batch on it."""
    if not mode.need_url:
        return
    if skipped:
        console.print(f"[yellow]Image hosts skipped:[/yellow] {', '.join(skipped)}")
    if hosts:
        console.print(f"[dim]Image host chain: {', '.join(h.name for h in hosts)}[/dim]")
    else:
        console.print(
            "[yellow]No image host is configured. Any row whose direct URL fails Tier 1 will "
            "end with no image URL.[/yellow]"
        )


def _write_outputs(
    records: list[ProductRecord],
    settings: Settings,
    mode: ImageMode,
    out: Path | None = None,
) -> None:
    """Emit listings.csv and review.csv, atomically."""
    from .output.csv_writer import HaatCsvWriter, HeaderMismatch
    from .output.review_writer import ReviewWriter
    from .store.ledger import Ledger

    cfg = settings.config
    listings_path = out or (settings.root / cfg.paths.out_csv)
    review_path = settings.root / cfg.paths.review_csv

    try:
        with Ledger(settings.root / cfg.paths.ledger) as ledger:
            # A re-host run writes rows the ledger already knows about, so the
            # dedupe check would skip every one of them.
            dedupe = None if out is not None else ledger
            with HaatCsvWriter(listings_path, cfg, mode, ledger=dedupe) as csv_writer:
                for record in records:
                    csv_writer.write(record)
    except HeaderMismatch as exc:
        console.print(_error_panel(str(exc), "Refusing to append"))
        raise typer.Exit(code=2) from exc

    with ReviewWriter(review_path, cfg) as review_writer:
        for record in records:
            review_writer.write(record)

    manifest_writer = None
    if mode.need_file:
        from .output.manifest_writer import ManifestWriter

        manifest_path = settings.root / cfg.paths.manifest_csv
        with ManifestWriter(manifest_path, cfg) as manifest_writer:
            for record in records:
                manifest_writer.write(record)

    _render_output_summary(
        csv_writer, review_writer, manifest_writer, listings_path, review_path, settings, mode
    )


def _render_output_summary(
    csv_writer,
    review_writer,
    manifest_writer,
    listings_path: Path,
    review_path: Path,
    settings: Settings,
    mode: ImageMode,
) -> None:
    lines = [
        f"[green]{listings_path}[/green]   {csv_writer.written} row(s) written",
    ]
    if csv_writer.skipped_duplicates:
        lines.append(f"  {csv_writer.skipped_duplicates} skipped as already listed")
    if csv_writer.skipped_failed:
        lines.append(
            f"  {csv_writer.skipped_failed} failed row(s) not written -- see review.csv"
        )
    lines.append(f"[yellow]{review_path}[/yellow]   {review_writer.written} row(s) need a human")

    if manifest_writer is not None:
        images_dir = settings.root / settings.config.paths.images_dir
        lines.append(
            f"[green]{manifest_writer.path}[/green]   {manifest_writer.written} image(s) "
            f"under {images_dir}{Path('/')}<row_key>/"
        )
    if mode is ImageMode.MANIFEST:
        lines.append("[dim]Mode manifest: 19 columns exactly, zero image-host calls.[/dim]")

    console.print(Panel("\n".join(lines), title="Output", border_style="blue"))


_STATUS_STYLE = {
    RowStatus.OK: "green",
    RowStatus.NEEDS_REVIEW: "yellow",
    RowStatus.FAILED: "red",
}
_CONFIDENCE_STYLE = {"high": "green", "medium": "yellow", "low": "red", "none": "dim"}


def _render_record(record: ProductRecord) -> None:
    """Human-readable view of what was extracted and how much to trust it."""
    style = _STATUS_STYLE[record.status]
    console.print(
        Panel(
            f"[{style}]{record.status.value}[/{style}]  "
            f"stage={record.fetch_stage.value}  "
            f"structured={','.join(record.structured_syntaxes) or 'none'}\n"
            f"row_key: {record.row_key}\n"
            f"source:  {record.source_url}",
            title="single",
            border_style=style,
        )
    )

    if record.status is RowStatus.FAILED:
        console.print(f"[red]Failed:[/red] {record.failure_reason}")

    fields = Table(show_header=True, header_style="bold", expand=True)
    fields.add_column("Field", style="bold", no_wrap=True)
    fields.add_column("Confidence", no_wrap=True)
    fields.add_column("Source", no_wrap=True)
    fields.add_column("Value", ratio=1)

    for name in ("title", "description"):
        fv = getattr(record, name)
        conf = fv.confidence.value
        value = str(fv.value or "")
        if len(value) > 300:
            value = value[:300] + "…"
        fields.add_row(
            name,
            f"[{_CONFIDENCE_STYLE[conf]}]{conf}[/{_CONFIDENCE_STYLE[conf]}]",
            fv.source.value if fv.source else "-",
            value or "[dim](blank)[/dim]",
        )
    console.print(fields)

    _render_images(record)

    if record.notes:
        console.print(
            Panel(
                "\n".join(f"- {n}" for n in record.notes),
                title="Needs a human",
                border_style="yellow",
            )
        )


_METHOD_STYLE = {"direct": "green", "local": "green", "hosted": "yellow", "none": "red"}


def _render_images(record: ProductRecord) -> None:
    """What Tier 1 decided, and what we ended up with."""
    image = record.image
    style = _METHOD_STYLE.get(image.method.value, "white")

    header = (
        f"Tier 1 attempted on {len(image.candidate_results)} candidate(s) -- "
        f"[{style}]{image.method.value}[/{style}]"
    )
    if image.reason:
        header += f"  ({image.reason})"
    console.print(header)

    if image.url:
        console.print(f"  image_url: {image.url}")

    if image.files:
        table = Table(show_header=True, header_style="bold", expand=True)
        table.add_column("#", no_wrap=True)
        table.add_column("Local file", ratio=1, overflow="fold")
        table.add_column("Size", no_wrap=True)
        table.add_column("From", ratio=1, overflow="fold")
        for file in image.files:
            table.add_row(
                str(file.order),
                file.local_path,
                f"{file.width}x{file.height}  {file.bytes // 1024} KB",
                file.original_source_url,
            )
        console.print(table)
        console.print("[dim]The first file is the hero buyers see everywhere.[/dim]")

    failures = [r for r in image.candidate_results if not r.ok]
    if failures and not image.tier1_passed:
        console.print("[dim]Tier-1 failures: " + ", ".join(r.reason for r in failures) + "[/dim]")

    if not image.files and not image.url:
        console.print("[red]No usable image for this listing.[/red]")


@app.command()
def batch(
    urls_file: Annotated[Path, typer.Argument(help="File of product page URLs, one per line.")],
    provenance: ProvenanceOpt = None,
    images: ImagesOpt = None,
    job: Annotated[
        str | None,
        typer.Option("--job", help="Resume this job id instead of starting a new one."),
    ] = None,
    concurrency: Annotated[
        int,
        typer.Option("--concurrency", help="Rows in flight across the batch, not per host."),
    ] = 5,
    resume: Annotated[
        bool,
        typer.Option("--resume", help="Skip URLs already completed in an earlier run."),
    ] = False,
    ignore_robots: Annotated[
        bool, typer.Option("--ignore-robots", help="Skip robots.txt. Only for sites you own.")
    ] = False,
    render: RenderOpt = None,
    llm: LlmOpt = False,
    description_mode: Annotated[
        DescriptionMode, typer.Option("--description-mode")
    ] = DescriptionMode.RAW,
    seller_note: Annotated[
        str | None, typer.Option("--seller-note", help="Applied to every row.")
    ] = None,
    excel_bom: Annotated[bool, typer.Option("--excel-bom")] = False,
    config: ConfigOpt = None,
    verbose: VerboseOpt = 0,
    log_file: LogFileOpt = None,
) -> None:
    """Process a file of URLs: async, rate-limited, resumable.

    Ctrl-C stops after the rows already in flight and commits everything done so
    far, so `--resume` picks up where it left off without re-fetching a page.
    """
    import asyncio

    from .batch import BatchOptions
    from .config import assert_runnable
    from .jobs import new_job_id, plan_urls
    from .policy.provenance import effective_description_mode
    from .utils.logging import setup_logging

    setup_logging(verbose, log_file)
    prov = _resolve_provenance(provenance)
    settings = _load_settings(config)
    mode = images or settings.config.images.default_mode

    try:
        assert_runnable(settings, mode)
    except ConfigError as exc:
        console.print(_error_panel(str(exc), "Configuration error"))
        raise typer.Exit(code=2) from exc

    if not urls_file.exists():
        console.print(f"[red]No such file:[/red] {urls_file}")
        raise typer.Exit(code=2)

    if concurrency < 1:
        console.print("[red]--concurrency must be at least 1.[/red]")
        raise typer.Exit(code=2)

    if excel_bom:
        settings.config.csv.excel_bom = True

    if ignore_robots:
        console.print(
            "[yellow]--ignore-robots is set: robots.txt will not be consulted for any URL "
            "in this file. Only do this on sites you own.[/yellow]"
        )

    plan = plan_urls(urls_file.read_text(encoding="utf-8").splitlines())
    if not plan.accepted:
        console.print(
            _error_panel(
                f"{urls_file} has no usable product links.\n\n{plan.summary()}",
                "Nothing to do",
            )
        )
        raise typer.Exit(code=2)

    per_domain = settings.config.fetch.per_domain_concurrency
    delay = settings.config.fetch.per_domain_delay_s
    low, high = plan.estimate_seconds(concurrency, delay)
    console.print(
        Panel(
            f"{urls_file}\n"
            f"{plan.summary()}, across {len(plan.domains)} domain(s)\n"
            f"provenance: {prov.value}   images: {mode.value}\n"
            f"concurrency: {concurrency} across the batch, "
            f"{per_domain} per host, {delay:g}s between hits on the same host\n"
            f"resume: {'on' if resume else 'off'}   "
            f"robots: {'ignored' if ignore_robots else 'honoured'}\n"
            f"estimated {low // 60}-{high // 60 + 1} min\n\n"
            "[dim]Ctrl-C stops cleanly and commits what is done.[/dim]",
            title="batch",
            border_style="blue",
        )
    )
    _report_invalid(plan)

    options = BatchOptions(
        provenance=prov,
        image_mode=mode,
        job_id=job or new_job_id(),
        concurrency=concurrency,
        resume=resume,
        seller_note=seller_note,
        description_mode=effective_description_mode(description_mode, prov),
    )
    stats = asyncio.run(_run_batch(plan, settings, options, ignore_robots, render, llm))
    _render_batch_summary(stats, settings, options)

    if stats.stopped_early:
        raise typer.Exit(code=130)
    raise typer.Exit(code=1 if stats.failed else 0)


@contextmanager
def _stop_on_interrupt(stop: StopSignal) -> Iterator[None]:
    """Turn the first Ctrl-C into a request, and the second into a real one.

    The default handler raises KeyboardInterrupt wherever the interpreter is
    standing, which for us means possibly inside the CSV writer -- whose unwind
    path abandons the .tmp file on purpose. Right for a crash, wrong for someone
    deliberately stopping a run. So the first press asks the batch to wind down;
    if it does not wind down fast enough for you, the second press behaves
    exactly as it always did.
    """
    import signal

    # SIGBREAK is Ctrl-Break on Windows and does not exist elsewhere. Someone
    # reaching for it wants the run to stop just as much as someone pressing
    # Ctrl-C, so it gets the same treatment rather than killing the process.
    names = [signal.SIGINT] + ([signal.SIGBREAK] if hasattr(signal, "SIGBREAK") else [])
    original = {name: signal.getsignal(name) for name in names}

    def handler(signum: int, frame: object) -> None:
        for name, previous in original.items():
            signal.signal(name, previous)
        stop.set()
        console.print(
            "\n[yellow]Stopping.[/yellow] Finishing the rows already in flight, then writing "
            "output. Press Ctrl-C again to abandon the run."
        )

    for name in names:
        signal.signal(name, handler)
    try:
        yield
    finally:
        for name, previous in original.items():
            signal.signal(name, previous)


async def _run_batch(
    plan,
    settings: Settings,
    options: BatchOptions,
    ignore_robots: bool,
    render: bool | None = None,
    llm: bool = False,
) -> BatchStats:
    from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

    from .batch import BatchRunner, StopSignal
    from .enrich.rewrite import LlmEnricher
    from .extract.plugins import build_registry
    from .fetch.rendered import build_renderer
    from .fetch.static import build_client
    from .images.hosts import build_hosts
    from .images.pipeline import ImageResolver
    from .jobs import settings_for_job
    from .store.ledger import Ledger
    from .utils.robots import RobotsCache

    cfg = settings.config
    stop = StopSignal()

    with Ledger(settings.root / cfg.paths.ledger) as ledger:
        async with build_client(settings) as client:
            robots = (
                None
                if ignore_robots or not cfg.fetch.respect_robots
                else RobotsCache(client, settings.user_agent)
            )
            hosts, skipped = build_hosts(settings, client, options.image_mode)
            _warn_about_hosts(hosts, skipped, options.image_mode)
            _report_plugins(build_registry(cfg, settings.root))
            # The resolver writes photos into this job's directory, so a job is
            # one self-contained thing on disk. Done by pointing the config at
            # runs/<job_id>/images rather than by changing images/pipeline.py,
            # which owns the Tier 1 gate and stays untouched.
            resolver = ImageResolver(
                settings_for_job(settings, options.job_id),
                client,
                options.image_mode,
                hosts=hosts,
                ledger=ledger,
            )

            progress = Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("{task.completed} rows"),
                TimeElapsedColumn(),
                console=console,
                transient=True,
            )

            # total is unknown on purpose: the input file is streamed, and
            # counting it first would mean reading it twice to show a percentage.
            with progress:
                task = progress.add_task("Processing", total=None)

                def on_row(stats: BatchStats) -> None:
                    progress.update(
                        task,
                        completed=stats.processed,
                        description=(
                            f"Processing  [green]{stats.written} written[/green]  "
                            f"[yellow]{stats.needs_review} to review[/yellow]  "
                            f"[red]{stats.failed} failed[/red]"
                        ),
                    )

                renderer = build_renderer(settings, render)
                plugins = build_registry(cfg, settings.root)
                enricher = LlmEnricher(
                    settings, _llm_client(settings, llm), _vocabulary(settings), ledger
                )
                runner = BatchRunner(
                    settings,
                    options,
                    ledger=ledger,
                    client=client,
                    resolver=resolver,
                    renderer=renderer,
                    plugins=plugins,
                    enricher=enricher,
                    robots=robots,
                    stop=stop,
                    on_row=on_row,
                )
                try:
                    with _stop_on_interrupt(stop):
                        stats = await runner.run(plan)
                finally:
                    if renderer is not None and renderer.started:
                        await renderer.close()

            stats.host_calls = resolver.host_calls
            stats.pages_rendered = renderer.pages_rendered if renderer else 0
            stats.llm_calls = enricher.calls
            stats.llm_cache_hits = enricher.cache_hits
            return stats


def _report_invalid(plan) -> None:
    """Named in place, not counted in aggregate. A line an operator can fix is
    worth showing them; a number is not."""
    if not plan.invalid:
        return
    console.print("[yellow]Not product links, and skipped:[/yellow]")
    for entry in plan.invalid[:8]:
        console.print(f"  [dim]{entry.raw[:90]}[/dim]")
    if len(plan.invalid) > 8:
        console.print(f"  [dim]…and {len(plan.invalid) - 8} more[/dim]")


def _render_batch_summary(
    stats: BatchStats, settings: Settings, options: BatchOptions
) -> None:
    from .jobs import job_paths

    paths = job_paths(settings, stats.job_id)

    lines = [
        f"[bold]{paths.root}[/bold]",
        f"[green]listings.csv[/green]         {stats.written} row(s), in the order you pasted them",
        f"[yellow]review.csv[/yellow]           {stats.needs_review} row(s) need a human",
    ]
    if stats.failed_written:
        lines.append(
            f"[red]failed.csv[/red]           {stats.failed_written} URL(s) produced nothing"
        )
    if options.image_mode.need_file:
        lines.append("[green]image_manifest.csv[/green]   which photo belongs to which row")
    lines.append("[dim]job.json             the exact settings this used[/dim]")

    lines.append("")
    lines.append(f"URLs read {stats.seen}   processed {stats.processed}")

    for label, count in (
        ("already done in this job", stats.skipped_resume),
        ("duplicate product link", stats.skipped_duplicate_in_file),
        ("not a product link", stats.invalid),
    ):
        if count:
            lines.append(f"  skipped, {label}: {count}")
    if stats.peak_pending > 1:
        lines.append(
            f"  [dim]peak rows finished-but-waiting for an earlier one: "
            f"{stats.peak_pending}[/dim]"
        )

    lines.append(
        f"Image-host calls: {stats.host_calls}   peak rows in flight: {stats.peak_in_flight}"
    )
    if stats.pages_rendered:
        share = stats.pages_rendered / stats.processed * 100 if stats.processed else 0
        lines.append(
            f"Stage B renders: {stats.pages_rendered} ({share:.0f}% of rows)"
            + (
                "  [yellow]-- most of this catalogue needs a browser; expect it to be "
                "slow[/yellow]"
                if share > 50
                else ""
            )
        )

    if stats.llm_calls or stats.llm_cache_hits:
        lines.append(
            f"Model calls: {stats.llm_calls}   served from the ledger cache: "
            f"{stats.llm_cache_hits}"
        )

    if stats.stopped_early:
        lines.append("")
        if stats.not_started:
            lines.append(f"  {stats.not_started} URL(s) were dropped without being fetched")
        lines.append(
            "[yellow]Stopped early.[/yellow] Everything above is committed, and the URLs "
            "that never ran are in failed.csv.\n"
            f"Continue where it stopped:  [bold]--job {stats.job_id} --resume[/bold]"
        )

    console.print(
        Panel(
            "\n".join(lines),
            title="batch complete",
            border_style="yellow" if stats.stopped_early or stats.failed else "blue",
        )
    )


@app.command("validate-only")
def validate_only(
    urls_file: Annotated[Path, typer.Argument(help="File of product page URLs, one per line.")],
    no_hotlink_test: Annotated[
        bool, typer.Option("--no-hotlink-test", help="Skip predicate 7. Results become optimistic.")
    ] = False,
    ignore_robots: Annotated[bool, typer.Option("--ignore-robots")] = False,
    config: ConfigOpt = None,
    verbose: VerboseOpt = 0,
    log_file: LogFileOpt = None,
) -> None:
    """Tier 1 only. Zero downloads, zero uploads.

    Measures how often a direct source image URL actually survives across your
    real catalogue, before spending a single byte or host call.
    """
    import asyncio

    from .utils.logging import setup_logging

    setup_logging(verbose, log_file)
    settings = _load_settings(config)

    if not urls_file.exists():
        console.print(f"[red]No such file:[/red] {urls_file}")
        raise typer.Exit(code=2)

    urls = [
        line.strip()
        for line in urls_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    if not urls:
        console.print(f"[yellow]{urls_file} contains no URLs.[/yellow]")
        raise typer.Exit(code=2)

    console.print(
        Panel(
            f"Validating Tier 1 across {len(urls)} product page(s).\n"
            "No image is downloaded to keep, and no image host is contacted -- "
            "this command cannot reach either.",
            title="validate-only",
            border_style="blue",
        )
    )

    rows = asyncio.run(_run_validate_only(urls, settings, no_hotlink_test, ignore_robots))
    _render_validation_summary(rows, no_hotlink_test)
    raise typer.Exit(code=0)


@dataclass
class PageValidation:
    """Tier-1 outcome for one product page."""

    url: str
    page_failed: str | None = None
    winner: str | None = None
    width: int | None = None
    height: int | None = None
    attempts: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)


async def _run_validate_only(
    urls: list[str], settings: Settings, no_hotlink_test: bool, ignore_robots: bool
) -> list[PageValidation]:
    import asyncio
    import random

    from .fetch.static import build_client
    from .images.validator import Tier1Validator, validate_all_candidates
    from .pipeline import process_url
    from .store.ledger import Ledger
    from .utils.robots import RobotsCache

    cfg = settings.config
    rows: list[PageValidation] = []

    with Ledger(settings.root / cfg.paths.ledger) as ledger:
        async with build_client(settings) as client:
            robots = (
                None
                if ignore_robots or not cfg.fetch.respect_robots
                else RobotsCache(client, settings.user_agent)
            )
            validator = Tier1Validator(
                client,
                cfg.validator,
                ledger,
                hotlink_test=not no_hotlink_test,
                allow_private_hosts=cfg.fetch.allow_private_hosts,
            )

            for index, url in enumerate(urls):
                if index:
                    # Phase 9 replaces this with a per-domain limiter; sequential
                    # politeness is enough for a validation sweep.
                    jitter = random.uniform(0, cfg.fetch.per_domain_delay_jitter_s)
                    await asyncio.sleep(cfg.fetch.per_domain_delay_s + jitter)

                record = await process_url(url, Provenance.OWN, settings, client, robots)
                if record.status is RowStatus.FAILED:
                    rows.append(PageValidation(url=url, page_failed=record.failure_reason))
                    continue

                winner, results = await validate_all_candidates(record.image_candidates, validator)
                rows.append(
                    PageValidation(
                        url=url,
                        winner=winner.url if winner else None,
                        width=winner.width if winner else None,
                        height=winner.height if winner else None,
                        attempts=len(results),
                        failures=[(r.url, r.reason) for r in results if not r.ok],
                    )
                )
    return rows


def _render_validation_summary(rows: list[PageValidation], no_hotlink_test: bool) -> None:
    from collections import Counter

    table = Table(show_header=True, header_style="bold", expand=True)
    table.add_column("Result", no_wrap=True)
    table.add_column("Product page", ratio=1, overflow="fold")
    table.add_column("Detail", ratio=1, overflow="fold")

    reasons: Counter[str] = Counter()
    passed = failed_page = no_image = 0

    for row in rows:
        if row.page_failed:
            failed_page += 1
            table.add_row("[red]PAGE[/red]", row.url, f"page fetch failed: {row.page_failed}")
            continue

        for _, why in row.failures:
            reasons[why] += 1

        if row.winner:
            passed += 1
            table.add_row(
                "[green]PASS[/green]",
                row.url,
                f"{row.winner}\n{row.width}x{row.height} "
                f"(candidate {row.attempts} of {row.attempts})",
            )
        else:
            no_image += 1
            detail = "\n".join(f"{u} -> {why}" for u, why in row.failures)
            table.add_row("[yellow]NONE[/yellow]", row.url, detail or "no candidates")

    console.print(table)

    total = len(rows)
    usable = f"{passed / total * 100:.1f}%" if total else "-"
    console.print(
        f"\nPages {total}   [green]direct-valid {passed} ({usable})[/green]   "
        f"[yellow]no usable direct URL {no_image}[/yellow]   [red]page failed {failed_page}[/red]"
    )
    console.print("Image-host calls: 0     Images downloaded to keep: 0")

    if reasons:
        top = "  ".join(f"{reason} {count}" for reason, count in reasons.most_common(6))
        console.print(f"Top Tier-1 failures:  {top}")

    if no_hotlink_test:
        console.print(
            "[yellow]--no-hotlink-test was set: predicate 7 did not run, so these results are "
            "optimistic. A URL that passes here may still 403 for buyers.[/yellow]"
        )


@app.command("rehost-failed")
def rehost_failed(
    images: ImagesOpt = None,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Where to write the re-hosted rows."),
    ] = None,
    config: ConfigOpt = None,
    verbose: VerboseOpt = 0,
    log_file: LogFileOpt = None,
) -> None:
    """Retry Tier 2 for stored rows that ended with no image URL.

    Reads the ledger rather than a CSV, so it never re-scrapes a page and never
    re-uploads bytes it has already hosted. URL modes only -- in `manifest` mode
    there is nothing to re-host.
    """
    import asyncio

    from .utils.logging import setup_logging

    setup_logging(verbose, log_file)
    settings = _load_settings(config)
    mode = images or ImageMode.URL_COLUMNS

    if not mode.need_url:
        console.print(
            Panel(
                f"--images {mode.value} produces no image URL, so there is nothing to re-host.\n"
                "Use --images url_columns or both.",
                title="Nothing to do",
                border_style="yellow",
            )
        )
        raise typer.Exit(code=2)

    from .store.ledger import Ledger

    with Ledger(settings.root / settings.config.paths.ledger) as ledger:
        payloads = ledger.all_rows()

    stored = [ProductRecord.model_validate_json(p) for p in payloads]
    candidates = [r for r in stored if not r.image.url and r.image_candidates]

    if not candidates:
        console.print(
            "[green]Nothing to re-host.[/green] Every stored row either has an image URL "
            "or had no candidates to begin with."
        )
        raise typer.Exit(code=0)

    console.print(
        Panel(
            f"{len(candidates)} of {len(stored)} stored row(s) have no image URL.\n"
            "Tier 1 runs again first -- a URL that has started working since the last run "
            "costs nothing and skips the host entirely.",
            title="rehost-failed",
            border_style="blue",
        )
    )

    updated = asyncio.run(_run_rehost(candidates, settings, mode))

    out_path = out or (settings.root / "listings.rehosted.csv")
    _write_outputs(updated, settings, mode, out_path)

    fixed = sum(1 for r in updated if r.image.url)
    console.print(
        f"\n{fixed} of {len(updated)} row(s) now have an image URL.\n"
        f"[dim]Written to a separate file so your existing listings.csv is untouched; "
        f"merge when you are happy with it.[/dim]"
    )


async def _run_rehost(records: list[ProductRecord], settings: Settings, mode: ImageMode):
    from .fetch.static import build_client
    from .images.hosts import build_hosts
    from .images.pipeline import ImageResolver, apply_to_record
    from .store.ledger import Ledger

    cfg = settings.config
    with Ledger(settings.root / cfg.paths.ledger) as ledger:
        async with build_client(settings) as client:
            hosts, skipped = build_hosts(settings, client, mode)
            _warn_about_hosts(hosts, skipped, mode)

            resolver = ImageResolver(settings, client, mode, hosts=hosts, ledger=ledger)
            for record in records:
                record.status = RowStatus.OK
                record.notes = []
                apply_to_record(record, await resolver.resolve(record))
                ledger.record_row(record)

            console.print(
                f"[dim]Image-host calls this run: {resolver.host_calls}[/dim]"
            )
    return records


@app.command()
def review(
    config: ConfigOpt = None,
    verbose: VerboseOpt = 0,
) -> None:
    """Re-emit review.csv from the ledger, without re-scraping anything."""
    from .output.review_writer import ReviewWriter
    from .store.ledger import Ledger
    from .utils.logging import setup_logging

    setup_logging(verbose)
    settings = _load_settings(config)
    review_path = settings.root / settings.config.paths.review_csv

    with Ledger(settings.root / settings.config.paths.ledger) as ledger:
        payloads = ledger.all_rows()

    if not payloads:
        console.print(
            "[yellow]The ledger has no rows yet.[/yellow] Run `single` or `batch` first."
        )
        raise typer.Exit(code=1)

    records = [ProductRecord.model_validate_json(p) for p in payloads]
    with ReviewWriter(review_path, settings.config) as writer:
        for record in records:
            writer.write(record)

    console.print(
        f"[yellow]{review_path}[/yellow]   {writer.written} of {len(records)} stored row(s) "
        "need a human."
    )


@app.command()
def serve(
    host: Annotated[
        str,
        typer.Option("--host", help="Anything but 127.0.0.1 requires --token."),
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port")] = 8000,
    token: Annotated[
        str | None,
        typer.Option("--token", help="Require this bearer token. Mandatory off loopback."),
    ] = None,
    no_open: Annotated[
        bool, typer.Option("--no-open", help="Do not open a browser.")
    ] = False,
    dev: Annotated[
        bool,
        typer.Option("--dev", help="Allow the Vite dev server at :5173 to call this API."),
    ] = False,
    config: ConfigOpt = None,
    verbose: VerboseOpt = 0,
) -> None:
    """Run the local web console.

    Binds 127.0.0.1 by default, because this process holds your image-host and
    model keys and fetches arbitrary URLs from inside your network. Exposing it
    further is possible and requires you to say so twice: a --host and a --token.
    """
    import threading
    import webbrowser

    import uvicorn

    from .api.app import WEB_DIST, create_app, new_token
    from .utils.logging import setup_logging

    setup_logging(verbose)
    settings = _load_settings(config)

    loopback = host in ("127.0.0.1", "::1", "localhost")
    if not loopback and not token:
        console.print(
            _error_panel(
                f"--host {host} would expose this console beyond your own machine, so it "
                "needs a token.\n\n"
                "This process holds your image-host and Anthropic keys, and it fetches "
                "whatever URL it is handed. Anyone who can reach the port can drive it.\n\n"
                f"    haat-lister serve --host {host} --token {new_token()}\n\n"
                "The SSRF guard still applies, but it does not stop DNS rebinding -- do not "
                "put this on an untrusted network.",
                "A token is required off loopback",
            )
        )
        raise typer.Exit(code=2)

    if not loopback:
        console.print(
            Panel(
                f"[yellow]This console is listening on {host}:{port}, not just your own "
                "machine.[/yellow]\n"
                "Anyone who can reach that address and holds the token can start jobs, read "
                "your catalogue, and cause fetches from inside your network.",
                title="Exposed beyond loopback",
                border_style="yellow",
            )
        )

    url = f"http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}"  # noqa: S104
    if token:
        url += f"/?token={token}"

    built = (WEB_DIST / "index.html").exists()
    console.print(
        Panel(
            f"[bold]{url}[/bold]\n"
            f"config:   {settings.config_path}\n"
            f"console:  {'built' if built else 'NOT BUILT -- the API works, the page explains'}\n"
            f"auth:     {'token required' if token else 'none (loopback only)'}\n\n"
            "[dim]Ctrl-C to stop.[/dim]",
            title="haat-lister serve",
            border_style="blue",
        )
    )

    application = create_app(settings, token=token, dev=dev)

    if not no_open:
        # After a beat, so the first thing the browser sees is a server that is
        # already answering rather than a connection refused.
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    uvicorn.run(application, host=host, port=port, log_level="warning")


if __name__ == "__main__":  # pragma: no cover
    app()
