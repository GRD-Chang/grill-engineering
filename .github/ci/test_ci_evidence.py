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


class CurrentCollectionTests(unittest.TestCase):
    """Use real product collection, independently of the receipt doubles below."""

    nodes: ClassVar[list[str]]

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

    def test_boundary_selection_is_exact_current_subset_and_full_keeps_every_node(self):
        targets = scope.selection('boundary')
        self.assertEqual(targets, scope.BOUNDARY_TESTS)
        self.assertEqual(len(targets), 14)
        self.assertEqual(len(set(targets)), 14)
        for target in targets:
            with self.subTest(target=target):
                self.assertIn(target, self.nodes)
                self.assertEqual(sum(node == target for node in self.nodes), 1)
        # The full suite still comes from pytest collection, not the boundary
        # inventory. Existing receipt verification requires the entire set.
        self.assertTrue(set(targets) < set(self.nodes))

    def test_actual_official_partition_and_four_remainders_are_the_unchanged_full_collection(self):
        completed = scope.completed_boundary_nodes(self.nodes)
        self.assertEqual(len(completed), 33)
        self.assertEqual(completed, evidence.collect_nodeids((*scope.NATIVE_CONTRACT_TESTS, *scope.BOUNDARY_TESTS)))
        observed = set(completed)
        for index in range(4):
            result = subprocess.run([sys.executable, '-m', 'pytest', '--collect-only', '-q', '-n', '0',
                '-o', 'addopts=', '--test-completed-boundary', '--test-shard-count=4', f'--test-shard-index={index}'],
                cwd=evidence.PRODUCT_ROOT, env=dict(os.environ, PYTEST_ADDOPTS=''),
                capture_output=True, text=True, timeout=120, check=True)
            nodes = [line for line in result.stdout.splitlines() if line.startswith('tests/') and '::' in line]
            self.assertEqual(len(nodes), len(set(nodes)))
            self.assertFalse(observed & set(nodes))
            expected = [node for node in self.nodes if node not in set(completed)][index::4]
            self.assertEqual(set(nodes), set(expected))
            observed.update(nodes)
        self.assertEqual(observed, set(self.nodes))

    def test_two_boundary_phases_expand_to_exactly_the_original_fourteen(self):
        other = evidence.collect_nodeids(scope.selection('boundary-other'))
        commit = evidence.collect_nodeids(scope.selection('boundary-commit'))
        self.assertEqual(len(other), 13)
        self.assertEqual(len(commit), 1)
        self.assertFalse(set(other) & set(commit))
        self.assertEqual(sorted(other + commit), evidence.collect_nodeids(scope.BOUNDARY_TESTS))
        self.assertEqual(scope.selection('boundary'), scope.BOUNDARY_TESTS)

    def test_native_contracts_are_exact_current_subsets_and_keep_boundary_unchanged(self):
        targets = scope.selection('native-contract')
        self.assertEqual(targets, scope.NATIVE_CONTRACT_TESTS)
        self.assertEqual(len(targets), len(set(targets)))
        selected = evidence.collect_nodeids(targets)
        self.assertTrue(set(selected) < set(self.nodes))
        self.assertTrue(all(any(node == target or node.startswith(target + '[') for node in selected)
                            for target in targets))
        self.assertIn('tests/test_repository_save_fifo.py::test_native_calling_task_clock_uses_linux_top_level_offsets', targets)
        self.assertIn('tests/test_repository_save_fifo.py::test_native_nonleader_thread_clock_uses_its_own_top_level_offsets', targets)
        self.assertEqual(len(scope.BOUNDARY_TESTS), 14)

    def test_fixed_repair_scope_is_the_exact_24_current_parameters_not_the_full_inventory(self):
        targets = scope.selection('fixed-repair-regression')
        self.assertEqual(targets, scope.FIXED_REPAIR_REGRESSION_TESTS)
        self.assertEqual(len(targets), 24)
        self.assertEqual(len(set(targets)), 24)
        self.assertEqual(evidence.collect_nodeids(targets), sorted(targets))
        self.assertTrue(set(targets) < set(self.nodes))
        self.assertEqual(len(scope.BOUNDARY_TESTS), 14)
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
    def test_fixed_repair_requires_exact_passed_nodes_and_current_sha_run_without_full_receipt(self):
        folder = self.root / 'fixed-repair-regression'
        folder.mkdir()
        report = dict(deepcopy(self.report), ci_identity=evidence.identity())
        (folder / 'environment.json').write_text(json.dumps(report))
        nodes = scope.FIXED_REPAIR_REGRESSION_TESTS
        self.junit(folder, nodes)
        self.assertEqual(evidence.check_fixed_repair_regression(folder), 24)
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

    def test_boundary_requires_exact_all_pass_without_creating_full_receipt(self):
        folder = self.root / 'boundary'
        folder.mkdir()
        (folder / 'environment.json').write_text(json.dumps(self.report))
        nodes = list(scope.BOUNDARY_TESTS)
        self.boundary_junit(folder, nodes)
        self.assertEqual(evidence.check_boundary(folder), len(nodes))
        self.assertFalse((folder / 'evidence.json').exists())
        for changed in (nodes[:-1], nodes + [nodes[0]], nodes + ['tests/test_extra.py::test_extra']):
            self.boundary_junit(folder, changed)
            with self.subTest(nodes=changed), self.assertRaises(ValueError):
                evidence.check_boundary(folder)
        for outcome in ('skipped', 'failure', 'error'):
            self.boundary_junit(folder, nodes)
            tree = ET.parse(folder / 'junit.xml')
            suite = tree.getroot().find('testsuite')
            suite.set({'skipped': 'skipped', 'failure': 'failures', 'error': 'errors'}[outcome], '1')
            ET.SubElement(suite.find('testcase'), outcome)
            tree.write(folder / 'junit.xml')
            with self.subTest(outcome=outcome), self.assertRaises(ValueError):
                evidence.check_boundary(folder)
        self.boundary_junit(folder, nodes)
        report = deepcopy(self.report)
        report['commit']['stdout'] = 'b' * 40
        (folder / 'environment.json').write_text(json.dumps(report))
        with self.assertRaisesRegex(ValueError, 'candidate identity'):
            evidence.check_boundary(folder)
        self.assertFalse((folder / 'evidence.json').exists())

    def test_boundary_requires_one_official_property_on_the_exact_host_loss_case(self):
        folder = self.root / 'boundary-profile'
        folder.mkdir()
        (folder / 'environment.json').write_text(json.dumps(self.report))
        # An informational stage label cannot substitute for the test's property.
        (folder / 'boundary-stage.json').write_text(json.dumps({'host_loss_entry': 'official'}))
        for values in ((), ('diagnostic-presealed',), ('',), ('Official',),
                       ('official', 'official'), ('official', 'diagnostic-presealed')):
            tree = self.boundary_junit(folder, scope.BOUNDARY_TESTS)
            properties = tree.find('.//testcase/properties')
            properties.clear()
            for value in values:
                ET.SubElement(properties, 'property', name='host_loss_entry', value=value)
            tree.write(folder / 'junit.xml')
            with self.subTest(values=values), self.assertRaisesRegex(ValueError, 'official Host-loss'):
                evidence.check_boundary(folder)
        for location in ('suite', 'another-case'):
            tree = self.boundary_junit(folder, scope.BOUNDARY_TESTS)
            host = next(case for case in tree.iter('testcase') if case.find('properties') is not None)
            host.remove(host.find('properties'))
            target = (tree.find('testsuite') if location == 'suite' else
                      next(case for case in tree.iter('testcase') if case is not host))
            properties = ET.SubElement(target, 'properties')
            ET.SubElement(properties, 'property', name='host_loss_entry', value='official')
            tree.write(folder / 'junit.xml')
            with self.subTest(location=location), self.assertRaisesRegex(ValueError, 'official Host-loss'):
                evidence.check_boundary(folder)
        self.assertFalse((folder / 'evidence.json').exists())

    def test_official_property_written_before_the_story_cannot_hide_its_failure(self):
        folder = self.root / 'boundary-failed-host'
        folder.mkdir()
        (folder / 'environment.json').write_text(json.dumps(self.report))
        for outcome in ('skipped', 'failure', 'error'):
            for reported in ('0', '1'):
                tree = self.boundary_junit(folder, scope.BOUNDARY_TESTS)
                host = next(case for case in tree.iter('testcase') if case.find('properties') is not None)
                ET.SubElement(host, outcome)
                tree.find('testsuite').set(
                    {'skipped': 'skipped', 'failure': 'failures', 'error': 'errors'}[outcome], reported)
                tree.write(folder / 'junit.xml')
                with self.subTest(outcome=outcome, reported=reported), self.assertRaises(ValueError):
                    evidence.check_boundary(folder)
        self.assertFalse((folder / 'evidence.json').exists())

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

    def boundary_junit(self, folder, nodes):
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

    def change_receipt(self, index, **changes):
        path = self.folder(index) / "evidence.json"
        receipt = json.loads(path.read_text())
        receipt.update(changes)
        path.write_text(json.dumps(receipt))

    def declared_shards(self, count):
        self.results = self.root / f"declared-{count}"
        for index in range(count):
            folder = self.folder(index)
            folder.mkdir(parents=True)
            (folder / "environment.json").write_text(json.dumps(self.report))
            self.junit(folder, sorted(self.nodes)[index::count])
            evidence.record(folder, index, contract=True, shard_count=count)

    def completed_partition(self):
        expansions = {
            'test_registration_commit_unknown_reads_only_original_uuid_and_never_reinserts': ('False', 'True'),
            'test_registration_commit_known_failure_keeps_category_and_never_registers_again': ('13-storage_limit', '9-transaction_aborted'),
        }
        native = [target + '[' + value + ']' if values else target
                  for target in scope.NATIVE_CONTRACT_TESTS
                  for values in (expansions.get(target.split('::')[-1]),)
                  for value in (values or (None,))]
        self.nodes = sorted([*self.nodes, *native, *scope.BOUNDARY_TESTS])
        evidence.collect_nodeids.return_value = self.nodes
        self.enterContext(patch.object(evidence, 'tree_sha', return_value='b' * 40))
        self.report['ci_identity'] = dict(self.identity)
        self.report['user_namespace'] = {'unprivileged_userns_clone': '1', 'max_user_namespaces': '100',
                                         'bubblewrap_probe': {'returncode': 0, 'stdout': '', 'stderr': ''}}
        self.completed = self.root / 'completed-boundary'
        contracts = self.completed / 'native-contracts'
        contracts.mkdir(parents=True)
        (self.completed / 'commit').mkdir()
        for folder, nodes in ((contracts, native), (self.completed, scope.BOUNDARY_OTHER_TESTS),
                              (self.completed / 'commit', scope.BOUNDARY_COMMIT_TESTS)):
            (folder / 'environment.json').write_text(json.dumps(self.report))
            self.boundary_junit(folder, nodes)
            self.add_profile(folder)
        (self.completed / 'native-capabilities.json').write_text(json.dumps({
            'stage': 'native-capabilities', 'status': 'supported', 'complete_product_pass': False,
            'commit': self.report['commit'], 'ci_identity': self.identity,
            'checks': {name: {'available': True} for name in ('memfd', 'pidfd', 'save_clock', 'user_namespace')}}))
        (self.completed / 'boundary-stage.json').write_text(json.dumps({
            'stage': 'native-boundary-regression', 'complete_product_pass': False,
            'host_loss_entry': 'official', 'sha': self.identity['GITHUB_SHA']}))
        evidence.record_completed_boundary(self.completed)
        self.results = self.root / 'partition-shards'
        remaining = [n for n in self.nodes if n not in set(scope.completed_boundary_nodes(self.nodes))]
        for index in range(4):
            folder = self.folder(index)
            folder.mkdir(parents=True)
            (folder / 'environment.json').write_text(json.dumps(self.report))
            self.junit(folder, remaining[index::4])
            self.add_profile(folder)
            (folder / 'native-capabilities.json').write_bytes((self.completed / 'native-capabilities.json').read_bytes())
            evidence.record(folder, index, contract=True, shard_count=4, completed_boundary=self.completed)

    def add_profile(self, folder, profile=None):
        tree = ET.parse(folder / 'junit.xml')
        suite = next(tree.iter('testsuite'))
        properties = suite.find('properties')
        if properties is None:
            properties = ET.SubElement(suite, 'properties')
        ET.SubElement(properties, 'property', name=evidence.EXECUTION_PROFILE_PROPERTY,
                      value=json.dumps(evidence.OFFICIAL_PROFILE if profile is None else profile))
        tree.write(folder / 'junit.xml', encoding='utf-8')

    def verify_completed(self):
        return evidence.verify(self.results, selected='true', product='true', contract='true', result='success',
                               shard_count=4, completed_boundary=self.completed)

    def refresh_boundary_hashes(self):
        path = self.completed / 'completed-boundary.json'
        receipt = json.loads(path.read_text())
        receipt['files_sha256'] = {name: evidence.file_digest(self.completed / name) for name in evidence.BOUNDARY_FILES}
        path.write_text(json.dumps(receipt))

    def test_completed_boundary_and_four_shards_prove_the_exact_full_set(self):
        self.completed_partition()
        self.assertEqual(self.verify_completed(), len(self.nodes))
        receipt = evidence.read_completed_boundary(self.completed)
        self.assertIs(receipt['complete_product_pass'], False)
        self.assertEqual(len(receipt['completed_nodeids']), 33)
        self.assertEqual(sum(len(evidence.junit_cases(self.folder(i))) for i in range(4)) + 33, len(self.nodes))

    def test_split_boundary_requires_both_original_partitions_before_a_receipt(self):
        self.completed_partition()
        self.assertEqual(evidence.check_boundary(self.completed, split_commit=True), 14)
        output = StringIO()
        with patch.object(sys, 'argv', ['ci_evidence.py', 'check-boundary', '--results', str(self.completed),
                                       '--split-commit']), redirect_stdout(output):
            evidence.main()
        self.assertIn('14 passed; not full acceptance', output.getvalue())
        receipt = self.completed / 'completed-boundary.json'
        for folder, targets in ((self.completed, scope.BOUNDARY_OTHER_TESTS),
                                (self.completed / 'commit', scope.BOUNDARY_COMMIT_TESTS)):
            file = folder / 'junit.xml'
            original = file.read_bytes()
            for mode in ('missing-file', 'failure', 'error', 'skipped', 'missing', 'duplicate', 'extra', 'swapped'):
                receipt.unlink(missing_ok=True)
                if mode == 'missing-file':
                    file.unlink()
                else:
                    nodes = list(targets)
                    if mode == 'missing': nodes.pop()
                    elif mode == 'duplicate': nodes.append(nodes[0])
                    elif mode == 'extra': nodes.append('tests/test_foreign.py::test_extra')
                    elif mode == 'swapped': nodes = list(scope.BOUNDARY_COMMIT_TESTS if folder == self.completed else scope.BOUNDARY_OTHER_TESTS)
                    self.boundary_junit(folder, nodes); self.add_profile(folder)
                    if mode in ('failure', 'error', 'skipped'):
                        tree = ET.parse(file)
                        ET.SubElement(next(tree.iter('testcase')), mode)
                        tree.write(file)
                with self.subTest(folder=folder.name, mode=mode), self.assertRaises((ValueError, OSError)):
                    evidence.record_completed_boundary(self.completed)
                self.assertFalse(receipt.exists())
                file.write_bytes(original)

    def test_commit_phase_environment_identity_and_official_profile_are_checked(self):
        self.completed_partition()
        folder = self.completed / 'commit'
        file = folder / 'environment.json'
        original = file.read_bytes()
        for mode in ('sha', 'run', 'attempt', 'environment'):
            report = json.loads(original)
            if mode == 'sha': report['commit']['stdout'] = 'foreign'
            elif mode == 'run': report['ci_identity']['GITHUB_RUN_ID'] = 'foreign'
            elif mode == 'attempt': report['ci_identity']['GITHUB_RUN_ATTEMPT'] = '2'
            else: report['sqlite']['version'] = 'different'
            file.write_text(json.dumps(report))
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                evidence.check_boundary(self.completed, split_commit=True)
            file.write_bytes(original)
        file = folder / 'junit.xml'
        tree = ET.parse(file)
        prop = tree.find(f'.//property[@name="{evidence.EXECUTION_PROFILE_PROPERTY}"]')
        prop.set('value', json.dumps(evidence.OFFICIAL_PROFILE | {'workers': 1}))
        tree.write(file)
        with self.assertRaises(ValueError): evidence.check_boundary(self.completed, split_commit=True)

    def test_boundary_identity_tree_and_inventory_must_match(self):
        self.completed_partition()
        path = self.completed / 'completed-boundary.json'
        original = path.read_text()
        for field in evidence.IDENTITY_FIELDS:
            changed = json.loads(original)
            changed['identity'][field] = '999' if field == 'GITHUB_RUN_ATTEMPT' else 'foreign'
            path.write_text(json.dumps(changed))
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.verify_completed()
        for field, value in (('tree_sha', 'c' * 40), ('complete_product_pass', True),
                             ('completed_nodeids', []), ('stage', 'fixed-repair-regression')):
            changed = json.loads(original)
            changed[field] = value
            path.write_text(json.dumps(changed))
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.verify_completed()
        path.write_text(original)

    def test_failed_skipped_duplicate_missing_and_extra_boundary_cases_never_complete(self):
        self.completed_partition()
        receipt = self.completed / 'completed-boundary.json'
        original = receipt.read_text()
        native = [n for n in scope.completed_boundary_nodes(self.nodes) if n not in scope.BOUNDARY_TESTS]
        for folder, nodes in ((self.completed, scope.BOUNDARY_OTHER_TESTS),
                              (self.completed / 'commit', scope.BOUNDARY_COMMIT_TESTS),
                              (self.completed / 'native-contracts', native)):
            original_junit = (folder / 'junit.xml').read_bytes()
            for mode in ('failure', 'error', 'skipped', 'duplicate', 'missing', 'extra'):
                receipt.unlink(missing_ok=True)
                selected = list(nodes)
                if mode == 'duplicate': selected.append(nodes[0])
                elif mode == 'missing': selected.pop()
                elif mode == 'extra': selected.append('tests/test_unexpected.py::test_extra')
                self.boundary_junit(folder, selected)
                self.add_profile(folder)
                if mode in ('failure', 'error', 'skipped'):
                    tree = ET.parse(folder / 'junit.xml')
                    suite = next(tree.iter('testsuite'))
                    suite.set({'failure': 'failures', 'error': 'errors', 'skipped': 'skipped'}[mode], '1')
                    ET.SubElement(next(tree.iter('testcase')), mode)
                    tree.write(folder / 'junit.xml')
                with self.subTest(folder=folder.name, mode=mode), self.assertRaises(ValueError):
                    evidence.record_completed_boundary(self.completed)
                self.assertFalse(receipt.exists())
            (folder / 'junit.xml').write_bytes(original_junit)
        receipt.write_text(original)

    def test_profile_capabilities_and_cross_job_environment_are_not_labels_only(self):
        self.completed_partition()
        for filename in evidence.BOUNDARY_FILES:
            file = self.completed / filename
            original = file.read_bytes()
            file.write_bytes(original + b' ')
            with self.subTest(tamper=filename), self.assertRaises(ValueError):
                self.verify_completed()
            file.write_bytes(original)
        for kind in ('diagnostic', 'workers', 'dist', 'namespace', 'capabilities', 'host_loss_property'):
            originals = {name: (self.completed / name).read_bytes() for name in evidence.BOUNDARY_FILES}
            if kind in ('diagnostic', 'workers', 'dist'):
                tree = ET.parse(self.completed / 'junit.xml')
                prop = tree.find(f'.//property[@name="{evidence.EXECUTION_PROFILE_PROPERTY}"]')
                profile = dict(evidence.OFFICIAL_PROFILE)
                profile[{'diagnostic': 'host_loss_io_diagnostics', 'workers': 'workers', 'dist': 'dist'}[kind]] = {'diagnostic': True, 'workers': 1, 'dist': 'load'}[kind]
                prop.set('value', json.dumps(profile)); tree.write(self.completed / 'junit.xml')
            elif kind == 'host_loss_property':
                tree = ET.parse(self.completed / 'junit.xml')
                tree.find('.//property[@name="host_loss_entry"]').set('value', 'diagnostic-presealed')
                tree.write(self.completed / 'junit.xml')
            else:
                name = 'native-capabilities.json' if kind == 'capabilities' else 'environment.json'
                value = json.loads((self.completed / name).read_text())
                if kind == 'capabilities': value['checks']['save_clock']['available'] = False
                else: value['user_namespace']['bubblewrap_probe']['returncode'] = 1
                (self.completed / name).write_text(json.dumps(value))
            self.refresh_boundary_hashes()
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.verify_completed()
            for name, raw in originals.items(): (self.completed / name).write_bytes(raw)
            self.refresh_boundary_hashes()

    def test_remainder_rejects_overlap_omission_extra_nodes_and_changed_collection(self):
        self.completed_partition()
        folder = self.folder(0)
        original = (folder / 'junit.xml').read_bytes()
        remaining = [n for n in self.nodes if n not in set(scope.completed_boundary_nodes(self.nodes))][::4]
        for nodes in (remaining[:-1], [*remaining, scope.BOUNDARY_TESTS[0]],
                      [*remaining[:-1], 'tests/test_unexpected.py::test_extra']):
            self.junit(folder, nodes); self.add_profile(folder)
            with self.subTest(nodes=nodes), self.assertRaises(ValueError):
                evidence.record(folder, 0, contract=True, shard_count=4, completed_boundary=self.completed)
        (folder / 'junit.xml').write_bytes(original)
        evidence.collect_nodeids.return_value = sorted([*self.nodes, 'tests/test_new.py::test_added'])
        with self.assertRaisesRegex(ValueError, 'collections differ'):
            evidence.record(folder, 0, contract=True, shard_count=4, completed_boundary=self.completed)
        with self.assertRaises(ValueError):
            evidence.verify(self.results, selected='true', product='true', contract='true', result='success', shard_count=4)

    def test_suite_profile_cannot_be_forged_on_a_case_or_with_wrong_value_types(self):
        self.completed_partition()
        file = self.completed / 'junit.xml'
        original = file.read_bytes()
        for changed in ('case-only', 'duplicate', 'integer-flag', 'float-workers'):
            tree = ET.fromstring(original)
            properties = tree.find('./testsuite/properties')
            prop = properties.find(f'property[@name="{evidence.EXECUTION_PROFILE_PROPERTY}"]')
            if changed == 'case-only':
                properties.remove(prop)
                ET.SubElement(next(tree.iter('testcase')), 'properties').append(prop)
            elif changed == 'duplicate':
                ET.SubElement(properties, 'property', **prop.attrib)
            else:
                profile = dict(evidence.OFFICIAL_PROFILE)
                profile['host_loss_io_diagnostics' if changed == 'integer-flag' else 'workers'] = 0 if changed == 'integer-flag' else 2.0
                prop.set('value', json.dumps(profile))
            ET.ElementTree(tree).write(file)
            self.refresh_boundary_hashes()
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.verify_completed()
        file.write_bytes(original)
        self.refresh_boundary_hashes()

    def test_cross_job_environment_and_shard_tree_are_rechecked(self):
        self.completed_partition()
        folder = self.folder(0)
        file = folder / 'environment.json'
        original = file.read_text()
        for kind in ('namespace', 'sqlite', 'identity'):
            report = json.loads(original)
            if kind == 'namespace': report['user_namespace']['max_user_namespaces'] = 'changed'
            elif kind == 'sqlite': report['sqlite']['version'] = 'different'
            else: report['ci_identity']['GITHUB_RUN_ID'] = 'another-run'
            file.write_text(json.dumps(report))
            self.change_receipt(0, environment_sha256=evidence.file_digest(file))
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.verify_completed()
        file.write_text(original)
        self.change_receipt(0, environment_sha256=evidence.file_digest(file), tree_sha='c' * 40)
        with self.assertRaises(ValueError): self.verify_completed()

    def test_same_run_prior_success_can_be_kept_but_changed_boundary_cannot_be_mixed(self):
        self.completed_partition()
        os.environ['GITHUB_RUN_ATTEMPT'] = '2'
        self.assertEqual(self.verify_completed(), len(self.nodes))
        path = self.completed / 'completed-boundary.json'
        value = json.loads(path.read_text()); value['unused'] = 'changed artifact identity'
        path.write_text(json.dumps(value))
        with self.assertRaises(ValueError): self.verify_completed()

    def test_each_remainder_requires_its_own_supported_capability_artifact(self):
        self.completed_partition()
        folder = self.folder(0)
        file = folder / 'native-capabilities.json'
        original = file.read_bytes()
        file.unlink()
        with self.assertRaises(OSError): self.verify_completed()
        for kind in ('status', 'save_clock', 'identity'):
            caps = json.loads(original)
            if kind == 'status': caps['status'] = 'unsupported'
            elif kind == 'save_clock': caps['checks']['save_clock']['available'] = False
            else: caps['ci_identity']['GITHUB_RUN_ID'] = 'foreign'
            file.write_text(json.dumps(caps))
            self.change_receipt(0, native_capabilities_sha256=evidence.file_digest(file))
            with self.subTest(kind=kind), self.assertRaises(ValueError): self.verify_completed()

    def verify_declared(self, count):
        return evidence.verify(self.results, selected="true", product="true", contract="true", result="success",
                               shard_count=count)

    def test_predeclared_two_and_four_shards_keep_complete_parent_coverage(self):
        for count in (2, 4):
            with self.subTest(count=count):
                self.declared_shards(count)
                self.assertEqual(self.verify_declared(count), len(self.nodes))
                for index in range(count):
                    receipt = json.loads((self.folder(index) / 'evidence.json').read_text())
                    self.assertEqual(receipt['shard_count'], count)
                    self.assertEqual(receipt['selected_count'], len(sorted(self.nodes)[index::count]))

    def test_four_shards_reject_missing_duplicate_wrong_count_and_foreign_sha(self):
        self.declared_shards(4)
        folder = self.folder(3)
        relocated = self.root / 'missing-fourth'
        folder.rename(relocated)
        try:
            with self.assertRaisesRegex(ValueError, 'shards'):
                self.verify_declared(4)
        finally:
            relocated.rename(folder)
        path = folder / 'evidence.json'
        original = path.read_text()
        for changed in ({'shard_index': 2}, {'shard_count': 2},
                        {'identity': self.identity | {'GITHUB_SHA': 'b' * 40}},
                        {'collection_sha256': 'another-collection'}):
            with self.subTest(changed=changed):
                self.change_receipt(3, **changed)
                with self.assertRaises(ValueError):
                    self.verify_declared(4)
                path.write_text(original)
        for count in (0, 2, 3, 5):
            with self.subTest(expected=count), self.assertRaises(ValueError):
                self.verify_declared(count)
        self.assertEqual(self.verify_declared(4), len(self.nodes))

    def test_four_shards_check_late_shard_overlap_and_environment(self):
        self.declared_shards(4)
        folder = self.folder(3)
        xml, receipt = (folder / 'junit.xml').read_bytes(), (folder / 'evidence.json').read_bytes()
        self.junit(folder, sorted(self.nodes)[2::4])
        self.change_receipt(3, junit_sha256=evidence.file_digest(folder / 'junit.xml'))
        with self.assertRaisesRegex(ValueError, 'overlap'):
            self.verify_declared(4)
        (folder / 'junit.xml').write_bytes(xml)
        (folder / 'evidence.json').write_bytes(receipt)
        report = self.report | {'cpu_count': 9}
        (folder / 'environment.json').write_text(json.dumps(report))
        self.change_receipt(3, environment_sha256=evidence.file_digest(folder / 'environment.json'))
        with self.assertRaisesRegex(ValueError, 'environments'):
            self.verify_declared(4)

    def test_four_shard_record_requires_its_exact_partition(self):
        self.declared_shards(4)
        folder, nodes = self.folder(3), sorted(self.nodes)[3::4]
        for changed in ([], nodes[:-1], sorted(self.nodes)[2::4], nodes + [nodes[0]]):
            self.junit(folder, changed)
            with self.subTest(nodes=changed), self.assertRaises(ValueError):
                evidence.record(folder, 3, contract=True, shard_count=4)
        self.junit(folder, nodes)
        for count, index in ((0, 0), (257, 0), (4, 4), (4, -1), (True, 0), (4, True)):
            with self.subTest(count=count, index=index), self.assertRaises(ValueError):
                evidence.record(folder, index, contract=True, shard_count=count)

    def test_verify_cli_consumes_declared_count_and_rejects_missing_matrix_output(self):
        self.declared_shards(4)
        environment = {'CHECKS_SELECTED': 'true', 'PRODUCT_SELECTED': 'true', 'CONTRACT_SELECTED': 'true',
                       'PRODUCT_RESULT': 'success'}
        arguments = ['ci_evidence.py', 'verify', '--results', str(self.results), '--shard-count']
        output = StringIO()
        with patch.dict(os.environ, environment), patch.object(sys, 'argv', [*arguments, '4']), redirect_stdout(output):
            evidence.main()
        self.assertIn(str(len(self.nodes)) + ' product tests', output.getvalue())
        with patch.dict(os.environ, environment), patch.object(sys, 'argv', [*arguments, '0']), redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit) as failure:
                evidence.main()
            self.assertEqual(failure.exception.code, 1)
        environment.update(PRODUCT_SELECTED='false', CONTRACT_SELECTED='false')
        with patch.dict(os.environ, environment), patch.object(sys, 'argv', [*arguments, '0']), redirect_stdout(StringIO()):
            evidence.main()  # Private-only checks declare no product matrix.

    def test_real_four_shard_pytest_results_form_one_complete_evidence_set(self):
        (self.product / 'pyproject.toml').write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
        (self.product / 'tests/test_sample.py').write_text(
            'import pytest\n@pytest.mark.parametrize("value", range(8))\n'
            'def test_value(value): assert value >= 0\n')
        self.results = self.root / 'real-four-shards'
        with patch.object(evidence, 'collect_nodeids', self.real_collect):
            for index in range(4):
                folder = self.folder(index)
                folder.mkdir(parents=True)
                (folder / 'environment.json').write_text(json.dumps(self.report))
                result = self.run_pytest(folder, '--test-shard-count=4', f'--test-shard-index={index}',
                                         '-n', '2', '--dist', 'worksteal')
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                evidence._official_profile(folder)  # Actual controller-emitted JUnit, not a fixture property.
                evidence.record(folder, index, contract=False, shard_count=4)
        self.assertEqual(evidence.verify(self.results, selected='true', product='true', contract='false',
                                         result='success', shard_count=4), 8)

    def test_complete_candidate_is_accepted_without_running_tests_again(self):
        self.assertEqual(self.verify(), len(self.nodes))

    def test_all_current_contract_parameter_instances_are_required(self):
        missing = evidence.CONTRACT_TESTS[2] + "[d]"
        index = sorted(self.nodes).index(missing) % 2
        self.junit(self.folder(index), [node for node in sorted(self.nodes)[index::2] if node != missing])
        with self.assertRaisesRegex(ValueError, "coverage"):
            evidence.record(self.folder(index), index, contract=True)

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
