/*
  Past jobs, newest first.

  Every row links back into a job that is still fully reconstructable from the
  ledger, and the files are still on disk -- so this is a working index rather
  than a log. The download links go straight to the artifacts, because the
  commonest reason to come here is "I need that CSV again".
*/

import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { ApiError, api, withToken, type JobSummary } from "../api/client";

export function History() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["jobs"],
    queryFn: api.jobs,
    staleTime: 5_000,
  });

  if (isLoading) {
    return (
      <div className="bench">
        <p className="lede">Looking…</p>
      </div>
    );
  }

  if (error) {
    const dead = error instanceof ApiError && error.status === 0;
    return (
      <div className="bench">
        <h1 className="screen-title">Jobs</h1>
        <p className="lede">
          {dead ? (
            <>
              The agent isn&rsquo;t running. Start it with{" "}
              <code className="mono">haat-lister serve</code> and reload.
            </>
          ) : (
            String(error instanceof Error ? error.message : error)
          )}
        </p>
      </div>
    );
  }

  if (!data?.length) {
    return (
      <div className="bench">
        <h1 className="screen-title">No jobs yet</h1>
        <p className="lede">
          <Link to="/">Paste some product links</Link> and the first one appears here, with its
          files still downloadable.
        </p>
      </div>
    );
  }

  return (
    <div className="bench">
      <h1 className="screen-title">Jobs</h1>
      <p className="lede">
        {data.length} run{data.length === 1 ? "" : "s"}, newest first. Files stay on disk under{" "}
        <code className="mono">runs/</code>.
      </p>

      <table className="grid history">
        <caption className="visually-hidden">Past jobs, their counts, and their files</caption>
        <thead>
          <tr>
            <th scope="col">job</th>
            <th scope="col">when</th>
            <th scope="col">state</th>
            <th scope="col" className="num">
              written
            </th>
            <th scope="col" className="num">
              failed
            </th>
            <th scope="col">files</th>
          </tr>
        </thead>
        <tbody>
          {data.map((job) => (
            <Row key={job.job_id} job={job} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Row({ job }: { job: JobSummary }) {
  const written = job.counts.listed ?? 0;
  const failed = job.counts.failed ?? 0;
  const tone =
    job.state === "done"
      ? "depth-high"
      : job.state === "error"
        ? "is-failed"
        : job.state === "cancelled"
          ? "is-review"
          : "depth-medium";

  return (
    <tr>
      <td className="mono">
        <Link to={`/jobs/${job.job_id}`}>{job.job_id}</Link>
      </td>
      <td className="depth-low">{when(job.created_at)}</td>
      <td className={tone}>
        {/* A glyph as well as a colour: these screens get printed and
            screenshotted, and colour alone would not survive either. */}
        {job.state === "done" ? "✓ " : job.state === "cancelled" ? "◦ " : ""}
        {job.state}
      </td>
      <td className="num mono">{written}</td>
      <td className={`num mono ${failed ? "is-failed" : "depth-none"}`}>{failed}</td>
      <td className="files mono">
        <a
          href={withToken(`/api/jobs/${job.job_id}/download/listings`)}
          download
          aria-label={`Download listings.csv for ${job.job_id}`}
        >
          listings
        </a>
        <a
          href={withToken(`/api/jobs/${job.job_id}/download/zip`)}
          download
          aria-label={`Download everything for ${job.job_id} as a zip`}
        >
          zip
        </a>
      </td>
    </tr>
  );
}

function when(iso: string): string {
  const date = new Date(iso);
  const days = Math.floor((Date.now() - date.getTime()) / 86_400_000);
  if (days === 0) return `today ${date.toLocaleTimeString([], { timeStyle: "short" })}`;
  if (days === 1) return "yesterday";
  if (days < 7) return `${days} days ago`;
  return date.toLocaleDateString();
}
