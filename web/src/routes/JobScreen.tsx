/*
  The running screen.

  The rail is the vat: the fill grid and the column profile, the thing being
  made. Navigation stays a line of text links at the top -- if the rail carried
  navigation this would be an admin panel.

  Rows come from the event stream merged over the ledger. The ledger is
  authoritative and a refresh rebuilds from it alone; the stream only ever moves
  a row forward. That is what makes both a refresh and a dropped connection cost
  nothing.
*/

import { useCallback, useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api } from "../api/client";
import { useJobEvents } from "../hooks/useJobEvents";
import { FillGrid } from "../components/FillGrid";
import { RowStream, TierCounts, mergeRows, tierSummary, type LiveRow } from "../components/RowStream";
import { Downloads, RunFacts } from "../components/Downloads";
import { need } from "../lib/plural";

export function JobScreen() {
  const { jobId = "" } = useParams();
  const queries = useQueryClient();
  const [picked, setPicked] = useState<LiveRow | null>(null);

  const refetch = useCallback(() => {
    queries.invalidateQueries({ queryKey: ["job", jobId] });
  }, [queries, jobId]);

  const { events, finished, connected } = useJobEvents(jobId, refetch);

  const { data: job } = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => api.job(jobId),
    // Slow, because the stream carries the detail. This is the safety net for a
    // browser that lost the stream, not the mechanism.
    refetchInterval: finished ? false : 8000,
  });

  // One more fetch when the stream ends. Without it the screen can settle on a
  // snapshot taken microseconds before the job flipped to done, and sit there
  // saying "Processing" over a finished job -- which is exactly what it did the
  // first time this ran in a browser.
  useEffect(() => {
    if (finished) refetch();
  }, [finished, refetch]);

  if (!job) {
    return (
      <div className="bench">
        <p className="lede">Looking for {jobId}…</p>
      </div>
    );
  }

  const rows = mergeRows(job.rows, events);
  const processed = rows.filter((r) => r.outcome === "listed" || r.outcome === "failed").length;
  const written = rows.filter((r) => r.outcome === "listed").length;
  const failed = rows.filter((r) => r.outcome === "failed").length;
  const needsHuman = rows.filter((r) => r.needs_human).length;
  const running = job.state === "running" || job.state === "queued";

  return (
    <div className="workbench">
      <aside className="vat">
        <div className="vat-id mono">{job.job_id}</div>
        <div className="vat-settings depth-low">
          {String(job.settings.provenance)} · {String(job.settings.images)}
        </div>

        <FillGrid columns={job.columns} rows={rows} total={job.total} onPickRow={setPicked} />

        <div className="vat-count mono">
          {processed}/{job.total}
          {running && !connected && <span className="is-review"> · reconnecting</span>}
        </div>
      </aside>

      <div className="bench">
        <h1 className="screen-title">
          {running ? "Processing" : job.state === "cancelled" ? "Stopped" : "Processed"}
        </h1>

        <div className="job-summary">
          <Stat n={written} label="written" tone="depth-high" />
          <Stat n={needsHuman} label={`${need(needsHuman)} a human`} tone="is-review" />
          {/* Refused is not failed. The site declined and the tool was correct
              to stop, so it gets its own number in brass rather than madder --
              and it is excluded from the retry, because retrying a refusal
              produces the same refusal forever. */}
          <Stat n={job.refused} label="refused" tone={job.refused ? "is-review" : "depth-none"} />
          <Stat n={failed} label="failed" tone={failed ? "is-failed" : "depth-none"} />
          <div className="job-actions">
            {running && (
              <button className="quiet" onClick={() => api.cancel(jobId).then(refetch)}>
                Cancel
              </button>
            )}
            {!running && failed > 0 && (
              <button className="quiet" onClick={() => api.resume(jobId).then(refetch)}>
                Retry the {failed} that failed
              </button>
            )}
          </div>
        </div>

        {!running && written === 0 && failed > 0 && <NothingWorked rows={rows} />}

        {running ? (
          <TierCounts rows={rows} />
        ) : (
          <>
            <RunFacts job={job} tiers={tierSummary(rows)} />
            <Downloads job={job} needsHuman={needsHuman} />
          </>
        )}

        {picked && (
          <RowDetail row={picked} columns={job.columns} onClose={() => setPicked(null)} />
        )}

        <RowStream rows={rows} onPick={setPicked} />

        {!running && (
          <p className="lede done-note">
            Everything is also on disk in <code className="mono">runs/{job.job_id}/</code>.{" "}
            <Link to="/">Start another job</Link>.
          </p>
        )}
      </div>
    </div>
  );
}

/*
  A job where nothing worked. Shrugging here is the worst thing the screen can
  do -- an operator has just waited several minutes for zero rows, and the
  reason is nearly always one of three things the failure column already knows.
*/
function NothingWorked({ rows }: { rows: LiveRow[] }) {
  const reasons = new Map<string, number>();
  for (const row of rows) {
    if (row.outcome === "failed" && row.reason) {
      reasons.set(row.reason, (reasons.get(row.reason) ?? 0) + 1);
    }
  }
  const top = [...reasons.entries()].sort((a, b) => b[1] - a[1])[0]?.[0] ?? "";

  const explanation = top.startsWith("robots")
    ? "Those shops ask crawlers to stay out of these pages. Nothing was fetched, which is the correct outcome — but it means there is nothing to import. If you own the shop, turn off robots.txt in settings."
    : top === "blocked_address"
      ? "The addresses resolved somewhere this tool refuses to fetch from — loopback or a private network. If they are machines you own, add their hostnames to fetch.allow_private_hosts in config.yaml."
      : top.startsWith("http_4")
        ? "The shops answered, but not with a product page. Check the links open in a browser; a 404 here usually means the URLs are stale."
        : top.startsWith("http_5") || top === "timeout"
          ? "The shops did not answer in time. This is usually temporary — failed.csv holds every URL, and Retry will run just those."
          : "Every URL produced nothing. failed.csv holds them all with a reason each, and its URL column pastes straight back into a new job.";

  return (
    <div className="nothing-worked" role="status">
      <strong>No rows were produced.</strong>{" "}
      {top && (
        <>
          The commonest reason was <code className="mono">{top}</code>.{" "}
        </>
      )}
      {explanation}
    </div>
  );
}

function Stat({ n, label, tone }: { n: number; label: string; tone: string }) {
  return (
    <div className="stat">
      <span className={`stat-n mono ${tone}`}>{n}</span>
      <span className="stat-label">{label}</span>
    </div>
  );
}

const DEPTH_WORD: Record<string, string> = {
  "3": "read straight from the page",
  "2": "inferred — worth a glance",
  "1": "guessed",
  "0": "nothing found",
  "-": "locked — a GI tag is a government certification",
};

/** What one row put in each of the 19 columns. Reached by clicking a cell in
 *  the grid, which is the promise the grid makes. */
function RowDetail({
  row,
  columns,
  onClose,
}: {
  row: LiveRow;
  columns: string[];
  onClose: () => void;
}) {
  return (
    <aside className="row-detail">
      <div className="row-detail-head">
        <span className="mono depth-low">row {row.input_index + 1}</span>
        <strong>{row.title || row.source_url}</strong>
        <button className="quiet small" onClick={onClose}>
          Close
        </button>
      </div>
      <dl className="cells">
        {columns.map((name, index) => {
          const depth = row.cells ? row.cells[index] : " ";
          return (
            <div key={name} className="cellrow">
              <dt className="mono">{name}</dt>
              <dd className={depthTone(depth)}>{DEPTH_WORD[depth] ?? "not yet"}</dd>
            </div>
          );
        })}
      </dl>
    </aside>
  );
}

function depthTone(depth: string): string {
  if (depth === "3") return "depth-high";
  if (depth === "2") return "depth-medium";
  if (depth === "1") return "depth-low";
  return "depth-none";
}
