"""A stand-in for the `claude` CLI: replays scripted stream-json runs and records how it was invoked.

The scenario file (`FAKE_CLAUDE_SCENARIO`) holds {"runs": [{"events": [...], "exit": 0, "stderr": ""}, ...]}; run i
answers the i-th invocation (the last one repeats). Each invocation appends {"argv", "env", "stdin", "cwd_files"} to
the JSON-lines file `FAKE_CLAUDE_LOG`.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
from pathlib import Path


def main() -> int:
    scenario = json.loads(Path(os.environ["FAKE_CLAUDE_SCENARIO"]).read_text())
    log = Path(os.environ["FAKE_CLAUDE_LOG"])
    runs = scenario["runs"]
    record = json.dumps({
        "argv": sys.argv[1:],
        "env": dict(os.environ),
        "stdin": sys.stdin.read(),
        "cwd_files": sorted(os.listdir(os.getcwd())),
    }) + "\n"  # fmt: skip
    # Counting the log and appending to it happen under one lock, so invocations running in parallel (the finder
    # panel) each take a distinct run instead of racing for the same index.
    with log.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        count = sum(1 for _ in handle)
        run = runs[min(count, len(runs) - 1)]
        handle.write(record)
        handle.flush()
        fcntl.flock(handle, fcntl.LOCK_UN)
    for event in run.get("events", []):
        print(json.dumps(event))
    sys.stderr.write(run.get("stderr", ""))
    return int(run.get("exit", 0))


if __name__ == "__main__":
    sys.exit(main())
