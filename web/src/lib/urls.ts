/*
  What is NOT in this file is the point.

  There used to be a `countLines` here: a client-side URL parser feeding the
  counter beside the textarea. It was a second implementation of something the
  server already did, and it did it differently -- it split on newlines, so a
  comma-separated paste of twelve links was counted as one malformed line and
  the operator was told so in confident red text.

  The fix was not a better client parser. It was deleting it. The counter is now
  served by `POST /api/jobs/parse`, which runs the same `plan_urls` the job runs,
  so the number on the screen is the number that will be honoured. See
  `hooks/useParsedLinks.ts`.
*/

/** "4-6 min", "about 40 seconds". Ranges, because an estimate that pretends to
 *  precision teaches an operator to ignore estimates. */
export function humanRange(lowSeconds: number, highSeconds: number): string {
  if (highSeconds < 90) return `under 2 minutes`;
  const low = Math.max(1, Math.round(lowSeconds / 60));
  const high = Math.max(low + 1, Math.round(highSeconds / 60));
  return `${low}-${high} minutes`;
}
