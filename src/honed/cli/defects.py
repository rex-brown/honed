"""`mine-defects`: escaped-defect gold issues from later bug-fix PRs (ARCHITECTURE.md section 5)."""

from __future__ import annotations

import argparse
import sys

from honed import config
from honed.cli.wiring import git_reader, github_host, open_store, require_online
from honed.core.filters import is_excluded
from honed.core.types import GoldProvenance
from honed.learn.defects import DefectMiner, DefectOptions
from honed.ports.code_host import HostError


def cmd_mine_defects(settings: config.Settings, args: argparse.Namespace) -> int:
    require_online(settings, "mine-defects")
    store = open_store(settings)
    d = settings.defects
    options = DefectOptions(
        corpus_window=settings.harvest.window, after_days=d.after_days, fix_title=d.fix_title,
        fix_labels=d.fix_labels, max_fix_lines=d.max_fix_lines, max_files=d.max_files,
        conf=settings.metrics.gold_conf[GoldProvenance.ESCAPED_DEFECT], limit=args.limit,
    )  # fmt: skip
    repos = args.repos or sorted({key.repo for key in store.pr_keys()})
    miner = DefectMiner(store, store, github_host(settings), lambda repo: git_reader(settings, repo), options)
    try:
        for repo in repos:
            if is_excluded(repo, settings.corpus.excluded):
                continue
            try:
                report = miner.mine(repo)
            except HostError as error:
                print(f"{repo}: stopped: {error}", file=sys.stderr)
                return 3
            print(f"{repo}: {report.listed} merged PRs listed, {report.fixes} bug fixes, {report.blamed_lines} lines "
                  f"blamed, {len(report.defects)} escaped defects; skipped {report.skipped or 'none'}")  # fmt: skip
            for defect in report.defects:
                issue = defect.issue
                print(f"  {defect.pr} {issue.path}:{issue.start_line}-{issue.end_line} <- fix #{defect.fix_pr} "
                      f"{defect.fix_title[:70]!r}")  # fmt: skip
    finally:
        store.close()
    return 0
