"""绑定当前 CI 的分片结果，核验完整覆盖后汇总 quality；不运行重复测试。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

from test_scope import BOUNDARY_TESTS, CONTRACT_TESTS, NATIVE_CONTRACT_TESTS, FIXED_REPAIR_REGRESSION_TESTS

PRODUCT_ROOT = Path(__file__).resolve().parents[2]
DEPENDENCY_FILES = ("pyproject.toml", "tests/dev-requirements.txt", "tests/build-requirements.txt")
IDENTITY_FIELDS = (
    "GITHUB_REPOSITORY", "GITHUB_SHA", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_WORKFLOW_REF",
)
PASSED_SUBTESTS_PROPERTY = "agent-run-pytest-passed-subtests-v1"


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


def environment_signature(report: dict) -> str:
    # 成功分片的 MemTotal 可有少量差异；实际内存计量仍保留在报告中。
    # 等价性比较运行版本、CPU/控制组配置，排除实时负载和带进程号的探针输出。
    keys = (
        "python", "sqlite", "platform", "os_release", "packages", "git", "bubblewrap",
        "cpu_count", "cpu_affinity_count", "cgroup_cpu_max", "cgroup_memory_max", "memfd", "pidfd",
    )
    return digest({key: report[key] for key in keys})


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
        # support.sharding 从真实 SubtestReport 计数；未知差额仍按缺失报告拒绝。
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


def check_boundary(folder: Path) -> int:
    """A partial gate, never a full receipt; require every trusted case passed."""
    read_environment(folder)
    expected = {junit_key(node) for node in BOUNDARY_TESTS}
    if not expected or len(expected) != len(BOUNDARY_TESTS):
        raise ValueError('Invalid trusted boundary selection')
    if junit_cases(folder) != expected:
        raise ValueError('Incomplete or unexpected boundary coverage')
    return len(expected)


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
    """Require the fixed 24 actual parameters, without producing a full receipt."""
    report = read_environment(folder)
    if report.get('ci_identity') != identity():
        raise ValueError('Fixed repair environment belongs to another CI run identity')
    expected = {junit_key(node) for node in FIXED_REPAIR_REGRESSION_TESTS}
    if len(FIXED_REPAIR_REGRESSION_TESTS) != 24 or len(expected) != 24:
        raise ValueError('Invalid fixed repair regression selection')
    if junit_cases(folder) != expected:
        raise ValueError('Incomplete or unexpected fixed repair regression coverage')
    return len(expected)


def collect_nodeids(targets: tuple[str, ...] = ()) -> list[str]:
    # 只收集，不执行。清除隐含过滤，使少跑用例不能生成完整分片证据。
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "--disable-warnings",
         "-p", "no:cacheprovider", "-o", "addopts=", *targets],
        cwd=PRODUCT_ROOT, env=dict(os.environ, PYTEST_ADDOPTS="", PYTHONDONTWRITEBYTECODE="1"),
        capture_output=True, text=True, timeout=120, check=True,
    )
    nodes = [line for line in result.stdout.splitlines() if line.startswith("tests/") and "::" in line]
    if not nodes or len(nodes) != len(set(nodes)):
        raise ValueError("Empty or duplicate complete collection")
    return sorted(nodes)


def contract_scope(nodes: list[str]) -> list[str]:
    selected = sorted(node for node in nodes if node.partition("[")[0] in CONTRACT_TESTS)
    if {node.partition("[")[0] for node in selected} != set(CONTRACT_TESTS):
        raise ValueError("Missing consumed CLI contract collection")
    return selected


def _shard_count(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 256:
        raise ValueError("Expected an explicit shard count from 1 through 256")
    return value


def record(folder: Path, shard: int, *, contract: bool, shard_count: int = 2) -> None:
    shard_count = _shard_count(shard_count)
    if type(shard) is not int or not 0 <= shard < shard_count:
        raise ValueError("Unexpected shard index")
    report = read_environment(folder)
    if (report["python"]["version"] != sys.version
            or Path(report["python"]["executable"]).resolve() != Path(sys.executable).resolve()):
        raise ValueError("Test environment differs from the recording interpreter")
    cases = junit_cases(folder)
    nodes = collect_nodeids()
    expected = {junit_key(node) for node in nodes[shard::shard_count]}
    if len(nodes) < shard_count or cases != expected or len({junit_key(node) for node in nodes}) != len(nodes):
        raise ValueError("JUnit coverage does not match the complete shard collection")
    receipt = {
        "identity": identity(), "shard_count": shard_count, "shard_index": shard,
        "collection_sha256": digest(nodes), "total_count": len(nodes), "selected_count": len(cases),
        "dependencies_sha256": dependency_digest(),
        "junit_sha256": file_digest(folder / "junit.xml"),
        "environment_sha256": file_digest(folder / "environment.json"),
        "contract_nodeids": contract_scope(nodes) if contract else [],
    }
    (folder / "evidence.json").write_text(json.dumps(receipt, sort_keys=True) + "\n")


def boolean(value: str) -> bool:
    if value not in ("true", "false"):
        raise ValueError("Missing or invalid check selection")
    return value == "true"


def verify(folder: Path, *, selected: str, product: str, contract: str, result: str, shard_count: int = 2) -> int:
    runs, tests, consumed = boolean(selected), boolean(product), boolean(contract)
    if (tests and not runs) or (consumed and not tests):
        raise ValueError("Inconsistent check selection")
    if result != ("success" if runs else "skipped"):
        raise ValueError(f"Unexpected checks result: {result!r}")
    if not tests:
        return 0
    shard_count = _shard_count(shard_count)
    expected = {f"test-diagnostics-shard-{index}" for index in range(shard_count)}
    if {path.name for path in folder.iterdir()} != expected:
        raise ValueError("Missing or unexpected test shards")
    receipts, environments, observed = [], [], []
    for index in range(shard_count):
        shard = folder / f"test-diagnostics-shard-{index}"
        receipt = json.loads((shard / "evidence.json").read_text())
        current, source = identity(), receipt["identity"]
        attempt = "GITHUB_RUN_ATTEMPT"
        if ({key: value for key, value in source.items() if key != attempt}
                != {key: value for key, value in current.items() if key != attempt}):
            raise ValueError("Test shard belongs to another CI identity")
        # 同一 Run 的失败重跑保留已成功分片的原身份，不强制重跑已覆盖的测试。
        if (not isinstance(source.get(attempt), str) or not source[attempt].isdigit()
                or not 1 <= int(source[attempt]) <= int(current[attempt])):
            raise ValueError("Test shard has an invalid or future attempt identity")
        if (type(receipt["shard_count"]) is not int or receipt["shard_count"] != shard_count
                or type(receipt["shard_index"]) is not int or receipt["shard_index"] != index):
            raise ValueError("Duplicate or inconsistent shard identity")
        if receipt["dependencies_sha256"] != dependency_digest():
            raise ValueError("Test dependency inputs changed")
        for name in ("junit", "environment"):
            suffix = "xml" if name == "junit" else "json"
            if receipt[f"{name}_sha256"] != file_digest(shard / f"{name}.{suffix}"):
                raise ValueError(f"Test {name} artifact changed")
        cases = junit_cases(shard)
        if type(receipt["total_count"]) is not int or receipt["total_count"] < shard_count:
            raise ValueError("Invalid complete collection count")
        if (receipt["selected_count"] != len(cases)
                or len(cases) != len(range(index, receipt["total_count"], shard_count))):
            raise ValueError("Incomplete shard coverage")
        receipts.append(receipt)
        environments.append(environment_signature(read_environment(shard)))
        observed.append(cases)
    if len(set(environments)) != 1:
        raise ValueError("Test shards used different environments")
    for field in ("collection_sha256", "total_count", "contract_nodeids"):
        if any(receipt[field] != receipts[0][field] for receipt in receipts[1:]):
            raise ValueError(f"Test shards used different collection scope: {field}")
    all_cases: set[tuple[str, str]] = set()
    for cases in observed:
        if all_cases & cases:
            raise ValueError("Test shard coverage overlaps")
        all_cases.update(cases)
    if len(all_cases) != receipts[0]["total_count"]:
        raise ValueError("Incomplete complete collection coverage")
    scope = receipts[0]["contract_nodeids"]
    if consumed:
        bases = {junit_key(node) for node in CONTRACT_TESTS}
        covered_contract = {(module, name) for module, name in all_cases
                            if (module, name.partition("[")[0]) in bases}
        if (scope != contract_scope(scope) or len(scope) != len(set(scope))
                or {junit_key(node) for node in scope} != covered_contract):
            raise ValueError("Incomplete consumed CLI contract coverage")
    elif scope:
        raise ValueError("Unexpected consumed CLI contract scope")
    return len(all_cases)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    writer = commands.add_parser("record")
    writer.add_argument("--results", type=Path, required=True)
    writer.add_argument("--shard", type=int, required=True)
    writer.add_argument("--shard-count", type=int, default=2)
    writer.add_argument("--contract", choices=("true", "false"), default="false")
    reader = commands.add_parser("verify")
    reader.add_argument("--results", type=Path, required=True)
    reader.add_argument("--shard-count", type=int, default=2)
    boundary = commands.add_parser('check-boundary')
    boundary.add_argument('--results', type=Path, required=True)
    native = commands.add_parser('check-native-contracts')
    native.add_argument('--results', type=Path, required=True)
    repair = commands.add_parser('check-fixed-repair-regression')
    repair.add_argument('--results', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "record":
            record(args.results, args.shard, contract=boolean(args.contract), shard_count=args.shard_count)
            print(f"Recorded complete test shard {args.shard}")
        elif args.command == 'check-boundary':
            print(f'Boundary regression verified: {check_boundary(args.results)} passed; not full acceptance')
        elif args.command == 'check-native-contracts':
            print(f'Native contracts verified: {check_native_contracts(args.results)} passed; not full acceptance')
        elif args.command == 'check-fixed-repair-regression':
            print(f'Fixed repair regression verified: {check_fixed_repair_regression(args.results)} passed; '
                  'original 19/14/full UNRUN; complete_product_pass=false')
        else:
            total = verify(
                args.results, selected=os.environ["CHECKS_SELECTED"], product=os.environ["PRODUCT_SELECTED"],
                contract=os.environ.get("CONTRACT_SELECTED", "false"), result=os.environ["PRODUCT_RESULT"],
                shard_count=args.shard_count,
            )
            print(f"CI checks verified: {total} product tests")
    except (ValueError, OSError, KeyError, TypeError, ET.ParseError, subprocess.SubprocessError) as error:
        parser.exit(1, f"CI evidence rejected: {error}\n")


if __name__ == "__main__":
    main()
