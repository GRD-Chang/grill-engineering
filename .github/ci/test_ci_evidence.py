"""CI 只接受同一运行、完整且成功的候选证据。"""

from copy import deepcopy
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import json
import os
from pathlib import Path
import shlex
import sys
import subprocess
import tempfile
import unittest
from typing import ClassVar
from unittest.mock import patch
import xml.etree.ElementTree as ET

import ci_evidence as evidence
import test_scope as scope


class LocalFeedbackScopeTests(unittest.TestCase):
    def test_fast_prompt_contract_keeps_real_role_allocation_in_the_full_scope(self):
        fast = scope.selection('fast')
        self.assertEqual(fast, tuple(dict.fromkeys(fast)))
        self.assertTrue(set(scope.PROMPT_CONTRACT_TESTS).issubset(fast))
        allocation = 'tests/test_kernel_prompt_facts.py'
        self.assertNotIn(allocation, fast)
        self.assertIn(allocation, scope.selection('prompts'))
        self.assertTrue(set(scope.PROMPT_CONTRACT_TESTS).issubset(scope.selection('prompts')))

    def test_fast_scope_keeps_its_policy_state_and_query_contracts(self):
        fast = set(scope.selection('fast'))
        self.assertTrue(all(set(scope.selection(name)).issubset(fast)
            for name in ('policy', 'state', 'locator')))
        self.assertIn('tests/test_budget_policy.py', scope.selection('policy'))


class CollectionFailureTests(unittest.TestCase):
    def test_failed_collection_keeps_the_actual_output_for_diagnosis(self):
        failed = subprocess.CompletedProcess([], 2, 'collection error detail', 'import error detail')
        with patch.object(evidence.subprocess, 'run', return_value=failed):
            with self.assertRaises(ValueError) as caught:
                evidence.collect_nodeids()
        self.assertIn('Product collection failed (2)', str(caught.exception))
        self.assertIn(failed.stdout, str(caught.exception))
        self.assertIn(failed.stderr, str(caught.exception))


class CurrentCollectionTests(unittest.TestCase):
    """Use real product collection, independently of the receipt doubles below."""

    nodes: ClassVar[list[str]]

    @classmethod
    def setUpClass(cls):
        cls.nodes = evidence.collect_nodeids()

    def test_consumed_cli_behaviors_are_in_the_actual_product_collection(self):
        expected = {
            "tests/test_kernel_foreground_entry.py::test_installed_foreground_parent_only_public_lifecycle",
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

    def test_native_contracts_are_exact_current_subsets(self):
        targets = scope.selection('native-contract')
        self.assertEqual(targets, scope.NATIVE_CONTRACT_TESTS)
        self.assertEqual(len(targets), len(set(targets)))
        selected = evidence.collect_nodeids(targets)
        self.assertTrue(set(selected) < set(self.nodes))
        self.assertTrue(all(any(node == target or node.startswith(target + '[') for node in selected)
                            for target in targets))
        self.assertIn('tests/test_repository_save_fifo.py::test_native_calling_task_clock_uses_linux_top_level_offsets', targets)
        self.assertIn('tests/test_repository_save_fifo.py::test_native_nonleader_thread_clock_uses_its_own_top_level_offsets', targets)

    def test_fixed_repair_scope_is_the_exact_23_current_parameters_not_the_full_inventory(self):
        targets = scope.selection('fixed-repair-regression')
        self.assertEqual(targets, scope.FIXED_REPAIR_REGRESSION_TESTS)
        self.assertEqual(len(targets), 23)
        self.assertEqual(len(set(targets)), 23)
        self.assertEqual(evidence.collect_nodeids(targets), sorted(targets))
        self.assertTrue(set(targets) < set(self.nodes))
        native_nodes = [node for node in self.nodes if any(node == target or node.startswith(target + '[')
                        for target in scope.NATIVE_CONTRACT_TESTS)]
        self.assertEqual(len(native_nodes), 19)

    def test_local_contract_entry_stops_before_pytest_when_capability_check_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = root / 'calls.json'
            interpreter = root / 'python-control'
            interpreter.write_text(f'#!{sys.executable}\nimport json, sys\nfrom pathlib import Path\n'
                                   f'Path({str(calls)!r}).write_text(json.dumps(sys.argv[1:]))\nsys.exit(2)\n')
            interpreter.chmod(0o700)
            result = subprocess.run(['make', 'test-native-contracts', f'PYTHON={interpreter}'],
                                    cwd=evidence.PRODUCT_ROOT, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads(calls.read_text()),
                             ['tests/support/report_environment.py', '--require-native'])
            self.assertNotIn('-m pytest', result.stdout)

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
        self.results = self.root / "test-diagnostics"
        self.results.mkdir()
        self.enterContext(patch.object(evidence, 'tree_sha', return_value='b' * 40))
        self.full_run_artifacts()
        evidence.record(self.results, contract=True)

    def full_run_artifacts(self):
        folder = self.results
        self.report['ci_identity'] = dict(self.identity)
        self.report['user_namespace'] = {'bubblewrap_probe': {'returncode': 0}}
        (folder / 'environment.json').write_text(json.dumps(self.report))
        (folder / 'native-capabilities.json').write_text(json.dumps({
            'stage': 'native-capabilities', 'status': 'supported', 'complete_product_pass': False,
            'commit': self.report['commit'], 'ci_identity': self.identity,
            'checks': {name: {'available': True} for name in ('memfd', 'pidfd', 'save_clock', 'user_namespace')}}))
        self.junit(folder, self.nodes)
        return folder

    def change_receipt(self, **changes):
        path = self.results / 'evidence.json'
        receipt = json.loads(path.read_text())
        receipt.update(changes)
        path.write_text(json.dumps(receipt))

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
        self.add_profile(folder)

    def add_profile(self, folder, profile=None):
        tree = ET.parse(folder / 'junit.xml')
        suite = next(tree.iter('testsuite'))
        properties = suite.find('properties')
        if properties is None:
            properties = ET.SubElement(suite, 'properties')
        for old in list(properties):
            if old.get('name') == evidence.EXECUTION_PROFILE_PROPERTY:
                properties.remove(old)
        ET.SubElement(properties, 'property', name=evidence.EXECUTION_PROFILE_PROPERTY,
                      value=json.dumps(evidence.OFFICIAL_PROFILE if profile is None else profile))
        tree.write(folder / 'junit.xml', encoding='utf-8')

    def test_fixed_repair_requires_exact_passed_nodes_and_current_sha_run_without_full_receipt(self):
        folder = self.root / 'fixed-repair-regression'
        folder.mkdir()
        report = dict(deepcopy(self.report), ci_identity=evidence.identity())
        (folder / 'environment.json').write_text(json.dumps(report))
        nodes = scope.FIXED_REPAIR_REGRESSION_TESTS
        self.junit(folder, nodes)
        self.assertEqual(evidence.check_fixed_repair_regression(folder), 23)
        for changed in (nodes[:-1], (*nodes, nodes[0]), (*nodes, 'tests/test_extra.py::test_extra')):
            self.junit(folder, changed)
            with self.subTest(nodes=changed), self.assertRaises(ValueError):
                evidence.check_fixed_repair_regression(folder)
        for outcome in ('skipped', 'failure', 'error'):
            self.junit(folder, nodes)
            tree = ET.parse(folder / 'junit.xml')
            suite = tree.getroot().find('testsuite')
            suite.set({'skipped': 'skipped', 'failure': 'failures', 'error': 'errors'}[outcome], '1')
            ET.SubElement(suite.find('testcase'), outcome)
            tree.write(folder / 'junit.xml')
            with self.subTest(outcome=outcome), self.assertRaises(ValueError):
                evidence.check_fixed_repair_regression(folder)
        self.junit(folder, nodes)
        for field in ('GITHUB_SHA', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT'):
            wrong = deepcopy(report)
            wrong['ci_identity'][field] = 'another-identity'
            (folder / 'environment.json').write_text(json.dumps(wrong))
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'CI run identity'):
                evidence.check_fixed_repair_regression(folder)
        wrong = deepcopy(report)
        wrong['commit']['stdout'] = 'b' * 40
        (folder / 'environment.json').write_text(json.dumps(wrong))
        with self.assertRaisesRegex(ValueError, 'candidate identity'):
            evidence.check_fixed_repair_regression(folder)
        (folder / 'environment.json').write_text(json.dumps(report))
        with patch.object(evidence, 'FIXED_REPAIR_REGRESSION_TESTS', (*nodes[:-1], nodes[0])):
            with self.assertRaisesRegex(ValueError, 'Invalid fixed'):
                evidence.check_fixed_repair_regression(folder)
        self.assertFalse((folder / 'evidence.json').exists())

    def test_native_contracts_require_current_exact_junit_without_full_receipt(self):
        folder = self.root / 'native-contracts'
        folder.mkdir()
        current_report = dict(self.report, ci_identity=evidence.identity())
        (folder / 'environment.json').write_text(json.dumps(current_report))
        nodes = ['tests/test_component.py::test_contract[a]', 'tests/test_component.py::test_contract[b]']
        with patch.object(evidence, 'collect_nodeids', return_value=nodes) as collected:
            self.junit(folder, nodes)
            self.assertEqual(evidence.check_native_contracts(folder), 2)
            collected.assert_called_once_with(scope.NATIVE_CONTRACT_TESTS)
            for changed in (nodes[:-1], nodes + [nodes[0]], nodes + ['tests/test_unexpected.py::test_extra']):
                self.junit(folder, changed)
                with self.subTest(nodes=changed), self.assertRaises(ValueError):
                    evidence.check_native_contracts(folder)
            for outcome in ('skipped', 'failure', 'error'):
                self.junit(folder, nodes)
                tree = ET.parse(folder / 'junit.xml')
                suite = tree.getroot().find('testsuite')
                suite.set({'skipped': 'skipped', 'failure': 'failures', 'error': 'errors'}[outcome], '1')
                ET.SubElement(suite.find('testcase'), outcome)
                tree.write(folder / 'junit.xml')
                with self.subTest(outcome=outcome), self.assertRaises(ValueError):
                    evidence.check_native_contracts(folder)
            self.junit(folder, nodes)
            report = deepcopy(current_report)
            report['ci_identity']['GITHUB_RUN_ID'] = 'another-run'
            (folder / 'environment.json').write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError, 'CI run identity'):
                evidence.check_native_contracts(folder)
            report = deepcopy(self.report)
            report['commit']['stdout'] = 'b' * 40
            (folder / 'environment.json').write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError, 'candidate identity'):
                evidence.check_native_contracts(folder)
        self.assertFalse((folder / 'evidence.json').exists())

    def host_loss_junit(self, folder, nodes):
        self.junit(folder, nodes)
        host_loss = evidence.junit_key(
            'tests/test_kernel_managed_entry_end_to_end.py::'
            'test_public_resume_after_exact_foreground_host_loss_preserves_parallel_work')
        tree = ET.parse(folder / 'junit.xml')
        for case in tree.iter('testcase'):
            if (case.get('classname'), case.get('name')) == host_loss:
                properties = ET.SubElement(case, 'properties')
                ET.SubElement(properties, 'property', name='host_loss_entry', value='official')
        tree.write(folder / 'junit.xml', encoding='utf-8')
        return tree

    def verify(self):
        return evidence.verify(self.results, selected="true", product="true", contract="true", result="success")

    def test_one_full_job_requires_complete_success_and_actual_native_profile(self):
        folder = self.full_run_artifacts()
        evidence.record(folder, contract=True)
        self.assertEqual(self.verify(), len(self.nodes))
        for nodes, outcome in ((self.nodes[:-1], None), (self.nodes, 'skipped'),
                               (self.nodes, 'failure'), (self.nodes, 'error')):
            self.junit(folder, nodes, outcome)
            self.add_profile(folder)
            with self.subTest(nodes=nodes, outcome=outcome), self.assertRaises(ValueError):
                evidence.record(folder, contract=True)
        for profile in (dict(evidence.OFFICIAL_PROFILE, workers=6),
                        dict(evidence.OFFICIAL_PROFILE, host_loss_io_diagnostics=True)):
            self.junit(folder, self.nodes)
            self.add_profile(folder, profile)
            with self.subTest(profile=profile), self.assertRaisesRegex(ValueError, 'profile'):
                evidence.record(folder, contract=True)

    def test_full_receipt_rechecks_capabilities_profile_and_candidate(self):
        folder = self.full_run_artifacts()
        evidence.record(folder, contract=True)
        self.change_receipt(tree_sha='foreign')
        with self.assertRaisesRegex(ValueError, 'candidate'):
            self.verify()
        self.change_receipt(tree_sha='b' * 40)
        caps = folder / 'native-capabilities.json'
        data = json.loads(caps.read_text())
        data['checks']['save_clock']['available'] = False
        caps.write_text(json.dumps(data))
        self.change_receipt(native_capabilities_sha256=evidence.file_digest(caps))
        with self.assertRaisesRegex(ValueError, 'capability'):
            self.verify()
        data['checks']['save_clock']['available'] = True
        caps.write_text(json.dumps(data))
        self.change_receipt(native_capabilities_sha256=evidence.file_digest(caps))
        self.junit(folder, self.nodes)
        self.add_profile(folder, dict(evidence.OFFICIAL_PROFILE, workers=1))
        self.change_receipt(junit_sha256=evidence.file_digest(folder / 'junit.xml'))
        with self.assertRaisesRegex(ValueError, 'profile'):
            self.verify()

    def test_full_suite_cannot_relabel_the_diagnostic_host_loss_entry(self):
        self.nodes.append('tests/test_kernel_managed_entry_end_to_end.py::'
                          'test_public_resume_after_exact_foreground_host_loss_preserves_parallel_work')
        evidence.collect_nodeids.return_value = sorted(self.nodes)
        folder = self.full_run_artifacts()
        with self.assertRaisesRegex(ValueError, 'Host-loss'):
            evidence.record(folder, contract=True)
        self.host_loss_junit(folder, self.nodes)
        self.add_profile(folder)
        evidence.record(folder, contract=True)
        self.assertEqual(self.verify(), len(self.nodes))
        path = folder / 'junit.xml'
        original = path.read_bytes()
        for values in ((), ('diagnostic-presealed',), ('official', 'official'), ('official', 'diagnostic-presealed')):
            tree = ET.fromstring(original)
            properties = tree.find('.//testcase/properties')
            properties.clear()
            for value in values:
                ET.SubElement(properties, 'property', name='host_loss_entry', value=value)
            ET.ElementTree(tree).write(path)
            self.change_receipt(junit_sha256=evidence.file_digest(path))
            with self.subTest(values=values), self.assertRaisesRegex(ValueError, 'Host-loss'):
                self.verify()


    def test_complete_candidate_is_accepted_without_running_tests_again(self):
        self.assertEqual(self.verify(), len(self.nodes))

    def test_failed_cancelled_skipped_unknown_and_missing_jobs_are_rejected(self):
        for result in ("failure", "cancelled", "timed_out", "skipped", "unknown", ""):
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
        path = self.results / "evidence.json"
        original = path.read_text()
        for name in self.identity:
            changed = self.identity | {name: "other"}
            self.change_receipt(identity=changed)
            with self.subTest(identity=name), self.assertRaisesRegex(ValueError, "identity"):
                self.verify()
            path.write_text(original)

    def test_subtest_counts_require_explicit_exact_valid_metadata(self):
        folder = self.results
        for value in (None, "-1", "invalid", "", "1", "3", "duplicate", "unknown"):
            self.junit(folder, sorted(self.nodes), subtests=2)
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
                evidence.record(folder, contract=True)
            self.change_receipt(junit_sha256=evidence.file_digest(folder / "junit.xml"))
            with self.assertRaises(ValueError):
                self.verify()

    def test_artifact_or_dependency_changes_invalidate_evidence(self):
        for name in ("junit.xml", "environment.json"):
            path = self.results / name
            original = path.read_bytes()
            path.write_bytes(original + b"\n")
            with self.subTest(file=name), self.assertRaises(ValueError):
                self.verify()
            path.write_bytes(original)
        (self.product / evidence.DEPENDENCY_FILES[0]).write_text("changed dependencies")
        with self.assertRaises(ValueError):
            self.verify()

    def test_other_commit_or_python_environment_cannot_be_recorded(self):
        path = self.results / "environment.json"
        for key, value in (("commit", {"returncode": 0, "stdout": "other"}), ("python", {"version": "other", "executable": sys.executable})):
            path.write_text(json.dumps(self.report | {key: value}))
            with self.subTest(key=key), self.assertRaises(ValueError):
                evidence.record(self.results, contract=True)

    def test_junit_mapping_preserves_classes_and_parameter_separators(self):
        self.assertEqual(evidence.junit_key("tests/test_sample.py::TestGroup::test_value[a::b]"), ("tests.test_sample.TestGroup", "test_value[a::b]"))

    def run_pytest(self, folder, *options):
        source_tests = Path(__file__).resolve().parents[2] / "tests"
        env = dict(os.environ, PYTHONPATH=str(source_tests), PYTEST_ADDOPTS="", PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "support.reporting",
             f"--junitxml={folder / 'junit.xml'}", *options],
            cwd=self.product, env=env, capture_output=True, text=True, timeout=30,
        )

    def test_same_run_prior_success_is_reused_without_rewriting_identity(self):
        with patch.dict(os.environ, {"GITHUB_RUN_ATTEMPT": "2"}):
            self.assertEqual(self.verify(), len(self.nodes))
            self.assertEqual(json.loads((self.results / 'evidence.json').read_text())['identity'], self.identity)
            for value in ('0', '3', '', 'other'):
                self.change_receipt(identity=self.identity | {'GITHUB_RUN_ATTEMPT': value})
                with self.subTest(attempt=value), self.assertRaises(ValueError):
                    self.verify()

    def test_missing_artifacts_and_changed_collection_fail_closed(self):
        for name in ('junit.xml', 'environment.json', 'native-capabilities.json', 'evidence.json'):
            path = self.results / name
            original = path.read_bytes()
            path.unlink()
            with self.subTest(missing=name), self.assertRaises(OSError):
                self.verify()
            path.write_bytes(original)
        path = self.results / 'evidence.json'
        original = path.read_bytes()
        for changes in ({'stage': 'partial'}, {'collection_sha256': 'other'}, {'total_count': True},
                        {'collection_nodeids': self.nodes[:-1]}, {'contract_nodeids': self.nodes[:1]}):
            self.change_receipt(**changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.verify()
            path.write_bytes(original)

    def test_complete_collection_requires_every_parameter_and_no_duplicate_or_extra(self):
        missing = evidence.CONTRACT_TESTS[2] + '[d]'
        for nodes in ([], [n for n in self.nodes if n != missing], self.nodes + [self.nodes[0]],
                      self.nodes + ['tests/other.py::test_extra']):
            self.junit(self.results, nodes)
            with self.subTest(nodes=nodes), self.assertRaises(ValueError):
                evidence.record(self.results, contract=True)
            self.change_receipt(junit_sha256=evidence.file_digest(self.results / 'junit.xml'))
            with self.assertRaises(ValueError):
                self.verify()

    def test_native_capability_and_environment_fields_are_rechecked(self):
        for filename, fields in (
            ('native-capabilities.json', (('status',), ('checks', 'save_clock', 'available'),
                                         ('ci_identity', 'GITHUB_RUN_ID'))),
            ('environment.json', (('memfd', 'available'), ('pidfd', 'available'),
                                  ('user_namespace', 'bubblewrap_probe', 'returncode'),
                                  ('ci_identity', 'GITHUB_RUN_ID')))):
            path = self.results / filename
            original = path.read_bytes()
            for fields_path in fields:
                data = json.loads(original)
                cursor = data
                for key in fields_path[:-1]:
                    cursor = cursor[key]
                cursor[fields_path[-1]] = 'invalid'
                path.write_text(json.dumps(data))
                key = 'native_capabilities' if filename.startswith('native') else 'environment'
                self.change_receipt(**{key + '_sha256': evidence.file_digest(path)})
                with self.subTest(fields=fields_path), self.assertRaises(ValueError):
                    self.verify()
            path.write_bytes(original)
            self.change_receipt(**{key + '_sha256': evidence.file_digest(path)})

    def test_official_profile_requires_one_suite_property_with_exact_types(self):
        path = self.results / 'junit.xml'
        original = path.read_bytes()
        for changed in ('case-only', 'duplicate', 'integer-flag', 'float-workers', 'diagnostic'):
            tree = ET.fromstring(original)
            properties = tree.find('./testsuite/properties')
            prop = properties.find(f'property[@name="{evidence.EXECUTION_PROFILE_PROPERTY}"]')
            if changed == 'case-only':
                properties.remove(prop)
                ET.SubElement(next(tree.iter('testcase')), 'properties').append(prop)
            elif changed == 'duplicate':
                properties.append(deepcopy(prop))
            else:
                profile = dict(evidence.OFFICIAL_PROFILE)
                if changed == 'float-workers':
                    profile['workers'] = 2.0
                else:
                    profile['host_loss_io_diagnostics'] = True if changed == 'diagnostic' else 0
                prop.set('value', json.dumps(profile))
            ET.ElementTree(tree).write(path)
            self.change_receipt(junit_sha256=evidence.file_digest(path))
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, 'profile'):
                self.verify()

    def test_real_two_worker_pytest_subtests_and_junit_form_one_full_receipt(self):
        (self.product / 'pyproject.toml').write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
        (self.product / 'tests/test_sample.py').write_text(
            'import pytest\nimport unittest\n@pytest.mark.parametrize("value", ["a::b", "[c]"])\n'
            'def test_values(value): assert value\n'
            'class TestGroup:\n    def test_value(self): assert True\n'
            'def test_plain(): assert True\n'
            'def test_subtests(subtests):\n'
            '    for value in (1, 2):\n'
            '        with subtests.test(value=value): assert value > 0\n'
            'class TestSubtests(unittest.TestCase):\n'
            '    def test_values(self):\n'
            '        for value in (1, 2):\n'
            '            with self.subTest(value=value): self.assertGreater(value, 0)\n')
        with patch.object(evidence, 'collect_nodeids', self.real_collect):
            result = self.run_pytest(self.results, '-n', '2', '--dist', 'worksteal')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            suite = ET.parse(self.results / 'junit.xml').getroot().find('testsuite')
            self.assertEqual(int(suite.get('tests')), len(suite.findall('testcase')) + 4)
            evidence.record(self.results, contract=False)
        with patch.object(evidence, 'collect_nodeids', side_effect=AssertionError('quality does not recollect')):
            self.assertEqual(evidence.verify(self.results, selected='true', product='true', contract='false', result='success'), 6)

    def test_real_diagnostic_two_worker_run_cannot_record_full_acceptance(self):
        (self.product / 'pyproject.toml').write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
        (self.product / 'tests/conftest.py').write_text(
            'def pytest_addoption(parser):\n'
            '    parser.addoption("--host-loss-io-diagnostics", action="store_true", default=False)\n')
        (self.product / 'tests/test_sample.py').write_text('def test_passes(): assert True\n')
        (self.results / 'evidence.json').unlink()
        result = self.run_pytest(self.results, '-n', '2', '--dist', 'worksteal', '--host-loss-io-diagnostics')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        suite = ET.parse(self.results / 'junit.xml').getroot().find('testsuite')
        profile = suite.find(f'./properties/property[@name="{evidence.EXECUTION_PROFILE_PROPERTY}"]')
        self.assertEqual(json.loads(profile.get('value')), {
            'host_loss_io_diagnostics': True, 'workers': 2, 'dist': 'worksteal'})
        with self.assertRaisesRegex(ValueError, 'official two-worker profile'):
            evidence.record(self.results, contract=False)
        self.assertFalse((self.results / 'evidence.json').exists())

    def test_real_pytest_subtest_failures_skips_and_errors_are_rejected(self):
        (self.product / 'pyproject.toml').write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
        (self.product / 'tests/test_sample.py').write_text(
            'import pytest\n'
            'def test_failure(subtests):\n    with subtests.test(): assert False\n'
            'def test_skipped(subtests):\n    with subtests.test(): pytest.skip("subtest skipped")\n'
            '@pytest.fixture\ndef broken_teardown():\n    yield\n    raise RuntimeError("teardown failed")\n'
            'def test_error(subtests, broken_teardown):\n    with subtests.test(): assert True\n')
        self.change_receipt(dependencies_sha256=evidence.dependency_digest())
        for outcome in ('failure', 'skipped', 'error'):
            result = self.run_pytest(self.results, '-n', '2', '--dist', 'worksteal', '-k', f'test_{outcome}')
            self.assertEqual(result.returncode, 0 if outcome == 'skipped' else 1, result.stdout + result.stderr)
            self.assertIsNotNone(ET.parse(self.results / 'junit.xml').getroot().find(f'.//testcase/{outcome}'))
            with self.subTest(outcome=outcome), self.assertRaisesRegex(ValueError, 'failed, errored or skipped'):
                evidence.record(self.results, contract=False)
            self.change_receipt(junit_sha256=evidence.file_digest(self.results / 'junit.xml'))
            with self.assertRaisesRegex(ValueError, 'failed, errored or skipped'):
                self.verify()

    def test_cli_records_and_verifies_the_single_receipt_and_rejects_missing_output(self):
        with patch.object(sys, 'argv', ['ci_evidence', 'record', '--results', str(self.results), '--contract', 'true']), redirect_stdout(StringIO()):
            evidence.main()
        env = dict(CHECKS_SELECTED='true', PRODUCT_SELECTED='true', CONTRACT_SELECTED='true', PRODUCT_RESULT='success')
        with patch.dict(os.environ, env), patch.object(sys, 'argv', ['ci_evidence', 'verify', '--results', str(self.results)]):
            with redirect_stdout(StringIO()):
                evidence.main()
            (self.results / 'evidence.json').unlink()
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as caught:
                evidence.main()
            self.assertEqual(caught.exception.code, 1)


if __name__ == '__main__':
    unittest.main()
