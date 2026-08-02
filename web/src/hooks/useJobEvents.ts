import { useEffect, useRef, useState } from "react";
import { withToken } from "../api/client";

/*
  The live feed. EventSource rather than a WebSocket: the traffic is one
  directional, the browser reconnects on its own, and a laptop that sleeps
  mid-job wakes up and carries on.

  The server resumes from Last-Event-ID rather than replaying, and EventSource
  sends that header itself -- so a dropped connection costs nothing and, more
  importantly, does not show a 200-row job as 400 rows. The one case it cannot
  handle is a gap longer than the server's buffer, and for that the server sends
  `resync`: refetch the job and start again. Both paths end in the same place,
  because GET /api/jobs/{id} is authoritative.
*/

export interface JobEvent {
  id: number;
  name: string;
  data: Record<string, any>;
}

export function useJobEvents(jobId: string, onResync: () => void) {
  const [events, setEvents] = useState<JobEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const [finished, setFinished] = useState(false);
  const resync = useRef(onResync);
  resync.current = onResync;

  useEffect(() => {
    setEvents([]);
    setFinished(false);
    const source = new EventSource(withToken(`/api/jobs/${jobId}/events`));

    const push = (name: string) => (raw: MessageEvent) => {
      const event: JobEvent = { id: Number(raw.lastEventId ?? 0), name, data: JSON.parse(raw.data) };
      // Keyed by the server's monotonic id, so a replayed frame is a no-op
      // rather than a second row on screen.
      setEvents((prev) => (prev.some((e) => e.id === event.id) ? prev : [...prev, event]));
      if (name === "job_done" || name === "job_error") {
        setFinished(true);
        source.close();
      }
    };

    for (const name of [
      "job_queued", "job_started", "job_progress", "job_cancelling",
      "row_started", "row_stage", "row_done", "row_failed", "job_done", "job_error",
    ]) {
      source.addEventListener(name, push(name));
    }
    source.addEventListener("resync", () => {
      source.close();
      resync.current();
    });

    source.onopen = () => setConnected(true);
    source.onerror = () => setConnected(false);

    return () => source.close();
  }, [jobId]);

  return { events, connected, finished };
}
