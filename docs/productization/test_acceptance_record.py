"""Offline checks of worksheet accounting, not release or product acceptance."""

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from check_acceptance import complete, unique_object


def filled_record():
    record = json.loads(Path(__file__).with_name("acceptance-record.template.json").read_text())
    record.update(source_commit="1" * 40, documentation_commit="1" * 40, archive_sha256="2" * 64)
    for index, check in enumerate(record["checks"].values(), 1):
        check.update(status="pass", evidence=[{"id": f"evidence-{index:03d}", "sha256": "3" * 64}])
    return record


class WorksheetTests(unittest.TestCase):
    def test_unrun_template_is_not_complete(self):
        template = json.loads(Path(__file__).with_name("acceptance-record.template.json").read_text())
        self.assertFalse(complete(template))

    def test_all_sections_need_pass_and_evidence(self):
        record = filled_record()
        self.assertTrue(complete(record))  # Synthetic shape only, not real-world success.
        for name in record["checks"]:
            for status in ("fail", "blocked", "unrun", "skip", "pending", True):
                changed = copy.deepcopy(record)
                changed["checks"][name]["status"] = status
                self.assertFalse(complete(changed), (name, status))
            changed = copy.deepcopy(record)
            changed["checks"][name]["evidence"] = []
            self.assertFalse(complete(changed))

    def test_missing_cloud_or_different_version_cannot_pass(self):
        record = filled_record()
        del record["checks"]["parent_cloud_delivery"]
        self.assertFalse(complete(record))
        record = filled_record()
        record["documentation_commit"] = "4" * 40
        self.assertFalse(complete(record))

    def test_unknown_fields_bad_evidence_and_duplicate_json_are_rejected(self):
        record = filled_record()
        record["environment"] = {"SECRET": "not-real"}
        self.assertFalse(complete(record))
        record = filled_record()
        record["checks"]["real_worker"]["evidence"][0]["id"] = "/private/path"
        self.assertFalse(complete(record))
        with self.assertRaises(ValueError):
            json.loads('{"format":1,"format":2}', object_pairs_hook=unique_object)

    def test_cli_rejects_incomplete_invalid_and_oversized_records_without_echo(self):
        script = Path(__file__).with_name("check_acceptance.py")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "record.json"
            for payload, expected in (
                (Path(__file__).with_name("acceptance-record.template.json").read_bytes(), 1),
                (b'{"private-marker": INVALID', 1),
                (b"private-marker" + b" " * 65536, 1),
                (json.dumps(filled_record()).encode(), 0),
            ):
                path.write_bytes(payload)
                result = subprocess.run([sys.executable, "-B", str(script), str(path)],
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, expected)
                self.assertNotIn("private-marker", result.stdout + result.stderr)
                self.assertNotIn(str(path), result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
