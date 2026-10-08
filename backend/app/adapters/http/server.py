"""HTTP adapter: routing table, request context and the request handler."""

from __future__ import annotations

import json
import logging
import mimetypes
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import parse_qs, unquote, urlparse

from ...application.services.tickets import TicketService
from ...domain.errors import DomainError, ValidationError
from ...domain.rules.engine import CompiledPolicy
from .problem import PROBLEM_CONTENT_TYPE, Problem

LOGGER = logging.getLogger("flowops.http")

MAX_BODY_BYTES = 256 * 1024


@dataclass(slots=True)
class Request:
    method: str
    path: str
    params: Mapping[str, str]
    query: Mapping[str, list[str]]
    body: Mapping[str, Any]
    headers: Mapping[str, str]
    request_id: str
    raw_body: bytes = b""

    def header(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name.lower(), default)

    def q(self, name: str, default: str | None = None) -> str | None:
        values = self.query.get(name)
        return values[0] if values else default

    def q_many(self, name: str) -> list[str]:
        out: list[str] = []
        for value in self.query.get(name, []):
            out.extend(item.strip() for item in value.split(",") if item.strip())
        return out

    def q_int(self, name: str, default: int) -> int:
        raw = self.q(name)
        if raw is None or raw == "":
            return default
        try:
            return int(raw)
        except ValueError as exc:
            raise ValidationError(f"query parameter {name!r} must be an integer") from exc

    def q_float(self, name: str, default: float) -> float:
        raw = self.q(name)
        if raw is None or raw == "":
            return default
        try:
            return float(raw)
        except ValueError as exc:
            raise ValidationError(f"query parameter {name!r} must be a number") from exc

    def q_bool(self, name: str, default: bool = False) -> bool:
        raw = self.q(name)
        if raw is None:
            return default
        return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(slots=True)
class Response:
    status: int = 200
    body: Any = None
    content_type: str = "application/json; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)

    def encoded(self) -> bytes:
        if self.body is None:
            return b""
        if isinstance(self.body, (bytes, bytearray)):
            return bytes(self.body)
        if isinstance(self.body, str):
            return self.body.encode("utf-8")
        return json.dumps(self.body, ensure_ascii=False, default=str).encode("utf-8")


Handler = Callable[[Request], Response]


@dataclass(slots=True)
class Route:
    method: str
    pattern: re.Pattern[str]
    handler: Handler
    name: str


class Router:
    def __init__(self) -> None:
        self.routes: list[Route] = []

    def add(self, method: str, pattern: str, handler: Handler, name: str = "") -> None:
        compiled = re.compile(f"^{pattern}$")
        self.routes.append(Route(method.upper(), compiled, handler, name or pattern))

    def match(self, method: str, path: str) -> tuple[Handler | None, dict[str, str], list[str]]:
        allowed: list[str] = []
        for route in self.routes:
            if not route.pattern.match(path):
                continue
            if route.method == method:
                found = route.pattern.match(path)
                return route.handler, (found.groupdict() if found else {}), allowed
            allowed.append(route.method)
        return None, {}, sorted(set(allowed))


class FlowOpsHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        service: TicketService,
        policy: CompiledPolicy,
        router: Router,
        *,
        web_root: Path | None = None,
        cors_origin: str = "*",
    ) -> None:
        self.service = service
        self.policy = policy
        self.router = router
        self.web_root = web_root
        self.cors_origin = cors_origin
        self.started_at = time.time()
        self.request_counter = 0
        self._counter_lock = threading.Lock()
        super().__init__(address, FlowOpsRequestHandler)

    def next_request_index(self) -> int:
        with self._counter_lock:
            self.request_counter += 1
            return self.request_counter


class FlowOpsRequestHandler(BaseHTTPRequestHandler):
    server_version = "FlowOps/1.0"
    protocol_version = "HTTP/1.1"

    # -- logging ---------------------------------------------------------
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        return None  # replaced by log_request

    def log_request_line(self, *, request_id: str, status: int, duration_ms: float, extra: Mapping[str, Any]) -> None:
        LOGGER.info(
            "%s",
            json.dumps(
                {
                    "event": "http_request",
                    "request_id": request_id,
                    "method": self.command,
                    "path": self.path.split("?")[0],
                    "status": status,
                    "duration_ms": round(duration_ms, 3),
                    "client": self.client_address[0] if self.client_address else None,
                    **extra,
                },
                ensure_ascii=False,
            ),
        )

    # -- verbs -----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch("PATCH")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._dispatch("OPTIONS")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("HEAD")

    # -- plumbing --------------------------------------------------------
    def _dispatch(self, method: str) -> None:
        started = time.perf_counter()
        server = self.server
        assert isinstance(server, FlowOpsHTTPServer)
        request_id = self.headers.get("X-Request-Id") or f"req_{uuid.uuid4().hex[:12]}"
        status = 500
        response: Response
        try:
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            handler, params, allowed = server.router.match(method, path)
            if handler is None and method == "OPTIONS":
                response = Response(status=204)
            elif handler is None:
                if allowed:
                    raise _MethodNotAllowed(method, allowed)
                response = self._static(path)
                if response is None:
                    raise _NotFound(path)
            else:
                body, raw = self._read_body()
                request = Request(
                    method=method,
                    path=path,
                    params=params,
                    query=parse_qs(parsed.query, keep_blank_values=True),
                    body=body,
                    headers={key.lower(): value for key, value in self.headers.items()},
                    request_id=request_id,
                    raw_body=raw,
                )
                response = handler(request)
        except DomainError as error:
            response = self._problem_response(Problem.from_domain(error, request_id=request_id))
        except _MethodNotAllowed as error:
            response = self._problem_response(
                Problem.method_not_allowed(error.method, error.allowed, request_id=request_id)
            )
        except _NotFound as error:
            response = self._problem_response(Problem.not_found(error.detail, request_id=request_id))
        except json.JSONDecodeError as error:
            response = self._problem_response(
                Problem.bad_request(f"request body is not valid JSON: {error.msg}", request_id=request_id)
            )
        except Exception as error:  # noqa: BLE001 - last line of defence
            LOGGER.exception("unhandled error request_id=%s", request_id)
            response = self._problem_response(
                Problem.internal(f"{type(error).__name__}: {error}", request_id=request_id)
            )

        status = response.status
        response.headers.setdefault("X-Request-Id", request_id)
        response.headers.setdefault("Access-Control-Allow-Origin", server.cors_origin)
        response.headers.setdefault("Access-Control-Allow-Headers", "Content-Type, If-Match, Idempotency-Key, X-Request-Id")
        response.headers.setdefault("Access-Control-Allow-Methods", "GET, POST, PATCH, OPTIONS")
        response.headers.setdefault("Access-Control-Expose-Headers", "ETag, X-Request-Id, X-Total-Count")

        payload = response.encoded()
        try:
            self.send_response(status)
            for key, value in response.headers.items():
                self.send_header(key, value)
            if status != 204:
                self.send_header("Content-Type", response.content_type)
                self.send_header("Content-Length", str(len(payload)))
            else:
                self.send_header("Content-Length", "0")
            self.end_headers()
            if method != "HEAD" and status != 204 and payload:
                self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover - client went away
            LOGGER.warning("client disconnected request_id=%s", request_id)

        duration_ms = (time.perf_counter() - started) * 1000.0
        self.log_request_line(
            request_id=request_id,
            status=status,
            duration_ms=duration_ms,
            extra={"index": server.next_request_index()},
        )

    def _read_body(self) -> tuple[Mapping[str, Any], bytes]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}, b""
        if length > MAX_BODY_BYTES:
            raise ValidationError(f"request body exceeds {MAX_BODY_BYTES // 1024} KiB")
        raw = self.rfile.read(length)
        if not raw.strip():
            return {}, raw
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValidationError("request body must be a JSON object")
        return parsed, raw

    def _problem_response(self, problem: Problem) -> Response:
        return Response(
            status=problem.status,
            body=problem.to_json(),
            content_type=PROBLEM_CONTENT_TYPE,
        )

    def _static(self, path: str) -> Response | None:
        server = self.server
        assert isinstance(server, FlowOpsHTTPServer)
        # The API namespace never falls through to the single-page app: an
        # unknown endpoint must produce a problem document, not the console's
        # HTML with a 200 status.
        if path.startswith("/api/"):
            return None
        root = server.web_root
        if root is None or not root.exists():
            return None
        relative = path.lstrip("/") or "index.html"
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            return None  # path traversal attempt: treat as not found
        if candidate.is_dir():
            candidate = candidate / "index.html"
        if not candidate.exists():
            if "." in Path(relative).name:
                return None
            candidate = root / "index.html"  # single-page-app fallback
            if not candidate.exists():
                return None
        content_type, _ = mimetypes.guess_type(str(candidate))
        payload = candidate.read_bytes()
        return Response(
            status=200,
            body=payload,
            content_type=f"{content_type or 'application/octet-stream'}"
            + ("; charset=utf-8" if (content_type or "").startswith("text/") or content_type == "application/javascript" else ""),
            headers={"Cache-Control": "no-cache"},
        )


class _NotFound(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class _MethodNotAllowed(Exception):
    def __init__(self, method: str, allowed: Sequence[str]) -> None:
        super().__init__(method)
        self.method = method
        self.allowed = list(allowed)


def serve(
    address: tuple[str, int],
    service: TicketService,
    policy: CompiledPolicy,
    router: Router,
    *,
    web_root: Path | None = None,
    cors_origin: str = "*",
) -> FlowOpsHTTPServer:
    return FlowOpsHTTPServer(address, service, policy, router, web_root=web_root, cors_origin=cors_origin)
