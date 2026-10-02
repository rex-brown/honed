"""`honed human-labels serve | check`: the labeling page for the blind audit sample, and the check a labels file
must pass before it is merged (METRICS.md section 5; CONTRIBUTING.md, "Calibrate the judge"). Also the loader the
judge audits share: every `labels-*.json`, combined into one answer per item (`core/consensus.py`)."""

from __future__ import annotations

import argparse
import importlib.metadata
import sys
import webbrowser
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from honed import config
from honed.adapters import human_labels
from honed.adapters.label_server import LabelServer, PageFiles
from honed.core.consensus import AnswerStatus, InterAnnotator, ItemAnswer, answers, inter_annotator
from honed.core.types import PRKey


def honed_version() -> str:
    try:
        return importlib.metadata.version("honed")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


@dataclass
class People:
    """Every valid labels file, combined."""

    refs: dict[str, human_labels.SampleRef]
    files: list[human_labels.LabelsFile] = field(default_factory=list)
    errors: list[human_labels.LabelsError] = field(default_factory=list)
    answers: list[ItemAnswer] = field(default_factory=list)
    agreement: InterAnnotator = field(default_factory=lambda: InterAnnotator("", None, 0, 0))

    def where(self, thread_id: str) -> PRKey | None:
        ref = self.refs.get(thread_id)
        return PRKey(ref.repo, ref.pr) if ref else None

    def count(self, status: AnswerStatus) -> int:
        return sum(a.status is status for a in self.answers)

    def summary(self) -> str:
        if not self.files:
            return "human labels: none yet (yardstick/human_labels/labels-*.json; CONTRIBUTING.md)"
        who = ", ".join(sorted(f.labeler for f in self.files))
        return (f"human labels: {len(self.files)} labelers ({who}) on {len(self.answers)} items: "
                f"{self.count(AnswerStatus.MAJORITY)} with a majority of at least 2 (counted), "
                f"{self.count(AnswerStatus.SINGLE)} single-labeler (flagged, not counted), "
                f"{self.count(AnswerStatus.TIE)} tied, {self.count(AnswerStatus.UNSURE)} only unsure")  # fmt: skip

    def agreement_line(self) -> str:
        a = self.agreement
        if not a.method:
            return "inter-annotator agreement: needs at least 2 labelers"
        name = "Fleiss' kappa" if a.method == "fleiss" else "Cohen's kappa"
        value = "undefined" if a.kappa is None else f"{a.kappa:.3f}"
        return f"inter-annotator agreement: {name} {value} over {a.items} items, {a.labelers} labelers"


def load_people(settings: config.Settings) -> People:
    directory = settings.paths.human_labels
    people = People(human_labels.load_sample(directory / human_labels.SAMPLE_NAME))
    people.files, people.errors = human_labels.load_all(directory, people.refs.keys())
    labels = [lab for f in people.files for lab in f.labels]
    people.answers = answers(labels)
    people.agreement = inter_annotator(labels)
    return people


def cmd_human_labels(settings: config.Settings, args: argparse.Namespace) -> int:
    if args.human_labels_command == "serve":
        return serve(settings, port=args.port, open_browser=not args.no_browser)
    return check(settings, args.files)


def serve(settings: config.Settings, *, port: int, open_browser: bool) -> int:
    sample = settings.paths.human_labels / human_labels.SAMPLE_NAME
    page = settings.paths.human_labels_page
    for path in (page, sample):
        if not path.exists():
            print(f"error: {path} is missing", file=sys.stderr)
            return 2
    meta = {"honed_version": honed_version(), "context_lines": settings.label.region_context_lines}
    try:
        server = LabelServer(PageFiles(page, sample, meta), port)
    except OSError as error:
        print(f"error: can't listen on 127.0.0.1:{port}: {error} (try --port)", file=sys.stderr)
        return 2
    print(f"the labeling page: {server.url}  (Ctrl-C stops the server; your labels stay saved in the browser)")
    if open_browser:
        webbrowser.open(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.shutdown()
    return 0


def check(settings: config.Settings, files: Sequence[Path]) -> int:
    """Validate labels files against the committed sample: the given ones, or every one in the yardstick (then also
    one file per person)."""
    directory = settings.paths.human_labels
    refs = human_labels.load_sample(directory / human_labels.SAMPLE_NAME)
    if not refs:
        print(f"error: no sample at {directory / human_labels.SAMPLE_NAME}", file=sys.stderr)
        return 2
    if files:
        good, bad = [], []
        for path in files:
            try:
                good.append(human_labels.read(path, refs.keys()))
            except human_labels.LabelsError as error:
                bad.append(error)
    else:
        good, bad = human_labels.load_all(directory, refs.keys())
        if not good and not bad:
            print(f"no labels files in {directory}")
            return 0
    for labels in good:
        counts = {v: sum(lab.verdict.value == v for lab in labels.labels) for v in sorted(human_labels.VERDICTS)}
        print(f"{labels.path}: ok, {labels.labeler}, {len(labels.labels)} of {len(refs)} items {counts}")
    for error in bad:
        print(f"{error.path}: invalid\n  " + "\n  ".join(error.problems[:20]))
    return 1 if bad else 0
