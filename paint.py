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
UPSTREAM_URL = "https://github.com/lkorczowski/github-activity-painting.git"
COMMUNITY_BRANCH = "my-painting"
PAINT_MESSAGE = re.compile(rf"Paint (\d{{4}}-\d{{2}}-\d{{2}}) \((\d+)/(\d+)\)\n\n{TRAILER}\n*")
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
        # Commits reachable from `base` (e.g. the shared community wall) are
        # never removed or rewritten: only the painting made on top counts as ours.
        self.base: str | None = None
        self.community: dict | None = None

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

    def own_range(self) -> str:
        """Revision range holding the commits this tool may rewrite."""
        return f"{self.base}..HEAD" if self.base else "HEAD"

    def painted_shas(self) -> set[str]:
        if not self.head():
            return set()
        out = self.text("rev-list", self.own_range(), f"--grep=^{TRAILER_KEY}: true$")
        return set(out.split())

    def _counts(self, rev: str) -> tuple[dict[str, int], dict[str, int]]:
        painted: dict[str, int] = {}
        other: dict[str, int] = {}
        fmt = f"%ad%x09%(trailers:key={TRAILER_KEY},valueonly,separator=%x2C)"
        # format: keeps the commit's own time zone, like GitHub does.
        out = self.text("log", rev, "--date=format:%Y-%m-%d", f"--format={fmt}")
        for line in out.splitlines():
            day, _, trailer = line.partition("\t")
            if not day:
                continue
            bucket = painted if trailer.strip() == "true" else other
            bucket[day] = bucket.get(day, 0) + 1
        return painted, other

    def daily_counts(self) -> dict:
        """Commits per day reachable from HEAD: painted / other, and our own painting."""
        if not self.head():
            return {"painted": {}, "other": {}, "mine": {}}
        painted, other = self._counts("HEAD")
        mine = self._counts(self.own_range())[0] if self.base else painted
        return {"painted": painted, "other": other, "mine": mine}

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
            "mine": counts["mine"],
            "mine_total": sum(counts["mine"].values()),
            "community": self.community,
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
# community wall
# --------------------------------------------------------------------------- #

def github_slug(url: str) -> tuple[str, str] | None:
    """('owner', 'repo') of a github.com remote URL, or None."""
    m = re.search(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$", url or "")
    return (m.group(1), m.group(2)) if m else None


def noreply_emails(login: str, user_id: int | str | None) -> set[str]:
    emails = {f"{login}@users.noreply.github.com".lower()}
    if user_id:
        emails.add(f"{user_id}+{login}@users.noreply.github.com".lower())
    return emails


def fetch_github_user_id(login: str) -> int | None:
    req = urllib.request.Request(f"https://api.github.com/users/{login}",
                                 headers={"User-Agent": "github-activity-painter"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return int(json.load(resp)["id"])
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        return None


def setup_community(repo: Repo, upstream_url: str = UPSTREAM_URL, branch: str = COMMUNITY_BRANCH) -> dict:
    """Prepare `branch` on top of the shared wall and remember it as the base.

    Your painting is then the commits on top of the upstream default branch; a
    pull request with them is checked and merged automatically.
    """
    up = github_slug(upstream_url)
    origin = repo.remote_url()
    mine = github_slug(origin)
    remotes = repo.text("remote").split()
    same = (mine and up and mine[0].lower() == up[0].lower() and mine[1].lower() == up[1].lower()) \
        or origin.rstrip("/") == upstream_url.rstrip("/")
    if same:
        remote = "origin"            # cloned the shared repository itself
    else:
        remote = "upstream"
        if remote not in remotes:
            repo.git("remote", "add", remote, upstream_url)
        elif repo.text("remote", "get-url", remote) != upstream_url:
            repo.git("remote", "set-url", remote, upstream_url)
    print(f"Fetching the community wall from {upstream_url} ...")
    repo.git("fetch", "--no-tags", remote)
    head_ref = repo.text("ls-remote", "--symref", remote, "HEAD")
    m = re.search(r"ref: refs/heads/(\S+)\s+HEAD", head_ref)
    if not m:
        raise PainterError(f"could not find the default branch of {upstream_url}")
    default = m.group(1)
    base = f"refs/remotes/{remote}/{default}"
    current = repo.text("symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if current != branch:
        if repo.text("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False):
            repo.git("switch", branch)
        else:
            repo.git("switch", "-c", branch, base)
    repo.base = base

    user_id = fetch_github_user_id(mine[0]) if mine else None
    pr_url = None
    if up and mine and remote == "upstream":
        pr_url = f"https://github.com/{up[0]}/{up[1]}/compare/{default}...{mine[0]}:{mine[1]}:{branch}?expand=1"
    repo.community = {
        "upstream": f"{up[0]}/{up[1]}" if up else upstream_url,
        "default_branch": default,
        "branch": branch,
        "fork": f"{mine[0]}/{mine[1]}" if mine else None,
        "can_open_pr": pr_url is not None,
        "pr_url": pr_url,
        "login": mine[0] if mine and remote == "upstream" else None,
        "suggested_email": (f"{user_id}+{mine[0]}@users.noreply.github.com"
                            if user_id and remote == "upstream" else None),
    }
    return repo.community


def validate_painting(repo: Repo, base: str, head: str, login: str, user_id: int | str | None,
                      today: dt.date | None = None, max_commits: int = 5000,
                      max_per_day: int = 100) -> dict:
    """Check that base..head is nothing but a painting by `login`.

    Only reads commit metadata: nothing from the commits is checked out or run.
    Returns {"verdict": "valid" | "invalid" | "not-a-painting", "problems": [...], ...}.
    """
    today = today or dt.date.today()
    oldest = today - dt.timedelta(days=372)
    allowed = noreply_emails(login, user_id)
    expected = f"{user_id}+{login}@users.noreply.github.com" if user_id else f"{login}@users.noreply.github.com"
    raw = repo.git("log", "--no-renames", "--name-only", "--date=format:%Y-%m-%d",
                   "--format=%x1e%H%x00%P%x00%ae%x00%ad%x00%B%x00", f"{base}..{head}")
    records = [r for r in raw.decode(errors="replace").split("\x1e") if r.strip()]
    found: dict[str, list] = {}     # kind -> [first message, count]

    def problem(kind: str, message: str) -> None:
        entry = found.setdefault(kind, [message, 0])
        entry[1] += 1

    per_day: dict[str, int] = {}
    painted = 0
    for rec in records:
        sha, parents, email, day, body, files = (rec.split("\x00") + [""] * 6)[:6]
        short = sha[:8]
        if TRAILER in body:
            painted += 1
        if len(parents.split()) != 1:
            problem("merge", f"`{short}` is a merge commit")
        if files.strip():
            changed = ", ".join(f"`{f}`" for f in files.split()[:5])
            problem("files", f"`{short}` changes files ({changed}): painted commits must be empty")
        msg = PAINT_MESSAGE.fullmatch(body.strip("\n") + "\n")
        if not msg:
            problem("message", f"`{short}` does not have a painting commit message")
        elif msg.group(1) != day:
            problem("date-msg", f"`{short}` is dated {day} but its message says {msg.group(1)}")
        if email.lower() not in allowed:
            problem("email", f"`{short}` is authored by `{email}` instead of your GitHub no-reply "
                             f"address `{expected}`")
        try:
            when = dt.date.fromisoformat(day)
            if not oldest <= when <= today + dt.timedelta(days=1):
                problem("window", f"`{short}` is dated {day}, outside the last year")
        except ValueError:
            problem("date", f"`{short}` has an invalid date")
        per_day[day] = per_day.get(day, 0) + 1

    problems = [msg if n == 1 else f"{msg} (and {n - 1} more commit{'s' if n > 2 else ''} like this)"
                for msg, n in found.values()]
    if not records or not painted:
        return {"verdict": "not-a-painting", "problems": [], "commits": len(records), "days": 0}
    if len(records) > max_commits:
        problems.append(f"{len(records)} commits: the limit is {max_commits} per pull request")
    busy = sorted(d for d, n in per_day.items() if n > max_per_day)
    if busy:
        problems.append(f"more than {max_per_day} commits on {len(busy)} day(s), e.g. {busy[0]}")
    return {"verdict": "invalid" if problems else "valid", "problems": problems,
            "commits": len(records), "days": len(per_day)}


def year_window(today: dt.date) -> tuple[dt.date, dt.date, int]:
    """(first day shown, first Sunday column, number of weeks) like the GitHub graph."""
    try:
        start = today.replace(year=today.year - 1)
    except ValueError:  # Feb 29
        start = today.replace(year=today.year - 1, day=28)
    first = start - dt.timedelta(days=(start.weekday() + 1) % 7)
    cols = ((today - first).days) // 7 + 1
    return start, first, cols


def wall_svg(repo: Repo, today: dt.date | None = None) -> str:
    """SVG picture of every painted commit of the branch (the community wall)."""
    today = today or dt.date.today()
    start, first, cols = year_window(today)
    counts: dict[str, int] = {}
    painters: set[str] = set()
    if repo.head():
        out = repo.text("log", "HEAD", f"--grep=^{TRAILER_KEY}: true$", "--date=format:%Y-%m-%d",
                        f"--since={(start - dt.timedelta(days=2)).isoformat()}", "--format=%ad %ae")
        for line in out.splitlines():
            day, _, email = line.partition(" ")
            if start.isoformat() <= day <= today.isoformat():
                counts[day] = counts.get(day, 0) + 1
                painters.add(email.lower())
    top = max(counts.values(), default=0)
    step, size, left, top_pad = 14, 11, 32, 22
    width, height = left + cols * step + 8, top_pad + 7 * step + 28
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif" font-size="10">',
        "<style>.t{fill:#59636e}.l0{fill:#ebedf0}.l1{fill:#9be9a8}.l2{fill:#40c463}.l3{fill:#30a14e}.l4{fill:#216e39}"
        "@media (prefers-color-scheme:dark){.t{fill:#9198a1}.l0{fill:#151b23}.l1{fill:#033a16}.l2{fill:#196c2e}"
        ".l3{fill:#2ea043}.l4{fill:#56d364}}</style>",
    ]
    for row, name in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        parts.append(f'<text class="t" x="0" y="{top_pad + row * step + 9}">{name}</text>')
    last_month, last_label = None, -9
    for c in range(cols):
        days = [first + dt.timedelta(days=c * 7 + r) for r in range(7)]
        shown = [d for d in days if start <= d <= today]
        if shown and shown[-1].month != last_month:
            last_month = shown[-1].month
            if c - last_label >= 3 and c < cols - 1:
                parts.append(f'<text class="t" x="{left + c * step}" y="12">{shown[-1].strftime("%b")}</text>')
                last_label = c
        for r, d in enumerate(days):
            if not start <= d <= today:
                continue
            n = counts.get(d.isoformat(), 0)
            level = 0 if not n else max(1, min(4, -(-4 * n // top)))
            parts.append(f'<rect class="l{level}" x="{left + c * step}" y="{top_pad + r * step}" '
                         f'width="{size}" height="{size}" rx="2"><title>{n} painted commits on {d.isoformat()}</title></rect>')
    total = sum(counts.values())
    parts.append(f'<text class="t" x="{left}" y="{height - 6}">{len(painters)} painter{"s" if len(painters) != 1 else ""}'
                 f' · {total} painted commits · updated {today.isoformat()}</text>')
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


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
                    result = {"output": self.repo.push(),
                              "pr_url": (self.repo.community or {}).get("pr_url")}
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
    parser.add_argument("--base", help="never touch commits reachable from this ref (e.g. upstream/main)")
    sub = parser.add_subparsers(dest="cmd")
    p_serve = sub.add_parser("serve", help="start the web interface (default)")
    p_serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    p_serve.add_argument("--no-browser", action="store_true", help="do not open a browser")
    p_comm = sub.add_parser("community", help="paint on the shared community wall (opens a pull request)")
    p_comm.add_argument("--port", type=int, default=DEFAULT_PORT)
    p_comm.add_argument("--no-browser", action="store_true", help="do not open a browser")
    p_comm.add_argument("--upstream", default=UPSTREAM_URL, help="shared repository URL")
    p_comm.add_argument("--branch", default=COMMUNITY_BRANCH, help="local branch for your painting")
    p_val = sub.add_parser("validate", help="(CI) check that BASE..HEAD only contains a painting")
    p_val.add_argument("--base", dest="val_base", required=True)
    p_val.add_argument("--head", required=True)
    p_val.add_argument("--login", required=True, help="GitHub login of the pull request author")
    p_val.add_argument("--user-id", default="", help="GitHub numeric user id of the author")
    p_val.add_argument("--json", dest="json_out", help="write the result as JSON to this file")
    p_wall = sub.add_parser("wall", help="draw every painted commit of the branch as an SVG")
    p_wall.add_argument("output", help="SVG file to write")
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
        repo.base = args.base
        cmd = args.cmd or "serve"
        if cmd == "serve":
            serve(repo, getattr(args, "port", DEFAULT_PORT), not getattr(args, "no_browser", False))
        elif cmd == "community":
            info = setup_community(repo, args.upstream, args.branch)
            print(f"Community mode: painting on branch '{info['branch']}' on top of {info['upstream']}.")
            serve(repo, args.port, not args.no_browser)
        elif cmd == "validate":
            result = validate_painting(repo, args.val_base, args.head, args.login, args.user_id)
            if args.json_out:
                Path(args.json_out).write_text(json.dumps(result))
            print(f"{result['verdict']}: {result['commits']} commits on {result['days']} days")
            for problem in result["problems"]:
                print(f"  - {problem}")
        elif cmd == "wall":
            Path(args.output).write_text(wall_svg(repo))
            print(f"Wrote {args.output}")
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
