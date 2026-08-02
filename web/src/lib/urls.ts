/*
  Client-side URL counting, for the live counter beside the textarea.

  Deliberately a *preview*, not a decision. The server's `plan_urls` is the one
  that counts, and its canonicalisation strips tracking parameters this cannot
  see the full list of. So this exists to give an operator feedback while they
  type -- "3 duplicates" the moment they paste -- and preflight is what they act
  on. Where the two disagree, the server is right and preflight will say so.
*/

export type LineStatus = "ok" | "duplicate" | "invalid" | "blank";

export interface CountedLine {
  line: number;
  raw: string;
  status: LineStatus;
}

export interface Counts {
  pasted: number;
  unique: number;
  duplicates: number;
  invalid: number;
}

// The same list haat_lister/utils/urls.py strips. Kept in step by hand, and
// only ever used for the preview -- see the note above.
const TRACKING = new Set([
  "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
  "gclid", "fbclid", "msclkid", "mc_cid", "mc_eid", "igshid", "_ga", "_gl",
  "ref", "referrer", "source", "spm", "srsltid",
]);

function canonical(raw: string): string | null {
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return null;
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") return null;
  if (!url.hostname) return null;

  for (const key of [...url.searchParams.keys()]) {
    if (TRACKING.has(key.toLowerCase())) url.searchParams.delete(key);
  }
  url.hash = "";
  url.hostname = url.hostname.toLowerCase();
  return url.toString().replace(/\/$/, "");
}

export function countLines(text: string): { lines: CountedLine[]; counts: Counts } {
  const seen = new Set<string>();
  const lines: CountedLine[] = [];
  let pasted = 0;
  let unique = 0;
  let duplicates = 0;
  let invalid = 0;

  text.split("\n").forEach((rawLine, index) => {
    const raw = rawLine.trim();
    if (!raw || raw.startsWith("#")) {
      lines.push({ line: index + 1, raw, status: "blank" });
      return;
    }
    pasted += 1;

    const key = canonical(raw);
    if (key === null) {
      invalid += 1;
      lines.push({ line: index + 1, raw, status: "invalid" });
      return;
    }
    if (seen.has(key)) {
      duplicates += 1;
      lines.push({ line: index + 1, raw, status: "duplicate" });
      return;
    }
    seen.add(key);
    unique += 1;
    lines.push({ line: index + 1, raw, status: "ok" });
  });

  return { lines, counts: { pasted, unique, duplicates, invalid } };
}

/** "4-6 min", "about 40 seconds". Ranges, because an estimate that pretends to
 *  precision teaches an operator to ignore estimates. */
export function humanRange(lowSeconds: number, highSeconds: number): string {
  if (highSeconds < 90) return `under 2 minutes`;
  const low = Math.max(1, Math.round(lowSeconds / 60));
  const high = Math.max(low + 1, Math.round(highSeconds / 60));
  return `${low}-${high} minutes`;
}
