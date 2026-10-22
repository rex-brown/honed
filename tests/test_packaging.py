"""Installing the project runs nothing from outside the lockfile (ARCHITECTURE.md section 8, Dependencies). `uv sync`
builds the project itself with the backend `[build-system]` names, and any backend package is resolved from the index
at that moment, outside `uv.lock` and unchecked by its hashes, except `uv_build` alone, which uv runs from its own
binary. The requirement names the series `[tool.uv] required-version` allows, so every uv that may run here builds
with the backend it carries and none warns that its version is outside the requirement."""

from __future__ import annotations

import tomllib

from reviewkit import ROOT


def test_the_project_builds_with_the_backend_inside_uv():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    uv_series = project["tool"]["uv"]["required-version"]
    assert project["build-system"] == {"requires": [f"uv_build{uv_series}"], "build-backend": "uv_build"}
