/*
  The live parse under the textarea.

  It calls the server rather than counting in the browser, and that is the whole
  point of the hook. There is exactly one URL parser in this project -- the one
  the job itself runs -- and a second one written in TypeScript would drift
  within a week and show a count the run does not honour. The previous client
  parser split on newlines, so a comma-separated paste of twelve links counted
  as one malformed line.

  "Off the main thread" is satisfied literally: the parse happens in another
  process. What the browser does per keystroke is start a timer.

  Debounced, and every new keystroke aborts the request in flight, so a fast
  typist leaves one live request rather than thirty and cannot see an older
  answer land after a newer one.
*/

import { useEffect, useRef, useState } from "react";
import { api, ApiError, type Parse } from "../api/client";

const DEBOUNCE_MS = 180;

export const EMPTY_PARSE: Parse = {
  pasted: 0,
  unique: 0,
  duplicates: 0,
  invalid: 0,
  links: [],
  unparsed: [],
  domains: {},
  truncated: false,
  summary: "",
};

export function useParsedLinks(text: string): { parse: Parse; pending: boolean; error: string | null } {
  const [parse, setParse] = useState<Parse>(EMPTY_PARSE);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inFlight = useRef<AbortController | null>(null);

  useEffect(() => {
    if (text.trim() === "") {
      inFlight.current?.abort();
      setParse(EMPTY_PARSE);
      setPending(false);
      setError(null);
      return;
    }

    setPending(true);
    const timer = window.setTimeout(() => {
      inFlight.current?.abort();
      const controller = new AbortController();
      inFlight.current = controller;

      api
        .parse(text.split("\n"), controller.signal)
        .then((result) => {
          setParse(result);
          setError(null);
          setPending(false);
        })
        .catch((err: unknown) => {
          // Superseded by a later keystroke. Not a failure, and showing one
          // would make the box flash red while someone types.
          if (err instanceof DOMException && err.name === "AbortError") return;
          setError(err instanceof ApiError ? err.message : String(err));
          setPending(false);
        });
    }, DEBOUNCE_MS);

    return () => window.clearTimeout(timer);
  }, [text]);

  return { parse, pending, error };
}
