"""CI 只接受同一运行、完整且成功的候选证据。"""

from copy import deepcopy
import json
import os
from pathlib import Path
import shlex
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import ci_evidence as evidence


class CurrentCollectionTests(unittest.TestCase):
    """Use real product collection, independently of the receipt doubles below."""

    @classmethod
    def setUpClass(cls):
        cls.nodes = evidence.collect_nodeids()

    def test_consumed_cli_behaviors_are_in_the_actual_product_collection(self):
        expected = {
            "tests/test_kernel_installed_parent_only.py::test_installed_public_parent_only_reaches_approval_close_cleanup_and_normal_host_exit",
            "tests/test_kernel_query_cli.py::test_parent_selector_works_in_a_repo_and_across_directories",
            "tests/test_doctor_readiness.py::test_doctor_cli_separates_installation_and_execution_readiness",
            "tests/test_kernel_query_cli.py::test_history_keeps_raw_feedback_and_pages_without_losing_events",
        }
        scope = evidence.contract_scope(self.nodes)
        self.assertEqual({node.partition("[")[0] for node in scope}, expected)
        for base in expected:
            with self.subTest(base=base), self.assertRaisesRegex(ValueError, "Missing consumed"):
                evidence.contract_scope([node for node in self.nodes if node.partition("[")[0] != base])

    def test_default_make_entry_collects_actual_current_tests(self):
        result = subprocess.run(
            ["make", "test", f"PYTHON={sys.executable}", "PYTEST_ARGS=--collect-only -q"],
            cwd=evidence.PRODUCT_ROOT, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        nodes = [line for line in result.stdout.splitlines() if line.startswith("tests/") and "::" in line]
        self.assertTrue(nodes)
        self.assertEqual(len(nodes), len(set(nodes)))
        self.assertTrue(set(nodes) <= set(self.nodes))

    def test_named_make_groups_only_select_collected_current_targets(self):
        for group in ("policy", "state", "prompts", "locator", "github", "delivery", "run", "executor"):
            output = subprocess.check_output(
                ["make", "-n", "test-" + group, f"PYTHON={sys.executable}"],
                cwd=evidence.PRODUCT_ROOT, text=True, timeout=10,
            )
            command = next(shlex.split(line) for line in output.splitlines() if " -m pytest " in line)
            targets = [value for value in command[command.index("pytest") + 1:] if value.startswith("tests/")]
            self.assertTrue(targets, group)
            for target in targets:
                with self.subTest(group=group, target=target):
                    self.assertTrue(any(node.partition("[")[0] == target or node.startswith(target + "::")
                                        for node in self.nodes), target)

    def test_broken_selection_fails_instead_of_running_the_complete_suite(self):
        result = subprocess.run(
            ["make", "test", f"PYTHON={sys.executable}", "TEST_SCOPE=false", "PYTEST_ARGS=--collect-only -q"],
            cwd=evidence.PRODUCT_ROOT, capture_output=True, text=True, timeout=30,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(".invalid-test-selection", result.stdout + result.stderr)
        self.assertNotIn("::", result.stdout)


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.product = self.root / "product"
        for name in evidence.DEPENDENCY_FILES:
            path = self.product / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixed dependency input\n")
        self.identity = {
            "GITHUB_REPOSITORY": "example/product", "GITHUB_SHA": "a" * 40,
            "GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_WORKFLOW_REF": "example/product/.github/workflows/ci.yml@refs/heads/main",
        }
        self.enterContext(patch.dict(os.environ, self.identity))
        self.enterContext(patch.object(evidence, "PRODUCT_ROOT", self.product))
        self.nodes = [
            evidence.CONTRACT_TESTS[0], evidence.CONTRACT_TESTS[1],
            *[evidence.CONTRACT_TESTS[2] + f"[{value}]" for value in ("a", "b", "c", "d")],
            *evidence.CONTRACT_TESTS[3:],
            "tests/test_sample.py::TestGroup::test_value[a::b]",
            "tests/test_sample.py::test_plain",
        ]
        self.real_collect = evidence.collect_nodeids
        self.enterContext(patch.object(evidence, "collect_nodeids", return_value=sorted(self.nodes)))
        self.report = {
            "commit": {"returncode": 0, "stdout": self.identity["GITHUB_SHA"]},
            "python": {"version": sys.version, "executable": sys.executable},
            "packages": {"pytest": "9.0.2"}, "sqlite": {"version": "test"},
            "platform": "Linux", "os_release": {"VERSION_ID": "test"},
            "cpu_count": 2, "cpu_affinity_count": 2,
            "cgroup_cpu_max": None, "cgroup_memory_max": None,
            "git": {"stdout": "git version test"}, "bubblewrap": {"stdout": "test"},
            "memory": {"MemTotal": "8 GiB", "MemAvailable": "4 GiB"},
            "memfd": {"available": True}, "pidfd": {"available": True},
        }
        self.results = self.root / "artifacts"
        for index in range(2):
            folder = self.folder(index)
            folder.mkdir(parents=True)
            (folder / "environment.json").write_text(json.dumps(self.report))
            self.junit(folder, sorted(self.nodes)[index::2])
            evidence.record(folder, index, contract=True)

    def folder(self, index):
        return self.results / f"test-diagnostics-shard-{index}"

    def junit(self, folder, nodes, outcome=None, *, subtests=None):
        root = ET.Element("testsuites")
        suite = ET.SubElement(root, "testsuite", tests=str(len(nodes) + (subtests or 0)), errors="0", failures="0", skipped="0")
        if subtests is not None:
            properties = ET.SubElement(suite, "properties")
            ET.SubElement(properties, "property", name=evidence.PASSED_SUBTESTS_PROPERTY, value=str(subtests))
        for node in nodes:
            classname, name = evidence.junit_key(node)
            case = ET.SubElement(suite, "testcase", classname=classname, name=name)
            if outcome:
                ET.SubElement(case, outcome)
        ET.ElementTree(root).write(folder / "junit.xml", encoding="utf-8")

    def verify(self):
        return evidence.verify(self.results, selected="true", product="true", contract="true", result="success")

    def change_receipt(self, index, **changes):
        path = self.folder(index) / "evidence.json"
        receipt = json.loads(path.read_text())
        receipt.update(changes)
        path.write_text(json.dumps(receipt))

    def test_complete_candidate_is_accepted_without_running_tests_again(self):
        self.assertEqual(self.verify(), len(self.nodes))

    def test_all_current_contract_parameter_instances_are_required(self):
        missing = evidence.CONTRACT_TESTS[2] + "[d]"
        index = sorted(self.nodes).index(missing) % 2
        self.junit(self.folder(index), [node for node in sorted(self.nodes)[index::2] if node != missing])
        with self.assertRaisesRegex(ValueError, "coverage"):
            evidence.record(self.folder(index), index, contract=True)

    def test_failed_cancelled_skipped_unknown_and_missing_jobs_are_rejected(self):
        for result in ("failure", "cancelled", "skipped", "unknown", ""):
            with self.subTest(result=result), self.assertRaises(ValueError):
                evidence.verify(self.results, selected="true", product="true", contract="true", result=result)

    def test_documentation_and_private_only_checks_have_distinct_expected_results(self):
        for selected, result in (("false", "skipped"), ("true", "success")):
            self.assertEqual(evidence.verify(self.root / "absent", selected=selected, product="false", contract="false", result=result), 0)
        for selected, product, contract, result in (
            ("false", "false", "false", "success"), ("true", "false", "false", "skipped"),
            ("false", "true", "false", "skipped"), ("true", "false", "true", "success"),
            ("", "false", "false", "skipped"), ("true", "", "false", "success"),
        ):
            with self.subTest(values=(selected, product, contract, result)), self.assertRaises(ValueError):
                evidence.verify(self.results, selected=selected, product=product, contract=contract, result=result)

    def test_other_repository_candidate_run_attempt_or_workflow_is_rejected(self):
        path = self.folder(1) / "evidence.json"
        original = path.read_text()
        for name in self.identity:
            changed = self.identity | {name: "other"}
            self.change_receipt(1, identity=changed)
            with self.subTest(identity=name), self.assertRaisesRegex(ValueError, "identity"):
                self.verify()
            path.write_text(original)

    def test_same_run_reuses_successful_earlier_attempt_without_rewriting_identity(self):
        with patch.dict(os.environ, {"GITHUB_RUN_ATTEMPT": "2"}):
            self.change_receipt(1, identity=self.identity | {"GITHUB_RUN_ATTEMPT": "2"})
            self.assertEqual(self.verify(), len(self.nodes))
            original = json.loads((self.folder(0) / "evidence.json").read_text())
            self.assertEqual(original["identity"]["GITHUB_RUN_ATTEMPT"], "1")
            for invalid in ("0", "3", "", "other"):
                self.change_receipt(1, identity=self.identity | {"GITHUB_RUN_ATTEMPT": invalid})
                with self.subTest(attempt=invalid), self.assertRaisesRegex(ValueError, "identity"):
                    self.verify()

    def test_missing_duplicate_and_unexpected_shards_are_rejected(self):
        original = (self.folder(1) / "evidence.json").read_text()
        self.change_receipt(1, shard_index=0)
        with self.assertRaises(ValueError):
            self.verify()
        (self.folder(1) / "evidence.json").write_text(original)
        (self.folder(1) / "evidence.json").unlink()
        with self.assertRaises(OSError):
            self.verify()
        (self.folder(1) / "evidence.json").write_text(original)
        (self.results / "test-diagnostics-shard-2").mkdir()
        with self.assertRaises(ValueError):
            self.verify()

    def test_failed_skipped_error_and_duplicate_test_results_cannot_be_recorded(self):
        nodes = sorted(self.nodes)[0::2]
        for outcome in ("failure", "error", "skipped"):
            self.junit(self.folder(0), nodes, outcome)
            with self.subTest(outcome=outcome), self.assertRaises(ValueError):
                evidence.record(self.folder(0), 0, contract=True)
        self.junit(self.folder(0), nodes + [nodes[0]])
        with self.assertRaises(ValueError):
            evidence.record(self.folder(0), 0, contract=True)

    def test_empty_filtered_wrong_and_extra_coverage_are_rejected(self):
        nodes = sorted(self.nodes)[0::2]
        for changed in ([], nodes[:-1], sorted(self.nodes)[1::2], nodes + ["tests/other.py::test_extra"]):
            self.junit(self.folder(0), changed)
            with self.subTest(nodes=changed), self.assertRaises(ValueError):
                evidence.record(self.folder(0), 0, contract=True)

    def test_successful_subtests_preserve_parent_coverage_and_shard_counts(self):
        for index in range(2):
            nodes = sorted(self.nodes)[index::2]
            self.junit(self.folder(index), nodes, subtests=2)
            evidence.record(self.folder(index), index, contract=True)
            receipt = json.loads((self.folder(index) / "evidence.json").read_text())
            self.assertEqual(receipt["selected_count"], len(nodes))
        self.assertEqual(self.verify(), len(self.nodes))
        nodes = sorted(self.nodes)[0::2]
        for changed in (nodes[:-1], nodes + ["tests/other.py::test_extra"], nodes + [nodes[0]], sorted(self.nodes)[1::2]):
            self.junit(self.folder(0), changed, subtests=2)
            with self.subTest(nodes=changed), self.assertRaises(ValueError):
                evidence.record(self.folder(0), 0, contract=True)
            self.change_receipt(0, junit_sha256=evidence.file_digest(self.folder(0) / "junit.xml"))
            with self.assertRaises(ValueError):
                self.verify()

    def test_subtest_counts_require_explicit_exact_valid_metadata(self):
        folder = self.folder(0)
        for value in (None, "-1", "invalid", "", "1", "3", "duplicate", "unknown"):
            self.junit(folder, sorted(self.nodes)[0::2], subtests=2)
            tree = ET.parse(folder / "junit.xml")
            properties = tree.getroot().find("testsuite/properties")
            counter = properties.find("property")
            if value is None:
                properties.remove(counter)
            elif value == "duplicate":
                properties.append(deepcopy(counter))
            elif value == "unknown":
                counter.set("name", "agent-run-pytest-passed-subtests-unknown")
            else:
                counter.set("value", value)
            tree.write(folder / "junit.xml", encoding="utf-8")
            with self.subTest(count=value), self.assertRaises(ValueError):
                evidence.record(folder, 0, contract=True)
            self.change_receipt(0, junit_sha256=evidence.file_digest(folder / "junit.xml"))
            with self.assertRaises(ValueError):
                self.verify()

    def test_artifact_or_dependency_changes_invalidate_evidence(self):
        for name in ("junit.xml", "environment.json"):
            path = self.folder(1) / name
            original = path.read_bytes()
            path.write_bytes(original + b"\n")
            with self.subTest(file=name), self.assertRaises(ValueError):
                self.verify()
            path.write_bytes(original)
        (self.product / evidence.DEPENDENCY_FILES[0]).write_text("changed dependencies")
        with self.assertRaises(ValueError):
            self.verify()

    def test_environment_and_collection_must_match_between_shards(self):
        path = self.folder(1) / "environment.json"
        report = deepcopy(self.report)
        report["packages"]["pytest"] = "other"
        path.write_text(json.dumps(report))
        evidence.record(self.folder(1), 1, contract=True)
        with self.assertRaisesRegex(ValueError, "environment"):
            self.verify()
        path.write_text(json.dumps(self.report))
        evidence.record(self.folder(1), 1, contract=True)
        self.change_receipt(1, collection_sha256="other")
        with self.assertRaisesRegex(ValueError, "collection"):
            self.verify()

    def test_boot_memory_and_live_load_do_not_change_environment_equivalence(self):
        report = deepcopy(self.report)
        report["memory"] = {"MemTotal": "8127928 kB", "MemAvailable": "other"}
        report["load_average"] = [10, 11, 12]
        report["user_namespace"] = {"bubblewrap_probe": {"stderr": "process 123"}}
        (self.folder(1) / "environment.json").write_text(json.dumps(report))
        evidence.record(self.folder(1), 1, contract=True)
        self.assertEqual(self.verify(), len(self.nodes))
        report["cgroup_memory_max"] = "different limit"
        (self.folder(1) / "environment.json").write_text(json.dumps(report))
        evidence.record(self.folder(1), 1, contract=True)
        with self.assertRaisesRegex(ValueError, "environment"):
            self.verify()

    def test_other_commit_or_python_environment_cannot_be_recorded(self):
        path = self.folder(1) / "environment.json"
        for key, value in (("commit", {"returncode": 0, "stdout": "other"}), ("python", {"version": "other", "executable": sys.executable})):
            path.write_text(json.dumps(self.report | {key: value}))
            with self.subTest(key=key), self.assertRaises(ValueError):
                evidence.record(self.folder(1), 1, contract=True)

    def test_contract_scope_cannot_be_replaced_by_a_smaller_list(self):
        self.change_receipt(1, contract_nodeids=self.nodes[:1])
        with self.assertRaises(ValueError):
            self.verify()
        smaller = sorted(self.nodes[:3])
        for index in range(2):
            self.change_receipt(index, contract_nodeids=smaller)
        with self.assertRaises(ValueError):
            self.verify()

    def test_junit_mapping_preserves_classes_and_parameter_separators(self):
        self.assertEqual(evidence.junit_key("tests/test_sample.py::TestGroup::test_value[a::b]"), ("tests.test_sample.TestGroup", "test_value[a::b]"))

    def run_pytest(self, folder, *options):
        source_tests = Path(__file__).resolve().parents[2] / "tests"
        env = dict(os.environ, PYTHONPATH=str(source_tests), PYTEST_ADDOPTS="", PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "support.sharding",
             f"--junitxml={folder / 'junit.xml'}", *options],
            cwd=self.product, env=env, capture_output=True, text=True, timeout=30,
        )

    def test_real_pytest_shards_and_junit_form_complete_evidence(self):
        # 同时保护 fixture/unittest subtests、参数地址和父测试分片。
        (self.product / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
        (self.product / "tests/test_sample.py").write_text(
            'import pytest\nimport unittest\n@pytest.mark.parametrize("value", ["a::b", "[c]"])\n'
            'def test_values(value): assert value\n'
            'class TestGroup:\n    def test_value(self): assert True\n'
            'def test_plain(): assert True\n'
            'def test_aaa_subtests(subtests):\n'
            '    for value in (1, 2):\n'
            '        with subtests.test(value=value): assert value > 0\n'
            'class TestSubtests(unittest.TestCase):\n'
            '    def test_values(self):\n'
            '        for value in (1, 2):\n'
            '            with self.subTest(value=value): self.assertGreater(value, 0)\n'
        )
        with patch.object(evidence, "collect_nodeids", self.real_collect):
            for index in range(2):
                folder = self.folder(index)
                workers = ("-n", "2", "--dist", "worksteal") if index else ()
                result = self.run_pytest(folder, "--test-shard-count=2", f"--test-shard-index={index}", *workers)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                suite = ET.parse(folder / "junit.xml").getroot().find("testsuite")
                self.assertEqual(int(suite.get("tests")), len(suite.findall("testcase")) + 2)
                counter = suite.find(f"./properties/property[@name='{evidence.PASSED_SUBTESTS_PROPERTY}']")
                self.assertEqual(counter.get("value"), "2")
                evidence.record(folder, index, contract=False)
        self.assertEqual(evidence.verify(self.results, selected="true", product="true", contract="false", result="success"), 6)

    def test_real_pytest_subtest_failures_skips_and_errors_are_rejected(self):
        (self.product / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
        (self.product / "tests/test_sample.py").write_text(
            'import pytest\n'
            'def test_failure(subtests):\n'
            '    with subtests.test(): assert False\n'
            'def test_skipped(subtests):\n'
            '    with subtests.test(): pytest.skip("subtest skipped")\n'
            '@pytest.fixture\ndef broken_teardown():\n'
            '    yield\n    raise RuntimeError("teardown failed")\n'
            'def test_error(subtests, broken_teardown):\n'
            '    with subtests.test(): assert True\n'
        )
        folder = self.folder(0)
        for index in range(2):
            self.change_receipt(index, dependencies_sha256=evidence.dependency_digest())
        for outcome in ("failure", "skipped", "error"):
            result = self.run_pytest(folder, "-k", f"test_{outcome}")
            self.assertEqual(result.returncode, 0 if outcome == "skipped" else 1, result.stdout + result.stderr)
            self.assertIsNotNone(ET.parse(folder / "junit.xml").getroot().find(f".//testcase/{outcome}"))
            with self.subTest(outcome=outcome), self.assertRaisesRegex(ValueError, "failed, errored or skipped"):
                evidence.record(folder, 0, contract=True)
            self.change_receipt(0, junit_sha256=evidence.file_digest(folder / "junit.xml"))
            with self.assertRaisesRegex(ValueError, "failed, errored or skipped"):
                self.verify()


if __name__ == "__main__":
    unittest.main()
