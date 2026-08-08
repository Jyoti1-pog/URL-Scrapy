/*
  Count-aware copy.

  "1 row need a human" was on the results banner for the whole of v2. The bug is
  small and the cause is not: the rule lived in three components, so two of them
  were right and one was wrong. It lives here now.
*/

/** "1 row needs" / "38 rows need" */
export function rowsNeed(count: number): string {
  return `${count} row${count === 1 ? "" : "s"} ${count === 1 ? "needs" : "need"}`;
}

/** "needs" / "need", when the subject is already written. */
export function need(count: number): string {
  return count === 1 ? "needs" : "need";
}
