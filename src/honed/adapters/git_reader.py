"""CodeReader on a bare, blobless, shallow clone of one repo (`git clone --bare --filter=blob:none --depth=1`).

Commits are fetched on demand (depth 1: trees, no blobs). Blobs are fetched in batches by `prefetch`, and every
other git call runs with GIT_NO_LAZY_FETCH=1, so git never falls back to downloading blobs one at a time. A `read`
of a file that was not prefetched fetches just that blob.

Repos hold files in any encoding (Latin-1, cp1252, ...), so git output is never assumed to be valid UTF-8: every call
decodes as UTF-8 with replacement characters and never raises on a stray byte.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path

from honed.core.types import FilePatch, GrepHit
from honed.ports.code_reader import BlameLine, ReaderError, ReaderUnavailable

log = logging.getLogger(__name__)

_NO_LAZY = {"GIT_NO_LAZY_FETCH": "1"}
# No automatic gc or maintenance after a fetch: git runs it detached, and a background gc rewriting the `shallow` file
# breaks the next fetch ("shallow file has changed since we read it"). Fetched packs need no gc.
_NO_AUTO_GC = {"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "gc.auto", "GIT_CONFIG_VALUE_0": "0",
               "GIT_CONFIG_KEY_1": "maintenance.auto", "GIT_CONFIG_VALUE_1": "false"}  # fmt: skip
_RACE = "shallow file has changed since we read it"
_FETCH_BATCH = 2000


_NETWORK = re.compile(
    r"could not resolve host|unable to access|failed to connect|couldn't connect"
    r"|connection (?:timed out|refused|reset)|operation timed out|network is unreachable|early EOF"
    r"|the remote end hung up|ssl_error|could not read from remote",
    re.I,
)


class GitError(ReaderError):
    pass


class GitUnavailable(GitError, ReaderUnavailable):
    """The remote could not be reached: stop, don't degrade."""


def _utf8(data: bytes) -> str:
    """Bytes from git as text: UTF-8, with U+FFFD for anything that isn't."""
    return data.decode("utf-8", errors="replace")


def _raise_if_offline(stderr: str | bytes, what: str) -> None:
    text = stderr if isinstance(stderr, str) else _utf8(stderr)
    if _NETWORK.search(text):
        raise GitUnavailable(f"{what}: {text.strip()[:300]}")


def parse_diff(text: str) -> dict[str, str | None]:
    """`git diff` output to {path: hunks} (None for a binary file, "" for one with no hunks, like a pure rename)."""
    out: dict[str, str | None] = {}
    for chunk in re.split(r"(?m)^diff --git ", text)[1:]:
        head, sep, hunks = chunk.partition("\n@@")
        old = new = None
        binary = False
        for line in head.split("\n"):
            if line.startswith("--- "):
                old = None if line[4:] == "/dev/null" else line[4:].removeprefix("a/")
            elif line.startswith("+++ "):
                new = None if line[4:] == "/dev/null" else line[4:].removeprefix("b/")
            elif line.startswith("rename from "):
                old = line[len("rename from ") :]
            elif line.startswith("rename to "):
                new = line[len("rename to ") :]
            elif line.startswith("Binary files ") or line == "GIT binary patch":
                binary = True
        if old is None and new is None:  # a mode change or a pure rename without ---/+++ lines
            first = head.split("\n", 1)[0]
            _, _, new = first.partition(" b/")
        path = new or old
        if not path:
            continue
        out[path] = None if binary else ("@@" + hunks.rstrip("\n") if sep else "")
    return out


class GitReader:
    def __init__(self, remote_url: str, clone_dir: Path, *, timeout_s: float = 900) -> None:
        self._url = remote_url
        self._dir = clone_dir
        self._timeout = timeout_s
        self._commits: set[str] = set()

    # ---- clone lifecycle -----------------------------------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._dir

    def ensure_clone(self) -> None:
        if (self._dir / "HEAD").exists():
            return
        self._dir.parent.mkdir(parents=True, exist_ok=True)
        log.info("cloning %s (bare, blobless, depth 1)", self._url)
        self._run(
            ["git", "clone", "--quiet", "--bare", "--filter=blob:none", "--depth=1", "--no-tags", self._url,
             str(self._dir)],
            cwd=None,
        )  # fmt: skip

    def ensure_commit(self, commit: str) -> None:
        """Fetch `commit` (with its trees, without blobs) unless the clone has it."""
        if commit in self._commits:
            return
        self.ensure_clone()
        probe = self._git(["cat-file", "-e", f"{commit}^{{commit}}"], check=False)
        if probe.returncode != 0:
            self._git(["fetch", "--quiet", "--depth=1", "--filter=blob:none", "--no-tags", "origin", commit],
                      lazy=True)  # fmt: skip
        self._commits.add(commit)

    def remove(self) -> None:
        shutil.rmtree(self._dir, ignore_errors=True)
        self._commits.clear()

    def disk_usage(self) -> int:
        """Bytes the clone occupies on disk."""
        if not self._dir.exists():
            return 0
        return sum(f.stat().st_size for f in self._dir.rglob("*") if f.is_file())

    # ---- CodeReader ----------------------------------------------------------------------------------------

    def read(self, path: str, commit: str) -> str | None:
        self.ensure_commit(commit)
        result = self._git(["cat-file", "blob", f"{commit}:{path}"], check=False, lazy=True, text=False)
        if result.returncode != 0:
            _raise_if_offline(result.stderr, f"reading {path} at {commit[:10]}")
        if result.returncode != 0 or b"\0" in result.stdout[:8192]:
            return None
        return _utf8(result.stdout)

    def grep(
        self,
        pattern: str,
        commit: str,
        paths: Sequence[str] | None = None,
        *,
        word: bool = False,
        max_per_file: int | None = None,
    ) -> list[GrepHit]:
        self._prefetch(commit, list(paths) if paths is not None else None)
        args = ["grep", "-n", "-I", "-z", "-P", "-e", pattern]
        if word:
            args.insert(1, "-w")
        if max_per_file:
            args[1:1] = ["-m", str(max_per_file)]
        # Bytes, split on git's own record separators before decoding: a matched line may hold non-UTF-8 bytes
        # (`-I` skips only files with a NUL) or a bare CR, which text mode would turn into a record break.
        result = self._git([*args, commit, "--", *(paths or [])], check=False, text=False)
        if result.returncode not in (0, 1):
            raise GitError(f"git grep failed: {_utf8(result.stderr).strip()[:300]}")
        hits = []
        prefix = f"{commit}:"
        for record in result.stdout.split(b"\n"):
            parts = record.split(b"\0", 2)
            if len(parts) != 3 or not parts[1].isdigit():
                continue
            name, line, text = parts
            hits.append(GrepHit(path=_utf8(name).removeprefix(prefix), line=int(line),
                                text=_utf8(text).removesuffix("\r")))  # fmt: skip
        return hits

    def list_files(self, commit: str, prefix: str = "") -> list[str]:
        self.ensure_commit(commit)
        result = self._git(["ls-tree", "-r", "-z", "--name-only", commit, "--", *([prefix] if prefix else [])])
        return [p for p in result.stdout.split("\0") if p]

    def prefetch(self, commit: str, paths: Sequence[str]) -> None:
        """Download, in batches, the missing blobs under `paths` at `commit`. No paths, no download."""
        if paths:
            self._prefetch(commit, list(paths))

    def _prefetch(self, commit: str, paths: list[str] | None) -> None:
        """As `prefetch`, where None means the whole tree."""
        self.ensure_commit(commit)
        listing = self._git(["ls-tree", "-r", "-z", commit, "--", *(paths or [])]).stdout
        oids = [entry.split(" ", 2)[2].split("\t", 1)[0] for entry in listing.split("\0") if " blob " in entry]
        if not oids:
            return
        check = self._git(["cat-file", "--batch-check"], stdin="\n".join(oids) + "\n")
        missing = [line.split(" ", 1)[0] for line in check.stdout.splitlines() if line.endswith(" missing")]
        for start in range(0, len(missing), _FETCH_BATCH):
            batch = missing[start : start + _FETCH_BATCH]
            self._git(
                ["-c", "fetch.negotiationAlgorithm=noop", "fetch", "--quiet", "origin", "--no-tags",
                 "--no-write-fetch-head", "--recurse-submodules=no", "--filter=blob:none", "--stdin"],
                stdin="\n".join(batch) + "\n",
                lazy=True,
            )  # fmt: skip
        if missing:
            log.debug("prefetched %d blobs at %s", len(missing), commit[:10])

    # ---- Differ (rehydrate) ---------------------------------------------------------------------------------

    def diff(self, base: str, head: str, paths: Sequence[str]) -> dict[str, str | None]:
        """`git diff -M base head -- paths`, split per file, headers dropped: the shape of the host's patches. Hunks
        one line apart are merged (`--inter-hunk-context=1`), as GitHub does: on data-dev 305 of 323 patches came back
        byte-identical to GitHub's (the rest differ in where the diff algorithm puts hunk boundaries)."""
        wanted = sorted({p for p in paths if p})
        if not wanted:
            return {}
        for commit in (base, head):
            self.prefetch(commit, wanted)
        args = ["-c", "core.quotePath=false", "diff", "--no-color", "--no-ext-diff", "--src-prefix=a/",
                "--dst-prefix=b/", "-M", "-U3", "--inter-hunk-context=1", base, head, "--", *wanted]  # fmt: skip
        text = self._git(args, lazy=True).stdout
        return parse_diff(text)

    # ---- History (escaped-defect mining) ----------------------------------------------------------------------

    def deepen(self, commit: str, since: str) -> None:
        """Fetch `commit` with its history back to `since` (trees only; blobs arrive lazily)."""
        self.ensure_clone()
        self._git(["fetch", "--quiet", f"--shallow-since={since}", "--filter=blob:none", "--no-tags", "origin",
                   commit], lazy=True)  # fmt: skip
        self._commits.add(commit)

    def commit_diff(self, commit: str) -> list[FilePatch]:
        """`commit` against its first parent, zero-context hunks (renames followed)."""
        names = self._git(["diff", "--name-status", "-M", f"{commit}^", commit], lazy=True).stdout
        files = []
        for row in names.splitlines():
            parts = row.split("\t")
            status, path = parts[0], parts[-1]
            previous = parts[1] if status.startswith("R") and len(parts) == 3 else None
            text = self._git(["diff", "-U0", "--no-color", "-M", f"{commit}^", commit, "--", *([previous] if previous
                              else []), path], lazy=True).stdout  # fmt: skip
            hunks = text[text.index("@@") :] if "@@" in text else None
            kind = {"A": "added", "D": "removed", "R": "renamed"}.get(status[:1], "modified")
            files.append(FilePatch(path, kind, hunks, previous))
        return files

    def blame(self, path: str, commit: str, start: int, end: int) -> list[BlameLine]:
        result = self._git(["blame", "--porcelain", "-L", f"{start},{end}", commit, "--", path], check=False,
                           lazy=True)  # fmt: skip
        if result.returncode != 0:
            _raise_if_offline(result.stderr, f"blame {path} at {commit[:10]}")
            raise GitError(f"blame {path} at {commit[:10]} failed: {result.stderr.strip()[:300]}")
        out: list[BlameLine] = []
        boundary: set[str] = set()
        current: tuple[str, int, int] | None = None
        for line in result.stdout.split("\n"):
            head = line.split(" ")
            if len(head) >= 3 and len(head[0]) == 40 and all(c in "0123456789abcdef" for c in head[0]):
                current = (head[0], int(head[1]), int(head[2]))
            elif line == "boundary" and current is not None:
                boundary.add(current[0])
            elif line.startswith("\t") and current is not None:
                out.append(BlameLine(current[2], current[0], current[1], line[1:]))
        return [BlameLine(b.line, b.commit, b.original_line, b.text, b.commit in boundary) for b in out]

    def message(self, commit: str) -> str:
        return self._git(["log", "-1", "--format=%B", commit], lazy=True).stdout

    # ---- plumbing ------------------------------------------------------------------------------------------

    def _git(
        self,
        args: list[str],
        *,
        check: bool = True,
        lazy: bool = False,
        stdin: str | None = None,
        text: bool = True,
    ) -> subprocess.CompletedProcess:
        return self._run(["git", "-C", str(self._dir), *args], cwd=None, check=check, lazy=lazy, stdin=stdin, text=text)

    def _run(
        self,
        cmd: list[str],
        *,
        cwd: Path | None,
        check: bool = True,
        lazy: bool = True,
        stdin: str | None = None,
        text: bool = True,
    ) -> subprocess.CompletedProcess:
        env = {**os.environ, **({} if lazy else _NO_LAZY), **_NO_AUTO_GC, "GIT_TERMINAL_PROMPT": "0"}
        # text=True alone would decode strictly in the locale's encoding and raise on the first non-UTF-8 byte.
        decoding = {"encoding": "utf-8", "errors": "replace"} if text else {}
        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd, cwd=cwd, env=env, input=stdin, capture_output=True, timeout=self._timeout, check=False,
                    **decoding,
                )  # fmt: skip
            except subprocess.TimeoutExpired as error:
                raise GitUnavailable(f"timed out: {' '.join(cmd[:6])}") from error
            stderr = result.stderr if text else _utf8(result.stderr)
            if result.returncode == 0 or _RACE not in stderr or attempt == 2:
                break
            log.info("git: %s; retrying", _RACE)  # another git process (an older detached gc) touched the clone
            time.sleep(1 + attempt)
        if check and result.returncode != 0:
            stderr = result.stderr if text else _utf8(result.stderr)
            _raise_if_offline(stderr, " ".join(cmd[3:7]))
            raise GitError(f"{' '.join(cmd[3:7])} failed: {stderr.strip()[:300]}")
        return result
