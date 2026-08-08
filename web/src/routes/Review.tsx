/*
  The review table. The screen that has to beat Excel.

  Dense on purpose: an operator clearing thirty-eight blank prices wants the
  next one on screen, not a card with generous padding. Keyboard end to end --
  arrows move, Enter edits, Escape cancels, Enter commits and drops to the same
  column one row down, because filling a column is what actually happens here.

  Two things the grid says that a spreadsheet cannot:

    how much to trust a cell -- depth of dye is confidence, so a value read from
    JSON-LD looks different from one guessed off a heading;

    what you changed -- an edited cell sits on resist-white and remembers what
    the page said, because the extraction is stored beside the edit rather than
    under it.

  gi_region is hatched and refuses focus. Not empty: locked.
*/

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api, ApiError, type Cell, type RowTable } from "../api/client";
import { useConfig } from "../hooks/useConfig";
import { rowsNeed } from "../lib/plural";

// Wide columns first: an operator scanning for what to fix reads the title.
const PRIORITY = [
  "title",
  "category_slug",
  "subcategory_slug",
  "price_inr",
  "hs_code",
  "availability",
  "weight_g",
  "length_cm",
  "width_cm",
  "height_cm",
  "sizes",
  "stock_qty",
  "gi_region",
];

export function Review() {
  const { jobId = "" } = useParams();
  const queries = useQueryClient();
  const { data: config } = useConfig();

  const [flaggedOnly, setFlaggedOnly] = useState(true);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [cursor, setCursor] = useState<{ row: number; col: number }>({ row: 0, col: 0 });
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const gridRef = useRef<HTMLTableSectionElement>(null);

  const { data: page } = useQuery({
    queryKey: ["rows", jobId, flaggedOnly],
    queryFn: () => api.rows(jobId, { flaggedOnly, limit: 500 }),
  });

  const invalidate = useCallback(() => {
    queries.invalidateQueries({ queryKey: ["rows", jobId] });
    queries.invalidateQueries({ queryKey: ["job", jobId] });
  }, [queries, jobId]);

  const save = useMutation({
    mutationFn: (v: { rowKey: string; field: string; value: string }) =>
      api.editRow(jobId, v.rowKey, { [v.field]: v.value }),
    onSuccess: () => {
      setError(null);
      invalidate();
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : String(err)),
  });

  const bulk = useMutation({
    mutationFn: (v: { keys: string[]; field: string; value: string }) =>
      api.editRows(jobId, v.keys, { [v.field]: v.value }),
    onSuccess: (result) => {
      const rejected = result.rejected.length;
      setError(rejected ? `${rejected} row(s) refused: ${result.rejected[0].reason}` : null);
      invalidate();
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : String(err)),
  });

  const exporting = useMutation({
    mutationFn: () => api.export(jobId),
    onSuccess: () => invalidate(),
  });

  const columns = useMemo(() => {
    const known = page?.columns ?? [];
    return PRIORITY.filter((c) => known.includes(c));
  }, [page?.columns]);

  const rows = page?.rows ?? [];
  const cellAt = (row: RowTable, field: string): Cell | undefined =>
    row.cells.find((c) => c.field === field);

  const commit = useCallback(
    (value: string) => {
      const row = rows[cursor.row];
      const field = columns[cursor.col];
      if (!row || !field) return;
      setEditing(false);
      const current = cellAt(row, field)?.value ?? "";
      if (value !== current) save.mutate({ rowKey: row.row_key, field, value });
    },
    [rows, columns, cursor, save],
  );

  // Keyboard, per the brief: arrows move, enter edits, escape cancels. Enter
  // while editing commits and drops one row in the same column, because filling
  // a column is the actual task.
  const onKeyDown = useCallback(
    (event: React.KeyboardEvent) => {
      const row = rows[cursor.row];
      const field = columns[cursor.col];
      const cell = row && field ? cellAt(row, field) : undefined;

      if (editing) {
        if (event.key === "Escape") {
          event.preventDefault();
          setEditing(false);
        } else if (event.key === "Enter") {
          event.preventDefault();
          commit(draft);
          setCursor((c) => ({ ...c, row: Math.min(rows.length - 1, c.row + 1) }));
        }
        return;
      }

      const move = (dr: number, dc: number) => {
        event.preventDefault();
        setCursor((c) => ({
          row: Math.max(0, Math.min(rows.length - 1, c.row + dr)),
          col: Math.max(0, Math.min(columns.length - 1, c.col + dc)),
        }));
      };

      switch (event.key) {
        case "ArrowDown":
          return move(1, 0);
        case "ArrowUp":
          return move(-1, 0);
        case "ArrowRight":
        case "Tab":
          if (event.shiftKey) return move(0, -1);
          return move(0, 1);
        case "ArrowLeft":
          return move(0, -1);
        case "Enter":
          if (cell?.editable) {
            event.preventDefault();
            setDraft(cell.value);
            setEditing(true);
          }
          return;
        case " ":
          if (row) {
            event.preventDefault();
            setSelected((s) => {
              const next = new Set(s);
              next.has(row.row_key) ? next.delete(row.row_key) : next.add(row.row_key);
              return next;
            });
          }
          return;
        default:
          return;
      }
    },
    [rows, columns, cursor, editing, draft, commit],
  );

  useEffect(() => {
    if (!editing) return;
    // Select the whole value, the way a spreadsheet does. Focus alone leaves
    // the caret after the existing text, so typing appends -- the audit run
    // typed 1999 then 2099 into one cell and got "19992099". Arrowing or
    // clicking inside still puts the caret where you meant it.
    inputRef.current?.focus();
    inputRef.current?.select();
  }, [editing]);

  // Focus follows the cursor. Without this the cursor moves and the keyboard
  // does not: the first edit works, and every keystroke after it goes to
  // whatever the browser still thinks is focused. Caught filling a column of
  // thirty-eight prices, where exactly one of them landed.
  useEffect(() => {
    if (editing) return;
    const cell = gridRef.current?.querySelector<HTMLElement>(
      `[data-cell="${cursor.row}-${cursor.col}"]`,
    );
    cell?.focus({ preventScroll: false });
  }, [cursor, editing]);

  if (!page) {
    return (
      <div className="bench">
        <p className="lede">Looking…</p>
      </div>
    );
  }

  const cursorField = columns[cursor.col];

  return (
    <div className="bench review">
      <div className="review-head">
        <div>
          <h1 className="screen-title">
            {flaggedOnly
              ? `${rowsNeed(page.total)} a human`
              : `${page.total} row${page.total === 1 ? "" : "s"}`}
          </h1>
          <p className="lede">
            <Link to={`/jobs/${jobId}`}>back to {jobId}</Link> · arrows move · enter edits ·
            space selects · escape cancels
          </p>
        </div>
        <button
          className="primary"
          onClick={() => exporting.mutate()}
          disabled={exporting.isPending}
        >
          {exporting.isPending
            ? "Re-exporting"
            : page.pending_edits > 0
              ? `Re-export with ${page.pending_edits} edit${page.pending_edits === 1 ? "" : "s"}`
              : "Re-export"}
        </button>
      </div>

      <div className="review-controls">
        <label className="check">
          <input
            type="checkbox"
            checked={flaggedOnly}
            onChange={(e) => setFlaggedOnly(e.target.checked)}
          />
          only rows that need a human
        </label>

        {selected.size > 0 && cursorField && (
          <BulkBar
            count={selected.size}
            field={cursorField}
            config={config}
            onApply={(value) =>
              bulk.mutate({ keys: [...selected], field: cursorField, value })
            }
            onClear={() => setSelected(new Set())}
          />
        )}
      </div>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {exporting.isSuccess && !exporting.isPending && (
        <p className="exported">
          Re-exported {exporting.data.rows} rows with {exporting.data.edits_applied} edit
          {exporting.data.edits_applied === 1 ? "" : "s"}.{" "}
          <a href={`/api/jobs/${jobId}/download/listings`} download>
            Download listings.csv
          </a>
        </p>
      )}

      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions */}
      <table className="review-grid" onKeyDown={onKeyDown}>
        <thead>
          <tr>
            <th className="pick" />
            <th className="num">#</th>
            {columns.map((name) => (
              <th key={name} className={`col-${name}`}>
                {name}
              </th>
            ))}
          </tr>
        </thead>
        <tbody ref={gridRef}>
          {rows.map((row, rowIndex) => (
            <tr key={row.row_key} className={selected.has(row.row_key) ? "picked" : ""}>
              <td className="pick">
                <input
                  type="checkbox"
                  aria-label={`select row ${row.input_index + 1}`}
                  checked={selected.has(row.row_key)}
                  onChange={(e) => {
                    const next = new Set(selected);
                    e.target.checked ? next.add(row.row_key) : next.delete(row.row_key);
                    setSelected(next);
                  }}
                />
              </td>
              <td className="num mono depth-low">{row.input_index + 1}</td>
              {columns.map((name, colIndex) => {
                const cell = cellAt(row, name);
                const here = cursor.row === rowIndex && cursor.col === colIndex;
                return (
                  <CellTd
                    key={name}
                    coords={`${rowIndex}-${colIndex}`}
                    cell={cell}
                    here={here}
                    editing={here && editing}
                    draft={draft}
                    inputRef={here && editing ? inputRef : undefined}
                    onDraft={setDraft}
                    onCommit={commit}
                    onFocus={() => setCursor({ row: rowIndex, col: colIndex })}
                    onStartEdit={() => {
                      if (!cell?.editable) return;
                      setCursor({ row: rowIndex, col: colIndex });
                      setDraft(cell.value);
                      setEditing(true);
                    }}
                  />
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>

      {rows.length === 0 && (
        <p className="lede">
          Nothing needs a human. <Link to={`/jobs/${jobId}`}>Download listings.csv</Link>.
        </p>
      )}
    </div>
  );
}

function CellTd({
  coords,
  cell,
  here,
  editing,
  draft,
  inputRef,
  onDraft,
  onCommit,
  onFocus,
  onStartEdit,
}: {
  coords: string;
  cell: Cell | undefined;
  here: boolean;
  editing: boolean;
  draft: string;
  inputRef?: React.RefObject<HTMLInputElement>;
  onDraft: (v: string) => void;
  onCommit: (v: string) => void;
  onFocus: () => void;
  onStartEdit: () => void;
}) {
  if (!cell) return <td />;

  if (!cell.editable) {
    // Locked, not merely empty, and it refuses focus so the keyboard skips it
    // rather than stopping on a cell that can never be filled.
    return (
      <td className="cell-td locked" data-cell={coords} title={cell.locked_reason ?? undefined}>
        <span aria-label="locked">▨</span>
      </td>
    );
  }

  const tone =
    cell.edited
      ? "is-edited"
      : cell.confidence === "high"
        ? "depth-high"
        : cell.confidence === "medium"
          ? "depth-medium"
          : cell.confidence === "low"
            ? "depth-low"
            : "depth-none";

  return (
    <td
      className={`cell-td ${tone}${here ? " here" : ""}${cell.edited ? " edited" : ""}`}
      data-cell={coords}
      tabIndex={here ? 0 : -1}
      onFocus={onFocus}
      onClick={onFocus}
      onDoubleClick={onStartEdit}
      title={
        cell.edited
          ? `was: ${cell.original || "(blank)"}`
          : (cell.note ?? `${cell.confidence} · ${cell.source || "not found"}`)
      }
    >
      {editing ? (
        <input
          ref={inputRef}
          className="cell-input mono"
          value={draft}
          onChange={(e) => onDraft(e.target.value)}
          onBlur={() => onCommit(draft)}
        />
      ) : (
        <span className="cell-value">
          {cell.value || <span className="blank">—</span>}
          {cell.edited && <span className="edited-mark">✎</span>}
        </span>
      )}
    </td>
  );
}

function BulkBar({
  count,
  field,
  config,
  onApply,
  onClear,
}: {
  count: number;
  field: string;
  config: ReturnType<typeof useConfig>["data"];
  onApply: (value: string) => void;
  onClear: () => void;
}) {
  const [value, setValue] = useState("");
  const options =
    field === "category_slug"
      ? (config?.categories.map((c) => c.slug) ?? [])
      : field === "availability"
        ? (config?.enums.availability ?? [])
        : null;

  return (
    <div className="bulkbar">
      <span>
        {count} selected · set <code className="mono">{field}</code> to
      </span>
      {options ? (
        <select value={value} onChange={(e) => setValue(e.target.value)}>
          <option value="">(blank)</option>
          {options.map((o) => (
            <option key={o} value={o}>
              {o}
            </option>
          ))}
        </select>
      ) : (
        <input
          type="text"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="value"
        />
      )}
      <button className="quiet small" onClick={() => onApply(value)}>
        Apply to {count}
      </button>
      <button className="quiet small" onClick={onClear}>
        Clear
      </button>
    </div>
  );
}
