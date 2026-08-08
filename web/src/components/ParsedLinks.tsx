/*
  What we made of the paste, shown before ten minutes get spent on it.

  Two disclosures, both closed by default and both driven by the same server
  parse. `<details>` rather than a hand-rolled toggle: it is keyboard-operable,
  announced, and findable by browser search with no work from us.

  The list shows the CANONICAL form -- what will actually be fetched and what
  dedupe keyed on -- with the pasted original underneath when the two differ.
  Showing only the canonical would be honest and useless: after tracking
  parameters come off, an operator cannot recognise their own link.
*/

import type { InvalidUrl, ParsedLink } from "../api/client";

export function ParsedLinks({
  links,
  truncated,
  total,
}: {
  links: ParsedLink[];
  truncated: boolean;
  total: number;
}) {
  if (links.length === 0) return null;

  return (
    <details className="parsed">
      <summary>
        Show the {total} link{total === 1 ? "" : "s"} I found
      </summary>
      <ol className="parsed-list">
        {links.map((link, index) => (
          <li key={`${link.line}-${index}`} className={link.status === "duplicate" ? "dupe" : ""}>
            <span className="mono lineno" aria-label={`line ${link.line}`}>
              {link.line}
            </span>
            <span className="parsed-url">
              <span className="mono canonical">{link.canonical}</span>
              {link.original !== link.canonical && (
                <span className="mono original" title={link.original}>
                  pasted as {link.original}
                </span>
              )}
            </span>
            <span className="parsed-marks">
              {link.assumed_scheme && (
                <span className="mark assumed" title="No scheme was given, so https was assumed.">
                  assumed https
                </span>
              )}
              {link.status === "duplicate" && <span className="mark dupe-mark">{link.note}</span>}
            </span>
          </li>
        ))}
      </ol>
      {truncated && (
        <p className="more">
          Showing the first {links.length}. All {total} will run.
        </p>
      )}
    </details>
  );
}

export function UnparsedFragments({
  fragments,
  count,
  open,
  onToggle,
}: {
  fragments: InvalidUrl[];
  count: number;
  open: boolean;
  onToggle: (open: boolean) => void;
}) {
  if (count === 0) return null;

  return (
    // Controlled, so the "not a link" figure in the counter can open it. That
    // number is the one thing on this screen someone will click at instinctively.
    <details
      className="parsed unparsed"
      id="unparsed"
      open={open}
      onToggle={(e) => onToggle((e.currentTarget as HTMLDetailsElement).open)}
    >
      <summary>
        {count} thing{count === 1 ? "" : "s"} I could not read as a link
      </summary>
      <p className="why">
        Kept exactly as written. Nothing here will run — fix it above, or leave it if it was
        never meant to be a link.
      </p>
      <ul className="parsed-list">
        {fragments.map((fragment, index) => (
          <li key={`${fragment.line}-${index}`}>
            <span className="mono lineno" aria-label={`line ${fragment.line}`}>
              {fragment.line}
            </span>
            <span className="mono raw is-failed">{fragment.raw}</span>
          </li>
        ))}
      </ul>
      {fragments.length < count && (
        <p className="more">Showing the first {fragments.length} of {count}.</p>
      )}
    </details>
  );
}
