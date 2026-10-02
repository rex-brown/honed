"""A bundle file (`ports.bundle`): compressed JSON lines. The first line is the manifest, every later line one
record, `{"kind": ..., <the record's fields>}`. `.zst` (Zstandard, Python 3.14's `compression.zstd`) or `.gz`
(gzip) by the file name on export; the content decides on import. Records are spooled to a temporary file while they
are written, so the manifest, which counts them, can come first."""

from __future__ import annotations

import gzip
import io
import json
import os
import shutil
import tempfile
from collections import Counter
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import IO, Any

from honed.adapters.codec import from_json, to_json
from honed.ports.bundle import (
    BUNDLE_SCHEMA,
    BenchmarkRecord,
    BundleError,
    CachedAnswerRecord,
    DecisionRecord,
    DefectRecord,
    GoldRecord,
    JudgedLabelsRecord,
    JudgmentRecord,
    Manifest,
    PolicyVersionRecord,
    PRRecord,
    Record,
    RoundDiffRecord,
    RoundGoldRecord,
    SplitRecord,
)

KINDS: dict[str, type] = {
    "pr": PRRecord, "judgment": JudgmentRecord, "judged_labels": JudgedLabelsRecord, "gold": GoldRecord,
    "round_gold": RoundGoldRecord, "escaped_defect": DefectRecord, "decision": DecisionRecord,
    "policy_version": PolicyVersionRecord, "split": SplitRecord, "round_diff": RoundDiffRecord,
    "benchmark": BenchmarkRecord, "cached_answer": CachedAnswerRecord,
}  # fmt: skip
_KIND_OF = {cls: kind for kind, cls in KINDS.items()}
_GZIP_MAGIC, _ZSTD_MAGIC = b"\x1f\x8b", b"\x28\xb5\x2f\xfd"


def _zstd() -> Any:
    try:
        from compression import zstd  # type: ignore[import-not-found]  # Python 3.14+
    except ImportError:
        raise BundleError("Zstandard bundles need Python 3.14 or later; use a .gz file name for gzip") from None
    return zstd


def _compressed_writer(path: Path) -> IO[bytes]:
    if path.suffix in (".zst", ".zstd"):
        return _zstd().open(path, "wb", level=10)
    if path.suffix == ".gz":
        return gzip.open(path, "wb", compresslevel=9)
    raise BundleError(f"{path.name}: name the bundle .jsonl.gz (gzip) or .jsonl.zst (Zstandard)")


def _compressed_reader(path: Path) -> IO[bytes]:
    with path.open("rb") as handle:
        magic = handle.read(4)
    if magic.startswith(_GZIP_MAGIC):
        return gzip.open(path, "rb")
    if magic == _ZSTD_MAGIC:
        return _zstd().open(path, "rb")
    raise BundleError(f"{path}: not a gzip- or Zstandard-compressed bundle")


class BundleFileWriter:
    def __init__(self, path: Path) -> None:
        if path.suffix not in (".gz", ".zst", ".zstd"):
            raise BundleError(f"{path.name}: name the bundle .jsonl.gz (gzip) or .jsonl.zst (Zstandard)")
        if path.suffix != ".gz":
            _zstd()  # fail before any work when Zstandard isn't available
        self._path = path
        self._spool = tempfile.TemporaryFile("w+b")  # noqa: SIM115 -- closed by close()
        self._counts: Counter[str] = Counter()

    def write(self, record: Record) -> None:
        kind = _KIND_OF[type(record)]
        line = json.dumps({"kind": kind, **to_json(record)}, sort_keys=True, ensure_ascii=False)
        self._spool.write(line.encode() + b"\n")
        self._counts[kind] += 1

    def close(self, manifest: Manifest) -> Manifest:
        """Write the file (atomically: a partial file is renamed into place) and return the manifest written."""
        manifest = replace(manifest, counts=dict(sorted(self._counts.items())))
        self._path.parent.mkdir(parents=True, exist_ok=True)
        partial = self._path.with_name(f".{self._path.name}.partial{self._path.suffix}")
        try:
            with _compressed_writer(partial) as out:
                out.write(json.dumps({"kind": "manifest", **to_json(manifest)}, sort_keys=True).encode() + b"\n")
                self._spool.seek(0)
                shutil.copyfileobj(self._spool, out)
            os.replace(partial, self._path)
        finally:
            self._spool.close()
            partial.unlink(missing_ok=True)
        return manifest


class BundleFileReader:
    def __init__(self, path: Path) -> None:
        self._path = path
        with _compressed_reader(path) as raw:
            first = io.TextIOWrapper(raw, encoding="utf-8").readline()
        try:
            data = json.loads(first)
        except json.JSONDecodeError:
            raise BundleError(f"{path}: the first line is not a manifest") from None
        if data.pop("kind", None) != "manifest":
            raise BundleError(f"{path}: the first line is not a manifest")
        schema = data.get("bundle_schema")
        if not isinstance(schema, int) or schema > BUNDLE_SCHEMA:
            raise BundleError(f"{path}: bundle schema {schema!r} is newer than this code reads ({BUNDLE_SCHEMA}); "
                              "update honed to import it")  # fmt: skip
        self._manifest = from_json(Manifest, data)

    def manifest(self) -> Manifest:
        return self._manifest

    def records(self) -> Iterator[Record]:
        with _compressed_reader(self._path) as raw:
            lines = io.TextIOWrapper(raw, encoding="utf-8")
            lines.readline()  # the manifest
            for n, line in enumerate(lines, 2):
                if not line.strip():
                    continue
                data = json.loads(line)
                kind = data.pop("kind", None)
                if kind not in KINDS:
                    raise BundleError(f"{self._path}: line {n}: unknown record kind {kind!r}")
                yield from_json(KINDS[kind], data)
