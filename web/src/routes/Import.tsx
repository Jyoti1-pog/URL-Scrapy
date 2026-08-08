/*
  Import: the door that works when fetching does not.

  A sibling of Compose, not a mode of it. The two screens ask for different
  things — Compose asks for links, this asks for a file the operator already
  has — and folding the second into the first would make the answer to "this
  site refuses us" a checkbox on the screen that just failed.

  TWO STEPS, ALWAYS, and the second one is the point. The file is inspected
  first and nothing is built; the operator sees every column, including the ones
  we did not understand, and confirms before a single row exists. A one-shot
  upload has nowhere to show them — by the time there is a response, the rows
  are already built under a mapping nobody agreed to.

  `provenance` is asked for here in the same words as everywhere else. An import
  is not a loophole in Rule 2.1: reading a page off local disk tells us nothing
  about who owns the photographs in it.
*/

import { useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError, type ImportInspect, type ImportRun } from "../api/client";

type Provenance = "own" | "authorised" | "third-party";

const PROVENANCE: { value: Provenance; label: string; note: string }[] = [
  { value: "own", label: "My own shop", note: "I own this catalogue and these photographs." },
  {
    value: "authorised",
    label: "Authorised",
    note: "I have the seller's permission to list these.",
  },
  {
    value: "third-party",
    label: "Someone else's",
    note: "Every row will need a human, photos will not be re-hosted, and descriptions get rewritten.",
  },
];

export function Import() {
  const [file, setFile] = useState<File | null>(null);
  const [report, setReport] = useState<ImportInspect | null>(null);
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const [provenance, setProvenance] = useState<Provenance | "">("");
  const [sourceUrl, setSourceUrl] = useState("");
  const [profileName, setProfileName] = useState("");
  const [result, setResult] = useState<ImportRun | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onFile(chosen: File) {
    setFile(chosen);
    setReport(null);
    setResult(null);
    setError(null);
    setBusy(true);
    try {
      const inspected = await api.inspectImport(chosen);
      setReport(inspected);
      setMapping(
        Object.fromEntries(inspected.columns.filter((c) => c.target).map((c) => [c.header, c.target])),
      );
      setSourceUrl(inspected.source_url);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function start() {
    if (!file || !provenance) return;
    setBusy(true);
    setError(null);
    try {
      setResult(
        await api.runImport({
          file,
          provenance,
          mapping: report?.kind === "export" ? mapping : undefined,
          source_url: sourceUrl || undefined,
          save_profile: profileName || undefined,
        }),
      );
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  const unmapped = report?.columns.filter((c) => !mapping[c.header]) ?? [];
  const hasUrl = Object.values(mapping).includes("source_url");
  const ready = Boolean(provenance) && (report?.kind === "saved_page" ? Boolean(sourceUrl) : hasUrl);

  return (
    <div className="compose">
      <h1>Import a file</h1>
      <p className="lede">
        For sites that refuse this tool, and for catalogues you already have. An export from your
        seller panel, or a product page you saved with <kbd>Ctrl+S</kbd> — the same extraction, the
        same photo checks, the same nineteen columns.
      </p>

      <section>
        <h2>The file</h2>
        <input
          type="file"
          accept=".csv,.tsv,.txt,.xlsx,.xlsm,.html,.htm,.mhtml,.mht"
          onChange={(e) => e.target.files?.[0] && onFile(e.target.files[0])}
        />
        {file && (
          <p className="hint">
            {file.name} · {(file.size / 1024).toFixed(0)} KB
          </p>
        )}
        <p className="hint">
          Seller export as <code>.csv</code>, <code>.tsv</code> or <code>.xlsx</code>. Saved page as{" "}
          <code>.html</code> or <code>.mhtml</code> — choose “Webpage, complete” so the photos come
          with it.
        </p>
      </section>

      {error && <p className="error">{error}</p>}

      {report?.kind === "saved_page" && (
        <section>
          <h2>Where it came from</h2>
          <input
            type="url"
            value={sourceUrl}
            onChange={(e) => setSourceUrl(e.target.value)}
            placeholder="https://shop.example/p/…"
          />
          <p className="hint">
            {report.source_url
              ? "Read from the file. Change it if it is wrong."
              : "This file does not record its own address. Without it the row has no source and cannot be deduplicated."}
          </p>
        </section>
      )}

      {report?.kind === "export" && (
        <section>
          <h2>
            Columns <span className="hint">{report.row_count} row(s)</span>
          </h2>
          {report.profile_used && (
            <p className="hint">
              Applied your saved mapping <strong>{report.profile_used}</strong>.
            </p>
          )}

          <table className="mapper">
            <thead>
              <tr>
                <th>In your file</th>
                <th>Becomes</th>
              </tr>
            </thead>
            <tbody>
              {report.columns.map((column) => (
                <tr key={column.header} className={mapping[column.header] ? "" : "empty"}>
                  <td>
                    <strong>{column.header}</strong>
                    {column.samples[0] && (
                      <span className="hint mono"> e.g. {column.samples[0].slice(0, 40)}</span>
                    )}
                  </td>
                  <td>
                    <select
                      value={mapping[column.header] ?? ""}
                      onChange={(e) =>
                        setMapping((m) => {
                          const next = { ...m };
                          if (e.target.value) next[column.header] = e.target.value;
                          else delete next[column.header];
                          return next;
                        })
                      }
                    >
                      {/* §4.1: an unmapped column is a choice you can see, not
                          a default that happened. And `gi_region` is not in
                          this list because the server does not send it. */}
                      <option value="">
                        {column.known_unused
                          ? `${column.known_unused} — no haat column for it`
                          : "not used"}
                      </option>
                      {report.targets.map((target) => (
                        <option key={target} value={target}>
                          {target}
                        </option>
                      ))}
                    </select>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {unmapped.length > 0 && (
            <p className="hint">
              {unmapped.length} column(s) will not be used:{" "}
              {unmapped.map((c) => c.header).join(", ")}. Nothing is thrown away silently — map
              anything that matters.
            </p>
          )}
          {!hasUrl && (
            <p className="error">
              Map one column to <code>source_url</code>. Every row needs the product’s address: it
              is what the row is keyed on and deduplicated by.
            </p>
          )}

          <label className="stacked">
            Remember this mapping as
            <input
              type="text"
              value={profileName}
              onChange={(e) => setProfileName(e.target.value)}
              placeholder="e.g. nilaya-panel (optional)"
            />
          </label>
        </section>
      )}

      {report && (
        <section>
          {/* The same fieldset, in the same words, as Compose. An import is
              not a loophole in Rule 2.1, and asking differently here would
              suggest it might be. */}
          <fieldset className="provenance">
            <legend>
              Who made this content? <span className="required">required</span>
            </legend>
            <div className="choices">
              {PROVENANCE.map((option) => (
                <label
                  key={option.value}
                  className={provenance === option.value ? "chosen" : ""}
                >
                  <input
                    type="radio"
                    name="provenance"
                    value={option.value}
                    checked={provenance === option.value}
                    onChange={() => setProvenance(option.value)}
                  />
                  {option.label}
                </label>
              ))}
            </div>
            {provenance && (
              <p className="help">{PROVENANCE.find((o) => o.value === provenance)?.note}</p>
            )}
          </fieldset>

          <div className="actions">
            <button type="button" className="primary" disabled={!ready || busy} onClick={start}>
              {busy ? "Importing…" : "Import"}
            </button>
          </div>
        </section>
      )}

      {result && (
        <section>
          <h2>
            {result.written} written · {result.needs_human} need a human · {result.failed} failed
          </h2>
          {result.profile_saved && (
            <p className="hint">Saved the mapping as {result.profile_saved}.</p>
          )}
          <ul className="rows">
            {result.rows.map((row) => (
              <li key={row.source_url}>
                <span className={`badge is-${row.status.replace("_", "-")}`}>{row.status}</span>{" "}
                <strong>{row.title || row.source_url}</strong>
                {row.no_image_reason && (
                  <span className="hint mono"> no photo: {row.no_image_reason}</span>
                )}
                {row.notes.length > 0 && <p className="hint">{row.notes[0]}</p>}
              </li>
            ))}
          </ul>
          <p className="hint">
            Rows are in <Link to="/sheet">the sheet</Link>. Anything needing a human is there too.
          </p>
        </section>
      )}
    </div>
  );
}
