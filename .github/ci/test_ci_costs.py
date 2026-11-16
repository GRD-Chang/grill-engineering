"""CI 成本的时间边界、失败缺失及只读元数据回归。"""

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ci_costs import api, collect, main, markdown, step_phase, summarize


def stamp(second):
    return f"2026-10-16T10:{second // 60:02}:{second % 60:02}Z"


def job(name, start, end, *, steps=(), conclusion="success", created=0, runner=1):
    return {
        "id": start + 1, "name": name, "created_at": stamp(created), "started_at": stamp(start),
        "completed_at": stamp(end) if end is not None else None,
        "status": "completed" if end is not None else "in_progress", "conclusion": conclusion,
        "runner_id": runner,
        "steps": [{"name": name, "started_at": stamp(first), "completed_at": stamp(last),
                   "status": "completed", "conclusion": state} for name, first, last, state in steps],
    }


class CostTests(unittest.TestCase):
    def test_single_product_job_has_full_test_and_preparation_costs(self):
        self.assertEqual(step_phase('product-tests', 'Run tests'), 'tests')
        jobs = [job('product-tests', 5, 90, steps=(
            ('Install product and development tools', 10, 25, 'success'),
            ('Run tests', 25, 80, 'success'),
            ('Record complete test evidence', 80, 85, 'success'),))]
        report = summarize(self.run, jobs, stamp(95))
        phases = {item['name']: item for item in report['jobs'][0]['steps']}
        self.assertEqual(phases['Run tests']['seconds'], 55)
        self.assertEqual(phases['Install product and development tools']['phase'], 'dependencies')
        self.assertEqual(phases['Record complete test evidence']['status'], 'completed')
        self.assertEqual(report['wall_seconds'], 90)


    def test_product_package_setup_and_validation_have_separate_cost_phases(self):
        self.assertEqual(step_phase("quality", "Prepare product package test dependencies"), "dependencies")
        self.assertEqual(step_phase("product-tests", "Run sync tests"), "checks")
        self.assertEqual(step_phase("quality", "Run private-only sync tests"), "checks")

    def setUp(self):
        self.run = {"id": 9, "run_attempt": 1, "head_sha": "candidate", "created_at": stamp(0),
                    "run_started_at": stamp(0), "status": "completed", "conclusion": "success"}

    def test_parallel_runner_total_differs_from_complete_check_path(self):
        jobs = [
            job("product-tests", 5, 90, steps=(
                ("Set up job", 7, 10, "success"), ("Set up Python", 10, 20, "success"),
                ("Run tests", 30, 85, "success"))),
            job("diagnostic", 6, 95, steps=(("Run diagnostic probe", 25, 30, "success"),)),
            job("quality", 100, 110, created=96, steps=(("Run tests", 104, 108, "success"),)),
        ]
        report = summarize(self.run, jobs, stamp(120))
        self.assertTrue(report["complete"])
        self.assertEqual(report["wall_seconds"], 110)
        self.assertEqual(report["necessary_checks_seconds"], 108)
        self.assertEqual(report["runner_seconds"], 184)
        first = report["jobs"][0]
        self.assertEqual((first["queue_seconds"], first["cold_start_seconds"]), (5, 5))
        phases = {step["name"]: step for step in first["steps"]}
        self.assertEqual(phases["Set up Python"]["phase"], "dependencies")
        self.assertEqual(phases["Run tests"]["seconds"], 55)
        self.assertEqual(report["jobs"][2]["queue_seconds"], 4)

    def test_cancelled_tests_keep_elapsed_but_missing_evidence_is_not_zero(self):
        self.run["conclusion"] = "failure"
        jobs = [job("product-tests", 5, 50, conclusion="cancelled", steps=(
            ("Run tests", 20, 48, "cancelled"), ("Record complete test evidence", 48, 48, "skipped")))]
        report = summarize(self.run, jobs, stamp(100))
        phases = {step["name"]: step for step in report["jobs"][0]["steps"]}
        self.assertEqual(phases["Run tests"]["seconds"], 28)
        self.assertEqual(phases["Run tests"]["conclusion"], "cancelled")
        self.assertIsNone(phases["Record complete test evidence"]["seconds"])
        self.assertEqual(phases["Set up Python"]["status"], "missing")
        self.assertIsNone(report["necessary_checks_seconds"])
        self.assertIn("取消/跳过/缺失", markdown(report))

    def test_in_progress_quality_is_lower_bound_including_its_observed_usage(self):
        self.run.update(status="in_progress", conclusion=None)
        jobs = [job("quality", 20, None, conclusion=None)]
        report = summarize(self.run, jobs, stamp(35))
        self.assertFalse(report["complete"])
        self.assertEqual(report["runner_seconds"], 15)
        self.assertEqual(report["wall_seconds"], 35)
        self.assertIsNone(report["necessary_checks_seconds"])
        self.assertIn("已观测下界", markdown(report))

    def test_rerun_does_not_count_idle_gap_from_original_run_creation(self):
        self.run.update(run_attempt=2, run_started_at=stamp(100))
        report = summarize(self.run, [job("quality", 105, 120)], stamp(150))
        self.assertEqual(report["wall_seconds"], 20)
        self.assertEqual(report["runner_seconds"], 15)
        self.assertIn("仅统计本 attempt", markdown(report))

    def test_no_jobs_and_missing_or_reversed_timestamps_stay_incomplete(self):
        report = summarize(self.run, [], stamp(10))
        self.assertFalse(report["complete"])
        self.assertIsNone(report["runner_seconds"])
        invalid = job("quality", 20, 10)
        invalid["created_at"] = None
        report = summarize(self.run, [invalid], stamp(30))
        self.assertFalse(report["complete"])
        self.assertIsNone(report["jobs"][0]["queue_seconds"])
        self.assertIsNone(report["runner_seconds"])

    def test_skipped_job_without_runner_has_no_allocated_usage(self):
        skipped = job("product-tests", 0, 0, conclusion="skipped", runner=None)
        report = summarize(self.run, [skipped], stamp(10))
        self.assertEqual(report["runner_seconds"], 0)
        self.assertIsNone(report["jobs"][0]["cold_start_seconds"])

    @patch("ci_costs.api")
    def test_api_collects_only_requested_attempt_with_pagination(self, api):
        self.run["run_attempt"] = 2
        api.side_effect = [self.run, {"total_count": 2, "jobs": [{"id": 1}]},
                           {"total_count": 2, "jobs": [{"id": 2}]}]
        run, jobs = collect("example/repository", 9, 2)
        self.assertEqual((run["run_attempt"], len(jobs)), (2, 2))
        paths = [call.args[0] for call in api.call_args_list]
        self.assertEqual(paths[0], "/repos/example/repository/actions/runs/9/attempts/2")
        self.assertTrue(all("/attempts/2/jobs?" in path for path in paths[1:]))

    @patch("ci_costs.api")
    def test_api_identity_duplicates_and_incomplete_pages_are_missing_observations(self, api):
        scenarios = [
            [{**self.run, "run_attempt": 2}],
            [self.run, {"total_count": 2, "jobs": [{"id": 1}, {"id": 1}]}],
            [self.run, {"total_count": 1, "jobs": []}],
        ]
        for replies in scenarios:
            with self.subTest(replies=replies):
                api.side_effect = deepcopy(replies)
                with self.assertRaises(ValueError):
                    collect("example/repository", 9, 1)

    @patch("ci_costs.collect", side_effect=OSError("metadata unavailable"))
    def test_api_failure_writes_missing_report_and_fails_visibly(self, collect):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "report"
            summary = Path(temp) / "step-summary"
            with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}), patch("builtins.print"), \
                    patch("sys.argv", ["ci_costs.py", "--run-id", "9", "--output", str(output)]):
                with self.assertRaises(SystemExit) as error:
                    main()
                self.assertEqual(error.exception.code, 1)
            report = json.loads((output / "costs.json").read_text())
            self.assertFalse(report["complete"])
            self.assertEqual(report["error"], "metadata unavailable")
            self.assertIn("元数据缺失", summary.read_text())

    @patch("ci_costs.subprocess.run")
    def test_read_only_api_reuses_inherited_gh_authentication(self, execute):
        execute.return_value.returncode = 0
        execute.return_value.stdout = '{"id": 9}'
        self.assertEqual(api("/repos/example/repository/actions/runs/9"), {"id": 9})
        command = execute.call_args.args[0]
        self.assertEqual(command[:4], ["gh", "api", "--method", "GET"])
        self.assertNotIn("env", execute.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
