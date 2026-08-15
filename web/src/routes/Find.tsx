/*
  Find photos: "why no photo?" for a catalogue.

  Deliberately not a job screen. Nothing here writes a listing, touches the
  sheet, or costs an image-host call, and the screen says so — an operator
  should be able to point this at two hundred links to decide whether the run is
  worth starting, without wondering what it just did.

  Rows arrive as they resolve and are re-sorted into input order for display.
  Completion order is right for the stream and wrong for a table someone is
  reading down.
*/

import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError, type FindRow, type ParsedTable } from "../api/client";
import { useParsedLinks } from "../hooks/useParsedLinks";

type Filter = "all" | "photo" | "none" | "lowres" | "failed";

export function Find() {
  const [text, setText] = useState("");
  const [file, setFile] = useState<{ name: string; text: string } | null>(null);
  const [table, setTable] = useState<ParsedTable | null>(null);
  const [findId, setFindId] = useState<string | null>(null);
  const [rows, setRows] = useState<Map<number, FindRow>>(new Map());
  const [total, setTotal] = useState(0);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const stream = useRef<EventSource | null>(null);

  // `error` and `checked` were previously dropped, so an unreachable agent
  // showed as `links 0` with no explanation anywhere on the screen -- the one
  // place a reader would look for one.
  const { parse, error: parseError, checked } = useParsedLinks(file ? "" : text);
  const ready = file ? (table?.found ?? 0) > 0 : parse.unique > 0;

  useEffect(() => () => stream.current?.close(), []);

  async function onFile(chosen: File) {
    const content = await chosen.text();
    setFile({ name: chosen.name, text: content });
    setError(null);
    try {
      setTable(await api.parseFindFile(content));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }

  async function onColumn(column: string) {
    if (!file) return;
    setTable(await api.parseFindFile(file.text, column));
  }

  async function start() {
    setError(null);
    setRows(new Map());
    try {
      const created = file
        ? await api.startFind({ file_text: file.text, url_column: table?.url_column })
        : await api.startFind({ urls: text.split("\n") });
      setFindId(created.find_id);
      setTotal(created.accepted);
      setRunning(true);
      listen(created.find_id);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }

  function listen(id: string) {
    stream.current?.close();
    const source = api.findStream(id);
    stream.current = source;
    source.addEventListener("find_row", (event) => {
      const row = JSON.parse((event as MessageEvent).data) as FindRow;
      setRows((prev) => new Map(prev).set(row.index, row));
    });
    source.addEventListener("find_done", () => {
      setRunning(false);
      source.close();
    });
    source.onerror = () => {
      // The stream ends when the find does; a closed connection is not an
      // error worth showing next to a finished table.
      if (source.readyState === EventSource.CLOSED) setRunning(false);
    };
  }

  const ordered = useMemo(
    () => [...rows.values()].sort((a, b) => a.index - b.index),
    [rows],
  );
  const shown = useMemo(() => ordered.filter((r) => matches(r, filter)), [ordered, filter]);
  const counts = useMemo(() => tally(ordered), [ordered]);

  return (
    <div className="bench find">
      <h1 className="screen-title">Find photos</h1>
      <p className="lede">
        Every photo for every product, before you commit to a run. Nothing is written, no
        listing is created, and no image host is contacted — this is a look, not a job.{" "}
        <Link to="/diagnose">One link at a time?</Link>
      </p>

      {!findId && (
        <>
          <div className="compose-grid">
            <div className="compose-input">
              <textarea
                value={text}
                onChange={(e) => {
                  setText(e.target.value);
                  setFile(null);
                  setTable(null);
                }}
                spellCheck={false}
                placeholder="https://yourshop.com/products/indigo-cotton-stole"
                aria-label="Product links"
                rows={10}
                disabled={Boolean(file)}
              />
              <p className="hint">
                Commas, newlines, tabs — however you have them. Or{" "}
                <label className="linkish file-label">
                  upload a .csv / .txt / .tsv
                  <input
                    type="file"
                    accept=".csv,.txt,.tsv"
                    onChange={(e) => e.target.files?.[0] && onFile(e.target.files[0])}
                  />
                </label>
                .
              </p>
              {file && table && (
                <UploadSummary
                  name={file.name}
                  table={table}
                  onColumn={onColumn}
                  onClear={() => {
                    setFile(null);
                    setTable(null);
                  }}
                />
              )}
            </div>

            <dl className="counter" aria-live="polite">
              <div>
                <dt>links</dt>
                <dd className="mono depth-high">
                  {file ? (table?.found ?? 0) : checked ? parse.unique : "--"}
                </dd>
              </div>
              {!file && (
                <div>
                  <dt>not a link</dt>
                  <dd className={`mono ${parse.invalid && checked ? "is-failed" : "depth-none"}`}>
                    {checked ? parse.invalid : "--"}
                  </dd>
                </div>
              )}
            </dl>
          </div>

          {(error || parseError) && (
            <p className="error" role="alert">
              {error ?? parseError}
            </p>
          )}

          <div className="actions">
            <button className="primary" onClick={start} disabled={!ready}>
              Find photos
            </button>
            {!ready && <span className="hint">Paste some links or upload a file.</span>}
          </div>
        </>
      )}

      {findId && (
        <>
          <div className="find-head">
            <p className="find-progress mono">
              {ordered.length} of {total}
              {running ? " · looking" : " · done"}
              {counts.cached > 0 && ` · ${counts.cached} from an earlier look`}
            </p>
            <div className="find-actions">
              {running && (
                <button className="quiet" onClick={() => api.cancelFind(findId)}>
                  Stop
                </button>
              )}
              {!running && ordered.length > 0 && (
                <a className="button primary" href={api.findDownloadUrl(findId)} download>
                  Download image_links.csv
                </a>
              )}
              <button
                className="quiet"
                onClick={() => {
                  stream.current?.close();
                  setFindId(null);
                  setRows(new Map());
                }}
              >
                New search
              </button>
            </div>
          </div>

          <Chips filter={filter} counts={counts} onPick={setFilter} />
          <ResultTable rows={shown} />
        </>
      )}
    </div>
  );
}

/* The uploaded file, understood — and arguable. A silent guess about which
   column holds the links is a run over the wrong data that looks like it
   worked, so the choice is shown with the count that won it. */
function UploadSummary({
  name,
  table,
  onColumn,
  onClear,
}: {
  name: string;
  table: ParsedTable;
  onColumn: (column: string) => void;
  onClear: () => void;
}) {
  return (
    <div className="upload-summary">
      <p>
        <span className="mono">{name}</span> — {table.found} link
        {table.found === 1 ? "" : "s"} found{" "}
        <button className="linkish" onClick={onClear} type="button">
          remove
        </button>
      </p>
      <label>
        Links are in column{" "}
        <select value={table.url_column} onChange={(e) => onColumn(e.target.value)}>
          {table.columns.map((column) => (
            <option key={column} value={column}>
              {column}
            </option>
          ))}
        </select>{" "}
        <span className="hint">
          ({table.url_column_hits} of the rows had a link there)
        </span>
      </label>
      {table.columns.length > 1 && (
        <p className="hint">
          Your other columns come back with the results, so you can line this up against your
          own spreadsheet.
        </p>
      )}
      {table.unparsed.length > 0 && (
        <p className="hint is-failed">
          {table.unparsed.length} cell{table.unparsed.length === 1 ? "" : "s"} in that column
          were not links.
        </p>
      )}
    </div>
  );
}

function Chips({
  filter,
  counts,
  onPick,
}: {
  filter: Filter;
  counts: ReturnType<typeof tally>;
  onPick: (f: Filter) => void;
}) {
  const chips: [Filter, string, number][] = [
    ["all", "all", counts.all],
    ["photo", "has photo", counts.photo],
    ["lowres", "low res", counts.lowres],
    ["none", "no photo", counts.none],
    ["failed", "failed", counts.failed],
  ];
  return (
    <div className="chips" role="group" aria-label="Filter results">
      {chips.map(([key, label, count]) => (
        <button
          key={key}
          type="button"
          className={`chip${filter === key ? " chosen" : ""}`}
          aria-pressed={filter === key}
          onClick={() => onPick(key)}
        >
          {label} <span className="mono">{count}</span>
        </button>
      ))}
    </div>
  );
}

function ResultTable({ rows }: { rows: FindRow[] }) {
  if (rows.length === 0) return <p className="hint">Nothing matching that yet.</p>;

  return (
    <div className="table-scroll">
      <table className="find-table">
        <thead>
          <tr>
            <th>#</th>
            <th>Product</th>
            <th>Photos</th>
            <th>Image URLs</th>
            <th>Size</th>
            <th>Method</th>
            <th>Price</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.index} className={row.failed ? "is-failed-row" : ""}>
              <td className="mono num">{row.index + 1}</td>
              <td className="find-title" title={row.title_original || row.title}>
                {row.title || <span className="depth-none">—</span>}
                {Object.entries(row.extra).length > 0 && (
                  <span className="mono extra">
                    {Object.entries(row.extra)
                      .slice(0, 2)
                      .map(([k, v]) => `${k}: ${v}`)
                      .join(" · ")}
                  </span>
                )}
              </td>
              <td className="mono">{row.image_count || <span className="depth-none">0</span>}</td>
              <td className="mono find-url">
                {row.primary_image_url ? (
                  <CopyableUrl url={row.primary_image_url} all={row.image_urls} />
                ) : (
                  <span className="depth-none">—</span>
                )}
              </td>
              <td className="mono">
                {row.width ? `${row.width}×${row.height}` : <span className="depth-none">—</span>}
              </td>
              <td className={`mono ${toneOf(row)}`} title={row.explanation}>
                {row.method === "none" ? row.reason || "none" : row.method}
              </td>
              <td className="mono">
                {row.price ? `${row.price} ${row.currency}` : <span className="depth-none">—</span>}
              </td>
              <td>
                {!row.primary_image_url && (
                  <Link
                    className="why-no-photo"
                    to={`/diagnose?url=${encodeURIComponent(row.source_url)}`}
                  >
                    Why?
                  </Link>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const NEWLINE = String.fromCharCode(10);

function CopyableUrl({ url, all }: { url: string; all: string[] }) {
  const [copied, setCopied] = useState("");
  const copy = (text: string, which: string) => {
    navigator.clipboard?.writeText(text).then(() => {
      setCopied(which);
      window.setTimeout(() => setCopied(""), 1500);
    });
  };

  /*
    Every URL, listed. Not one URL and a button claiming there are nine more.

    This screen's whole promise is "every photo for every product", and hiding
    nine of ten behind `copy all 10` meant an operator could not see what they
    were about to paste, could not click one to check it, and could not take
    just the two they wanted. A count is not a list.
  */
  const photos = all.length ? all : [url];
  return (
    <span className="copyable">
      <ol className="every-url">
        {photos.map((each, i) => (
          <li key={each}>
            <a href={each} target="_blank" rel="noreferrer" className="url-text">
              {each}
            </a>
            <button className="linkish" type="button" onClick={() => copy(each, `one-${i}`)}>
              {copied === `one-${i}` ? "copied" : "copy"}
            </button>
          </li>
        ))}
      </ol>
      {photos.length > 1 && (
        <button
          className="linkish"
          type="button"
          onClick={() => copy(photos.join(NEWLINE), "all")}
        >
          {copied === "all" ? "copied" : `copy all ${photos.length}`}
        </button>
      )}
    </span>
  );
}

function matches(row: FindRow, filter: Filter): boolean {
  if (filter === "all") return true;
  if (filter === "failed") return row.failed;
  if (filter === "photo") return Boolean(row.primary_image_url);
  if (filter === "lowres") return row.method.endsWith("_low_res");
  return !row.primary_image_url && !row.failed;
}

function tally(rows: FindRow[]) {
  return {
    all: rows.length,
    photo: rows.filter((r) => r.primary_image_url).length,
    lowres: rows.filter((r) => r.method.endsWith("_low_res")).length,
    none: rows.filter((r) => !r.primary_image_url && !r.failed).length,
    failed: rows.filter((r) => r.failed).length,
    cached: rows.filter((r) => r.from_cache).length,
  };
}

function toneOf(row: FindRow): string {
  if (row.failed) return "is-failed";
  if (row.method.endsWith("_low_res")) return "is-review";
  if (row.method === "none") return "is-failed";
  return "depth-medium";
}
