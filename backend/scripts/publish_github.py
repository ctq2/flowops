"""Publish this repository to GitHub without a `git` network transport.

Why this exists: on some corporate and mainland-China networks ``github.com:443``
is unreachable while ``api.github.com`` responds normally.  ``git push`` therefore
cannot work, but the REST API can still create a repository and materialise a
commit — the same three objects git would have uploaded (blobs, tree, commit).

It implements exactly that pipeline, using only the standard library:

1. ``GET /user``                      — verify the token
2. ``POST /user/repos``               — create the repository if absent
3. ``PUT /contents/<seed>``           — bootstrap the first commit

   GitHub refuses every Git Data API call (``409 Git Repository is empty``) until
   the repository has at least one commit, because a commit needs a parent tree.
   So the very first commit is created through the Contents API, which is allowed
   to start from nothing; everything after that uses the normal pipeline.

4. ``POST /git/blobs`` (parallel)     — one blob per file
5. ``POST /git/trees``                — a tree referencing those blobs
6. ``POST /git/commits``              — a commit pointing at the tree
7. ``PATCH /git/refs/heads/main``     — move the branch

Usage::

    python backend/scripts/publish_github.py \\
        --token <PAT> --repo flowops --public \\
        --message "feat: FlowOps SLA 工单运营平台"

The token is only ever sent in an HTTP header; it is never written to disk.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
API = "https://api.github.com"

TEXT_SUFFIXES = {
    ".py", ".js", ".css", ".html", ".json", ".md", ".txt", ".yml", ".yaml",
    ".toml", ".cfg", ".ini", ".sh", ".ps1", ".sql", ".gitignore", ".editorconfig",
}
TEXT_NAMES = {"Makefile", "Dockerfile", ".gitignore", "LICENSE"}

#: GitHub refuses to let a token without the `workflow` scope create or update
#: anything under `.github/workflows/`.  Writing those files is a one-time copy
#: and paste, so they are skipped rather than forcing a broader token — and the
#: script says exactly which files were left out.
PROTECTED_PREFIXES = (".github/workflows/",)

DESCRIPTION = (
    "FlowOps — SLA 工单运营平台：自研规则表达式引擎、中国法定节假日/调休感知的时限计算、"
    "零依赖全栈实现与 300+ 自动化测试"
)
TOPICS = ["python", "rule-engine", "sla", "workflow", "rest-api", "sqlite", "zero-dependencies", "observability"]


class GitHubError(RuntimeError):
    def __init__(self, status: int, payload: object, *, method: str = "", path: str = "") -> None:
        detail = payload
        if isinstance(payload, dict):
            detail = payload.get("message", payload)
        where = f" [{method} {path}]" if method or path else ""
        super().__init__(f"HTTP {status}{where}: {detail}")
        self.status = status
        self.payload = payload
        self.method = method
        self.path = path


def request(
    method: str,
    path: str,
    token: str,
    *,
    body: dict | None = None,
    expect: tuple[int, ...] = (200, 201),
    attempts: int = 5,
) -> dict:
    """Call the API with a small retry budget for transient 5xx/429 responses."""
    url = path if path.startswith("http") else f"{API}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    last: Exception | None = None
    for attempt in range(attempts):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "flowops-publisher")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                raw = response.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as error:
            payload: object
            try:
                payload = json.loads(error.read().decode("utf-8"))
            except Exception:  # noqa: BLE001 - best effort error surface
                payload = error.reason
            if error.code in expect:
                return payload if isinstance(payload, dict) else {}
            if error.code in (409, 429, 500, 502, 503, 504) and attempt < attempts - 1:
                # 409 shows up as "Git Repository is empty" for a few seconds after
                # creation — the Git data store is not ready yet, which is
                # transient, so it is retried like any other 5xx.
                time.sleep(1.5 * (attempt + 1))
                last = GitHubError(error.code, payload, method=method, path=path)
                continue
            # A 403 right after a repository is (re)created is GitHub propagating
            # the token's repository grant; retrying once is usually enough.
            if error.code == 403 and attempt < attempts - 1:
                time.sleep(3.0 * (attempt + 1))
                last = GitHubError(error.code, payload, method=method, path=path)
                continue
            raise GitHubError(error.code, payload, method=method, path=path) from error
        except urllib.error.URLError as error:
            last = error
            if attempt < attempts - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
    raise last if last else RuntimeError("request failed")


def _is_empty(token: str, owner: str, repo: str) -> bool:
    """True when the repository has no commit (and therefore rejects writes)."""
    try:
        request("GET", f"/repos/{owner}/{repo}/git/ref/heads/main", token)
    except GitHubError as error:
        if error.status == 409:
            return True
        if error.status == 404:
            return False  # exists, just no `main` branch yet
        raise
    return False


def _protected_entries(token: str, owner: str, repo: str, commit_sha: str) -> list[dict]:
    """Blob entries for protected paths already present on the branch.

    Reuses the blob SHAs that are already stored, so nothing is re-uploaded and
    nothing that a human added by hand is dropped.
    """
    try:
        commit = request("GET", f"/repos/{owner}/{repo}/git/commits/{commit_sha}", token)
        listing = request(
            "GET", f"/repos/{owner}/{repo}/git/trees/{commit['tree']['sha']}?recursive=1", token
        )
    except GitHubError:
        return []
    return [
        {"path": item["path"], "mode": item["mode"], "type": "blob", "sha": item["sha"]}
        for item in listing.get("tree", [])
        if item.get("type") == "blob" and item["path"].startswith(PROTECTED_PREFIXES)
    ]


def collect_files(root: Path) -> list[Path]:
    """Every file git would track: no ``.git``, no caches, no databases."""
    """Every file git would track: no ``.git``, no caches, no databases."""
    skip_dirs = {".git", "__pycache__", "node_modules", ".ruff_cache", ".mypy_cache", ".pytest_cache", "dist"}
    skip_suffixes = {".pyc", ".pyo", ".db", ".db-wal", ".db-shm", ".log"}
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in skip_dirs for part in path.parts):
            continue
        if path.suffix in skip_suffixes:
            continue
        files.append(path)
    return files


def split_protected(files: list[Path], root: Path) -> tuple[list[Path], list[str]]:
    """Separate files the API refuses to write from the ones it accepts."""
    allowed: list[Path] = []
    protected: list[str] = []
    for path in files:
        relative = path.relative_to(root).as_posix()
        if relative.startswith(PROTECTED_PREFIXES):
            protected.append(relative)
        else:
            allowed.append(path)
    return allowed, protected


def blob_payload(path: Path, root: Path) -> dict:
    """Text files are normalised to LF in git, exactly like ``core.autocrlf=input``."""
    raw = path.read_bytes()
    relative = path.relative_to(root).as_posix()
    if path.suffix in TEXT_SUFFIXES or path.name in TEXT_NAMES:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            pass
        else:
            normalised = text.replace("\r\n", "\n").replace("\r", "\n")
            return {"path": relative, "content": normalised, "encoding": "utf-8"}
    return {"path": relative, "content": base64.b64encode(raw).decode("ascii"), "encoding": "base64"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="publish this repository through the GitHub REST API")
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"), help="personal access token")
    parser.add_argument("--owner", default=None, help="account or org (defaults to the token's user)")
    parser.add_argument("--repo", default="flowops")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--message", default="chore: publish")
    parser.add_argument("--message-file", default=None, help="read the commit message from a file (multi-line)")
    parser.add_argument("--public", action="store_true")
    parser.add_argument("--root", default=str(REPO_ROOT))
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--dry-run", action="store_true", help="list what would be uploaded")
    args = parser.parse_args(argv)

    if not args.token:
        print("error: --token or GITHUB_TOKEN is required", file=sys.stderr)
        return 2

    message = args.message
    if args.message_file:
        message = Path(args.message_file).read_text(encoding="utf-8").strip()

    root = Path(args.root).resolve()
    files = collect_files(root)
    files, protected = split_protected(files, root)
    print(f"repository : {root}")
    print(f"files      : {len(files)}")
    if protected:
        print(f"skipped    : {len(protected)} file(s) under .github/workflows/ (needs the `workflow` scope)")
        for item in protected:
            print(f"             - {item}")

    if args.dry_run:
        total = sum(path.stat().st_size for path in files)
        for path in files:
            print(f"  {path.relative_to(root).as_posix()}")
        print(f"total bytes: {total}")
        return 0

    me = request("GET", "/user", args.token)
    owner = args.owner or me["login"]
    print(f"token owner: {me['login']} ({owner}/{args.repo})")

    # 1) repository.
    #
    # Created with `auto_init: true` on purpose.  A repository created *empty*
    # rejects every write through the API — Contents, blobs, refs — with
    # ``409 Git Repository is empty`` until a first commit exists, and there is no
    # API-only way to create that first commit *in* an empty repository.  Letting
    # GitHub write the initial commit removes the chicken-and-egg problem; the
    # generated README is replaced wholesale by the commit below.
    new_repo = {
        "name": args.repo,
        "description": DESCRIPTION,
        "private": not args.public,
        "has_issues": True,
        "has_wiki": False,
        "auto_init": True,
    }
    try:
        request("POST", "/user/repos", args.token, body=new_repo)
        print("created repository (initialised)")
    except GitHubError as error:
        if error.status != 422:
            raise
        print("repository already exists — reusing")
        if _is_empty(args.token, owner, args.repo):
            # Recover from an earlier attempt that left an unusable empty repo.
            print("repository is empty and unwritable — recreating it")
            request("DELETE", f"/repos/{owner}/{args.repo}", args.token, expect=(204,))
            request("POST", "/user/repos", args.token, body=new_repo)
            print("recreated repository (initialised)")

    # 2) sanity check: a repository that still has no commit cannot be written to.
    if _is_empty(args.token, owner, args.repo):
        print(
            "error: the repository has no commit and cannot be written to via the API; "
            "delete it on GitHub and re-run",
            file=sys.stderr,
        )
        return 1

    # 4) blobs, in parallel: 85 sequential round trips would be needlessly slow
    print(f"uploading {len(files)} blobs ...")
    payloads = [blob_payload(path, root) for path in files]
    tree_entries: list[dict] = []

    def upload(payload: dict) -> dict:
        result = request("POST", f"/repos/{owner}/{args.repo}/git/blobs", args.token, body=payload)
        executable = payload["path"].endswith((".sh", ".bash")) or payload["content"].startswith("#!/")
        return {
            "path": payload["path"],
            "mode": "100755" if executable else "100644",
            "type": "blob",
            "sha": result["sha"],
        }

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for index, entry in enumerate(pool.map(upload, payloads), start=1):
            tree_entries.append(entry)
            if index % 20 == 0 or index == len(payloads):
                print(f"  {index}/{len(payloads)}")

    # 5) the parent commit, if the branch already exists
    parents: list[str] = []
    try:
        ref = request("GET", f"/repos/{owner}/{args.repo}/git/ref/heads/{args.branch}", args.token)
        parents = [ref["object"]["sha"]]
        print(f"parent     : {parents[0][:12]}")
    except GitHubError as error:
        if error.status != 404:
            raise
        print("parent     : none (first commit on this branch)")

    # 6) tree — built *without* base_tree so the commit describes exactly this file
    #    set, then any protected paths already on the branch are carried over
    #    verbatim.  Without that second step a republish would delete files this
    #    token cannot write (`.github/workflows/*`) — precisely what a user adds
    #    by hand after the first push.
    tree = request(
        "POST",
        f"/repos/{owner}/{args.repo}/git/trees",
        args.token,
        body={"tree": tree_entries},
    )
    print(f"tree       : {tree['sha'][:12]} ({len(tree_entries)} entries)")

    if protected and parents:
        carried = _protected_entries(args.token, owner, args.repo, parents[0])
        if carried:
            tree = request(
                "POST",
                f"/repos/{owner}/{args.repo}/git/trees",
                args.token,
                body={"base_tree": tree["sha"], "tree": carried},
            )
            print(
                f"             carried over {len(carried)} protected file(s): "
                + ", ".join(entry["path"] for entry in carried)
            )

    # 7) commit
    commit_body: dict = {"message": message, "tree": tree["sha"]}
    if parents:
        commit_body["parents"] = parents
    commit = request("POST", f"/repos/{owner}/{args.repo}/git/commits", args.token, body=commit_body)
    print(f"commit     : {commit['sha'][:12]}")

    # 8) move the branch
    if parents:
        request(
            "PATCH",
            f"/repos/{owner}/{args.repo}/git/refs/heads/{args.branch}",
            args.token,
            body={"sha": commit["sha"], "force": False},
        )
    else:
        request(
            "POST",
            f"/repos/{owner}/{args.repo}/git/refs",
            args.token,
            body={"ref": f"refs/heads/{args.branch}", "sha": commit["sha"]},
        )

    # 8) make the repository discoverable
    try:
        request(
            "PATCH",
            f"/repos/{owner}/{args.repo}",
            args.token,
            body={
                "description": DESCRIPTION,
                "homepage": f"https://github.com/{owner}/{args.repo}",
            },
        )
        request("PUT", f"/repos/{owner}/{args.repo}/topics", args.token, body={"names": TOPICS})
    except GitHubError as error:  # topics need push access; description is best effort
        print(f"note: metadata update skipped ({error})")

    print(f"\npublished: https://github.com/{owner}/{args.repo}")
    print(f"commit   : https://github.com/{owner}/{args.repo}/commit/{commit['sha']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GitHubError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
