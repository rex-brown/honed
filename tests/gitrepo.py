"""A throwaway git repository to act as a remote for GitReader tests."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": os.devnull}  # fmt: skip


class GitRepo:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True)
        self.git("init", "-q", "-b", "main")
        # Let partial clones filter blobs and fetch commits by id, as GitHub does.
        self.git("config", "uploadpack.allowFilter", "true")
        self.git("config", "uploadpack.allowAnySHA1InWant", "true")

    @property
    def url(self) -> str:
        return self.root.as_uri()

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.root, env=_ENV, check=True, capture_output=True, text=True
        ).stdout.strip()

    def commit(self, files: dict[str, str | bytes | None], message: str = "change") -> str:
        """Write (or delete, for None) files and commit; returns the commit id. Bytes are written verbatim."""
        for path, text in files.items():
            target = self.root / path
            if text is None:
                target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(text, bytes):
                target.write_bytes(text)
            else:
                target.write_text(text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def diff(self, base: str, head: str, path: str) -> str:
        """The zero-context hunks of `path` between two commits, as a host returns them."""
        out = self.git("diff", "-U0", base, head, "--", path)
        return out[out.index("@@") :] if "@@" in out else ""
