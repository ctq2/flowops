"""Package a self-contained demo bundle.

``dist/flowops-demo/`` contains the server, the console, the demo data and its
own launcher, so the whole thing can be zipped and opened on a machine that has
nothing but Python 3.11+.  That is what makes the project demonstrable in an
interview or a client call without a build step.
"""

from __future__ import annotations

import json
import shutil
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
DIST = REPO / "dist" / "flowops-demo"

LAUNCHER = '''#!/usr/bin/env python3
"""FlowOps demo launcher — start the API and console, then open the URL."""

from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "backend"))

from app.adapters.http.api import build_router  # noqa: E402
from app.adapters.http.server import serve  # noqa: E402
from app.application.services.tickets import TicketService  # noqa: E402
from app.bootstrap import build_store, load_policy  # noqa: E402
from app.config.settings import Settings  # noqa: E402
from app.domain.values import Status  # noqa: E402
from app.seed import close_moment, seed_payloads, transition_path  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="FlowOps demo")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    settings = Settings(
        host=args.host,
        port=args.port,
        store="sqlite",
        database=HERE / "data" / "flowops.db",
        policy_path=HERE / "backend" / "config" / "policies" / "default.json",
        calendar_path=HERE / "backend" / "config" / "calendars" / "cn-holidays.json",
        web_root=HERE / "frontend",
    )
    policy, document = load_policy(settings)
    store = build_store(settings)
    service = TicketService(store, policy.compiled)

    if not store.all_tickets():
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        for payload, post in seed_payloads(now):
            ticket, _decision, _replayed = service.create(payload, actor="demo", now=now)
            status = post.pop("status", None)
            moment = close_moment(now, post) if "close_before" in post else now
            post.pop("close_before", None)
            if status:
                for step in transition_path(ticket.status, Status.parse(status)):
                    service.transition(ticket.id, step, actor="demo", now=moment)
            if post:
                service.update(ticket.id, post, actor="demo", now=moment)
        print(f"seeded {len(store.all_tickets())} demo tickets")

    router = build_router(
        service,
        {"policy": policy.compiled, "document": document, "store_kind": "sqlite", "store_stats": {}},
        started_at=0.0,
    )
    server = serve((args.host, args.port), service, policy.compiled, router, web_root=settings.web_root)
    url = f"http://{args.host}:{args.port}"
    print(f"FlowOps demo running at {url}")
    print("  press Ctrl+C to stop")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\\nbye")
    finally:
        server.server_close()
        closer = getattr(store, "close", None)
        if callable(closer):
            closer()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

README = """# FlowOps 演示包

零依赖，双击即跑（需要 Python 3.11+）。

```bash
python run.py            # 启动后自动打开浏览器
python run.py --port 9000 --no-browser
```

- 控制台：http://127.0.0.1:8787
- API 文档索引：http://127.0.0.1:8787/api/v1/meta
- 数据保存在 `data/flowops.db`（SQLite），删除该文件即恢复出厂演示数据。

离线查看：直接用浏览器打开 `frontend/index.html` 也能看到快照数据（只读）。
"""


def copy_tree(source: Path, target: Path, *, ignore: tuple[str, ...] = ("__pycache__",)) -> int:
    count = 0
    for path in sorted(source.rglob("*")):
        if any(part in ignore for part in path.parts):
            continue
        if path.is_dir():
            continue
        relative = path.relative_to(source)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        count += 1
    return count


def main() -> int:
    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir(parents=True)

    files = 0
    files += copy_tree(BACKEND / "app", DIST / "backend" / "app")
    files += copy_tree(BACKEND / "config", DIST / "backend" / "config")
    files += copy_tree(BACKEND / "scripts", DIST / "backend" / "scripts")
    files += copy_tree(REPO / "frontend", DIST / "frontend")
    files += copy_tree(REPO / "docs", DIST / "docs")

    (DIST / "run.py").write_text(LAUNCHER, encoding="utf-8")
    (DIST / "README.md").write_text(README, encoding="utf-8")
    (DIST / "data").mkdir(exist_ok=True)
    (DIST / "MANIFEST.json").write_text(
        json.dumps(
            {
                "name": "flowops-demo",
                "version": "1.0.0",
                "built_at": datetime.now(timezone.utc).isoformat(),
                "requires": "Python 3.11+ (no third-party packages)",
                "entrypoint": "python run.py",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    archive = REPO / "dist" / "flowops-demo.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(DIST.rglob("*")):
            if path.is_file():
                bundle.write(path, path.relative_to(DIST.parent).as_posix())

    print(f"built {DIST.relative_to(REPO)} — {files} files")
    print(f"built {archive.relative_to(REPO)} — {archive.stat().st_size / 1024:.0f} KiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
