/*
  THE FILL GRID -- the one place boldness is spent.

  Nineteen columns wide, one per CSV column, N rows tall. Each cell is shaded by
  how deeply that field is dyed: full depth for a value read straight out of
  JSON-LD, mid-vat for one inferred, the undyed ground for one nobody could
  find. It is literally a picture of the CSV assembling itself, and it replaces
  a progress bar with something that says what KIND of progress is happening.

  Paired with a column profile, and that pairing is the point. The brief's grid
  is legible at fifty rows and meaningless at five hundred, where a row is
  sub-pixel and the promised click-a-cell is impossible. But the operationally
  useful fact -- "price_inr is a pale stripe, title is solid" -- is a COLUMN
  fact, and column facts are readable at any N. So:

      the profile answers "what will I have to fix?"     (always readable)
      the grid answers   "how far along, and where?"     (rows, while they fit)

  Past the row budget the grid becomes a minimap: same picture, no longer
  clickable, and it says so rather than pretending the rows are still targets.

  gi_region is hatched, not blank. A blank cell invites someone to fill it in;
  this one is a government certification and is not theirs to assert.
*/

import { useMemo, useState } from "react";
import type { JobRow } from "../api/client";

// Above this, one row is thinner than a comfortable click target, so the grid
// stops claiming to be one.
const CLICKABLE_ROWS = 150;

interface Props {
  columns: string[];
  rows: JobRow[];
  total: number;
  onPickRow?: (row: JobRow) => void;
}

export function FillGrid({ columns, rows, total, onPickRow }: Props) {
  const [hover, setHover] = useState<{ col: number; row?: JobRow } | null>(null);
  const interactive = rows.length <= CLICKABLE_ROWS;

  const profile = useMemo(() => columnFill(columns, rows, total), [columns, rows, total]);

  return (
    <div className="fillgrid">
      <div
        className="profile"
        role="img"
        aria-label={profileLabel(columns, profile)}
        onMouseLeave={() => setHover(null)}
      >
        {columns.map((name, index) => {
          const share = profile[index];
          const locked = name === "gi_region";
          return (
            <span
              key={name}
              className={`profile-col${locked ? " locked" : ""}`}
              onMouseEnter={() => setHover({ col: index })}
            >
              <span
                className="profile-fill"
                style={{ height: locked ? "0%" : `${Math.round(share * 100)}%` }}
              />
            </span>
          );
        })}
      </div>

      <div className="profile-legend mono" aria-hidden>
        {hover ? (
          <span>
            {columns[hover.col]}{" "}
            {columns[hover.col] === "gi_region" ? (
              <span className="depth-low">locked</span>
            ) : (
              <span className="depth-low">{Math.round(profile[hover.col] * 100)}%</span>
            )}
          </span>
        ) : (
          <span className="depth-low">19 columns</span>
        )}
      </div>

      <div
        className={`grid-rows${interactive ? " interactive" : " minimap"}`}
        role="img"
        aria-label={`${rows.filter((r) => r.cells).length} of ${total} rows filled`}
      >
        {rows.map((row) => (
          <span key={row.input_index} className="grid-row">
            {columns.map((name, col) => (
              <Cell
                key={name}
                depth={row.cells ? row.cells[col] : " "}
                interactive={interactive}
                title={interactive ? `row ${row.input_index + 1} · ${name}` : undefined}
                onClick={interactive && onPickRow ? () => onPickRow(row) : undefined}
              />
            ))}
          </span>
        ))}
      </div>

      {!interactive && (
        <p className="grid-note depth-low">
          {rows.length} rows — too many to click. Use the list below.
        </p>
      )}
    </div>
  );
}

function Cell({
  depth,
  interactive,
  title,
  onClick,
}: {
  depth: string;
  interactive: boolean;
  title?: string;
  onClick?: () => void;
}) {
  // Colour never alone: depth is also opacity, and the two absences -- pending
  // and locked -- differ in kind, not only in shade.
  const kind =
    depth === "-" ? "locked" : depth === " " ? "pending" : depth === "0" ? "empty" : `d${depth}`;
  return (
    <span
      className={`cell cell-${kind}${interactive ? " clickable" : ""}`}
      title={title}
      onClick={onClick}
    />
  );
}

/** Share of *processed* rows that filled each column. Denominator is rows that
 *  have produced something -- measuring against rows not yet fetched would show
 *  every column as empty for the first minute and teach nobody anything. */
function columnFill(columns: string[], rows: JobRow[], total: number): number[] {
  const filled = rows.filter((r) => r.cells);
  const denominator = filled.length || total || 1;
  return columns.map((_, index) => {
    if (!filled.length) return 0;
    const n = filled.filter((r) => r.cells[index] !== "0" && r.cells[index] !== "-").length;
    return n / denominator;
  });
}

function profileLabel(columns: string[], profile: number[]): string {
  const thin = columns
    .map((name, i) => [name, profile[i]] as const)
    .filter(([name, share]) => name !== "gi_region" && share < 0.5)
    .map(([name]) => name);
  if (!thin.length) return "every column is filling";
  return `columns still mostly empty: ${thin.join(", ")}`;
}
