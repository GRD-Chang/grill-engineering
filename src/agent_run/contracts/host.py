"""Frozen foreground-domain facts; no process I/O or lifecycle decisions."""
from __future__ import annotations

from typing import Any, Mapping


def valid_foreground_domain(value: Mapping[str, Any], nonce: str) -> bool:
    try:
        observer, init, executor = (value[key] for key in ("observer", "init", "executor"))
        if value["nonce"] != nonce or not all(isinstance(row, dict) for row in (observer, init)) or executor is not None and not isinstance(executor, dict):
            return False
        if not all(isinstance(observer[key], str) and observer[key] for key in ("boot_id", "pid_namespace", "pid1_start")):
            return False
        for row in ((init,) if executor is None else (init, executor)):
            if (type(row["pid"]) is not int or row["pid"] <= 1
                    or not isinstance(row["start_token"], str) or not row["start_token"]
                    or row["boot_id"] != observer["boot_id"]
                    or row["observer_namespace"] != observer["pid_namespace"]
                    or row["pid_namespace"] == observer["pid_namespace"]
                    or not isinstance(row["namespace_pids"], list) or len(row["namespace_pids"]) < 2
                    or any(type(pid) is not int or pid < 1 for pid in row["namespace_pids"])
                    or row["namespace_pids"][0] != row["pid"]):
                return False
        return bool(init["namespace_pids"][-1] == 1 and (executor is None or
                    executor["namespace_pids"][-1] > 1 and init["pid_namespace"] == executor["pid_namespace"]))
    except (KeyError, TypeError):
        return False
