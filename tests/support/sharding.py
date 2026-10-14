"""将完整收集结果分成互斥且均衡的 CI 分片，默认仍运行全部用例。"""
from __future__ import annotations

import pytest


def select_shard(nodeids: list[str], count: int, index: int) -> set[str]:
    if count < 1 or not 0 <= index < count:
        raise ValueError("Test shard requires count >= 1 and 0 <= index < count")
    if len(set(nodeids)) != len(nodeids):
        raise ValueError("Test sharding requires unique collected node IDs")
    selected = set(sorted(nodeids)[index::count])
    if not selected:
        raise ValueError("Test shard has no collected tests")
    return selected


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("CI sharding")
    group.addoption("--test-shard-count", type=int, default=1)
    group.addoption("--test-shard-index", type=int, default=0)


def pytest_configure(config: pytest.Config) -> None:
    count, index = config.getoption("test_shard_count"), config.getoption("test_shard_index")
    if count < 1 or not 0 <= index < count:
        raise pytest.UsageError("Test shard requires count >= 1 and 0 <= index < count")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    count, index = config.getoption("test_shard_count"), config.getoption("test_shard_index")
    if count == 1:
        return
    try:
        selected = select_shard([item.nodeid for item in items], count, index)
    except ValueError as error:
        raise pytest.UsageError(str(error)) from error
    deselected = [item for item in items if item.nodeid not in selected]
    items[:] = [item for item in items if item.nodeid in selected]
    config.hook.pytest_deselected(items=deselected)
