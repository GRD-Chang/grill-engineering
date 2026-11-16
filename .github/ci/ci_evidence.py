"""绑定单次完整 CI 测试，核验原生执行与完整覆盖后汇总 quality。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

from test_scope import CONTRACT_TESTS, NATIVE_CONTRACT_TESTS, FIXED_REPAIR_REGRESSION_TESTS

PRODUCT_ROOT = Path(__file__).resolve().parents[2]
DEPENDENCY_FILES = ("pyproject.toml", "tests/dev-requirements.txt", "tests/build-requirements.txt")
IDENTITY_FIELDS = (
    "GITHUB_REPOSITORY", "GITHUB_SHA", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_WORKFLOW_REF",
)
PASSED_SUBTESTS_PROPERTY = "agent-run-pytest-passed-subtests-v1"
EXECUTION_PROFILE_PROPERTY = 'agent-run-execution-profile-v1'
OFFICIAL_PROFILE = {'host_loss_io_diagnostics': False, 'workers': 2, 'dist': 'worksteal'}


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dependency_digest() -> str:
    return digest({name: file_digest(PRODUCT_ROOT / name) for name in DEPENDENCY_FILES})


def identity() -> dict[str, str]:
    result = {name: os.environ.get(name, "") for name in IDENTITY_FIELDS}
    if not all(result.values()):
        raise ValueError("Missing CI identity")
    return result


def read_environment(folder: Path) -> dict:
    report = json.loads((folder / "environment.json").read_text())
    commit = report["commit"]
    if commit["returncode"] != 0 or commit["stdout"] != identity()["GITHUB_SHA"]:
        raise ValueError("Test environment belongs to another candidate identity")
    return report


def junit_key(nodeid: str) -> tuple[str, str]:
    # pytest xunit2 的地址格式；参数中的 :: 不参与类/函数拆分。
    address, bracket, parameters = nodeid.partition("[")
    parts = address.split("::")
    parts[0] = parts[0].removesuffix(".py").replace("/", ".")
    return ".".join(parts[:-1]), parts[-1] + bracket + parameters


def junit_cases(folder: Path) -> set[tuple[str, str]]:
    root = ET.parse(folder / "junit.xml").getroot()
    suites = list(root.iter("testsuite"))
    if not suites:
        raise ValueError("Missing JUnit test suite")
    for suite in suites:
        if any(int(suite.get(name, "-1")) != 0 for name in ("failures", "errors", "skipped")):
            raise ValueError("JUnit contains failed, errored or skipped tests")
        # support.reporting 从真实 SubtestReport 计数；未知差额仍按缺失报告拒绝。
        subtest_counts = suite.findall(f"./properties/property[@name='{PASSED_SUBTESTS_PROPERTY}']")
        if len(subtest_counts) > 1:
            raise ValueError("Duplicate JUnit subtest count")
        subtests = int(subtest_counts[0].get("value", "-1")) if subtest_counts else 0
        if subtests < 0:
            raise ValueError("Invalid JUnit subtest count")
        if int(suite.get("tests", "-1")) != len(list(suite.iter("testcase"))) + subtests:
            raise ValueError("Incomplete JUnit test suite")
    result = set()
    for case in root.iter("testcase"):
        if any(case.find(name) is not None for name in ("failure", "error", "skipped")):
            raise ValueError("JUnit contains failed, errored or skipped tests")
        key = case.attrib["classname"], case.attrib["name"]
        if key in result:
            raise ValueError("Duplicate JUnit test result")
        result.add(key)
    if not result:
        raise ValueError("Empty JUnit coverage")
    return result


def check_native_contracts(folder: Path) -> int:
    """Require this candidate's exact component collection, never a full receipt."""
    report = read_environment(folder)
    if report.get('ci_identity') != identity():
        raise ValueError('Native contract environment belongs to another CI run identity')
    if not NATIVE_CONTRACT_TESTS or len(set(NATIVE_CONTRACT_TESTS)) != len(NATIVE_CONTRACT_TESTS):
        raise ValueError('Invalid native contract selection')
    nodes = collect_nodeids(NATIVE_CONTRACT_TESTS)
    expected = {junit_key(node) for node in nodes}
    if len(expected) != len(nodes) or junit_cases(folder) != expected:
        raise ValueError('Incomplete or unexpected native contract coverage')
    return len(expected)


def check_fixed_repair_regression(folder: Path) -> int:
    """Require the fixed 23 actual parameters, without producing a full receipt."""
    report = read_environment(folder)
    if report.get('ci_identity') != identity():
        raise ValueError('Fixed repair environment belongs to another CI run identity')
    expected = {junit_key(node) for node in FIXED_REPAIR_REGRESSION_TESTS}
    if len(FIXED_REPAIR_REGRESSION_TESTS) != 23 or len(expected) != 23:
        raise ValueError('Invalid fixed repair regression selection')
    if junit_cases(folder) != expected:
        raise ValueError('Incomplete or unexpected fixed repair regression coverage')
    return len(expected)


def tree_sha() -> str:
    value = subprocess.check_output(['git', 'rev-parse', 'HEAD^{tree}'], cwd=PRODUCT_ROOT,
                                    text=True, timeout=10).strip()
    if len(value) not in (40, 64) or any(c not in '0123456789abcdef' for c in value):
        raise ValueError('Invalid checkout tree')
    return value


def _same_run(source: dict) -> None:
    current, attempt = identity(), 'GITHUB_RUN_ATTEMPT'
    if ({k: v for k, v in source.items() if k != attempt}
            != {k: v for k, v in current.items() if k != attempt}):
        raise ValueError('Test receipt belongs to another CI identity')
    if (not isinstance(source.get(attempt), str) or not source[attempt].isdigit()
            or not 1 <= int(source[attempt]) <= int(current[attempt])):
        raise ValueError('Test receipt has an invalid or future attempt identity')


def _official_profile(folder: Path) -> None:
    suites = list(ET.parse(folder / 'junit.xml').getroot().iter('testsuite'))
    if not suites:
        raise ValueError('Missing official execution profile')
    for suite in suites:
        values = [p.get('value') for p in suite.findall('./properties/property')
                  if p.get('name') == EXECUTION_PROFILE_PROPERTY]
        if len(values) != 1 or digest(json.loads(values[0])) != digest(OFFICIAL_PROFILE):
            raise ValueError('Full tests require the actual official two-worker profile')


def _native_environment(report: dict) -> None:
    namespace = report['user_namespace']
    if (report['memfd'].get('available') is not True or report['pidfd'].get('available') is not True
            or type(namespace['bubblewrap_probe'].get('returncode')) is not int
            or namespace['bubblewrap_probe']['returncode'] != 0):
        raise ValueError('Unproven native boundary environment')


def _require_capabilities(folder: Path, source: dict, report: dict) -> None:
    caps = json.loads((folder / 'native-capabilities.json').read_text())
    if (caps['stage'] != 'native-capabilities' or caps['status'] != 'supported'
            or caps['complete_product_pass'] is not False or caps['ci_identity'] != source
            or caps['commit'] != report['commit']
            or set(caps['checks']) != {'memfd', 'pidfd', 'save_clock', 'user_namespace'}
            or any(value.get('available') is not True for value in caps['checks'].values())):
        raise ValueError('Missing successful native capability evidence')


def _full_native_execution(folder: Path, source: dict, report: dict) -> None:
    """A full single-job receipt retains the original physical prerequisites."""
    if report.get('ci_identity') != source:
        raise ValueError('Full test environment belongs to another CI identity')
    _official_profile(folder)
    _native_environment(report)
    _require_capabilities(folder, source, report)
    host_loss = junit_key('tests/test_kernel_managed_entry_end_to_end.py::'
                          'test_public_resume_after_exact_foreground_host_loss_preserves_parallel_work')
    for case in ET.parse(folder / 'junit.xml').getroot().iter('testcase'):
        if (case.get('classname'), case.get('name')) == host_loss:
            entries = case.findall('./properties/property[@name="host_loss_entry"]')
            if len(entries) != 1 or entries[0].get('value') != 'official':
                raise ValueError('Full tests require the official Host-loss entry')


def collect_nodeids(targets: tuple[str, ...] = ()) -> list[str]:
    # 只收集，不执行。清除隐含过滤，使少跑用例不能生成完整测试证据。
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "--disable-warnings",
         "-p", "no:cacheprovider", "-o", "addopts=", *targets],
        cwd=PRODUCT_ROOT, env=dict(os.environ, PYTEST_ADDOPTS="", PYTHONDONTWRITEBYTECODE="1"),
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode:
        raise ValueError(f"Product collection failed ({result.returncode}):\n{result.stdout}\n{result.stderr}")
    nodes = [line for line in result.stdout.splitlines() if line.startswith("tests/") and "::" in line]
    if not nodes or len(nodes) != len(set(nodes)):
        raise ValueError("Empty or duplicate complete collection")
    return sorted(nodes)


def contract_scope(nodes: list[str]) -> list[str]:
    selected = sorted(node for node in nodes if node.partition("[")[0] in CONTRACT_TESTS)
    if {node.partition("[")[0] for node in selected} != set(CONTRACT_TESTS):
        raise ValueError("Missing consumed CLI contract collection")
    return selected


def boolean(value: str) -> bool:
    if value not in ("true", "false"):
        raise ValueError("Missing or invalid check selection")
    return value == "true"


def record(folder: Path, *, contract: bool) -> None:
    report = read_environment(folder)
    if (report["python"]["version"] != sys.version
            or Path(report["python"]["executable"]).resolve() != Path(sys.executable).resolve()):
        raise ValueError("Test environment differs from the recording interpreter")
    cases = junit_cases(folder)
    _full_native_execution(folder, identity(), report)
    nodes = collect_nodeids()
    if len({junit_key(node) for node in nodes}) != len(nodes) or cases != {junit_key(node) for node in nodes}:
        raise ValueError("JUnit coverage does not match the complete collection")
    receipt = {
        "stage": "complete-product-tests", "identity": identity(), "tree_sha": tree_sha(),
        "collection_nodeids": nodes, "collection_sha256": digest(nodes), "total_count": len(nodes),
        "dependencies_sha256": dependency_digest(),
        "junit_sha256": file_digest(folder / "junit.xml"),
        "environment_sha256": file_digest(folder / "environment.json"),
        "native_capabilities_sha256": file_digest(folder / "native-capabilities.json"),
        "contract_nodeids": contract_scope(nodes) if contract else [],
    }
    (folder / "evidence.json").write_text(json.dumps(receipt, sort_keys=True) + "\n")


def verify(folder: Path, *, selected: str, product: str, contract: str, result: str) -> int:
    runs, tests, consumed = boolean(selected), boolean(product), boolean(contract)
    if (tests and not runs) or (consumed and not tests):
        raise ValueError("Inconsistent check selection")
    if result != ("success" if runs else "skipped"):
        raise ValueError(f"Unexpected checks result: {result!r}")
    if not tests:
        return 0
    receipt = json.loads((folder / "evidence.json").read_text())
    _same_run(receipt["identity"])
    if receipt["stage"] != "complete-product-tests" or receipt["tree_sha"] != tree_sha():
        raise ValueError("Complete test candidate or stage changed")
    if receipt["dependencies_sha256"] != dependency_digest():
        raise ValueError("Test dependency inputs changed")
    for key, filename in (("junit", "junit.xml"), ("environment", "environment.json"),
                          ("native_capabilities", "native-capabilities.json")):
        if receipt[f"{key}_sha256"] != file_digest(folder / filename):
            raise ValueError(f"Test {key} artifact changed")
    cases = junit_cases(folder)
    nodes = receipt["collection_nodeids"]
    if (not isinstance(nodes, list) or not nodes or any(not isinstance(node, str) for node in nodes)
            or nodes != sorted(set(nodes)) or len({junit_key(node) for node in nodes}) != len(nodes)
            or receipt["collection_sha256"] != digest(nodes)
            or type(receipt["total_count"]) is not int or receipt["total_count"] != len(nodes)
            or cases != {junit_key(node) for node in nodes}):
        raise ValueError("Incomplete complete collection coverage")
    _full_native_execution(folder, receipt["identity"], read_environment(folder))
    if receipt["contract_nodeids"] != (contract_scope(nodes) if consumed else []):
        raise ValueError("Incomplete or unexpected consumed CLI contract coverage")
    return len(cases)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("record", "verify", "check-native-contracts", "check-fixed-repair-regression"):
        command = commands.add_parser(name)
        command.add_argument("--results", type=Path, required=True)
        if name == "record":
            command.add_argument("--contract", choices=("true", "false"), default="false")
    args = parser.parse_args()
    try:
        if args.command == "record":
            record(args.results, contract=boolean(args.contract))
            print("Recorded complete product tests")
        elif args.command == "check-native-contracts":
            print(f"Native contracts verified: {check_native_contracts(args.results)} passed; not full acceptance")
        elif args.command == "check-fixed-repair-regression":
            print(f"Fixed repair regression verified: {check_fixed_repair_regression(args.results)} passed; "
                  "complete_product_pass=false")
        else:
            total = verify(args.results, selected=os.environ["CHECKS_SELECTED"],
                           product=os.environ["PRODUCT_SELECTED"],
                           contract=os.environ.get("CONTRACT_SELECTED", "false"),
                           result=os.environ["PRODUCT_RESULT"])
            print(f"CI checks verified: {total} product tests")
    except (ValueError, OSError, KeyError, TypeError, ET.ParseError, subprocess.SubprocessError) as error:
        parser.exit(1, f"CI evidence rejected: {error}\n")


if __name__ == "__main__":
    main()
