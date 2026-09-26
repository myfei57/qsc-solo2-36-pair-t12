"""Records operator visible events and answers queries over them.

Every event is a committed record, so the trace a query returns is the same
trace a restart would replay.  A refused command is recorded too: the trail
stores what was attempted, why it was refused, and whether it was accepted,
so a later review can reconstruct both outcomes.
"""

from __future__ import annotations

from typing import Any, Mapping

from line_control.judgement.queries import RecordQuery
from line_control.runtime.clock import LogicalClock
from line_control.runtime.errors import ValidationError
from line_control.runtime.keys import scope_key, slug
from line_control.store.stream import RecordStream

TRACE_KIND = "trace"
OUTCOME_ACCEPTED = "accepted"
OUTCOME_REFUSED = "refused"
REFUSAL_KIND = "refusal"


class Recorder:
    """The event trail behind the console's record page."""

    def __init__(self, stream: RecordStream, clock: LogicalClock) -> None:
        self._stream = stream
        self._clock = clock

    def record(self, kind: str, unit: str, detail: str, **extra: Any) -> dict[str, Any]:
        """Append one accepted trace event and commit it."""
        if not kind:
            raise ValidationError("a trace event needs a kind")
        payload: dict[str, Any] = {
            "unit": unit,
            "detail": detail,
            "kind": kind,
            "outcome": OUTCOME_ACCEPTED,
        }
        payload.update(extra)
        return self._commit(payload)

    def record_refusal(
        self,
        *,
        action: str,
        unit: str,
        reason: str,
        detail: str,
        status: int,
        method: str = "POST",
        path: str = "",
        **extra: Any,
    ) -> dict[str, Any]:
        """Commit a trace for a command the process turned down.

        A refusal usually leaves the attempted write sitting above the
        watermark.  Such a half applied step must never become visible, so the
        pending tail is rolled back before the refusal is committed.
        """
        self._stream.discard_uncommitted()
        payload: dict[str, Any] = {
            "unit": unit,
            "detail": detail,
            "kind": REFUSAL_KIND,
            "outcome": OUTCOME_REFUSED,
            "action": action,
            "reason": reason,
            "status": int(status),
            "method": method,
            "path": path,
        }
        payload.update(extra)
        return self._commit(payload)

    def _commit(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        label = slug(str(payload.get("unit", ""))) or "line"
        record = self._stream.append(
            TRACE_KIND, scope_key("trace", label), dict(payload)
        )
        self._stream.commit_upto(record.seq)
        return {
            "seq": record.seq,
            "kind": payload.get("kind", ""),
            "unit": payload.get("unit", ""),
            "detail": payload.get("detail", ""),
            "tick": record.tick,
        }

    # ------------------------------------------------------------ read paths
    def query(self, query: RecordQuery | None = None) -> list[dict[str, Any]]:
        """Return the trace records that pass a filter."""
        active = query if query is not None else RecordQuery(kind=TRACE_KIND)
        return [record.to_dict() for record in active.apply(self._stream.visible(TRACE_KIND))]

    def by_unit(self, unit: str) -> list[dict[str, Any]]:
        """Return every trace record of one unit."""
        return self.query(RecordQuery(kind=TRACE_KIND, unit=unit))

    def by_unit_and_kind(self, unit: str, kind: str) -> list[dict[str, Any]]:
        """Return the trace records of one unit matching one event kind."""
        return [
            record
            for record in self.by_unit(unit)
            if record["payload"].get("kind") == kind
        ]

    def by_kind(self, kind: str) -> list[dict[str, Any]]:
        """Return every trace record of one event kind."""
        return [
            record
            for record in self.query(RecordQuery(kind=TRACE_KIND))
            if record["payload"].get("kind") == kind
        ]

    def window(self, start_tick: int, end_tick: int) -> list[dict[str, Any]]:
        """Return the trace records inside a closed tick interval."""
        return self.query(
            RecordQuery(kind=TRACE_KIND, start_tick=start_tick, end_tick=end_tick)
        )

    def trail(self, unit: str, limit: int = 20) -> list[dict[str, Any]]:
        """Return the newest trace records of one unit."""
        return self.query(RecordQuery(kind=TRACE_KIND, unit=unit, limit=limit))

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return the newest trace records across every unit."""
        return self.query(RecordQuery(kind=TRACE_KIND, limit=limit))

    def latest(self, kind: str) -> dict[str, Any] | None:
        """Return the newest trace record of one event kind."""
        records = self.by_kind(kind)
        return records[-1] if records else None

    def count_for(self, unit: str, kind: str | None = None) -> int:
        """Return how many trace records a unit produced."""
        records = self.by_unit(unit)
        if kind is None:
            return len(records)
        return len([record for record in records if record["payload"].get("kind") == kind])

    def counts(self) -> dict[str, int]:
        """Return how many trace records each event kind produced."""
        counts: dict[str, int] = {}
        for record in self.query(RecordQuery(kind=TRACE_KIND)):
            kind = str(record["payload"].get("kind", ""))
            counts[kind] = counts.get(kind, 0) + 1
        return counts

    def total(self) -> int:
        """Return the total number of trace records."""
        return len(self.query(RecordQuery(kind=TRACE_KIND)))

    def units(self) -> list[str]:
        """Return the units that appear in the trace, sorted."""
        seen: set[str] = set()
        for record in self.query(RecordQuery(kind=TRACE_KIND)):
            unit = str(record["payload"].get("unit", ""))
            if unit:
                seen.add(unit)
        return sorted(seen)

    def summary(self) -> dict[str, Any]:
        """Return a compact view of the trail for the console."""
        return {
            "total": self.total(),
            "kinds": self.counts(),
            "units": self.units(),
            "perUnit": {unit: self.count_for(unit) for unit in self.units()},
            "latest": self.query(RecordQuery(kind=TRACE_KIND, limit=1)),
        }

    def describe(self, record: Mapping[str, Any]) -> str:
        """Render one trace record as a single human readable line."""
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            # Accept the compact shape ``record`` returns as well.
            payload = record
        seq = record.get("seq", "")
        tick = record.get("tick", payload.get("tick", ""))
        kind = payload.get("kind", "")
        outcome = payload.get("outcome", OUTCOME_ACCEPTED)
        unit = payload.get("unit", "") or "line"
        detail = payload.get("detail", "")
        line = f"#{seq} tick={tick} {outcome} {kind} unit={unit}: {detail}"
        if outcome == OUTCOME_REFUSED and payload.get("reason"):
            line += f" [reason={payload['reason']}]"
        return line
