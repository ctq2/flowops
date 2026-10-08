"""Run the end-to-end smoke test against a server this script starts itself.

``make ci`` needs one command that proves the whole thing works: boot a real
server on a real port, wait for readiness, exercise it over HTTP, and tear it
down again — including on failure, so a broken build never leaves a stray process
holding the port.
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.adapters.http.api import build_router  # noqa: E402
from app.adapters.http.server import serve  # noqa: E402
from app.application.models import TicketQuery  # noqa: E402
from app.application.services.tickets import TicketService  # noqa: E402
from app.bootstrap import build_store, load_policy  # noqa: E402
from app.config.settings import REPO_ROOT, Settings  # noqa: E402
from app.domain.values import Status  # noqa: E402
from app.seed import close_moment, seed_payloads, transition_path  # noqa: E402

from smoke import main as smoke_main  # noqa: E402


def free_port(preferred: int) -> int:
    """Use ``preferred`` when it is free, otherwise let the OS pick one."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]


def seed_service(service: TicketService, now) -> None:
    for payload, post in seed_payloads(now):
        ticket, _decision, _replayed = service.create(payload, now=now)
        status = post.pop("status", None)
        moment = close_moment(now, post) if "close_before" in post else now
        post.pop("close_before", None)
        if status:
            for step in transition_path(ticket.status, Status.parse(status)):
                service.transition(ticket.id, step, now=moment)
        if post:
            service.update(ticket.id, post, now=moment)


def wait_ready(base: str, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/api/v1/health", timeout=2) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(0.2)
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="start a server and smoke test it")
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--store", choices=("sqlite", "memory"), default="sqlite")
    parser.add_argument("--keep-db", action="store_true", help="do not delete the smoke database")
    args = parser.parse_args(argv)

    port = free_port(args.port)
    database = REPO_ROOT / "data" / "ci-smoke.db"
    if database.exists():
        database.unlink()

    settings = Settings(
        host="127.0.0.1",
        port=port,
        store=args.store,
        database=database,
        web_root=REPO_ROOT / "frontend",
    )
    policy, document = load_policy(settings)
    store = build_store(settings)
    service = TicketService(store, policy.compiled)
    from datetime import datetime, timezone

    seed_service(service, datetime.now(timezone.utc))

    router = build_router(
        service,
        {
            "policy": policy.compiled,
            "document": document,
            "store_kind": settings.store,
            "store_stats": {"kind": type(store).__name__},
        },
        started_at=time.monotonic(),
    )
    server = serve(("127.0.0.1", port), service, policy.compiled, router, web_root=settings.web_root)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    exit_code = 1
    try:
        if not wait_ready(base):
            print(f"server did not become ready at {base}", file=sys.stderr)
            return 1
        print(f"server ready at {base} (store={settings.store})\n")
        exit_code = smoke_main(["--base", base])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        closer = getattr(store, "close", None)
        if callable(closer):
            closer()
        if not args.keep_db and database.exists():
            for suffix in ("", "-wal", "-shm"):
                sidecar = Path(f"{database}{suffix}")
                if sidecar.exists():
                    sidecar.unlink()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
