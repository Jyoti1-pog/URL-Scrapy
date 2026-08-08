/*
  The sheet: one file that fills up.

  A sibling of Jobs rather than a section inside one, because it is not about a
  job. It is the thing the operator is actually building, and the jobs are how
  it got there.

  Read-only on purpose. The sheet is written by exactly one thing -- a job
  finishing -- and a screen that could edit it would be a second author of the
  one file they depend on.
*/

import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError, type Finding, type Sheet as SheetData } from "../api/client";

/* The four columns worth showing at a glance. The file has nineteen; a preview
   that shows all of them is a spreadsheet, badly. */
const SHOWN = ["title", "price_inr", "category_slug", "availability"];

export function Sheet() {
  const [sheet, setSheet] = useState<SheetData | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .sheet()
      .then(setSheet)
      .catch((err: unknown) => setError(err instanceof ApiError ? err.message : String(err)));
  }, []);

  if (error) {
    return (
      <div className="bench">
        <h1 className="screen-title">Your sheet</h1>
        <p className="error" role="alert">
          {error}
        </p>
      </div>
    );
  }

  if (!sheet) return <div className="bench" />;

  if (!sheet.exists) {
    return (
      <div className="bench">
        <h1 className="screen-title">Your sheet</h1>
        <p className="lede">
          Every job you finish adds its rows here, so a week of work is one file rather than
          seven. Nothing has been added yet.
        </p>
        <p>
          <Link to="/">Process some links</Link> and this fills up.
        </p>
      </div>
    );
  }

  const indices = SHOWN.map((name) => sheet.columns.indexOf(name)).filter((i) => i >= 0);

  return (
    <div className="bench sheet">
      <h1 className="screen-title">Your sheet</h1>
      <p className="lede">
        Everything every finished job has produced, deduplicated by product link. It is a valid
        haat import file exactly as it stands.
      </p>

      <dl className="facts sheet-facts">
        <dt>rows</dt>
        <dd className="mono depth-high">{sheet.rows.toLocaleString()}</dd>
        <dt>jobs merged</dt>
        <dd className="mono">{sheet.jobs}</dd>
        {sheet.last_added && (
          <>
            <dt>last added</dt>
            <dd className="mono">{sheet.last_added.replace("T", " ").replace("+00:00", " UTC")}</dd>
          </>
        )}
        <dt>size</dt>
        <dd className="mono">{(sheet.bytes / 1024).toFixed(1)} KB</dd>
        <dt>folder</dt>
        <dd className="mono folder">{sheet.folder}</dd>
      </dl>

      {!sheet.header_ok && (
        <p className="error" role="alert">
          This file no longer has haat's 19 columns, so it will not import. Something outside
          this tool changed it.
        </p>
      )}

      <div className="actions">
        <a className="primary button-link" href="/api/sheet/download" download>
          Download master.csv
        </a>
      </div>

      <OpenItems warnings={sheet.warnings} />

      <h2 className="sheet-preview-title">
        First {Math.min(sheet.preview.length, sheet.preview_limit)} row
        {sheet.preview.length === 1 ? "" : "s"}
        {sheet.rows > sheet.preview.length && <span className="hint"> of {sheet.rows}</span>}
      </h2>
      <div className="table-scroll">
        <table className="sheet-table">
          <thead>
            <tr>
              {indices.map((i) => (
                <th key={sheet.columns[i]}>{sheet.columns[i]}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {sheet.preview.map((row, index) => (
              <tr key={index}>
                {indices.map((i) => (
                  <td key={i} className={i === 0 ? "" : "mono"}>
                    {row[i] || <span className="depth-none">—</span>}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/* The open items, on the screen where they matter.

   All three are facts about what is IN this sheet -- unconfirmed subcategory
   slugs, a missing availability value, a two-entry HS map -- and the person
   about to upload it is the person who needs to know. They were only in the
   startup banner, which is the one place nobody looks twice. */
function OpenItems({ warnings }: { warnings: Finding[] }) {
  if (warnings.length === 0) return null;
  return (
    <details className="parsed open-items">
      <summary>
        {warnings.length} thing{warnings.length === 1 ? "" : "s"} to confirm with haat
      </summary>
      <ul className="open-items-list">
        {warnings.map((warning) => (
          <li key={warning.title}>
            <strong>{warning.title}</strong>
            <span>{warning.detail}</span>
            {warning.fix && <span className="hint">{warning.fix}</span>}
          </li>
        ))}
      </ul>
    </details>
  );
}
