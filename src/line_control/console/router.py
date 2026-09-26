"""Transport independent request routing.

The router maps a method and a path onto a handler and turns a refusal into
the status that belongs to it, so the same entry point serves the real HTTP
server and an in-process test without either one special casing the other.

Every deliberate refusal is handed to an optional audit hook, so a turned down
command leaves the same trail as an accepted one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from line_control.runtime.errors import ControlError, ValidationError, http_status

Query = Mapping[str, list[str]]
Body = Mapping[str, Any]
Handler = Callable[[Query, Body], tuple[int, Any]]
RefusalHook = Callable[["RefusalEvent"], None]


@dataclass(frozen=True)
class RefusalEvent:
    """Everything the audit trail needs to reconstruct one turned down command."""

    method: str
    path: str
    code: str
    message: str
    status: int
    body: Mapping[str, Any] = field(default_factory=dict)
    context: Mapping[str, Any] = field(default_factory=dict)

    def unit(self) -> str:
        """Best effort name of the unit the refused command targeted."""
        candidate = self.context.get("unit") or self.body.get("unit")
        return "" if candidate is None else str(candidate)


class Router:
    """A method and path table over a set of handlers."""

    def __init__(self, on_refusal: RefusalHook | None = None) -> None:
        self._routes: dict[tuple[str, str], Handler] = {}
        self._on_refusal = on_refusal

    def add(self, method: str, path: str, handler: Handler) -> None:
        """Register one route."""
        key = (method.upper(), self._normalise(path))
        if key in self._routes:
            raise ValidationError(
                "route is already registered", method=key[0], path=key[1]
            )
        self._routes[key] = handler

    def routes(self) -> list[dict[str, str]]:
        """Return every registered route, sorted by path then method."""
        return [
            {"method": method, "path": path}
            for method, path in sorted(self._routes, key=lambda item: (item[1], item[0]))
        ]

    def handle(
        self,
        method: str,
        path: str,
        query: Query | None = None,
        body: Body | None = None,
    ) -> tuple[int, Any]:
        """Dispatch a request and never leak an exception to the transport."""
        handler = self._routes.get((method.upper(), self._normalise(path)))
        if handler is None:
            return 404, {
                "error": f"no route for {method.upper()} {path}",
                "code": "unknown_route",
            }
        try:
            return handler(query or {}, body or {})
        except ControlError as refusal:
            status = http_status(refusal)
            self._audit_refusal(method, path, body or {}, refusal, status)
            return status, refusal.to_dict()
        except (KeyError, TypeError, ValueError) as malformed:
            return 400, {"error": str(malformed), "code": "validation"}

    def _audit_refusal(
        self, method: str, path: str, body: Body, refusal: ControlError, status: int
    ) -> None:
        """Hand the refusal to the audit hook, never masking the original error."""
        if self._on_refusal is None:
            return
        event = RefusalEvent(
            method=method.upper(),
            path=self._normalise(path),
            code=refusal.code,
            message=refusal.message,
            status=status,
            body=body,
            context=refusal.context,
        )
        try:
            self._on_refusal(event)
        except Exception:  # pragma: no cover - auditing must not alter the verdict
            pass

    @staticmethod
    def _normalise(path: str) -> str:
        if not path:
            return "/"
        trimmed = path.split("?", 1)[0]
        if len(trimmed) > 1 and trimmed.endswith("/"):
            trimmed = trimmed.rstrip("/")
        return trimmed or "/"
