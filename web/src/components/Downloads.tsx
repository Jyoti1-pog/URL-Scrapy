/*
  The end of the flow: one primary action, everything else secondary.

  `listings.csv` is what gets uploaded to haat, so it is the button. The other
  three and the zip sit under it as quiet links, sized in rows rather than
  kilobytes -- "38 rows" is the number an operator is deciding about; "4.1 KB"
  is not.

  If anything needs a human the review link is prominent and says the number
  out loud, because "38 rows need a price before upload" is the difference
  between a CSV that imports and one that imports wrong.
*/

import { Link } from "react-router-dom";
import { rowsNeed } from "../lib/plural";
import type { MasterResult } from "../api/client";
import type { Job } from "../api/client";
import { withToken } from "../api/client";

const LABELS: Record<string, { title: string; blurb: string }> = {
  listings: { title: "listings.csv", blurb: "the import file — 19 columns, in your order" },
  listings_with_images: {
    title: "listings_with_images.csv",
    blurb: "the same rows plus every photo link — for your own records, not for haat",
  },
  review: { title: "review.csv", blurb: "every row that needs a human, and which cells" },
  manifest: { title: "image_manifest.csv", blurb: "which photo belongs to which row" },
  failed: { title: "failed.csv", blurb: "URLs that produced nothing — paste back to retry" },
};

export function Downloads({ job, needsHuman }: { job: Job; needsHuman: number }) {
  const by = new Map(job.artifacts.map((a) => [a.name, a]));
  const listings = by.get("listings");
  const href = (name: string) => withToken(`/api/jobs/${job.job_id}/download/${name}`);

  return (
    <section className="downloads">
      <div className="downloads-primary">
        <a className="button primary" href={href("listings")} download>
          Download listings.csv
        </a>
        <span className="hint">
          {listings?.rows ?? 0} row{listings?.rows === 1 ? "" : "s"} · {kb(listings?.bytes)}
        </span>
      </div>

      {/* The second file, right under the first. haat's template has no image
          column, so the photo links cannot live in the import file -- and an
          operator who has been asking where they went should not have to find
          this in a list of four. */}
      {by.get("listings_with_images") && (
        <p className="companion-line">
          <a href={href("listings_with_images")} download className="mono">
            listings_with_images.csv
          </a>{" "}
          <span className="depth-low">
            — the same {by.get("listings_with_images")?.rows ?? 0} row
            {by.get("listings_with_images")?.rows === 1 ? "" : "s"} plus every photo link, for your
            own records. Not for uploading to haat.
          </span>
        </p>
      )}

      <SheetLine master={job.master} />

      {/* §1.3: when nothing needs a human the banner does not render. It used
          to appear over three rows that had no content at all, explaining that
          the problem was "mostly a price" on pages that never loaded. */}
      {needsHuman > 0 && (
        <p className="needs-human-note">
          <strong>{rowsNeed(needsHuman)} a human before upload.</strong>{" "}
          Mostly a price, which is a business decision rather than something on the page.{" "}
          <Link to={`/jobs/${job.job_id}/review`} className="fix-link">
            Fix them here
          </Link>{" "}
          — or{" "}
          <a href={href("review")} download>
            download review.csv
          </a>
          .
        </p>
      )}

      <ul className="downloads-rest">
        {["review", "manifest", "failed"].map((name) => {
          const artifact = by.get(name);
          if (!artifact) return null;
          return (
            <li key={name}>
              <a href={href(name)} download className="mono">
                {LABELS[name].title}
              </a>
              <span className="depth-low">{LABELS[name].blurb}</span>
              <span className="mono depth-low count">
                {artifact.rows ?? 0} row{artifact.rows === 1 ? "" : "s"}
              </span>
            </li>
          );
        })}
        <li>
          <a href={href("zip")} download className="mono">
            everything.zip
          </a>
          <span className="depth-low">all four files, the photos, and what settings you used</span>
          <span className="mono depth-low count" />
        </li>
      </ul>
    </section>
  );
}

export function RunFacts({ job, tiers }: { job: Job; tiers: string }) {
  return (
    <p className="runfacts mono depth-low">
      {job.duration_s !== null && <span>{humanDuration(job.duration_s)}</span>}
      <span>{tiers}</span>
      {/* Rule 1's bill. Zero is the number to expect in manifest mode, and
          seeing it stay zero is how an operator knows the gate held. */}
      <span className={job.host_calls ? "is-review" : ""}>
        {job.host_calls} image-host call{job.host_calls === 1 ? "" : "s"}
      </span>
      {job.pages_rendered > 0 && <span>{job.pages_rendered} rendered in a browser</span>}
    </p>
  );
}

function kb(bytes: number | undefined): string {
  if (!bytes) return "0 KB";
  return bytes < 1024 ? `${bytes} B` : `${Math.round(bytes / 1024)} KB`;
}

function humanDuration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${Math.round(seconds % 60)}s`;
}

/* What the job did to the accumulating sheet.

   Said here rather than left to be discovered. The primary action stays
   "Download listings.csv" -- this is one secondary line, because an operator
   who does not know the sheet exists will keep merging files by hand. */
function SheetLine({ master }: { master: MasterResult | null }) {
  if (!master) return null;

  if (master.error) {
    return (
      <p className="sheet-line is-failed">
        {master.error.split("\n")[0]}{" "}
        <Link to="/sheet" className="fix-link">
          Your sheet
        </Link>
      </p>
    );
  }

  const added = master.added + master.replaced;
  return (
    <p className="sheet-line">
      {added > 0 ? (
        <>
          Also added {added} row{added === 1 ? "" : "s"} to{" "}
          <Link to="/sheet" className="fix-link">
            your sheet
          </Link>{" "}
          — now {master.total} row{master.total === 1 ? "" : "s"}.
        </>
      ) : (
        <>
          Nothing new for{" "}
          <Link to="/sheet" className="fix-link">
            your sheet
          </Link>{" "}
          — it already has {master.total} row{master.total === 1 ? "" : "s"}.
        </>
      )}
      {master.skipped > 0 && (
        <>
          {" "}
          {master.skipped} row{master.skipped === 1 ? " was" : "s were"} already in it.
        </>
      )}
    </p>
  );
}
