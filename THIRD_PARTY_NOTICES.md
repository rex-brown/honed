# Third-party notices

The project's own code is under the Apache License 2.0 (`LICENSE`), and the annotations in its dataset bundles under CC BY 4.0 (`DATA_LICENSE`). The seed review policy in `policy/`, and the improve loop's proposer prompts, adapt content from the following MIT-licensed work (pstack). Adapted files say so at the top. review-kit, below, inspired parts of the design; nothing was copied from it. `NOTICE` carries the attribution notices redistributors must keep. The two public benchmarks below are not part of the repository: `honed import-benchmark --fetch` downloads them, and a dataset bundle that carries their PRs (`honed bundle export`) names each benchmark and its license in its manifest and keeps their answer keys verbatim. The review comments and pull request text a bundle quotes are not licensed by the project: each keeps its author's login and URL (`DATASET.md`).

## pstack

- Source: https://github.com/cursor/plugins/tree/main/pstack (read at commit `69cf06fa253b`, 2026-09-29)
- Author: Lauren Tan (poteto)
- License: MIT
- Adapted into (each file names its sources on its first line):
  - `policy/prompts/finder.md`: `skills/interrogate/references/reviewer-prompt.md`, `skills/blast-radius/SKILL.md`
  - `policy/prompts/shared.md`: `skills/interrogate/SKILL.md`, `skills/interrogate/references/rubric.md`
  - `policy/prompts/intent.md`: `skills/interrogate/SKILL.md` (step 2)
  - `policy/prompts/lens_correctness.md`, `lens_root_cause.md`: `skills/interrogate/references/rubric.md`
  - `policy/prompts/lens_security.md`: `skills/interrogate/references/rubric.md`, `skills/poteto-mode/references/bugbot-triage.md`
  - `policy/prompts/lens_verification.md`: `skills/interrogate/references/rubric.md`, `skills/principle-test-behavior-not-implementation/SKILL.md`
  - `policy/prompts/lens_design.md`: `skills/interrogate/references/code-quality-review.md`, `skills/interrogate/references/rubric.md`
  - `policy/prompts/lens_comments.md`: `agents/comment-sicko.md`, `skills/no-comments/SKILL.md`
  - `policy/prompts/lang_typescript.md`: `skills/typescript-best-practices/SKILL.md` and `references/patterns.md`, `skills/principle-type-system-discipline/SKILL.md`
  - `policy/prompts/lang_cpp.md` (in part): `skills/principle-type-system-discipline/SKILL.md`
  - `policy/prompts/verifier.md`: `skills/interrogate/references/lead-judgment.md`, `skills/interrogate/SKILL.md` (step 5), `skills/blast-radius/SKILL.md`
  - `policy/prompts/writing.md`: `skills/unslop/SKILL.md`
  - `policy/lessons.yaml`: `skills/poteto-mode/references/bugbot-triage.md`, `skills/interrogate/references/code-quality-review.md`, `skills/typescript-best-practices/SKILL.md`, `skills/principle-test-behavior-not-implementation/SKILL.md`, `agents/comment-sicko.md`
  - Not adapted: `policy/prompts/lang_python.md`, `specialist.md`, `policy/config.toml`.
- The improve loop's proposer prompts (code, in `src/honed/learn/prompts/`) also adapt it:
  - `proposer.md`: `skills/hillclimb` and `skills/show-me-your-work`;
  - `lesson_miner.md`: `skills/reflect` and `skills/principle-encode-lessons-in-structure`.

```
MIT License

Copyright (c) 2026 Lauren Tan

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## review-kit (design inspiration only)

- Source: https://github.com/aowen14/review-kit
- Author: Alex Owen (https://github.com/aowen14)
- License: none published, so nothing from it is copied: no code and no text.
- What it inspired: the review-thread outcome taxonomy (`core/outcomes.py`), lessons that must cite their evidence (`policy/lessons.yaml`, `ARCHITECTURE.md` section 3), reading a repository's review rules from the base branch (`ARCHITECTURE.md` section 3), and the fixture-PR regression check (`scripts/replay_fixture.py`). Credited in `NOTICE` and `ARCHITECTURE.md` section 12.

## Martian Code Review Bench

- Source: https://github.com/withmartian/code-review-benchmark (read at commit `e616e849755441da38f18bf3adba2c9583b03803`, 2026-10-01): `offline/golden_comments/*.json` and `offline/results/benchmark_data.json`
- Author: Martian (withmartian.com)
- License: MIT
- Used by `src/honed/adapters/bench_martian.py`: its 50 offline-set PRs and 173 golden comments become test-split gold issues (`core/benchmarks.py` maps its severities and categories to ours). Its tools' reviews are not used. Its PRs' repos are held out of training (`[corpus.exclude] martian`).

```
MIT License

Copyright (c) 2025 Martian (withmartian.com)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## AACR-Bench

- Source: https://github.com/alibaba/aacr-bench (read at commit `68a569759289a83654a59d06db2a72910edf0a4a`, 2026-10-01): `dataset/positive_samples.json` and `dataset/negative_samples.json`
- Author: Alibaba (the AACR-Bench authors; https://arxiv.org/abs/2601.19494)
- License: Apache License 2.0 (the full text is the repository's `LICENSE`, which is the same license; AACR-Bench ships no NOTICE file)
- Used by `src/honed/adapters/bench_aacr.py`: its 200 PRs and 1,506 confirmed comments become test-split gold issues (`core/benchmarks.py` maps its categories to our severities and categories); the 639 comments its annotators rejected are kept in each PR's benchmark record, never as gold. Its 50 source repos are held out of training (`[corpus.exclude] aacr_bench`).

## scikit-learn (a test fixture)

- `tests/fixtures/github_sklearn.json`: recorded GitHub API responses for five scikit-learn pull requests (their metadata, review threads and diff patches), replayed by the harvester's offline tests. The patches are scikit-learn code.
- Source: https://github.com/scikit-learn/scikit-learn
- License: BSD 3-Clause

```
BSD 3-Clause License

Copyright (c) 2007-2026 The scikit-learn developers.
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

* Redistributions of source code must retain the above copyright notice, this
  list of conditions and the following disclaimer.

* Redistributions in binary form must reproduce the above copyright notice,
  this list of conditions and the following disclaimer in the documentation
  and/or other materials provided with the distribution.

* Neither the name of the copyright holder nor the names of its
  contributors may be used to endorse or promote products derived from
  this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```
