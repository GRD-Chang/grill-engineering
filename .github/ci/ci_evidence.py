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


PRODUCT_ROOT = Path(__file__).resolve().parents[2]
DEPENDENCY_FILES = ("pyproject.toml", "tests/dev-requirements.txt", "tests/build-requirements.txt")
CONTRACT_TESTS = (
    "tests/test_cli_mvp.py::test_run_reaches_explicit_approval_with_status_and_history",
    "tests/test_cli.py::test_parent_selector_works_in_a_repo_and_across_directories",
    "tests/test_doctor_readiness.py::test_doctor_cli_separates_installation_and_execution_readiness",
)
IDENTITY_FIELDS = (
    "GITHUB_REPOSITORY", "GITHUB_SHA", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_WORKFLOW_REF",
)


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
        if int(suite.get("tests", "-1")) != len(list(suite.iter("testcase"))):
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


def collect_nodeids() -> list[str]:
    # 只收集，不执行。清除隐含过滤，使少跑用例不能生成完整分片证据。
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "--disable-warnings",
         "-p", "no:cacheprovider", "-o", "addopts="],
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


def record(folder: Path, shard: int, *, contract: bool) -> None:
    if shard not in (0, 1):
        raise ValueError("Unexpected shard index")
    report = read_environment(folder)
    if (report["python"]["version"] != sys.version
            or Path(report["python"]["executable"]).resolve() != Path(sys.executable).resolve()):
        raise ValueError("Test environment differs from the recording interpreter")
    cases = junit_cases(folder)
    nodes = collect_nodeids()
    expected = {junit_key(node) for node in nodes[shard::2]}
    if cases != expected or len({junit_key(node) for node in nodes}) != len(nodes):
        raise ValueError("JUnit coverage does not match the complete shard collection")
    receipt = {
        "identity": identity(), "shard_count": 2, "shard_index": shard,
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


def verify(folder: Path, *, selected: str, product: str, contract: str, result: str) -> int:
    runs, tests, consumed = boolean(selected), boolean(product), boolean(contract)
    if (tests and not runs) or (consumed and not tests):
        raise ValueError("Inconsistent check selection")
    if result != ("success" if runs else "skipped"):
        raise ValueError(f"Unexpected checks result: {result!r}")
    if not tests:
        return 0
    expected = {f"test-diagnostics-shard-{index}" for index in range(2)}
    if {path.name for path in folder.iterdir()} != expected:
        raise ValueError("Missing or unexpected test shards")
    receipts, environments, observed = [], [], []
    for index in range(2):
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
        if receipt["shard_count"] != 2 or receipt["shard_index"] != index:
            raise ValueError("Duplicate or inconsistent shard identity")
        if receipt["dependencies_sha256"] != dependency_digest():
            raise ValueError("Test dependency inputs changed")
        for name in ("junit", "environment"):
            suffix = "xml" if name == "junit" else "json"
            if receipt[f"{name}_sha256"] != file_digest(shard / f"{name}.{suffix}"):
                raise ValueError(f"Test {name} artifact changed")
        cases = junit_cases(shard)
        if type(receipt["total_count"]) is not int or receipt["total_count"] < 2:
            raise ValueError("Invalid complete collection count")
        if (receipt["selected_count"] != len(cases)
                or len(cases) != len(range(index, receipt["total_count"], 2))):
            raise ValueError("Incomplete shard coverage")
        receipts.append(receipt)
        environments.append(environment_signature(read_environment(shard)))
        observed.append(cases)
    if environments[0] != environments[1]:
        raise ValueError("Test shards used different environments")
    for field in ("collection_sha256", "total_count", "contract_nodeids"):
        if receipts[0][field] != receipts[1][field]:
            raise ValueError(f"Test shards used different collection scope: {field}")
    if observed[0] & observed[1]:
        raise ValueError("Test shard coverage overlaps")
    all_cases = observed[0] | observed[1]
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
    writer.add_argument("--contract", choices=("true", "false"), default="false")
    reader = commands.add_parser("verify")
    reader.add_argument("--results", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "record":
            record(args.results, args.shard, contract=boolean(args.contract))
            print(f"Recorded complete test shard {args.shard}")
        else:
            total = verify(
                args.results, selected=os.environ["CHECKS_SELECTED"], product=os.environ["PRODUCT_SELECTED"],
                contract=os.environ.get("CONTRACT_SELECTED", "false"), result=os.environ["PRODUCT_RESULT"],
            )
            print(f"CI checks verified: {total} product tests")
    except (ValueError, OSError, KeyError, TypeError, ET.ParseError, subprocess.SubprocessError) as error:
        parser.exit(1, f"CI evidence rejected: {error}\n")


if __name__ == "__main__":
    main()
