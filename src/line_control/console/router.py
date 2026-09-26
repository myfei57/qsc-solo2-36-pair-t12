"""Transport independent request routing.

The router maps a method and a path onto a handler and turns a refusal into
the status that belongs to it, so the same entry point serves the real HTTP
server and an in-process test without either one special casing the other.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from line_control.runtime.errors import ControlError, ValidationError, http_status

Query = Mapping[str, list[str]]
Body = Mapping[str, Any]
Handler = Callable[[Query, Body], tuple[int, Any]]
RefusalListener = Callable[[str, str, Query, Body, ControlError], None]


class Router:
    """A method and path table over a set of handlers."""

    def __init__(self, on_refusal: RefusalListener | None = None) -> None:
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
        active_query = query or {}
        active_body = body or {}
        normalised = self._normalise(path)
        handler = self._routes.get((method.upper(), normalised))
        if handler is None:
            return 404, {
                "error": f"no route for {method.upper()} {path}",
                "code": "unknown_route",
            }
        try:
            return handler(active_query, active_body)
        except ControlError as refusal:
            self._notify_refusal(method, normalised, active_query, active_body, refusal)
            return http_status(refusal), refusal.to_dict()
        except (KeyError, TypeError, ValueError) as malformed:
            return 400, {"error": str(malformed), "code": "validation"}

    def _notify_refusal(
        self,
        method: str,
        path: str,
        query: Query,
        body: Body,
        refusal: ControlError,
    ) -> None:
        """Hand a deliberate refusal to the trail listener without masking it."""
        if self._on_refusal is None:
            return
        try:
            self._on_refusal(method, path, query, body, refusal)
        except Exception:  # pragma: no cover - auditing must not change the verdict
            pass

    @staticmethod
    def _normalise(path: str) -> str:
        if not path:
            return "/"
        trimmed = path.split("?", 1)[0]
        if len(trimmed) > 1 and trimmed.endswith("/"):
            trimmed = trimmed.rstrip("/")
        return trimmed or "/"
