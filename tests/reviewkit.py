"""Helpers for review and evaluation tests: the repo's settings and seed policy, an in-memory code reader, and a
scripted model that answers each review and judge stage from the prompt it receives."""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from honed import config
from honed.adapters import policy_dir
from honed.core import patches
from honed.core.policy import ModelChoice, Policy, PolicyRules
from honed.core.reviews import ReviewRequest
from honed.core.types import FilePatch, GrepHit
from honed.learn.jobs import run_jobs
from honed.learn.label import STOP_ON
from honed.ports.llm import LLMCall, LLMResult, Usage
from honed.review.pipeline import ReviewOptions, ReviewPipeline

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = config.load(ROOT / "honed.toml")
STORE = SETTINGS.paths.sqlite.name  # the store's file name under a data directory (`--data-dir`)
BASE, HEAD = "b" * 40, "h" * 40


def rules(settings: config.Settings = SETTINGS) -> PolicyRules:
    m = settings.models
    return PolicyRules(
        high_risk=frozenset(settings.safety.high_risk_categories), categories=settings.label.categories,
        languages=tuple(settings.metrics.language_weights),
        models={s: ModelChoice(getattr(m, s).online, getattr(m, s).allowed, getattr(m, s).tunable)
                for s in ("intent", "finders", "verifier")},
    )  # fmt: skip


def seed_policy() -> Policy:
    return policy_dir.load(ROOT / "policy", rules())


def options() -> ReviewOptions:
    return ReviewOptions(SETTINGS.label.categories, frozenset(SETTINGS.safety.high_risk_categories))


def runner(jobs, *, concurrency):
    return run_jobs(jobs, concurrency=concurrency, stop_on=STOP_ON)


class DictReader:
    """Files by (path, commit); the listing is every path known at the commit."""

    def __init__(self, files: dict[tuple[str, str], str]) -> None:
        self.files = files
        self.reads: list[tuple[str, str]] = []

    def read(self, path: str, commit: str) -> str | None:
        self.reads.append((path, commit))
        return self.files.get((path, commit))

    def grep(self, pattern, commit, paths=None, *, word=False, max_per_file=None) -> list[GrepHit]:
        regex = re.compile(rf"\b(?:{pattern})\b" if word else pattern)
        hits = []
        for (path, c), text in sorted(self.files.items()):
            if c != commit:
                continue
            found = 0
            for n, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    hits.append(GrepHit(path, n, line))
                    found += 1
                    if max_per_file and found >= max_per_file:
                        break
        return hits

    def list_files(self, commit: str, prefix: str = "") -> list[str]:
        return sorted({p for p, c in self.files if c in (commit, BASE, HEAD) and p.startswith(prefix)})

    def prefetch(self, commit, paths) -> None:
        return None


SERVICE_BASE = "def load(path):\n    with open(path) as f:\n        return f.read()\n"
SERVICE_HEAD = (
    "import json\n\n\n"
    "def load(path):\n    with open(path) as f:\n        return f.read()\n\n\n"
    "def parse(path):\n    data = json.loads(load(path))  # noqa: E501\n    return data['items'][0]\n"
)
CALLER = "from app.service import parse\n\nfirst = parse('x.json')\n"


def request(**overrides: Any) -> ReviewRequest:
    patch = patches.unified_diff(SERVICE_BASE, SERVICE_HEAD, "app/service.py")
    values: dict[str, Any] = dict(
        repo="o/r", number=7, title="Add a parser for item files", body="Parse the first item of a file.",
        author="dev", language="python", base_commit=BASE, head_commit=HEAD,
        files=(FilePatch("app/service.py", "modified", patch),), created_at="2026-03-01T00:00:00Z",
        commit_messages=("Add parse()",),
    )  # fmt: skip
    values.update(overrides)
    return ReviewRequest(**values)


def reader() -> DictReader:
    return DictReader({
        ("app/service.py", BASE): SERVICE_BASE, ("app/service.py", HEAD): SERVICE_HEAD,
        ("app/main.py", HEAD): CALLER, ("REVIEW.md", BASE): "Flag unchecked indexing into parsed JSON.\n",
        ("REVIEW.md", HEAD): "Flag unchecked indexing into parsed JSON.\n",
    })  # fmt: skip


def finding(path: str = "app/service.py", start: int = 11, *, title: str, severity: str = "important",
            category: str = "correctness", body: str = "",
            lessons: tuple[str, ...] = ()) -> dict[str, Any]:  # fmt: skip
    return {"path": path, "start_line": start, "end_line": start, "severity": severity, "category": category,
            "title": title, "body": body or f"{title}. Check the value first.", "trace": "parse -> [0]",
            "lessons": list(lessons)}  # fmt: skip


Verdict = Callable[[str, str], dict[str, Any]]  # (label, title) -> verdict fields


class ScriptedLLM:
    """Answers every stage from the prompt: the intent, each member's findings, the lead reviewer's verdicts (by
    each proposed finding's title), and the judge's match and validity questions."""

    def __init__(self, members: dict[str, list[dict[str, Any]]], verdict: Verdict | None = None,
                 match: Callable[[str, str], str | None] | None = None,
                 valid: Callable[[str], bool] | None = None,
                 severity: Callable[[str], str] | None = None) -> None:  # fmt: skip
        self.members = members
        self.verdict = verdict or (lambda label, title: {"bucket": "act_on", "severity": "important",
                                                         "evidence_level": 3, "confidence": 0.9})  # fmt: skip
        self.match = match or (lambda text, gold: None)
        self.valid = valid or (lambda text: True)
        self.severity = severity or (lambda text: "important")  # the judge's own rating of a valid finding
        self.calls: list[LLMCall] = []
        self._lock = threading.Lock()

    def complete(self, call: LLMCall) -> LLMResult:
        with self._lock:
            self.calls.append(call)
        if call.stage == "intent":
            data: Any = {"intent": "Adds parse(), which reads a JSON file and returns its first item."}
        elif call.stage.startswith("finder:"):
            data = {"findings": self.members.get(call.stage.split(":", 1)[1], [])}
        elif call.stage == "verifier":
            proposed = re.findall(r"### (P\d+)\n(?:.*\n)*?Title: (.*)", call.user)
            data = {"verdicts": [{"id": label, "reason": "checked", "duplicate_of": "", "title": "", "body": "",
                                  "lessons": [], "checked": "read the cited lines and the caller",
                                  **self.verdict(label, title)}
                                 for label, title in proposed]}  # fmt: skip
        elif call.stage == "match":
            golds = re.findall(r"=== (G\d+) ===\n(?:.*\n)*?Issue: (.*)", call.user)
            items = re.findall(r"=== ([FD]\d+) ===\n(?:.*\n)*?Finding:\n(.*)", call.user)
            findings, other = [], []
            for label, text in items:
                hit = next((g for g, desc in golds if self.match(text, desc)), None)
                entry = {"id": label, "gold": hit or "none", "reason": "compared"}
                if label.startswith("F"):
                    findings.append({**entry, "duplicate_of": ""})
                else:
                    other.append(entry)
            data = {"findings": findings, "other": other}
        elif call.stage == "validity":
            items = re.findall(r"=== (F\d+) ===\n(?:.*\n)*?Review comment:\n(.*)", call.user)
            data = {"verdicts": [{"id": label, "reason": "r", "valid": self.valid(text),
                                  "severity": self.severity(text)} for label, text in items]}  # fmt: skip
        else:
            raise AssertionError(f"unexpected stage {call.stage}")
        return LLMResult(text="", data=data, usage=Usage(100, 50, 0, 0, 0.02, 3.0), model=call.model)


def make_pipeline(llm: ScriptedLLM, policy: Policy | None = None, store: Any = None, *,
                  focus: str | None = None) -> ReviewPipeline:  # fmt: skip
    return ReviewPipeline(llm, policy or seed_policy(), runner, replace(options(), focus=focus), store)
