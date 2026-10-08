"""Problem+JSON responses.

Errors are part of the public contract, so they are typed.  Every failure carries
a stable ``type`` URI, a human ``title``/``detail``, the ``request_id`` needed to
find it in the logs, and — where the client can act on it — machine-readable
extras such as ``allowed`` transitions.

Spec: RFC 9457 (which obsoletes RFC 7807).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from ...domain.errors import DomainError

PROBLEM_CONTENT_TYPE = "application/problem+json"


@dataclass(frozen=True, slots=True)
class Problem:
    status: int
    title: str
    detail: str
    type: str = "about:blank"
    request_id: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def to_json(self) -> bytes:
        payload: dict[str, Any] = {
            "type": self.type,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
        }
        if self.request_id:
            payload["request_id"] = self.request_id
        for key, value in self.extra.items():
            if key not in payload:
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")

    @classmethod
    def from_domain(cls, error: DomainError, *, request_id: str | None = None) -> "Problem":
        return cls(
            status=error.status,
            title=error.title,
            detail=error.detail,
            type=error.kind,
            request_id=request_id,
            extra=dict(error.extra),
        )

    @classmethod
    def not_found(cls, detail: str, *, request_id: str | None = None) -> "Problem":
        return cls(
            status=404,
            title="Resource not found",
            detail=detail,
            type="https://flowops.dev/problems/not-found",
            request_id=request_id,
        )

    @classmethod
    def bad_request(cls, detail: str, *, request_id: str | None = None) -> "Problem":
        return cls(
            status=400,
            title="Bad request",
            detail=detail,
            type="https://flowops.dev/problems/bad-request",
            request_id=request_id,
        )

    @classmethod
    def method_not_allowed(cls, method: str, allowed: list[str], *, request_id: str | None = None) -> "Problem":
        return cls(
            status=405,
            title="Method not allowed",
            detail=f"{method} is not supported for this resource",
            type="https://flowops.dev/problems/method-not-allowed",
            request_id=request_id,
            extra={"allowed": allowed},
        )

    @classmethod
    def internal(cls, detail: str, *, request_id: str | None = None) -> "Problem":
        return cls(
            status=500,
            title="Internal server error",
            detail=detail,
            type="https://flowops.dev/problems/internal",
            request_id=request_id,
        )
