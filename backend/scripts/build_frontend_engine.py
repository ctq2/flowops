"""Build ``frontend/data/flowops-engine.zip``.

The console's policy sandbox runs the **real** Python rule engine in the browser
through Pyodide.  Shipping the engine as a zip is what keeps the browser and the
server from disagreeing: there is exactly one implementation of the DSL, one
calendar, and one set of SLA semantics.

Re-run this whenever anything under ``backend/app/domain`` or ``backend/config``
changes::

    python backend/scripts/build_frontend_engine.py
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
TARGET = REPO / "frontend" / "data" / "flowops-engine.zip"

#: Only dependency-free packages travel to the browser.  Adapters (HTTP, SQLite)
#: are deliberately excluded: Pyodide has no sockets or sqlite3 in Emscripten, and
#: the sandbox only needs the domain layer.
INCLUDE_DIRS = (BACKEND / "app" / "domain",)
INCLUDE_FILES = (
    BACKEND / "app" / "__init__.py",
    BACKEND / "config" / "policies" / "default.json",
    BACKEND / "config" / "calendars" / "cn-holidays.json",
)

EXCLUDE_PARTS = {"__pycache__"}


def main() -> int:
    if not TARGET.parent.exists():
        TARGET.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with zipfile.ZipFile(TARGET, "w", zipfile.ZIP_DEFLATED) as archive:
        for directory in INCLUDE_DIRS:
            for path in sorted(directory.rglob("*.py")):
                if EXCLUDE_PARTS & set(path.parts):
                    continue
                archive.write(path, path.relative_to(BACKEND).as_posix())
                written += 1
        for path in INCLUDE_FILES:
            if path.exists():
                archive.write(path, path.relative_to(BACKEND).as_posix())
                written += 1
    size = TARGET.stat().st_size
    print(f"wrote {TARGET.relative_to(REPO)} — {written} files, {size / 1024:.1f} KiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
