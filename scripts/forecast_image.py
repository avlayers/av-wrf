"""Decide whether a forecast pulls, waits, or rebuilds the solver image."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import cast


@dataclass(frozen=True)
class ImageRun:
    head_sha: str
    status: str
    conclusion: str


@dataclass(frozen=True)
class ImageDecision:
    action: str
    reason: str


def decide(
    runs: tuple[ImageRun, ...],
    sha: str,
    *,
    published: bool,
    asked: bool,
    confirmed: bool,
) -> ImageDecision:
    """Return pull, wait, dispatch, confirm, or fail. Runs are newest first."""
    mine = next((run for run in runs if run.head_sha == sha), None)
    if mine is not None and mine.status == "completed" and mine.conclusion != "success":
        return ImageDecision("fail", "images run for this commit did not publish")
    if any(run.status != "completed" for run in runs):
        return ImageDecision("wait", "an images run is still publishing")
    if published and mine is None and not confirmed:
        return ImageDecision("confirm", "this commit has no images run yet")
    if published:
        return ImageDecision("pull", "solver tag is current")
    if mine is not None and mine.conclusion == "success":
        return ImageDecision("wait", "published tag is not visible yet")
    if not asked:
        return ImageDecision("dispatch", "solver tag is missing")
    return ImageDecision("wait", "dispatched images run has not appeared")


def parse_runs(payload: object) -> tuple[ImageRun, ...]:
    """Read a GitHub workflow-runs list. In-progress conclusions are null."""
    if not isinstance(payload, dict):
        raise ValueError("images runs payload must be an object")
    raw = payload.get("workflow_runs")
    if not isinstance(raw, list):
        raise ValueError("images runs payload must list workflow_runs")
    return tuple(_run(item) for item in raw)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sha", required=True)
    parser.add_argument("--published", choices=("0", "1"), required=True)
    parser.add_argument("--asked", choices=("0", "1"), required=True)
    parser.add_argument("--confirmed", choices=("0", "1"), required=True)
    args = parser.parse_args(argv)
    decision = decide(
        parse_runs(cast(object, json.load(sys.stdin))),
        str(args.sha),
        published=args.published == "1",
        asked=args.asked == "1",
        confirmed=args.confirmed == "1",
    )
    if decision.action == "fail":
        print(decision.reason, file=sys.stderr)
        raise SystemExit(1)
    print(decision.action)


def _run(item: object) -> ImageRun:
    if not isinstance(item, dict):
        raise ValueError("workflow run must be an object")
    return ImageRun(_text(item, "head_sha"), _text(item, "status"), _text(item, "conclusion"))


def _text(item: dict[str, object], key: str) -> str:
    value = item.get(key)
    if value is None:
        return ""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a string")
    return value


if __name__ == "__main__":
    main()
