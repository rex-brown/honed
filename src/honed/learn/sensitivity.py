"""The sensitivity check (ARCHITECTURE.md section 6; METRICS.md sections 3 and 6; pstack `hillclimb`): before the eval
is trusted, it must separate the incumbent from a deliberately weakened policy by more than the noise floor.

- Noise floor: the incumbent evaluated K >= 2 times with fresh model samples (`--samples`). For each pair of samples,
  the standard deviation of delta-S over the paired bootstrap's resamples: with identical policies, every per-PR
  difference is model-sampling noise, so this estimates the spread of delta-S between two identical policies. sigma
  pools the pairs (the root mean square of their standard deviations); more samples estimate it better. min_gain is
  max(`[gate] min_gain_floor`, `min_gain_noise_multiplier` x sigma); it goes into the report and the decision log,
  never into honed.toml (only a human changes that).
- Weakened policy: the verifier off, every lesson removed, every stage at the lowest effort. The eval separates
  when the weakened policy scores lower than sample 0 by more than min_gain and the bootstrap interval of delta-S
  excludes 0.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import statistics
from collections.abc import Sequence
from dataclasses import replace
from itertools import combinations
from typing import Any

from honed.core.evals import Decision, EvalRun
from honed.core.policy import EFFORTS, Policy, derive
from honed.core.scoring import headline, min_gain
from honed.learn.eval_report import ReportSettings, paired

LOWEST_EFFORT = EFFORTS[0]


def weaken(policy: Policy) -> Policy:
    """The seed with its verifier off, no lessons, and every stage at the lowest effort."""
    config = policy.config
    weakened = replace(
        config,
        members=tuple(replace(m, effort=LOWEST_EFFORT) for m in config.members),
        intent=replace(config.intent, effort=LOWEST_EFFORT),
        verifier=replace(config.verifier, enabled=False, effort=LOWEST_EFFORT),
    )
    return derive(policy, config=weakened, lessons=())


def analyze(seeds: Sequence[EvalRun], weak: EvalRun, settings: ReportSettings, *, floor: float,
            multiplier: float) -> dict[str, Any]:  # fmt: skip
    """`seeds`: the incumbent at samples 0, 1, ... (at least two); `weak`: the weakened policy at sample 0."""
    if len(seeds) < 2:
        raise ValueError("the noise floor needs at least two samples of the policy")
    pairs = []
    for i, j in combinations(range(len(seeds)), 2):
        boot, info = paired(seeds[j], seeds[i], settings)
        pairs.append((boot, {**info, "samples": [seeds[i].sample, seeds[j].sample]}))
    sigma = math.sqrt(sum(boot.sd**2 for boot, _ in pairs) / len(pairs))
    gain = min_gain(sigma, floor=floor, multiplier=multiplier)
    seed = seeds[0]
    weak_boot, weak_info = paired(weak, seed, settings)
    separates = weak_boot.delta <= -gain and weak_boot.high < 0
    s_values = [headline(s.results, settings.params) for s in seeds]
    return {
        "noise_floor": {
            "samples": len(seeds), "S_samples": [round(v, 4) for v in s_values],
            "pairs": [info for _, info in pairs], "sigma": round(sigma, 4),
            "abs_delta_S_max": round(max(abs(boot.delta) for boot, _ in pairs), 4),
            "S_sd": round(statistics.pstdev(s_values), 4),
        },
        "min_gain": round(gain, 4),
        "min_gain_rule": f"max({floor}, {multiplier} x sigma)",
        "weakened": weak_info,
        "separates": separates,
        "verdict": (f"the eval separates the weakened policy: delta-S {weak_boot.delta:+.4f} beyond -min_gain "
                    f"{-gain:.4f}, interval [{weak_boot.low:+.4f}, {weak_boot.high:+.4f}] below 0") if separates
        else (f"the eval does NOT separate the weakened policy: delta-S {weak_boot.delta:+.4f}, min_gain {gain:.4f}, "
              f"interval [{weak_boot.low:+.4f}, {weak_boot.high:+.4f}]"),
    }  # fmt: skip


def decisions(seeds: Sequence[EvalRun], weak: EvalRun, result: dict[str, Any]) -> list[Decision]:
    """The decision log's rows for the sensitivity runs: the noise floor, then the separation."""
    now = dt.datetime.now(dt.UTC).isoformat()
    noise, weak_info = result["noise_floor"], result["weakened"]
    seed = seeds[0]
    deltas = ", ".join(f"{p['samples'][1]} vs {p['samples'][0]}: {p['delta_S']:+.4f}" for p in noise["pairs"])
    return [
        Decision(
            None, now,
            hypothesis=f"{len(seeds)} evaluations of the policy with fresh model samples differ only by noise.",
            change=f"none: policy {seed.policy_hash[:12]}, samples {', '.join(str(s.sample) for s in seeds)} "
                   f"(runs {', '.join(s.id for s in seeds)})",
            before=f"S={noise['S_samples'][0]}", after=f"S={noise['S_samples'][1:]}",
            delta=deltas,
            gate=json.dumps({"sigma": noise["sigma"], "min_gain": result["min_gain"], "samples": len(seeds)}),
            verdict="noise floor measured",
            note=f"min_gain = {result['min_gain_rule']} = {result['min_gain']}; not written to honed.toml",
        ),
        Decision(
            None, now,
            hypothesis="The eval separates a deliberately weakened policy (verifier off, lessons removed, lowest "
                       "effort) from the seed by more than the noise floor.",
            change=f"weakened policy {weak.policy_hash[:12]} (run {weak.id}) vs seed {seed.policy_hash[:12]} "
                   f"(run {seed.id})",
            before=f"S={weak_info['S_incumbent']}", after=f"S={weak_info['S_candidate']}",
            delta=f"{weak_info['delta_S']:+.4f} [{weak_info['ci'][0]:+.4f}, {weak_info['ci'][1]:+.4f}]",
            gate=json.dumps({"delta_beyond_min_gain": weak_info["delta_S"] <= -result["min_gain"],
                             "interval_below_zero": weak_info["ci"][1] < 0}),
            verdict="separates" if result["separates"] else "does not separate",
            note=result["verdict"],
        ),
    ]  # fmt: skip
