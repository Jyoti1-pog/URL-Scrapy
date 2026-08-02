"""Server-Sent Events, with a replay buffer so a reconnect loses nothing.

SSE rather than WebSockets because the traffic is one-directional, the browser
reconnects on its own, and a laptop that sleeps mid-job wakes up and carries on.
None of that is true of a raw WebSocket without writing the reconnection
yourself.

The property that costs the most to get right is the reconnect. `EventSource`
sends `Last-Event-ID` when it comes back, and a stream that ignored it would
replay from the beginning -- so a 200-row job with one dropped connection would
show 400 rows. Every event therefore carries a monotonic id, and a reconnect
gets only what it missed.

When the gap is bigger than the buffer -- a laptop asleep for an hour -- the
honest answer is not "here is a partial replay" but `resync`: refetch
`/api/jobs/{id}` and start again from now. The full state is always
reconstructible from the ledger, which is exactly why that is a cheap answer
rather than an apology.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from ..utils.logging import get_logger

log = get_logger(__name__)

# Enough for a 5,000-row job's terminal events plus progress. Beyond this a
# client is told to resync rather than handed a hole it cannot detect.
BUFFER = 8192

# Sent every 15s. Without it a proxy or a sleeping NIC can hold a silent
# connection open long past the point where it still works.
HEARTBEAT_S = 15.0


@dataclass
class Event:
    id: int
    name: str
    data: dict[str, Any]

    def encode(self) -> str:
        return f"id: {self.id}\nevent: {self.name}\ndata: {json.dumps(self.data)}\n\n"


@dataclass
class JobStream:
    """One job's event history and its live subscribers."""

    job_id: str
    buffer: deque[Event] = field(default_factory=lambda: deque(maxlen=BUFFER))
    subscribers: set[asyncio.Queue[Event]] = field(default_factory=set)
    next_id: int = 1
    closed: bool = False

    @property
    def floor(self) -> int:
        """The oldest id still replayable. Below this, a client must resync."""
        return self.buffer[0].id if self.buffer else self.next_id


class EventBroker:
    """Per-job pub/sub. Publishing never blocks on a slow reader.

    A subscriber that cannot keep up is dropped rather than allowed to stall the
    job: this is a progress feed, and a browser tab that fell behind can always
    refetch state. Losing a reader must never cost a row.
    """

    def __init__(self) -> None:
        self._streams: dict[str, JobStream] = {}

    def stream(self, job_id: str) -> JobStream:
        if job_id not in self._streams:
            self._streams[job_id] = JobStream(job_id)
        return self._streams[job_id]

    def publish(self, job_id: str, name: str, **data: Any) -> Event:
        stream = self.stream(job_id)
        event = Event(id=stream.next_id, name=name, data=data)
        stream.next_id += 1
        stream.buffer.append(event)

        for queue in list(stream.subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                log.debug("Dropping a subscriber to %s: it stopped reading", job_id)
                stream.subscribers.discard(queue)
        return event

    def close(self, job_id: str) -> None:
        self.stream(job_id).closed = True

    def forget(self, job_id: str) -> None:
        self._streams.pop(job_id, None)

    async def subscribe(
        self, job_id: str, last_event_id: int | None = None
    ) -> AsyncIterator[str]:
        """Yield encoded SSE frames: what was missed, then what happens next.

        The queue is created and registered *before* the replay is sent, so an
        event published mid-replay is buffered rather than lost in the gap
        between "caught up" and "listening".
        """
        stream = self.stream(job_id)
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=BUFFER)
        stream.subscribers.add(queue)

        try:
            replayed_through = 0
            if last_event_id is not None:
                if last_event_id < stream.floor - 1:
                    yield Event(
                        id=stream.next_id,
                        name="resync",
                        data={
                            "reason": "the gap is longer than the replay buffer",
                            "refetch": f"/api/jobs/{job_id}",
                        },
                    ).encode()
                else:
                    for event in list(stream.buffer):
                        if event.id > last_event_id:
                            yield event.encode()
                            replayed_through = event.id
            else:
                for event in list(stream.buffer):
                    yield event.encode()
                    replayed_through = event.id

            # A job that already finished has its terminal event in the buffer,
            # so the replay above just delivered it. Returning here rather than
            # falling into the wait loop is what keeps opening a completed job's
            # stream instant instead of a fifteen-second heartbeat wait -- and a
            # browser landing on a finished job does exactly that.
            #
            # The test is on what was actually SENT, not on what the buffer holds
            # now. The buffer is live: a job that finishes while the replay is
            # still yielding would put job_done in it, and returning on that
            # would cut the stream off mid-replay -- which is precisely what the
            # first version of this did.
            if replayed_through and any(
                event.name in ("job_done", "job_error")
                for event in stream.buffer
                if event.id <= replayed_through
            ):
                return

            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_S)
                except TimeoutError:
                    if stream.closed and queue.empty():
                        return
                    # A comment frame. Keeps the connection provably alive
                    # without inventing an event the client has to handle.
                    yield ": keep-alive\n\n"
                    continue

                # Skip anything the replay already covered: a publish that
                # landed in the queue while we were replaying it.
                if event.id <= replayed_through:
                    continue
                yield event.encode()
                if event.name in ("job_done", "job_error"):
                    return
        finally:
            stream.subscribers.discard(queue)


def parse_last_event_id(raw: str | None) -> int | None:
    """`EventSource` sends the header; a polling fallback sends the query param."""
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None
