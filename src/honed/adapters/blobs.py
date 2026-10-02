"""Content-addressed blob directory: `<root>/ab/cd/<sha256>`, zlib-compressed. The digest is of the raw bytes."""

from __future__ import annotations

import hashlib
import os
import tempfile
import zlib
from pathlib import Path


class BlobStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def _path(self, digest: str) -> Path:
        return self._root / digest[:2] / digest[2:4] / digest

    def put(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        path = self._path(digest)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
                tmp.write(zlib.compress(data, 6))
            os.replace(tmp.name, path)
        return digest

    def get(self, digest: str) -> bytes:
        try:
            return zlib.decompress(self._path(digest).read_bytes())
        except FileNotFoundError:
            raise KeyError(digest) from None

    def exists(self, digest: str) -> bool:
        return self._path(digest).exists()

    def disk_usage(self) -> int:
        """Bytes the blob directory occupies on disk."""
        return sum(f.stat().st_size for f in self._root.rglob("*") if f.is_file()) if self._root.exists() else 0
