"""Store on one SQLite file, with large text (diff hunks, patches, pack files and manifests) in a `BlobStore`.

Upserting a PR replaces it and all of its child rows in one transaction, so re-harvesting updates in place.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from honed.adapters.blobs import BlobStore
from honed.adapters.codec import from_json, to_json
from honed.core.benchmarks import BenchmarkPR
from honed.core.evals import Decision, EscapedDefect, EvalRun
from honed.core.improve import CandidateRecord, PolicyRecord
from honed.core.types import (
    Actor,
    Addressed,
    AuthorKind,
    Comment,
    CommitInfo,
    Compare,
    CompareSource,
    ContextPack,
    Corpus,
    Cursor,
    DateWindow,
    FilePatch,
    GoldIssue,
    GoldProvenance,
    GoldSet,
    HarvestedPR,
    JudgedLabel,
    Judgment,
    JudgmentKind,
    LineBasis,
    Outcome,
    Polarity,
    PRFact,
    PriorThread,
    PRKey,
    PRSource,
    PullRequest,
    Reaction,
    Review,
    SampledAs,
    Severity,
    Stance,
    Strength,
    Thread,
    ThreadFact,
    ThreadLabel,
)

# 2: judgments, judged labels, gold sets; 3: bug-targeted sampling; 4: evaluation tables; 5: human labels, improve-loop
# candidates and policy versions; 6: a PR's source (corpus or benchmark) and fixed split, review-round diffs (bundles),
# benchmark records; human labels move to the yardstick (`yardstick/human_labels/`), their table is no longer used
SCHEMA_VERSION = 6

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS prs (
  repo TEXT NOT NULL, number INTEGER NOT NULL, title TEXT NOT NULL, url TEXT, body TEXT,
  author_login TEXT, author_type TEXT, author_kind TEXT NOT NULL,
  created_at TEXT NOT NULL, landed_at TEXT, base_ref TEXT, base_oid TEXT, head_oid TEXT,
  additions INTEGER, deletions INTEGER, changed_files INTEGER, force_pushes INTEGER, commit_count INTEGER,
  language TEXT NOT NULL, corpus TEXT NOT NULL, reviewed_commit TEXT NOT NULL, harvested_at TEXT NOT NULL,
  sampled_as TEXT NOT NULL DEFAULT 'general', source TEXT NOT NULL DEFAULT 'corpus', split TEXT,
  PRIMARY KEY (repo, number));
CREATE TABLE IF NOT EXISTS threads (
  id TEXT NOT NULL, repo TEXT NOT NULL, number INTEGER NOT NULL, ord INTEGER NOT NULL, path TEXT NOT NULL,
  is_resolved INTEGER, is_outdated INTEGER, diff_side TEXT, subject_type TEXT,
  line INTEGER, original_line INTEGER, start_line INTEGER, original_start_line INTEGER,
  resolved_by TEXT, comment_count INTEGER,
  author_kind TEXT, outcome TEXT, lines_changed INTEGER, line_basis TEXT, created_at TEXT,
  PRIMARY KEY (repo, number, id));
CREATE INDEX IF NOT EXISTS threads_path_time ON threads (repo, path, created_at);
CREATE TABLE IF NOT EXISTS comments (
  id TEXT NOT NULL, thread_id TEXT NOT NULL, repo TEXT NOT NULL, number INTEGER NOT NULL, ord INTEGER NOT NULL,
  author_login TEXT, author_type TEXT, body TEXT, created_at TEXT, diff_hunk_blob TEXT,
  commit_oid TEXT, original_commit TEXT, line INTEGER, original_line INTEGER, start_line INTEGER,
  original_start_line INTEGER, reactions TEXT, reaction_count INTEGER, PRIMARY KEY (repo, number, id));
CREATE TABLE IF NOT EXISTS reviews (
  id TEXT NOT NULL, repo TEXT NOT NULL, number INTEGER NOT NULL, ord INTEGER NOT NULL,
  author_login TEXT, author_type TEXT, state TEXT, submitted_at TEXT, commit_oid TEXT, PRIMARY KEY (repo, number, id));
CREATE TABLE IF NOT EXISTS commits (
  repo TEXT NOT NULL, number INTEGER NOT NULL, ord INTEGER NOT NULL, oid TEXT NOT NULL,
  committed_date TEXT, authored_date TEXT, message_headline TEXT, PRIMARY KEY (repo, number, ord));
CREATE TABLE IF NOT EXISTS compares (
  id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT NOT NULL, number INTEGER NOT NULL, role TEXT NOT NULL,
  ord INTEGER NOT NULL, base TEXT, head TEXT, status TEXT, merge_base TEXT, complete INTEGER, source TEXT);
CREATE INDEX IF NOT EXISTS compares_pr ON compares (repo, number);
CREATE TABLE IF NOT EXISTS compare_files (
  compare_id INTEGER NOT NULL, ord INTEGER NOT NULL, path TEXT NOT NULL, previous_path TEXT, status TEXT,
  patch_blob TEXT, PRIMARY KEY (compare_id, ord));
CREATE TABLE IF NOT EXISTS cursors (
  repo TEXT NOT NULL, corpus TEXT NOT NULL, window TEXT NOT NULL, after TEXT, offset INTEGER NOT NULL,
  exhausted INTEGER NOT NULL, sample TEXT NOT NULL DEFAULT 'general', PRIMARY KEY (repo, corpus, window, sample));
CREATE TABLE IF NOT EXISTS packs (
  repo TEXT NOT NULL, number INTEGER NOT NULL, base_commit TEXT, head_commit TEXT, manifest_blob TEXT NOT NULL,
  files INTEGER, bytes INTEGER, built_at TEXT, PRIMARY KEY (repo, number));
CREATE TABLE IF NOT EXISTS judgments (
  repo TEXT NOT NULL, number INTEGER NOT NULL, thread_id TEXT NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL,
  reason TEXT, category TEXT, model TEXT, judged_at TEXT NOT NULL, PRIMARY KEY (repo, number, thread_id, kind));
CREATE TABLE IF NOT EXISTS judged_labels (
  repo TEXT NOT NULL, number INTEGER NOT NULL, thread_id TEXT NOT NULL, ord INTEGER NOT NULL, author_kind TEXT,
  outcome TEXT NOT NULL, polarity TEXT, strength TEXT, applied_suggestion INTEGER, addressed TEXT, stance TEXT,
  category TEXT, high_risk_dismissal INTEGER, PRIMARY KEY (repo, number, thread_id));
CREATE TABLE IF NOT EXISTS gold_sets (
  repo TEXT NOT NULL, number INTEGER NOT NULL, reviewed_commit TEXT NOT NULL, candidates INTEGER NOT NULL,
  excluded_later_round TEXT NOT NULL, excluded_unreadable TEXT NOT NULL, built_at TEXT NOT NULL,
  PRIMARY KEY (repo, number));
CREATE TABLE IF NOT EXISTS gold_issues (
  repo TEXT NOT NULL, number INTEGER NOT NULL, ord INTEGER NOT NULL, id TEXT NOT NULL, path TEXT NOT NULL,
  start_line INTEGER, end_line INTEGER, severity TEXT NOT NULL, category TEXT, provenance TEXT NOT NULL,
  conf REAL NOT NULL, description TEXT, source_threads TEXT NOT NULL, PRIMARY KEY (repo, number, ord));
CREATE TABLE IF NOT EXISTS round_packs (
  repo TEXT NOT NULL, number INTEGER NOT NULL, head_commit TEXT NOT NULL, manifest_blob TEXT NOT NULL,
  files INTEGER, bytes INTEGER, built_at TEXT, PRIMARY KEY (repo, number, head_commit));
CREATE TABLE IF NOT EXISTS round_gold (
  repo TEXT NOT NULL, number INTEGER NOT NULL, rounds INTEGER NOT NULL, commit_oid TEXT NOT NULL,
  gold_blob TEXT NOT NULL, built_at TEXT NOT NULL, PRIMARY KEY (repo, number, rounds, commit_oid));
CREATE TABLE IF NOT EXISTS eval_runs (
  id TEXT PRIMARY KEY, policy_hash TEXT NOT NULL, split TEXT NOT NULL, backend TEXT NOT NULL,
  rounds INTEGER NOT NULL, sample INTEGER NOT NULL, created_at TEXT NOT NULL, complete INTEGER NOT NULL,
  run_blob TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS eval_runs_key ON eval_runs (policy_hash, split, backend, rounds, sample, created_at);
CREATE TABLE IF NOT EXISTS incumbents (
  split TEXT NOT NULL, backend TEXT NOT NULL, rounds INTEGER NOT NULL, run_id TEXT NOT NULL, set_at TEXT NOT NULL,
  PRIMARY KEY (split, backend, rounds));
CREATE TABLE IF NOT EXISTS decision_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, hypothesis TEXT NOT NULL, change TEXT NOT NULL,
  before TEXT NOT NULL, after TEXT NOT NULL, delta TEXT NOT NULL, gate TEXT NOT NULL, verdict TEXT NOT NULL,
  note TEXT);
CREATE TABLE IF NOT EXISTS escaped_defects (
  repo TEXT NOT NULL, number INTEGER NOT NULL, id TEXT NOT NULL, fix_pr INTEGER NOT NULL, record_blob TEXT NOT NULL,
  found_at TEXT NOT NULL, PRIMARY KEY (repo, number, id));
CREATE TABLE IF NOT EXISTS round_diffs (
  repo TEXT NOT NULL, number INTEGER NOT NULL, head_commit TEXT NOT NULL, diff_blob TEXT NOT NULL,
  PRIMARY KEY (repo, number, head_commit));
CREATE TABLE IF NOT EXISTS stripped_patches (
  repo TEXT NOT NULL, number INTEGER NOT NULL, files TEXT NOT NULL, PRIMARY KEY (repo, number));
CREATE TABLE IF NOT EXISTS benchmark_prs (
  repo TEXT NOT NULL, number INTEGER NOT NULL, benchmark TEXT NOT NULL, url TEXT NOT NULL, record_blob TEXT NOT NULL,
  imported_at TEXT NOT NULL, PRIMARY KEY (repo, number));
CREATE TABLE IF NOT EXISTS candidates (
  id TEXT PRIMARY KEY, round INTEGER NOT NULL, created_at TEXT NOT NULL, outcome TEXT NOT NULL,
  policy_hash TEXT NOT NULL, record_blob TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS policy_versions (
  hash TEXT PRIMARY KEY, parent TEXT NOT NULL, created_at TEXT NOT NULL, provisional INTEGER NOT NULL,
  record_blob TEXT NOT NULL);
"""


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def _sampling(db: sqlite3.Connection) -> None:
    """Every PR stored before bug-targeted sampling was sampled generally; cursors gain the sample in their key.
    Tables that _SCHEMA has just created already have both."""
    if "sampled_as" not in _columns(db, "prs"):
        db.execute("ALTER TABLE prs ADD COLUMN sampled_as TEXT NOT NULL DEFAULT 'general'")
    if "sample" not in _columns(db, "cursors"):
        db.execute("ALTER TABLE cursors RENAME TO cursors_v2")
        db.execute(
            "CREATE TABLE cursors (repo TEXT NOT NULL, corpus TEXT NOT NULL, window TEXT NOT NULL, after TEXT,"
            " offset INTEGER NOT NULL, exhausted INTEGER NOT NULL, sample TEXT NOT NULL DEFAULT 'general',"
            " PRIMARY KEY (repo, corpus, window, sample))"
        )
        db.execute(
            "INSERT INTO cursors SELECT repo, corpus, window, after, offset, exhausted, 'general' FROM cursors_v2"
        )
        db.execute("DROP TABLE cursors_v2")


def _source_and_split(db: sqlite3.Connection) -> None:
    """Every PR stored before schema 6 came from the corpus, with a time-ordered split."""
    if "source" not in _columns(db, "prs"):
        db.execute("ALTER TABLE prs ADD COLUMN source TEXT NOT NULL DEFAULT 'corpus'")
    if "split" not in _columns(db, "prs"):
        db.execute("ALTER TABLE prs ADD COLUMN split TEXT")


# Upgrades from each older schema version to the next (SQL, or a function of the connection); new tables come from
# _SCHEMA itself.
_MIGRATIONS: dict[int, list[str | Callable[[sqlite3.Connection], None]]] = {
    1: ["ALTER TABLE commits ADD COLUMN message_headline TEXT"],
    2: [_sampling],
    3: [],  # evaluation tables only
    4: [],  # human labels, candidates and policy versions: new tables only
    5: [_source_and_split],  # round diffs and benchmark records are new tables
}

_THREAD_COMPARE, _REVIEWED_COMPARE = "thread", "reviewed"


class SchemaMismatch(RuntimeError):
    pass


def _actor(login: str | None, typename: str | None) -> Actor | None:
    return Actor(login, typename or "User") if login else None


def _login(actor: Actor | None) -> tuple[str | None, str | None]:
    return (actor.login, actor.typename) if actor else (None, None)


class _Rows:
    """A statement's result, fetched while the connection was held."""

    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self._rows = cursor.fetchall()
        self.lastrowid = cursor.lastrowid
        self.rowcount = cursor.rowcount

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[Any]:
        return list(self._rows)

    def __iter__(self) -> Iterator[Any]:
        return iter(self._rows)


class _Serialized:
    """One connection shared by the job runner's worker threads (the evaluation's gold and round jobs read the store
    in parallel): each statement, and each transaction as a whole, runs under one re-entrant lock, and results are
    fetched before the lock is released."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection
        self._lock = threading.RLock()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> _Rows:
        with self._lock:
            return _Rows(self._db.execute(sql, params))

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> _Rows:
        with self._lock:
            return _Rows(self._db.executemany(sql, rows))

    def executescript(self, script: str) -> None:
        with self._lock:
            self._db.executescript(script)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self) -> _Serialized:
        self._lock.acquire()
        self._db.__enter__()
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            self._db.__exit__(*exc)
        finally:
            self._lock.release()


class SqliteStore:
    def __init__(self, path: Path, blobs: BlobStore) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Safe to share between worker threads: every statement and transaction is serialized (`_Serialized`).
        connection = sqlite3.connect(path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        self._db: Any = _Serialized(connection)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._blobs = blobs
        self._migrate()

    def close(self) -> None:
        self._db.close()

    def _migrate(self) -> None:
        with self._tx() as db:
            db.executescript(_SCHEMA)
            row = db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            if row is None:
                db.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
                return
            version = int(row["value"])
            if version > SCHEMA_VERSION or any(v not in _MIGRATIONS for v in range(version, SCHEMA_VERSION)):
                raise SchemaMismatch(f"store schema {version}, code expects {SCHEMA_VERSION}")
            for step in range(version, SCHEMA_VERSION):
                for statement in _MIGRATIONS[step]:
                    if callable(statement):
                        statement(db)
                    else:
                        db.execute(statement)
            db.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION),))

    @contextmanager
    def _tx(self) -> Iterator[Any]:
        with self._db:
            yield self._db

    # ---- blobs ---------------------------------------------------------------------------------------------

    def put_blob(self, data: bytes) -> str:
        return self._blobs.put(data)

    def get_blob(self, digest: str) -> bytes:
        return self._blobs.get(digest)

    def _put_text(self, text: str | None) -> str | None:
        return None if text is None else self._blobs.put(text.encode())

    def _get_text(self, digest: str | None) -> str | None:
        return None if digest is None else self._blobs.get(digest).decode()

    # ---- PRs -----------------------------------------------------------------------------------------------

    def upsert_pr(self, item: HarvestedPR) -> None:
        pr, key = item.pr, (item.pr.repo, item.pr.number)
        labels = {label.thread_id: label for label in item.labels}
        with self._tx() as db:
            self._delete_pr(db, key)
            db.execute(
                "INSERT INTO prs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (*key, pr.title, pr.url, pr.body, *_login(pr.author), item.author_kind.value,
                 pr.created_at, pr.landed_at, pr.base_ref, pr.base_oid, pr.head_oid,
                 pr.additions, pr.deletions, pr.changed_files, pr.force_pushes, pr.commit_count,
                 item.language, item.corpus.value, item.reviewed_commit, dt.datetime.now(dt.UTC).isoformat(),
                 item.sampled_as.value, item.source.value, item.split),
            )  # fmt: skip
            for ord_, thread in enumerate(pr.threads):
                self._insert_thread(db, key, ord_, thread, labels.get(thread.id))
            db.executemany(
                "INSERT INTO reviews VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    (r.id, *key, i, *_login(r.author), r.state, r.submitted_at, r.commit)
                    for i, r in enumerate(pr.reviews)
                ],
            )
            db.executemany(
                "INSERT INTO commits (repo, number, ord, oid, committed_date, authored_date, message_headline)"
                " VALUES (?,?,?,?,?,?,?)",
                [
                    (*key, i, c.oid, c.committed_date, c.authored_date, c.message_headline)
                    for i, c in enumerate(pr.commits)
                ],
            )
            for ord_, compare in enumerate(pr.compares):
                self._insert_compare(db, key, _THREAD_COMPARE, ord_, compare)
            if pr.reviewed_diff is not None:
                self._insert_compare(db, key, _REVIEWED_COMPARE, 0, pr.reviewed_diff)

    def _delete_pr(self, db: sqlite3.Connection, key: tuple[str, int]) -> None:
        db.execute(
            "DELETE FROM compare_files WHERE compare_id IN (SELECT id FROM compares WHERE repo=? AND number=?)", key
        )
        for table in ("compares", "comments", "threads", "reviews", "commits", "prs"):
            db.execute(f"DELETE FROM {table} WHERE repo=? AND number=?", key)

    def _insert_thread(
        self, db: sqlite3.Connection, key: tuple[str, int], ord_: int, t: Thread, label: ThreadLabel | None
    ) -> None:
        db.execute(
            "INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (t.id, *key, ord_, t.path, t.is_resolved, t.is_outdated, t.diff_side, t.subject_type,
             t.line, t.original_line, t.start_line, t.original_start_line, t.resolved_by, t.comment_count,
             label.author_kind.value if label else None, label.outcome.value if label else None,
             label.lines_changed if label else None, label.line_basis.value if label else None, t.created_at),
        )  # fmt: skip
        db.executemany(
            "INSERT INTO comments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (c.id, t.id, *key, i, *_login(c.author), c.body, c.created_at,
                 self._put_text(c.diff_hunk) if c.diff_hunk else None, c.commit, c.original_commit,
                 c.line, c.original_line, c.start_line, c.original_start_line,
                 json.dumps([[r.content, r.user] for r in c.reactions]), c.reaction_count)
                for i, c in enumerate(t.comments)
            ],
        )  # fmt: skip

    def _insert_compare(self, db: sqlite3.Connection, key: tuple[str, int], role: str, ord_: int, c: Compare) -> None:
        cursor = db.execute(
            "INSERT INTO compares (repo, number, role, ord, base, head, status, merge_base, complete, source)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (*key, role, ord_, c.base, c.head, c.status, c.merge_base, c.complete, c.source.value),
        )
        db.executemany(
            "INSERT INTO compare_files VALUES (?,?,?,?,?,?)",
            [
                (cursor.lastrowid, i, f.path, f.previous_path, f.status, self._put_text(f.patch))
                for i, f in enumerate(c.files)
            ],
        )

    def get_pr(self, key: PRKey) -> HarvestedPR | None:
        k = (key.repo, key.number)
        row = self._db.execute("SELECT * FROM prs WHERE repo=? AND number=?", k).fetchone()
        if row is None:
            return None
        threads, labels = self._threads(k)
        compares = self._compares(k)
        reviewed = [c for role, c in compares if role == _REVIEWED_COMPARE]
        pr = PullRequest(
            repo=row["repo"], number=row["number"], title=row["title"],
            author=_actor(row["author_login"], row["author_type"]), created_at=row["created_at"],
            landed_at=row["landed_at"], base_ref=row["base_ref"], base_oid=row["base_oid"], head_oid=row["head_oid"],
            url=row["url"], body=row["body"], additions=row["additions"], deletions=row["deletions"],
            changed_files=row["changed_files"], force_pushes=row["force_pushes"], threads=tuple(threads),
            reviews=tuple(
                Review(r["id"], _actor(r["author_login"], r["author_type"]), r["state"], r["submitted_at"],
                       r["commit_oid"])
                for r in self._db.execute("SELECT * FROM reviews WHERE repo=? AND number=? ORDER BY ord", k)
            ),
            commits=tuple(
                CommitInfo(c["oid"], c["committed_date"], c["authored_date"], c["message_headline"] or "")
                for c in self._db.execute("SELECT * FROM commits WHERE repo=? AND number=? ORDER BY ord", k)
            ),
            commit_count=row["commit_count"],
            compares=tuple(c for role, c in compares if role == _THREAD_COMPARE),
            reviewed_diff=reviewed[0] if reviewed else None,
        )  # fmt: skip
        return HarvestedPR(
            pr=pr,
            language=row["language"],
            corpus=Corpus(row["corpus"]),
            author_kind=AuthorKind(row["author_kind"]),
            reviewed_commit=row["reviewed_commit"],
            labels=tuple(labels),
            sampled_as=SampledAs(row["sampled_as"]),
            source=PRSource(row["source"]),
            split=row["split"],
        )

    def _threads(self, key: tuple[str, int]) -> tuple[list[Thread], list[ThreadLabel]]:
        comments: dict[str, list[Comment]] = {}
        for c in self._db.execute("SELECT * FROM comments WHERE repo=? AND number=? ORDER BY thread_id, ord", key):
            comments.setdefault(c["thread_id"], []).append(
                Comment(
                    id=c["id"], author=_actor(c["author_login"], c["author_type"]), body=c["body"],
                    created_at=c["created_at"], diff_hunk=self._get_text(c["diff_hunk_blob"]) or "",
                    commit=c["commit_oid"], original_commit=c["original_commit"], line=c["line"],
                    original_line=c["original_line"], start_line=c["start_line"],
                    original_start_line=c["original_start_line"],
                    reactions=tuple(Reaction(content, user) for content, user in json.loads(c["reactions"])),
                    reaction_count=c["reaction_count"],
                )
            )  # fmt: skip
        threads, labels = [], []
        for t in self._db.execute("SELECT * FROM threads WHERE repo=? AND number=? ORDER BY ord", key):
            threads.append(
                Thread(
                    id=t["id"], path=t["path"], comments=tuple(comments.get(t["id"], [])),
                    is_resolved=bool(t["is_resolved"]), is_outdated=bool(t["is_outdated"]), diff_side=t["diff_side"],
                    subject_type=t["subject_type"], line=t["line"], original_line=t["original_line"],
                    start_line=t["start_line"], original_start_line=t["original_start_line"],
                    resolved_by=t["resolved_by"], comment_count=t["comment_count"],
                )
            )  # fmt: skip
            if t["outcome"] is not None:
                labels.append(
                    ThreadLabel(
                        thread_id=t["id"],
                        author_kind=AuthorKind(t["author_kind"]),
                        outcome=Outcome(t["outcome"]),
                        lines_changed=bool(t["lines_changed"]),
                        line_basis=LineBasis(t["line_basis"]),
                    )
                )
        return threads, labels

    def _compares(self, key: tuple[str, int]) -> list[tuple[str, Compare]]:
        result = []
        for c in self._db.execute("SELECT * FROM compares WHERE repo=? AND number=? ORDER BY role, ord", key):
            files = tuple(
                FilePatch(f["path"], f["status"], self._get_text(f["patch_blob"]), f["previous_path"])
                for f in self._db.execute("SELECT * FROM compare_files WHERE compare_id=? ORDER BY ord", (c["id"],))
            )
            compare = Compare(
                base=c["base"], head=c["head"], status=c["status"], files=files, merge_base=c["merge_base"],
                complete=bool(c["complete"]), source=CompareSource(c["source"]),
            )  # fmt: skip
            result.append((c["role"], compare))
        return result

    def has_pr(self, key: PRKey) -> bool:
        query = "SELECT 1 FROM prs WHERE repo=? AND number=?"
        return self._db.execute(query, (key.repo, key.number)).fetchone() is not None

    def pr_keys(self, repo: str | None = None) -> list[PRKey]:
        query = "SELECT repo, number FROM prs" + (" WHERE repo=?" if repo else "") + " ORDER BY repo, number"
        return [PRKey(r["repo"], r["number"]) for r in self._db.execute(query, (repo,) if repo else ())]

    def count_prs(self, repo: str, corpus: Corpus, created: DateWindow | None = None,
                  sampled_as: SampledAs | None = None) -> int:  # fmt: skip
        query, args = "SELECT COUNT(*) FROM prs WHERE repo=? AND corpus=?", [repo, corpus.value]
        if created is not None:
            query += " AND substr(created_at, 1, 10) BETWEEN ? AND ?"
            args += [created.start, created.end]
        if sampled_as is not None:
            query += " AND sampled_as=?"
            args.append(sampled_as.value)
        return self._db.execute(query, args).fetchone()[0]

    def set_corpus(self, key: PRKey, corpus: Corpus) -> None:
        with self._tx() as db:
            db.execute("UPDATE prs SET corpus = ? WHERE repo = ? AND number = ?", (corpus.value, key.repo, key.number))

    def set_split(self, key: PRKey, split: str | None) -> None:
        with self._tx() as db:
            db.execute("UPDATE prs SET split = ? WHERE repo = ? AND number = ?", (split, key.repo, key.number))

    def set_splits(self, splits: Mapping[PRKey, str]) -> None:
        with self._tx() as db:
            db.executemany("UPDATE prs SET split = ? WHERE repo = ? AND number = ?",
                           [(split, key.repo, key.number) for key, split in splits.items()])  # fmt: skip

    # ---- cursors -------------------------------------------------------------------------------------------

    def get_cursor(self, repo: str, corpus: Corpus, window: DateWindow,
                   sample: SampledAs = SampledAs.GENERAL) -> Cursor:  # fmt: skip
        row = self._db.execute(
            "SELECT * FROM cursors WHERE repo=? AND corpus=? AND window=? AND sample=?",
            (repo, corpus.value, str(window), sample.value),
        ).fetchone()
        return Cursor(row["after"], row["offset"], bool(row["exhausted"])) if row else Cursor()

    def set_cursor(self, repo: str, corpus: Corpus, window: DateWindow, cursor: Cursor,
                   sample: SampledAs = SampledAs.GENERAL) -> None:  # fmt: skip
        with self._tx() as db:
            db.execute(
                "INSERT OR REPLACE INTO cursors (repo, corpus, window, after, offset, exhausted, sample)"
                " VALUES (?,?,?,?,?,?,?)",
                (repo, corpus.value, str(window), cursor.after, cursor.offset, cursor.exhausted, sample.value),
            )

    # ---- reporting -----------------------------------------------------------------------------------------

    def pr_facts(self) -> list[PRFact]:
        query = (
            "SELECT p.repo, p.number, p.language, p.corpus, p.author_kind, p.created_at, p.sampled_as, p.source,"
            " p.split, (SELECT COUNT(*) FROM threads t WHERE t.repo = p.repo AND t.number = p.number) AS threads"
            " FROM prs p ORDER BY p.repo, p.number"
        )
        return [
            PRFact(r["repo"], r["number"], r["language"], Corpus(r["corpus"]), AuthorKind(r["author_kind"]),
                   r["created_at"], r["threads"], SampledAs(r["sampled_as"]), PRSource(r["source"]), r["split"])
            for r in self._db.execute(query)
        ]  # fmt: skip

    def thread_facts(self) -> list[ThreadFact]:
        query = (
            "SELECT t.repo, t.number, p.language, p.corpus, t.author_kind, t.outcome, t.lines_changed, t.line_basis"
            " FROM threads t JOIN prs p ON p.repo = t.repo AND p.number = t.number"
            " WHERE t.outcome IS NOT NULL ORDER BY t.repo, t.number, t.ord"
        )
        return [
            ThreadFact(r["repo"], r["number"], r["language"], Corpus(r["corpus"]), AuthorKind(r["author_kind"]),
                       Outcome(r["outcome"]), bool(r["lines_changed"]), LineBasis(r["line_basis"]))
            for r in self._db.execute(query)
        ]  # fmt: skip

    def threads_before(self, repo: str, paths: Sequence[str], before: str) -> list[PriorThread]:
        if not paths:
            return []
        marks = ",".join("?" * len(paths))
        query = (
            "SELECT repo, number, id, path, created_at, author_kind, outcome FROM threads"
            f" WHERE repo = ? AND path IN ({marks}) AND created_at < ? AND outcome IS NOT NULL"
            " ORDER BY created_at, repo, number, ord"
        )
        return [
            PriorThread(r["repo"], r["number"], r["id"], r["path"], r["created_at"], AuthorKind(r["author_kind"]),
                        Outcome(r["outcome"]))
            for r in self._db.execute(query, (repo, *paths, before))
        ]  # fmt: skip

    # ---- context packs -------------------------------------------------------------------------------------

    def save_pack(self, pack: ContextPack) -> None:
        manifest = self._blobs.put(json.dumps(to_json(pack), sort_keys=True).encode())
        with self._tx() as db:
            db.execute(
                "INSERT OR REPLACE INTO packs VALUES (?,?,?,?,?,?,?,?)",
                (pack.repo, pack.number, pack.base_commit, pack.head_commit, manifest, len(pack.files),
                 pack.total_bytes, dt.datetime.now(dt.UTC).isoformat()),
            )  # fmt: skip

    def get_pack(self, key: PRKey) -> ContextPack | None:
        row = self._db.execute(
            "SELECT manifest_blob FROM packs WHERE repo=? AND number=?", (key.repo, key.number)
        ).fetchone()
        return from_json(ContextPack, json.loads(self._blobs.get(row["manifest_blob"]))) if row else None

    # ---- labels (LabelStore) -------------------------------------------------------------------------------

    def save_judgment(self, judgment: Judgment) -> None:
        j = judgment
        with self._tx() as db:
            db.execute(
                "INSERT OR REPLACE INTO judgments VALUES (?,?,?,?,?,?,?,?,?)",
                (j.repo, j.number, j.thread_id, j.kind.value, j.value, j.reason, j.category, j.model,
                 dt.datetime.now(dt.UTC).isoformat()),
            )  # fmt: skip

    def judgments(self, key: PRKey) -> list[Judgment]:
        rows = self._db.execute(
            "SELECT * FROM judgments WHERE repo=? AND number=? ORDER BY thread_id, kind", (key.repo, key.number)
        )
        return [
            Judgment(r["repo"], r["number"], r["thread_id"], JudgmentKind(r["kind"]), r["value"], r["reason"] or "",
                     r["category"], r["model"] or "")
            for r in rows
        ]  # fmt: skip

    def save_judged_labels(self, key: PRKey, labels: Sequence[JudgedLabel]) -> None:
        k = (key.repo, key.number)
        with self._tx() as db:
            db.execute("DELETE FROM judged_labels WHERE repo=? AND number=?", k)
            db.executemany(
                "INSERT INTO judged_labels VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (*k, lab.thread_id, i, lab.author_kind.value, lab.outcome.value,
                     lab.polarity.value if lab.polarity else None, lab.strength.value if lab.strength else None,
                     lab.applied_suggestion, lab.addressed.value if lab.addressed else None,
                     lab.stance.value if lab.stance else None, lab.category, lab.high_risk_dismissal)
                    for i, lab in enumerate(labels)
                ],
            )  # fmt: skip

    def judged_labels(self, key: PRKey) -> list[JudgedLabel]:
        rows = self._db.execute(
            "SELECT * FROM judged_labels WHERE repo=? AND number=? ORDER BY ord", (key.repo, key.number)
        )
        return [
            JudgedLabel(
                thread_id=r["thread_id"], author_kind=AuthorKind(r["author_kind"]), outcome=Outcome(r["outcome"]),
                polarity=Polarity(r["polarity"]) if r["polarity"] else None,
                strength=Strength(r["strength"]) if r["strength"] else None,
                applied_suggestion=bool(r["applied_suggestion"]),
                addressed=Addressed(r["addressed"]) if r["addressed"] else None,
                stance=Stance(r["stance"]) if r["stance"] else None, category=r["category"],
                high_risk_dismissal=bool(r["high_risk_dismissal"]),
            )
            for r in rows
        ]  # fmt: skip

    def save_gold(self, gold: GoldSet) -> None:
        k = (gold.repo, gold.number)
        with self._tx() as db:
            db.execute("DELETE FROM gold_issues WHERE repo=? AND number=?", k)
            db.execute(
                "INSERT OR REPLACE INTO gold_sets VALUES (?,?,?,?,?,?,?)",
                (*k, gold.reviewed_commit, gold.candidates, json.dumps(list(gold.excluded_later_round)),
                 json.dumps(list(gold.excluded_unreadable)), dt.datetime.now(dt.UTC).isoformat()),
            )  # fmt: skip
            db.executemany(
                "INSERT INTO gold_issues VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (*k, i, g.id, g.path, g.start_line, g.end_line, g.severity.value, g.category, g.provenance.value,
                     g.conf, g.description, json.dumps(list(g.source_threads)))
                    for i, g in enumerate(gold.issues)
                ],
            )  # fmt: skip

    def get_gold(self, key: PRKey) -> GoldSet | None:
        k = (key.repo, key.number)
        row = self._db.execute("SELECT * FROM gold_sets WHERE repo=? AND number=?", k).fetchone()
        if row is None:
            return None
        issues = tuple(
            GoldIssue(
                id=g["id"], path=g["path"], start_line=g["start_line"], end_line=g["end_line"],
                severity=Severity(g["severity"]), provenance=GoldProvenance(g["provenance"]), conf=g["conf"],
                description=g["description"] or "", source_threads=tuple(json.loads(g["source_threads"])),
                category=g["category"] or "",
            )
            for g in self._db.execute("SELECT * FROM gold_issues WHERE repo=? AND number=? ORDER BY ord", k)
        )  # fmt: skip
        return GoldSet(
            repo=row["repo"], number=row["number"], reviewed_commit=row["reviewed_commit"], issues=issues,
            candidates=row["candidates"], excluded_later_round=tuple(json.loads(row["excluded_later_round"])),
            excluded_unreadable=tuple(json.loads(row["excluded_unreadable"])),
        )  # fmt: skip

    # ---- review rounds -------------------------------------------------------------------------------------

    def save_round_pack(self, pack: ContextPack) -> None:
        manifest = self._blobs.put(json.dumps(to_json(pack), sort_keys=True).encode())
        with self._tx() as db:
            db.execute(
                "INSERT OR REPLACE INTO round_packs VALUES (?,?,?,?,?,?,?)",
                (pack.repo, pack.number, pack.head_commit, manifest, len(pack.files), pack.total_bytes,
                 dt.datetime.now(dt.UTC).isoformat()),
            )  # fmt: skip

    def get_round_pack(self, key: PRKey, commit: str) -> ContextPack | None:
        own = self.get_pack(key)
        if own is not None and own.head_commit == commit:
            return own
        row = self._db.execute(
            "SELECT manifest_blob FROM round_packs WHERE repo=? AND number=? AND head_commit=?",
            (key.repo, key.number, commit),
        ).fetchone()
        return from_json(ContextPack, json.loads(self._blobs.get(row["manifest_blob"]))) if row else None

    def save_round_diff(self, key: PRKey, diff: Compare) -> None:
        """A later review round's diff (merge base -> the round's commit), kept without its round pack: an imported
        bundle's, for `rehydrate` to rebuild the round pack from git."""
        blob = self._blobs.put(json.dumps(to_json(diff), sort_keys=True).encode())
        with self._tx() as db:
            db.execute("INSERT OR REPLACE INTO round_diffs VALUES (?,?,?,?)", (key.repo, key.number, diff.head, blob))

    def mark_stripped(self, key: PRKey, files: Sequence[str]) -> None:
        with self._tx() as db:
            if files:
                db.execute("INSERT OR REPLACE INTO stripped_patches VALUES (?,?,?)",
                           (key.repo, key.number, json.dumps(sorted(set(files)))))  # fmt: skip
            else:
                db.execute("DELETE FROM stripped_patches WHERE repo=? AND number=?", (key.repo, key.number))

    def stripped(self, key: PRKey) -> list[str]:
        row = self._db.execute("SELECT files FROM stripped_patches WHERE repo=? AND number=?",
                               (key.repo, key.number)).fetchone()  # fmt: skip
        return json.loads(row["files"]) if row else []

    def stripped_keys(self) -> list[PRKey]:
        rows = self._db.execute("SELECT repo, number FROM stripped_patches ORDER BY repo, number").fetchall()
        return [PRKey(r["repo"], r["number"]) for r in rows]

    def round_diffs(self, key: PRKey) -> list[Compare]:
        """The PR's later-round diffs: from its round packs, and any kept on their own (an imported bundle's)."""
        rows = self._db.execute(
            "SELECT manifest_blob AS blob, 'pack' AS kind FROM round_packs WHERE repo=? AND number=? UNION ALL "
            "SELECT diff_blob AS blob, 'diff' AS kind FROM round_diffs WHERE repo=? AND number=?",
            (key.repo, key.number, key.repo, key.number),
        ).fetchall()
        found: dict[str, Compare] = {}
        for row in rows:
            data = json.loads(self._blobs.get(row["blob"]))
            diff = from_json(ContextPack, data).diff if row["kind"] == "pack" else from_json(Compare, data)
            if diff is not None:
                found.setdefault(diff.head, diff)
        return [found[head] for head in sorted(found)]

    def save_round_gold(self, gold: GoldSet, rounds: int) -> None:
        blob = self._blobs.put(json.dumps(to_json(gold), sort_keys=True).encode())
        with self._tx() as db:
            db.execute(
                "INSERT OR REPLACE INTO round_gold VALUES (?,?,?,?,?,?)",
                (gold.repo, gold.number, rounds, gold.reviewed_commit, blob, dt.datetime.now(dt.UTC).isoformat()),
            )

    def round_golds(self, key: PRKey) -> list[tuple[GoldSet, int]]:
        rows = self._db.execute(
            "SELECT gold_blob, rounds FROM round_gold WHERE repo=? AND number=? ORDER BY rounds, commit_oid",
            (key.repo, key.number),
        ).fetchall()
        return [(from_json(GoldSet, json.loads(self._blobs.get(r["gold_blob"]))), r["rounds"]) for r in rows]

    def get_round_gold(self, key: PRKey, commit: str, rounds: int) -> GoldSet | None:
        row = self._db.execute(
            "SELECT gold_blob FROM round_gold WHERE repo=? AND number=? AND rounds=? AND commit_oid=?",
            (key.repo, key.number, rounds, commit),
        ).fetchone()
        return from_json(GoldSet, json.loads(self._blobs.get(row["gold_blob"]))) if row else None

    def save_escaped_defect(self, defect: EscapedDefect) -> None:
        blob = self._blobs.put(json.dumps(to_json(defect), sort_keys=True).encode())
        with self._tx() as db:
            db.execute(
                "INSERT OR REPLACE INTO escaped_defects VALUES (?,?,?,?,?,?)",
                (defect.pr.repo, defect.pr.number, defect.issue.id, defect.fix_pr, blob,
                 dt.datetime.now(dt.UTC).isoformat()),
            )  # fmt: skip

    def escaped_defects(self, key: PRKey | None = None) -> list[EscapedDefect]:
        query, args = "SELECT record_blob FROM escaped_defects", ()
        if key is not None:
            query, args = query + " WHERE repo=? AND number=?", (key.repo, key.number)
        rows = self._db.execute(query + " ORDER BY repo, number, id", args).fetchall()
        return [from_json(EscapedDefect, json.loads(self._blobs.get(r["record_blob"]))) for r in rows]

    def clear_escaped_defects(self, repo: str) -> None:
        with self._tx() as db:
            db.execute("DELETE FROM escaped_defects WHERE repo=?", (repo,))

    # ---- evaluation runs (EvalStore) -----------------------------------------------------------------------

    def save_eval_run(self, run: EvalRun) -> None:
        blob = self._blobs.put(json.dumps(to_json(run), sort_keys=True).encode())
        with self._tx() as db:
            db.execute(
                "INSERT OR REPLACE INTO eval_runs VALUES (?,?,?,?,?,?,?,?,?)",
                (run.id, run.policy_hash, run.split, run.backend, run.rounds, run.sample, run.created_at,
                 run.stopped is None, blob),
            )  # fmt: skip

    def get_eval_run(self, run_id: str) -> EvalRun | None:
        row = self._db.execute("SELECT run_blob FROM eval_runs WHERE id=?", (run_id,)).fetchone()
        return from_json(EvalRun, json.loads(self._blobs.get(row["run_blob"]))) if row else None

    def latest_eval_run(self, policy_hash: str, split: str, backend: str, rounds: int, sample: int) -> EvalRun | None:
        """The latest complete run with this key."""
        row = self._db.execute(
            "SELECT id FROM eval_runs WHERE policy_hash=? AND split=? AND backend=? AND rounds=? AND sample=?"
            " AND complete=1 ORDER BY created_at DESC LIMIT 1",
            (policy_hash, split, backend, rounds, sample),
        ).fetchone()
        return self.get_eval_run(row["id"]) if row else None

    def eval_run_ids(self, split: str | None = None, backend: str | None = None) -> list[str]:
        query, args = "SELECT id FROM eval_runs WHERE complete=1", []
        for column, value in (("split", split), ("backend", backend)):
            if value is not None:
                query += f" AND {column}=?"
                args.append(value)
        return [r["id"] for r in self._db.execute(query + " ORDER BY created_at DESC", args).fetchall()]

    def set_incumbent(self, split: str, backend: str, rounds: int, run_id: str) -> None:
        with self._tx() as db:
            db.execute("INSERT OR REPLACE INTO incumbents VALUES (?,?,?,?,?)",
                       (split, backend, rounds, run_id, dt.datetime.now(dt.UTC).isoformat()))  # fmt: skip

    def incumbent(self, split: str, backend: str, rounds: int) -> str | None:
        row = self._db.execute(
            "SELECT run_id FROM incumbents WHERE split=? AND backend=? AND rounds=?", (split, backend, rounds)
        ).fetchone()
        return row["run_id"] if row else None

    def add_decision(self, decision: Decision) -> int:
        d = decision
        with self._tx() as db:
            cursor = db.execute(
                "INSERT INTO decision_log (at, hypothesis, change, before, after, delta, gate, verdict, note)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (d.at, d.hypothesis, d.change, d.before, d.after, d.delta, d.gate, d.verdict, d.note),
            )
            return int(cursor.lastrowid or 0)

    def decisions(self) -> list[Decision]:
        rows = self._db.execute("SELECT * FROM decision_log ORDER BY id").fetchall()
        return [Decision(r["id"], r["at"], r["hypothesis"], r["change"], r["before"], r["after"], r["delta"], r["gate"],
                         r["verdict"], r["note"] or "") for r in rows]  # fmt: skip

    # ---- benchmarks -----------------------------------------------------------------------------------------

    def save_benchmark_pr(self, record: BenchmarkPR, key: PRKey) -> None:
        """The benchmark's own record of an imported PR (its golden comments and, for AACR-Bench, the comments its
        annotators rejected), kept beside the stored PR."""
        blob = self._blobs.put(json.dumps(to_json(record), sort_keys=True).encode())
        with self._tx() as db:
            db.execute("INSERT OR REPLACE INTO benchmark_prs VALUES (?,?,?,?,?,?)",
                       (key.repo, key.number, record.benchmark, record.url, blob,
                        dt.datetime.now(dt.UTC).isoformat()))  # fmt: skip

    def benchmark_prs(self) -> list[tuple[PRKey, BenchmarkPR]]:
        rows = self._db.execute("SELECT repo, number, record_blob FROM benchmark_prs ORDER BY repo, number").fetchall()
        return [(PRKey(r["repo"], r["number"]), from_json(BenchmarkPR, json.loads(self._blobs.get(r["record_blob"]))))
                for r in rows]  # fmt: skip

    # ---- the improve loop (ImproveStore) -------------------------------------------------------------------

    def save_candidate(self, record: CandidateRecord) -> None:
        blob = self._blobs.put(json.dumps(to_json(record), sort_keys=True).encode())
        with self._tx() as db:
            db.execute("INSERT OR REPLACE INTO candidates VALUES (?,?,?,?,?,?)",
                       (record.id, record.round, record.created_at or dt.datetime.now(dt.UTC).isoformat(),
                        record.outcome.value, record.policy_hash, blob))  # fmt: skip

    def candidates(self) -> list[CandidateRecord]:
        rows = self._db.execute("SELECT record_blob FROM candidates ORDER BY created_at, id").fetchall()
        return [from_json(CandidateRecord, json.loads(self._blobs.get(r["record_blob"]))) for r in rows]

    def save_policy_version(self, record: PolicyRecord) -> None:
        blob = self._blobs.put(json.dumps(to_json(record), sort_keys=True).encode())
        with self._tx() as db:
            db.execute("INSERT OR REPLACE INTO policy_versions VALUES (?,?,?,?,?)",
                       (record.hash, record.parent, record.created_at, record.provisional, blob))  # fmt: skip

    def policy_version(self, content_hash: str) -> PolicyRecord | None:
        row = self._db.execute("SELECT record_blob FROM policy_versions WHERE hash=?", (content_hash,)).fetchone()
        return from_json(PolicyRecord, json.loads(self._blobs.get(row["record_blob"]))) if row else None

    def policy_versions(self) -> list[PolicyRecord]:
        rows = self._db.execute("SELECT record_blob FROM policy_versions ORDER BY created_at").fetchall()
        return [from_json(PolicyRecord, json.loads(self._blobs.get(r["record_blob"]))) for r in rows]
