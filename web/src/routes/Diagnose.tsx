/*
  "Why no photo?" — the whole answer, on one screen.

  A route rather than a modal, and that is a deliberate choice twice over. It is
  linkable, so an operator can send it to whoever runs the shop; and it can be
  reached without a job at all, which is what you want when you are deciding
  whether a site is worth pointing this tool at.

  Every number here comes from a real run against the real page, made when the
  screen was opened. Nothing is cached and nothing is inferred: the point of the
  report is that it is what happened, not what should have happened.
*/

import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, ApiError, type Diagnosis } from "../api/client";

/*
  §3.1. Three answers, and the third is not "no". `bot check: no` about a page
  that never arrived is a true statement about a variable and a false one about
  the shop, and it sends the reader to the extractor for a transport bug.
*/
const notReached = <span className="muted">— not reached</span>;

function Check({ value }: { value: string }) {
  if (value === "not reached") return notReached;
  return <>{value}</>;
}

export function Diagnose() {
  const [params, setParams] = useSearchParams();
  const url = params.get("url") ?? "";
  const [report, setReport] = useState<Diagnosis | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState(url);

  useEffect(() => {
    if (!url) return;
    setBusy(true);
    setError(null);
    setReport(null);
    api
      .diagnose(url)
      .then(setReport)
      .catch((err: unknown) => setError(err instanceof ApiError ? err.message : String(err)))
      .finally(() => setBusy(false));
  }, [url]);

  return (
    <div className="bench diagnose">
      <h1 className="screen-title">Why no photo?</h1>
      <p className="lede">
        One fetch of the page, then the same nine checks the run does, reported step by step.
        Nothing is written and no image host is contacted.
      </p>

      <form
        className="diagnose-form"
        onSubmit={(e) => {
          e.preventDefault();
          setParams(draft.trim() ? { url: draft.trim() } : {});
        }}
      >
        <label htmlFor="diagnose-url">Product page URL</label>
        <div className="row">
          <input
            id="diagnose-url"
            className="mono"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="https://yourshop.com/products/indigo-cotton-stole"
            spellCheck={false}
          />
          <button className="primary" type="submit" disabled={busy || !draft.trim()}>
            {busy ? "Looking" : "Look"}
          </button>
        </div>
      </form>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {busy && <p className="hint">Fetching the page and checking each photo in turn…</p>}
      {report && <Report report={report} />}
    </div>
  );
}

function Report({ report }: { report: Diagnosis }) {
  const f = report.fetch;
  return (
    <div className="report">
      <Verdict report={report} />

      <section>
        <h2>Fetch</h2>
        <dl className="facts">
          <Fact label="robots.txt">
            {f.robots_checked ? (f.robots_allowed ? "allowed" : "disallowed") : "not consulted"}
          </Fact>
          <Fact label="stage A">
            {f.ok
              ? `${f.status_code} · ${f.content_type || "?"} · ${kb(f.bytes)} · ${f.elapsed_ms}ms`
              : `${f.error_reason || "failed"} — ${f.error_detail.split("\n")[0]}`}
          </Fact>
          {f.redirected && <Fact label="redirected to">{f.final_url}</Fact>}
          <Fact label="looks like a product page">
            {report.shape.looks_like_product === "yes"
              ? report.shape.product_signals.join(", ")
              : report.shape.looks_like_product === "no"
                ? "no — nothing on it says product"
                : notReached}
          </Fact>
          <Fact label="bot check">
            <Check value={report.shape.captcha} />
          </Fact>
          <Fact label="sign-in wall">
            <Check value={report.shape.login_wall} />
          </Fact>
          <Fact label="stage B">
            {/* §3.3. The state is decided once, server-side. The console used
                to derive it from three booleans and reached a different word
                than the CLI for the same report. */}
            {report.stage_b.state === "ran"
              ? `ran (${report.stage_b.triggers.join(", ")}) — ${
                  report.stage_b.candidates_after - report.stage_b.candidates_before
                } more candidates`
              : report.stage_b.error
                ? `${report.stage_b.state}: ${report.stage_b.error}`
                : report.stage_b.state}
          </Fact>
        </dl>

        {/* §3.2. One row per rung. A host that needs HTTP/1.1 is invisible
            when only the winner is shown. */}
        {f.attempts.length > 0 && (
          <ul className="evidence">
            {f.attempts.map((a) => (
              <li key={a.rung} className="mono">
                {a.ok ? "ok" : "fail"} · {a.transport} · {a.outcome} · {a.elapsed_ms}ms
              </li>
            ))}
          </ul>
        )}
        {report.shape.evidence.length > 0 && (
          <ul className="evidence">
            {report.shape.evidence.map((line) => (
              <li key={line} className="mono">
                {line}
              </li>
            ))}
          </ul>
        )}
      </section>

      {report.title.value && (
        <section>
          <h2>Title</h2>
          <p className="title-value">{report.title.value}</p>
          <p className="hint mono">
            {report.title.source} · {report.title.confidence}
          </p>
        </section>
      )}

      <section>
        <h2>Where photos were looked for</h2>
        {/* Ten rules each reporting zero is indistinguishable from ten rules
            that were never run, and the second one is not a fact about the
            shop. */}
        {!report.images.collected && <p className="hint">{notReached} — no page arrived</p>}
        <ul className="rules">
          {report.images.rules.map((rule) => (
            <li key={rule.rule} className={rule.found ? "" : "empty"}>
              <span className="mono glyph">{rule.found ? "✓" : "·"}</span>
              <span>{rule.rule}</span>
              <span className="mono count">{rule.found}</span>
            </li>
          ))}
        </ul>
        {report.images.plugin_used && (
          <p className="hint">
            Plugin <strong>{report.images.plugin_used}</strong> matched this page
            {report.images.plugin_replaced_candidates ? " and supplied its own photos." : "."}
          </p>
        )}
        {report.images.dropped.length > 0 && (
          <details className="parsed">
            <summary>{report.images.dropped.length} reference(s) dropped before checking</summary>
            <ul className="parsed-list">
              {report.images.dropped.map((drop, i) => (
                <li key={`${drop.url}-${i}`}>
                  <span className="mono raw">{drop.url}</span>
                  <span className="hint">{drop.why}</span>
                </li>
              ))}
            </ul>
          </details>
        )}
      </section>

      {report.images.candidates.length > 0 && (
        <section>
          <h2>
            Each photo, checked in order
            <span className="hint">
              {" "}
              — minimum {report.thresholds.min_width}×{report.thresholds.min_height}
            </span>
          </h2>
          <ol className="candidates">
            {report.images.candidates.map((candidate) => (
              <li key={candidate.index} className={candidate.ok ? "won" : ""}>
                <p className="mono candidate-url">{candidate.url}</p>
                {!candidate.checked ? (
                  <p className="hint">not tried — an earlier photo already passed</p>
                ) : (
                  <ol className="steps">
                    {candidate.steps.map((step) => (
                      <li key={step.predicate} className={`step-${step.outcome.replace(" ", "-")}`}>
                        <span className="mono num">{step.predicate}</span>
                        <span>{step.name}</span>
                        <span className="mono detail">{step.detail}</span>
                      </li>
                    ))}
                  </ol>
                )}
              </li>
            ))}
          </ol>
        </section>
      )}
    </div>
  );
}

function Verdict({ report }: { report: Diagnosis }) {
  const won = report.images.method !== "none";
  return (
    <div className={`verdict ${won ? "verdict-ok" : "verdict-none"}`}>
      <p className="verdict-line">
        <span className="mono verdict-word">{won ? report.images.method : report.images.reason}</span>
        {won && report.images.winner && (
          <span className="mono verdict-url"> {report.images.winner}</span>
        )}
      </p>
      {!won && <p className="verdict-why">{report.images.explanation}</p>}
      {report.shape.verdict && (
        <p className="verdict-why">
          A run over this link fails the row with this reason rather than writing it out with an
          empty photo column.
        </p>
      )}
    </div>
  );
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <>
      <dt>{label}</dt>
      <dd className="mono">{children}</dd>
    </>
  );
}

function kb(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** The link that gets an operator here from a row with no photo. */
export function WhyNoPhoto({ url }: { url: string }) {
  return (
    <Link className="why-no-photo" to={`/diagnose?url=${encodeURIComponent(url)}`}>
      Why no photo?
    </Link>
  );
}
