"""Records operator visible events and answers queries over them.

Every event is a committed record, so the trace a query returns is the same
trace a restart would replay.
"""

from __future__ import annotations

from typing import Any, Mapping

from line_control.judgement.queries import RecordQuery
from line_control.runtime.clock import LogicalClock
from line_control.runtime.errors import ValidationError
from line_control.runtime.keys import scope_key, slug
from line_control.store.stream import RecordStream

TRACE_KIND = "trace"


class Recorder:
    """The event trail behind the console's record page."""

    def __init__(self, stream: RecordStream, clock: LogicalClock) -> None:
        self._stream = stream
        self._clock = clock

    def _commit(
        self, kind: str, unit: str, detail: str, extra: Mapping[str, Any]
    ) -> dict[str, Any]:
        if not kind:
            raise ValidationError("a trace event needs a kind")
        payload: dict[str, Any] = {"unit": unit, "detail": detail, "kind": kind}
        payload.update(extra)
        label = slug(unit) or "line"
        record = self._stream.append(TRACE_KIND, scope_key("trace", label), payload)
        # A trace event is itself the whole transaction: commit it immediately so
        # every recorded event, including a refused action, survives a restart.
        self._stream.commit_upto(record.seq)
        return {
            "seq": record.seq,
            "kind": kind,
            "unit": unit,
            "detail": detail,
            "tick": record.tick,
        }

    def record(self, kind: str, unit: str, detail: str, **extra: Any) -> dict[str, Any]:
        """Append one trace event and commit it."""
        return self._commit(kind, unit, detail, extra)

    def record_refusal(
        self,
        action: str,
        unit: str,
        code: str,
        message: str,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Commit a trace entry for an action the console refused.

        A rejected command still records what was attempted and why, so a
        post-incident review can separate limit violations from expired or
        duplicate credentials even though nothing changed state.
        """
        extra: dict[str, Any] = {
            "outcome": "rejected",
            "reason": code,
            "code": code,
        }
        if context:
            extra["context"] = dict(context)
        return self._commit(action or "command", unit, message, extra)

    # ------------------------------------------------------------ read paths
    def _visible(self, query: RecordQuery | None = None) -> list[dict[str, Any]]:
        """Return the committed trace records matching a filter, newest last."""
        records = self._stream.visible(TRACE_KIND)
        if query is None:
            return [record.to_dict() for record in records]
        # The stream kind is always TRACE_KIND; the event kind lives in the
        # payload, so honour the query's remaining filters and apply its limit.
        narrowed = RecordQuery(
            unit=query.unit,
            kind=TRACE_KIND,
            key_prefix=query.key_prefix,
            start_tick=query.start_tick,
            end_tick=query.end_tick,
            generation=query.generation,
            min_generation=query.min_generation,
            include_tombstones=query.include_tombstones,
            limit=query.limit,
        )
        return [record.to_dict() for record in narrowed.apply(records)]

    def query(self, query: RecordQuery | None = None) -> list[dict[str, Any]]:
        """Return the trace records that pass a filter."""
        return self._visible(query)

    def by_unit(self, unit: str) -> list[dict[str, Any]]:
        """Return every trace record of one unit."""
        return self._visible(RecordQuery(kind=TRACE_KIND, unit=unit))

    def by_unit_and_kind(self, unit: str, kind: str) -> list[dict[str, Any]]:
        """Return the trace records of one unit matching one event kind."""
        selected = self.by_unit(unit)
        return [record for record in selected if record["payload"].get("kind") == kind]

    def by_kind(self, kind: str) -> list[dict[str, Any]]:
        """Return every trace record of one event kind."""
        return [
            record
            for record in self._visible(RecordQuery(kind=TRACE_KIND))
            if record["payload"].get("kind") == kind
        ]

    def window(self, start_tick: int, end_tick: int) -> list[dict[str, Any]]:
        """Return the trace records inside a closed tick interval."""
        return self._visible(
            RecordQuery(kind=TRACE_KIND, start_tick=start_tick, end_tick=end_tick)
        )

    def trail(self, unit: str, limit: int = 20) -> list[dict[str, Any]]:
        """Return the newest trace records of one unit."""
        if limit <= 0:
            return []
        return self.by_unit(unit)[-limit:]

    def latest(self, kind: str) -> dict[str, Any] | None:
        """Return the newest trace record of one event kind."""
        rows = self.by_kind(kind)
        return rows[-1] if rows else None

    def count_for(self, unit: str, kind: str | None = None) -> int:
        """Return how many trace records a unit produced."""
        records = self.by_unit(unit)
        if kind is None:
            return len(records)
        return len([record for record in records if record["payload"].get("kind") == kind])

    def counts(self) -> dict[str, int]:
        """Return how many trace records each event kind produced."""
        counts: dict[str, int] = {}
        for record in self._visible(RecordQuery(kind=TRACE_KIND)):
            kind = str(record["payload"].get("kind", ""))
            counts[kind] = counts.get(kind, 0) + 1
        return counts

    def total(self) -> int:
        """Return the total number of trace records."""
        return len(self._visible(RecordQuery(kind=TRACE_KIND)))

    def units(self) -> list[str]:
        """Return the units that appear in the trace, sorted."""
        seen: set[str] = set()
        for record in self._visible(RecordQuery(kind=TRACE_KIND)):
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
            "latest": self._visible(RecordQuery(kind=TRACE_KIND, limit=1)),
        }

    def describe(self, record: Mapping[str, Any]) -> str:
        """Render one trace record as a single human readable line."""
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            payload = record
        kind = payload.get("kind", "trace")
        unit = payload.get("unit") or "line"
        detail = payload.get("detail", "")
        outcome = payload.get("outcome")
        reason = payload.get("reason")
        head = f"{kind} for unit {unit}" if unit != "line" else str(kind)
        if outcome == "rejected":
            code = payload.get("code", "control_error")
            return f"{head} rejected ({code}): {detail or reason or ''}".rstrip(": ")
        if detail:
            return f"{head}: {detail}"
        return head
