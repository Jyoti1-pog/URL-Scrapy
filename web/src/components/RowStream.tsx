/*
  The row stream: what is happening now, and what each finished row produced.

  Rows arrive from the event feed rather than a poll, so a row appears the
  moment it lands rather than up to a second and a half later. Failures show
  inline, in madder, immediately -- never held back until the end, because the
  operator watching this screen is deciding whether to let it keep running.

  The in-flight rows sit at the top with their current stage. Those stages are
  real transitions in `process_url`, not a timer: `fetching` really means a
  request is open.
*/

import type { JobRow } from "../api/client";
import type { JobEvent } from "../hooks/useJobEvents";

export interface LiveRow extends JobRow {
  stage?: string;
  live?: boolean;
}

/**
 * Merge what the ledger knows with what the stream has said since.
 *
 * The ledger is authoritative -- a refresh rebuilds from it alone -- and events
 * only ever move a row *forward*. Where they disagree, the newer of the two
 * wins by construction: a streamed row_done supersedes a pending ledger row,
 * and a ledger row that already has an outcome ignores a stale row_started.
 */
export function mergeRows(rows: JobRow[], events: JobEvent[]): LiveRow[] {
  const merged = new Map<number, LiveRow>(rows.map((r) => [r.input_index, { ...r }]));

  for (const event of events) {
    const index = event.data.index;
    if (typeof index !== "number") continue;
    const current = merged.get(index) ?? {
      input_index: index,
      source_url: event.data.url ?? "",
      outcome: null,
      row_key: null,
      title: "",
      status: "",
      image_tier: "",
      reason: "",
      needs_human: false,
      missing: [],
      cells: "",
    };

    if (event.name === "row_started") {
      if (!current.outcome) merged.set(index, { ...current, live: true, stage: "starting" });
    } else if (event.name === "row_stage") {
      if (!current.outcome) merged.set(index, { ...current, live: true, stage: event.data.stage });
    } else if (event.name === "row_done" || event.name === "row_failed") {
      merged.set(index, {
        ...current,
        live: false,
        stage: undefined,
        outcome: event.name === "row_done" ? "listed" : "failed",
        row_key: event.data.row_key ?? current.row_key,
        title: event.data.title ?? current.title,
        status: event.data.status ?? current.status,
        image_tier: event.data.image_tier ?? current.image_tier,
        reason: event.data.reason ?? current.reason,
        needs_human: event.data.needs_human ?? current.needs_human,
        missing: event.data.missing ?? current.missing,
        cells: event.data.cells ?? current.cells,
      });
    }
  }

  return [...merged.values()].sort((a, b) => a.input_index - b.input_index);
}

const STAGE_WORDS: Record<string, string> = {
  starting: "starting",
  fetching: "fetching the page",
  extracting: "reading it",
  rendering: "rendering in a browser",
  enriching: "categorising",
  images: "checking photos",
  written: "writing",
};

export function RowStream({ rows, onPick }: { rows: LiveRow[]; onPick?: (r: LiveRow) => void }) {
  const live = rows.filter((r) => r.live);
  const settled = rows.filter((r) => !r.live);

  return (
    <>
      {live.length > 0 && (
        <ol className="rows rows-live" aria-label="in progress">
          {live.map((row) => (
            <li key={row.input_index} className="row row-live">
              <span className="row-glyph spin" aria-hidden>
                ·
              </span>
              <span className="mono row-index">{row.input_index + 1}</span>
              <span className="row-title depth-low mono">{shortUrl(row.source_url)}</span>
              <span className="row-tier mono depth-low">
                {STAGE_WORDS[row.stage ?? ""] ?? row.stage}
              </span>
              <span />
            </li>
          ))}
        </ol>
      )}

      <ol className="rows">
        {settled.map((row) => (
          <li
            key={row.input_index}
            className={`row row-${row.outcome ?? "pending"}`}
            onClick={onPick ? () => onPick(row) : undefined}
          >
            <span className="row-glyph" aria-hidden>
              {row.outcome === "listed" ? "✓" : row.outcome === "failed" ? "✕" : "·"}
            </span>
            <span className="mono row-index">{row.input_index + 1}</span>
            <span className="row-title">
              {row.title || <span className="depth-low mono">{shortUrl(row.source_url)}</span>}
            </span>
            <TierBadge row={row} />
            <span className="row-flag mono is-review" title={row.missing.join(", ")}>
              {row.needs_human ? `⚑ ${row.missing.length || ""}` : ""}
            </span>
          </li>
        ))}
      </ol>
    </>
  );
}

/*
  The tier badge. Rule 1's whole economics in one word per row:

    direct  the shop's own URL survived the nine predicates. Nothing was paid.
    local   the URL failed, so the bytes were downloaded and kept as files.
    hosted  the URL failed AND a URL was needed, so it was re-uploaded. Costs money.
    none    nothing usable. This row has no photo.

  hosted is brass and none is madder, on purpose: a rising hosted ratio is a
  thing an operator should notice while it is happening, not in a summary.
*/
function TierBadge({ row }: { row: LiveRow }) {
  if (row.outcome === "failed") {
    return <span className="row-tier mono is-failed">{row.reason || "failed"}</span>;
  }
  const tone =
    row.image_tier === "direct" || row.image_tier === "local"
      ? "depth-medium"
      : row.image_tier === "hosted"
        ? "is-review"
        : row.image_tier === "none"
          ? "is-failed"
          : "depth-none";
  return (
    <span className={`row-tier mono ${tone}`} title={row.reason}>
      {row.image_tier}
    </span>
  );
}

/** The same four numbers as one line, for the finished screen. */
export function tierSummary(rows: LiveRow[]): string {
  const done = rows.filter((r) => r.outcome === "listed");
  const by = (tier: string) => done.filter((r) => r.image_tier === tier).length;
  return [
    `direct ${by("direct")}`,
    `local ${by("local")}`,
    `hosted ${by("hosted")}`,
    by("none") ? `no photo ${by("none")}` : null,
  ]
    .filter(Boolean)
    .join(" · ");
}

export function TierCounts({ rows }: { rows: LiveRow[] }) {
  const done = rows.filter((r) => r.outcome === "listed");
  const by = (tier: string) => done.filter((r) => r.image_tier === tier).length;
  const hosted = by("hosted");

  return (
    <p className="tiers mono">
      <span className="depth-medium">direct {by("direct")}</span>
      <span className="depth-medium">local {by("local")}</span>
      <span className={hosted ? "is-review" : "depth-none"}>hosted {hosted}</span>
      <span className={by("none") ? "is-failed" : "depth-none"}>no photo {by("none")}</span>
    </p>
  );
}

function shortUrl(url: string): string {
  return url.replace(/^https?:\/\//, "").slice(0, 64);
}
