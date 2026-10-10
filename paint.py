#!/usr/bin/env python3
"""GitHub Activity Painter.

Turns a 7 x 53 picture (days of the week x weeks) into back-dated, empty git
commits so that it shows up on your GitHub contribution graph.

Usage:
    python3 paint.py                     # start the web interface (default)
    python3 paint.py serve [--port N] [--no-browser]
    python3 paint.py status              # painted commits per day
    python3 paint.py apply design.json   # (re)paint from an exported design
    python3 paint.py clear               # delete every painted commit

Only the Python standard library and git are needed.

How commits are managed
-----------------------
Every painted commit is an *empty* commit (same tree as its parent) whose
message carries the trailer ``Activity-Painter: true``. Because painted commits
never change any file, they can be removed again at any time by rebuilding the
branch without them; the content of your other commits is never touched.
Removing commits rewrites history, so the next push must be a force push
(``git push --force-with-lease``).
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TRAILER_KEY = "Activity-Painter"
TRAILER = f"{TRAILER_KEY}: true"
HTML_FILE = "github-activity-painter.html"
MAX_COMMITS_PER_DAY = 10_000
DEFAULT_PORT = 8765
HERE = Path(__file__).resolve().parent
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


class PainterError(Exception):
    """An error that should be reported to the user as-is."""


# --------------------------------------------------------------------------- #
# git helpers
# --------------------------------------------------------------------------- #

def git(*args: str, input: bytes | None = None, cwd: Path | None = None,
        check: bool = True) -> bytes:
    proc = subprocess.run(
        ["git", *args], input=input, cwd=cwd or HERE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if check and proc.returncode != 0:
        msg = proc.stderr.decode(errors="replace").strip()
        raise PainterError(f"git {' '.join(args[:2])} failed: {msg}")
    return proc.stdout


def git_text(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    return git(*args, cwd=cwd, check=check).decode(errors="replace").strip()


class Repo:
    """Painting operations on one git working copy."""

    def __init__(self, path: Path = HERE):
        self.path = Path(path)
        root = git_text("rev-parse", "--show-toplevel", cwd=self.path, check=False)
        if not root:
            raise PainterError(f"{self.path} is not inside a git repository")
        self.path = Path(root)

    def git(self, *args: str, input: bytes | None = None, check: bool = True) -> bytes:
        return git(*args, input=input, cwd=self.path, check=check)

    def text(self, *args: str, check: bool = True) -> str:
        return git_text(*args, cwd=self.path, check=check)

    # -- basic info -------------------------------------------------------- #

    def branch(self) -> str:
        name = self.text("symbolic-ref", "--quiet", "--short", "HEAD", check=False)
        if not name:
            raise PainterError("HEAD is detached: check out a branch first "
                               "(usually your default branch, e.g. `git switch main`).")
        return name

    def head(self) -> str | None:
        return self.text("rev-parse", "--verify", "--quiet", "HEAD", check=False) or None

    def identity(self) -> dict:
        name = os.environ.get("PAINTER_NAME") or self.text("config", "user.name", check=False)
        email = os.environ.get("PAINTER_EMAIL") or self.text("config", "user.email", check=False)
        return {"name": name, "email": email}

    def remote_url(self) -> str:
        return self.text("remote", "get-url", "origin", check=False)

    def default_branch(self) -> str:
        ref = self.text("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", check=False)
        return ref.split("/", 1)[1] if "/" in ref else ""

    # -- reading the painting --------------------------------------------- #

    def painted_shas(self) -> set[str]:
        if not self.head():
            return set()
        out = self.text("rev-list", "HEAD", f"--grep=^{TRAILER_KEY}: true$")
        return set(out.split())

    def daily_counts(self) -> dict:
        """Commits per day reachable from HEAD, split into painted / other."""
        painted: dict[str, int] = {}
        other: dict[str, int] = {}
        if self.head():
            fmt = f"%ad%x09%(trailers:key={TRAILER_KEY},valueonly,separator=%x2C)"
            # format: keeps the commit's own time zone, like GitHub does.
            out = self.text("log", "HEAD", "--date=format:%Y-%m-%d", f"--format={fmt}")
            for line in out.splitlines():
                day, _, trailer = line.partition("\t")
                if not day:
                    continue
                bucket = painted if trailer.strip() == "true" else other
                bucket[day] = bucket.get(day, 0) + 1
        return {"painted": painted, "other": other}

    def status(self) -> dict:
        counts = self.daily_counts()
        branch = self.text("symbolic-ref", "--quiet", "--short", "HEAD", check=False)
        return {
            "repo": str(self.path),
            "branch": branch,
            "detached": not branch,
            "default_branch": self.default_branch(),
            "head": self.head(),
            "remote": self.remote_url(),
            "identity": self.identity(),
            "painted": counts["painted"],
            "other": counts["other"],
            "painted_total": sum(counts["painted"].values()),
            "other_total": sum(counts["other"].values()),
            "today": dt.date.today().isoformat(),
        }

    # -- clearing ---------------------------------------------------------- #

    def clear(self) -> int:
        """Remove every painted commit from the current branch.

        Returns the number of commits removed. Non-painted commits are kept
        byte-for-byte (except their parent pointers, and any signature, which
        would no longer be valid).
        """
        branch = self.branch()
        head = self.head()
        painted = self.painted_shas()
        if not head or not painted:
            return 0

        lines = self.text("log", "--reverse", "--topo-order", "--format=%H %T %P", "HEAD").splitlines()
        trees: dict[str, str] = {}
        mapping: dict[str, str | None] = {}
        removed = 0
        for line in lines:
            sha, tree, *parents = line.split()
            trees[sha] = tree
            # Safety net: only drop commits that really are empty paint commits.
            parent_tree = trees.get(parents[0]) if parents else EMPTY_TREE
            if sha in painted and len(parents) <= 1 and tree == parent_tree:
                removed += 1
            else:
                painted.discard(sha)
            new_parents: list[str] = []
            for p in parents:
                q = mapping.get(p, p)
                if q and q not in new_parents:
                    new_parents.append(q)
            if sha in painted:
                # Painted commits are empty and linear: collapse onto the parent.
                mapping[sha] = new_parents[0] if new_parents else None
                continue
            if new_parents == parents:
                mapping[sha] = sha
                continue
            mapping[sha] = self._rewrite_parents(sha, new_parents)

        new_head = mapping[head]
        ref = f"refs/heads/{branch}"
        if new_head is None:
            # The branch contained nothing but painted commits.
            self.git("update-ref", "-d", ref, head)
        else:
            self.git("update-ref", "-m", "activity-painter: clear", ref, new_head, head)
        return removed

    def _rewrite_parents(self, sha: str, parents: list[str]) -> str:
        raw = self.git("cat-file", "commit", sha)
        header, sep, message = raw.partition(b"\n\n")
        out: list[bytes] = []
        skipping_sig = False
        for line in header.split(b"\n"):
            if skipping_sig and line.startswith(b" "):
                continue
            skipping_sig = False
            if line.startswith(b"parent "):
                continue
            if line.startswith(b"gpgsig") or line.startswith(b"gpgsig-sha256"):
                skipping_sig = True
                continue
            out.append(line)
            if line.startswith(b"tree "):
                out.extend(b"parent " + p.encode() for p in parents)
        data = b"\n".join(out) + sep + message
        return self.git("hash-object", "-t", "commit", "-w", "--stdin", input=data).decode().strip()

    # -- painting ---------------------------------------------------------- #

    def paint(self, counts: dict[str, int], name: str = "", email: str = "",
              replace: bool = True) -> dict:
        """Create ``counts[day]`` empty commits dated on each day.

        With ``replace`` (the default) the previous painting is removed first.
        """
        clean: dict[str, int] = {}
        for day, n in counts.items():
            try:
                dt.date.fromisoformat(day)
            except (TypeError, ValueError):
                raise PainterError(f"invalid date {day!r} (expected YYYY-MM-DD)") from None
            if not isinstance(n, int) or isinstance(n, bool) or n < 0:
                raise PainterError(f"invalid commit count {n!r} for {day}")
            if n > MAX_COMMITS_PER_DAY:
                raise PainterError(f"{n} commits on {day} is more than {MAX_COMMITS_PER_DAY}")
            if n:
                clean[day] = n

        ident = self.identity()
        name = (name or ident["name"]).strip()
        email = (email or ident["email"]).strip()
        if not name or not email:
            raise PainterError("no author identity: set `git config user.name` / `user.email` "
                               "or fill in the author fields in the interface")
        if any(c in name + email for c in "<>\n"):
            raise PainterError("author name/email must not contain <, > or newlines")

        branch = self.branch()
        removed = self.clear() if replace else 0
        created = 0
        if clean:
            created = self._fast_import(branch, clean, name, email)
        return {"removed": removed, "created": created, "days": len(clean)}

    def _fast_import(self, branch: str, counts: dict[str, int], name: str, email: str) -> int:
        parent = self.head()
        chunks: list[bytes] = []
        created = 0
        for day in sorted(counts):
            n = counts[day]
            # Noon UTC: the day stays the same whatever time zone GitHub uses.
            base = int(dt.datetime.fromisoformat(day + "T12:00:00+00:00").timestamp())
            for i in range(n):
                stamp = f"{base + i} +0000"
                msg = f"Paint {day} ({i + 1}/{n})\n\n{TRAILER}\n".encode()
                chunks.append(f"commit refs/heads/{branch}\n".encode())
                chunks.append(f"author {name} <{email}> {stamp}\n".encode())
                chunks.append(f"committer {name} <{email}> {stamp}\n".encode())
                chunks.append(f"data {len(msg)}\n".encode() + msg)
                if created == 0 and parent:
                    chunks.append(f"from {parent}\n".encode())
                chunks.append(b"\n")
                created += 1
        chunks.append(b"done\n")
        self.git("fast-import", "--quiet", "--done", input=b"".join(chunks))
        return created

    def push(self) -> str:
        branch = self.branch()
        if not self.remote_url():
            raise PainterError("no `origin` remote configured")
        proc = subprocess.run(
            ["git", "push", "--force-with-lease", "-u", "origin", branch],
            cwd=self.path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        output = proc.stdout.decode(errors="replace").strip()
        if proc.returncode != 0:
            raise PainterError(f"git push failed:\n{output}")
        return output


# --------------------------------------------------------------------------- #
# designs
# --------------------------------------------------------------------------- #

def level_counts(min_commits: int, max_commits: int) -> list[int]:
    """Commits per day for GitHub levels 0..4.

    Level 0 gets ``min`` commits, level 4 gets ``max`` and levels 1..3 are
    spread evenly in between (always at least one commit for levels >= 1).
    """
    lo, hi = int(min_commits), int(max_commits)
    if lo < 0 or hi < lo:
        raise PainterError("contrast must satisfy 0 <= min <= max")
    out = [lo]
    for level in range(1, 5):
        out.append(max(1, int(lo + (hi - lo) * level / 4 + 0.5)))  # same rounding as the UI
    return out


def design_to_counts(design: dict) -> dict[str, int]:
    """Convert an exported design (see README) to commits per day."""
    if "counts" in design:
        return {d: int(n) for d, n in design["counts"].items()}
    contrast = design.get("contrast", {})
    per_level = level_counts(contrast.get("min", 0), contrast.get("max", 10))
    levels = design.get("levels", {})
    counts: dict[str, int] = {}
    rng = design.get("range")
    if per_level[0] and rng:
        day = dt.date.fromisoformat(rng["start"])
        end = dt.date.fromisoformat(rng["end"])
        while day <= end:
            counts[day.isoformat()] = per_level[0]
            day += dt.timedelta(days=1)
    for day, level in levels.items():
        level = int(level)
        if not 0 <= level <= 4:
            raise PainterError(f"invalid level {level} for {day}")
        counts[day] = per_level[level]
    return counts


# --------------------------------------------------------------------------- #
# GitHub profile (optional, used to pick a contrast)
# --------------------------------------------------------------------------- #

def parse_github_contributions(page: str) -> dict[str, int]:
    """Parse https://github.com/users/<login>/contributions into {day: count}."""
    ids: dict[str, str] = {}
    for m in re.finditer(r"<td\b[^>]*>", page):
        tag = m.group(0)
        date = re.search(r'data-date="(\d{4}-\d{2}-\d{2})"', tag)
        ident = re.search(r'\bid="([^"]+)"', tag)
        if date and ident:
            ids[ident.group(1)] = date.group(1)
    counts = {d: 0 for d in ids.values()}
    for m in re.finditer(r'<tool-tip\b[^>]*\bfor="([^"]+)"[^>]*>(.*?)</tool-tip>', page, re.S):
        day = ids.get(m.group(1))
        text = html.unescape(m.group(2)).strip()
        n = re.match(r"([\d,]+) contribution", text)
        if day and n:
            counts[day] = int(n.group(1).replace(",", ""))
    return counts


def fetch_github_contributions(login: str) -> dict[str, int]:
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", login or ""):
        raise PainterError("invalid GitHub username")
    url = f"https://github.com/users/{login}/contributions"
    req = urllib.request.Request(url, headers={"User-Agent": "github-activity-painter"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            page = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError) as exc:
        raise PainterError(f"could not fetch {url}: {exc}") from None
    counts = parse_github_contributions(page)
    if not counts:
        raise PainterError("could not read the contribution graph (GitHub page format changed?)")
    return counts


# --------------------------------------------------------------------------- #
# web server
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    repo: Repo
    lock = threading.Lock()

    def log_message(self, fmt, *args):  # keep the terminal readable
        if os.environ.get("PAINTER_DEBUG"):
            super().log_message(fmt, *args)

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, status: int = 200) -> None:
        self._send(status, json.dumps(data).encode(), "application/json")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length))

    def _same_origin(self) -> bool:
        # Refuse cross-site requests: only the page served by us may call the API.
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        return origin is None or origin in (f"http://{host}",)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        try:
            if path in ("/", f"/{HTML_FILE}"):
                body = (HERE / HTML_FILE).read_bytes()
                return self._send(200, body, "text/html; charset=utf-8")
            if path == "/api/status":
                with self.lock:
                    return self._json(self.repo.status())
            if path == "/api/github":
                query = dict(p.split("=", 1) for p in self.path.partition("?")[2].split("&") if "=" in p)
                return self._json({"counts": fetch_github_contributions(query.get("user", ""))})
            self._send(404, b"not found", "text/plain")
        except PainterError as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def do_POST(self):
        if not self._same_origin():
            return self._json({"error": "cross-origin request refused"}, HTTPStatus.FORBIDDEN)
        try:
            body = self._body()
            with self.lock:
                if self.path == "/api/paint":
                    result = self.repo.paint(body.get("counts", {}), body.get("name", ""),
                                             body.get("email", ""), body.get("replace", True))
                elif self.path == "/api/clear":
                    result = {"removed": self.repo.clear()}
                elif self.path == "/api/push":
                    result = {"output": self.repo.push()}
                else:
                    return self._send(404, b"not found", "text/plain")
                result["status"] = self.repo.status()
            self._json(result)
        except (PainterError, ValueError) as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)


def serve(repo: Repo, port: int, open_browser: bool) -> None:
    Handler.repo = repo
    server = None
    for candidate in range(port, port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
            break
        except OSError:
            continue
    if server is None:
        raise PainterError(f"no free port between {port} and {port + 19}")
    url = f"http://127.0.0.1:{server.server_address[1]}/{HTML_FILE}"
    print(f"GitHub Activity Painter running on {url}")
    print(f"Repository: {repo.path}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBye!")
    finally:
        server.server_close()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Paint your GitHub contribution graph.")
    parser.add_argument("--repo", default=str(HERE), help="git repository to paint (default: this one)")
    sub = parser.add_subparsers(dest="cmd")
    p_serve = sub.add_parser("serve", help="start the web interface (default)")
    p_serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    p_serve.add_argument("--no-browser", action="store_true", help="do not open a browser")
    sub.add_parser("status", help="show painted commits per day")
    p_apply = sub.add_parser("apply", help="paint an exported design (replaces the current painting)")
    p_apply.add_argument("design", help="design JSON exported from the interface")
    p_apply.add_argument("--min", type=int, help="override min commits per day (level 0)")
    p_apply.add_argument("--max", type=int, help="override max commits per day (level 4)")
    p_apply.add_argument("--name", default="", help="author name (default: git config user.name)")
    p_apply.add_argument("--email", default="", help="author email (default: git config user.email)")
    sub.add_parser("clear", help="delete every painted commit")
    args = parser.parse_args(argv)

    try:
        repo = Repo(Path(args.repo))
        cmd = args.cmd or "serve"
        if cmd == "serve":
            serve(repo, getattr(args, "port", DEFAULT_PORT), not getattr(args, "no_browser", False))
        elif cmd == "status":
            st = repo.status()
            print(f"Branch: {st['branch'] or '(detached)'}   author: {st['identity']['name']} <{st['identity']['email']}>")
            print(f"Painted commits: {st['painted_total']} on {len(st['painted'])} days; "
                  f"other commits: {st['other_total']}")
            for day in sorted(st["painted"]):
                print(f"  {day}  {st['painted'][day]:>4}  {'#' * min(st['painted'][day], 60)}")
        elif cmd == "apply":
            design = json.loads(Path(args.design).read_text())
            if args.min is not None or args.max is not None:
                design.pop("counts", None)
                contrast = design.setdefault("contrast", {})
                if args.min is not None:
                    contrast["min"] = args.min
                if args.max is not None:
                    contrast["max"] = args.max
            result = repo.paint(design_to_counts(design), args.name, args.email)
            print(f"Removed {result['removed']} old painted commits, created {result['created']} "
                  f"commits on {result['days']} days.")
            print("Now push with:  git push --force-with-lease")
        elif cmd == "clear":
            print(f"Removed {repo.clear()} painted commits.")
            print("Now push with:  git push --force-with-lease")
        return 0
    except PainterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
