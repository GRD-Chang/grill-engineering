"""分片完整性、真实 pytest 收集与错误参数的回归。"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from support.sharding import select_shard


@pytest.mark.parametrize("count", [1, 2, 3, 7])
def test_shards_are_disjoint_complete_balanced_and_order_independent(count: int) -> None:
    nodes = [f"tests/test_sample.py::test_case[{i}]" for i in range(101)]
    shards = [select_shard(nodes, count, index) for index in range(count)]
    assert set.union(*shards) == set(nodes)
    assert sum(map(len, shards)) == len(nodes)
    assert max(map(len, shards)) - min(map(len, shards)) <= 1
    assert shards == [select_shard(list(reversed(nodes)), count, index) for index in range(count)]


@pytest.mark.parametrize("count,index,nodes", [
    (0, 0, ["a"]), (2, -1, ["a"]), (2, 2, ["a"]),
    (2, 1, ["a"]), (2, 0, []), (2, 0, ["a", "a"]),
])
def test_invalid_or_empty_shards_cannot_report_success(count: int, index: int, nodes: list[str]) -> None:
    with pytest.raises(ValueError):
        select_shard(nodes, count, index)


def test_real_pytest_shards_cover_the_unfiltered_collection(tmp_path: Path) -> None:
    sample = tmp_path / "test_sample.py"
    sample.write_text("import pytest\n@pytest.mark.parametrize('value',range(7))\ndef test_case(value):\n    assert value >= 0\n")
    command = [sys.executable, "-m", "pytest", "--noconftest", "-p", "support.sharding", "--collect-only", "-q", str(sample)]
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent), "PYTHONDONTWRITEBYTECODE": "1"}

    def collect(*args: str) -> set[str]:
        result = subprocess.run([*command, *args], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stdout + result.stderr
        return {line for line in result.stdout.splitlines() if "::test_case[" in line}

    complete = collect()
    shards = [collect("--test-shard-count=2", f"--test-shard-index={index}") for index in range(2)]
    assert len(complete) == 7
    assert set.union(*shards) == complete
    assert shards[0].isdisjoint(shards[1])
    invalid = subprocess.run([*command, "--test-shard-count=2", "--test-shard-index=2"], cwd=tmp_path, env=env,
                             capture_output=True, text=True, timeout=15)
    assert invalid.returncode == 4
    assert "0 <= index < count" in invalid.stderr
