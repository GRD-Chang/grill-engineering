"""Check a manually reviewed release worksheet; never execute delivery or authorize it."""

import json
from pathlib import Path
import re
import sys


CHECKS = frozenset({
    "archive_install", "identity_access", "real_worker", "ticket_delivery", "ci_repair",
    "approval_boundary", "stop_resume", "background_delivery", "parent_cloud_delivery",
})


def complete(record):
    if not isinstance(record, dict) or set(record) != {
        "format", "source_commit", "archive_sha256", "documentation_commit", "checks",
    }:
        return False
    if record["format"] != "agent-run-release-checklist-v1":
        return False
    for field, size in (("source_commit", 40), ("documentation_commit", 40), ("archive_sha256", 64)):
        if not isinstance(record[field], str) or not re.fullmatch(r"[0-9a-f]{%d}" % size, record[field]):
            return False
    if record["source_commit"] != record["documentation_commit"]:
        return False
    checks = record["checks"]
    if not isinstance(checks, dict) or set(checks) != CHECKS:
        return False
    for check in checks.values():
        if not isinstance(check, dict) or set(check) != {"status", "evidence"} or check["status"] != "pass":
            return False
        evidence = check["evidence"]
        if not isinstance(evidence, list) or not evidence:
            return False
        for item in evidence:
            if not isinstance(item, dict) or set(item) != {"id", "sha256"}:
                return False
            if not isinstance(item["id"], str) or not re.fullmatch(r"evidence-[0-9]{3,6}", item["id"]):
                return False
            if not isinstance(item["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
                return False
    return True


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate field")
        result[key] = value
    return result


def main(arguments):
    if len(arguments) != 1:
        print("Usage: python3 check_acceptance.py RECORD.json", file=sys.stderr)
        return 2
    try:
        with Path(arguments[0]).open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("Record too large")
        record = json.loads(raw, object_pairs_hook=unique_object)
        passed = complete(record)
    except (OSError, ValueError, RecursionError):
        # Do not echo supplied paths, JSON documents or arbitrary exception text.
        passed = False
    print("Worksheet complete; independent evidence review required." if passed else
          "Worksheet incomplete or invalid; no release verdict.")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
