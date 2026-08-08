/*
  Preflight: what this job would do, before it does any of it.

  This screen is where a mistake costs seconds instead of ten minutes, so it is
  a deliberate stop rather than a toast. The two things it can tell an operator
  that they could not work out themselves are the robots position and the
  per-host time floor -- and both change whether they want to press the button.
*/

import { Link } from "react-router-dom";
import type { Preflight } from "../api/client";

interface Props {
  preflight: Preflight;
  estimate: string;
  busy: boolean;
  onBack: () => void;
  onStart: () => void;
}

export function PreflightPanel({ preflight, estimate, busy, onBack, onStart }: Props) {
  const domains = Object.entries(preflight.domains);
  const blocked = preflight.robots_disallowed.length;

  return (
    <section className="preflight" aria-labelledby="preflight-title">
      <h2 id="preflight-title">Before it starts</h2>

      <p className="preflight-lede">
        <strong className="mono">{preflight.unique}</strong> product
        {preflight.unique === 1 ? "" : "s"} across{" "}
        <strong className="mono">{domains.length}</strong> shop
        {domains.length === 1 ? "" : "s"}. About <strong>{estimate}</strong>.
      </p>

      <dl className="preflight-facts">
        {preflight.duplicates > 0 && (
          <Fact
            n={preflight.duplicates}
            label={`duplicate link${preflight.duplicates === 1 ? "" : "s"} collapsed`}
            detail="Same product, different tracking parameters. Fetched once."
          />
        )}
        {preflight.invalid.length > 0 && (
          <Fact
            n={preflight.invalid.length}
            tone="is-failed"
            label={`line${preflight.invalid.length === 1 ? "" : "s"} that are not links`}
            detail={preflight.invalid.map((i) => `line ${i.line}: ${i.raw}`).join(" · ")}
          />
        )}
        {blocked > 0 && (
          <Fact
            n={blocked}
            tone="is-review"
            label="will be skipped: robots.txt says no"
            detail="Those shops ask crawlers to stay out of these pages. They will appear in failed.csv with the reason, and nothing will be fetched from them."
          />
        )}
        {preflight.observed.length > 0 && (
          /* §9. A warning that names a problem and not the way round it is
             half a message. The route that works on a refusing host is the
             operator's own export or a saved page, so the offer is here, next
             to the news, rather than three screens away. */
          <p className="help">
            Hosts that refuse this tool can still be listed from a file you already have:{" "}
            <Link to="/import">import a seller export or a saved page</Link>. Nothing here
            stops this run -- it will ask them again.
          </p>
        )}
        {preflight.observed.map((host) => (
          /* §4.4. Said before the run rather than four minutes into it. Toned
             as a note rather than a failure on purpose: this is what happened
             last time, and the run is going to ask again. */
          <Fact
            key={host.host}
            n={host.urls}
            tone="is-review"
            label={`on ${host.host}, which answered ${host.reason} last time`}
            detail={host.detail}
          />
        ))}
        {!preflight.robots_checked && (
          <Fact
            n={0}
            tone="is-review"
            label="robots.txt was not consulted"
            detail="You turned it off. Only do that for shops you own."
          />
        )}
      </dl>

      {domains.length > 1 && (
        <div className="domains">
          {domains.slice(0, 8).map(([host, n]) => (
            <span key={host} className="domain mono">
              {host} <span className="depth-low">{n}</span>
            </span>
          ))}
          {domains.length > 8 && (
            <span className="domain depth-low">and {domains.length - 8} more</span>
          )}
        </div>
      )}

      {domains.length === 1 && preflight.unique > 20 && (
        <p className="help">
          Everything is on one shop, so it will be fetched one page at a time however high
          you set "at once". That is the politeness floor, and it is why the estimate is
          what it is.
        </p>
      )}

      <div className="actions">
        <button className="primary" onClick={onStart} disabled={busy}>
          {busy ? "Starting" : `Process ${preflight.unique}`}
        </button>
        <button className="quiet" onClick={onBack} disabled={busy}>
          Back to the links
        </button>
      </div>
    </section>
  );
}

function Fact({
  n,
  label,
  detail,
  tone = "depth-low",
}: {
  n: number;
  label: string;
  detail: string;
  tone?: string;
}) {
  return (
    <div className="fact">
      <dt className={`mono ${tone}`}>{n > 0 ? n : "—"}</dt>
      <dd>
        <span className={tone}>{label}</span>
        <span className="detail">{detail}</span>
      </dd>
    </div>
  );
}
