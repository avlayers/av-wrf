"""Forecast pull decisions while the solver image is publishing."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from scripts.forecast_image import ImageRun, decide, main, parse_runs
from scripts.forecast_job import load_targets, solver_label

_SHA = "abc123"
_ROOT = Path(__file__).resolve().parents[2]
_OPS = _ROOT / "config" / "operations" / "forecasts.yaml"


def _run(status: str, conclusion: str, sha: str = _SHA) -> ImageRun:
    return ImageRun(sha, status, conclusion)


class TestForecastImage(unittest.TestCase):
    def test_waits_while_a_newer_publish_is_still_running(self) -> None:
        runs = (_run("in_progress", "", sha="newer"), _run("completed", "success", sha="older"))
        decision = decide(runs, "older", published=True, asked=False, confirmed=False)
        self.assertEqual(decision.action, "wait")

    def test_pulls_a_current_tag_when_nothing_is_publishing(self) -> None:
        decision = decide(
            (_run("completed", "success"),),
            _SHA,
            published=True,
            asked=False,
            confirmed=False,
        )
        self.assertEqual(decision.action, "pull")

    def test_dispatches_once_when_the_tag_and_the_run_are_missing(self) -> None:
        missing = decide((), _SHA, published=False, asked=False, confirmed=False)
        waiting = decide((), _SHA, published=False, asked=True, confirmed=False)
        self.assertEqual(missing.action, "dispatch")
        self.assertEqual(waiting.action, "wait")

    def test_fails_a_commit_whose_images_run_did_not_publish(self) -> None:
        decision = decide(
            (_run("completed", "failure"),),
            _SHA,
            published=True,
            asked=False,
            confirmed=False,
        )
        self.assertEqual(decision.action, "fail")

    def test_newest_run_for_this_commit_wins(self) -> None:
        runs = (_run("completed", "success"), _run("completed", "failure"))
        decision = decide(runs, _SHA, published=True, asked=False, confirmed=False)
        self.assertEqual(decision.action, "pull")

    def test_waits_for_registry_lag_after_a_successful_publish(self) -> None:
        decision = decide(
            (_run("completed", "success"),),
            _SHA,
            published=False,
            asked=True,
            confirmed=False,
        )
        self.assertEqual(decision.action, "wait")

    def test_parses_a_null_conclusion_and_fails_a_bad_publish(self) -> None:
        queued = {"workflow_runs": [{"head_sha": _SHA, "status": "queued", "conclusion": None}]}
        self.assertEqual(parse_runs(queued)[0].conclusion, "")
        failed = {
            "workflow_runs": [{"head_sha": _SHA, "status": "completed", "conclusion": "failure"}]
        }
        stderr = io.StringIO()
        with (
            redirect_stderr(stderr),
            patch("sys.stdin", io.StringIO(json.dumps(failed))),
            self.assertRaises(SystemExit) as caught,
        ):
            main(["--sha", _SHA, "--published", "1", "--asked", "0", "--confirmed", "0"])
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("did not publish", stderr.getvalue())

    def test_solver_label_honors_both_spot_values(self) -> None:
        target = load_targets(_OPS)[0]
        self.assertIn("spot=true", solver_label("run-1", target))
        self.assertIn("spot=false", solver_label("run-1", replace(target, spot=False)))

    def test_mirror_asks_the_image_gate(self) -> None:
        forecast = (_ROOT / ".github" / "workflows" / "forecast.yml").read_text(encoding="utf-8")
        mirror = forecast.split("\n  mirror:", 1)[1].split("\n  prepare:", 1)[0]
        self.assertIn("scripts.forecast_image", mirror)
        self.assertIn('--confirmed "$confirmed"', mirror)
        self.assertLess(mirror.index("runs-on/action@"), mirror.index("actions/checkout@"))

    def test_confirms_once_before_pulling_a_commit_with_no_images_run(self) -> None:
        other = (_run("completed", "success", sha="older"),)
        first = decide(other, _SHA, published=True, asked=False, confirmed=False)
        second = decide(other, _SHA, published=True, asked=False, confirmed=True)
        self.assertEqual(first.action, "confirm")
        self.assertEqual(second.action, "pull")
