import json
from pathlib import Path
import pytest
from agent_run.task_identity import TaskKey
from agent_run_kernel import Kernel
from agent_run_kernel.effects import LocalGit
from agent_run_kernel.migration_records import V6RecordReader
from agent_run_kernel.model import wire_json
from agent_run_kernel.model import KernelError
from test_kernel_migration_original_whole_run import (
    test_original_whole_run_formal_cutover_continues_c_and_final_against_original as forward,
)

class Checked(Exception):
    pass

@pytest.mark.parametrize("change", ["historical_base", "foreign_owner"])
def test_formal_consumer_rejects_changed_proof(tmp_path, git_repo, monkeypatch, change):
    advance = Kernel.advance_cleanup

    def inspect(kernel, subject, execution, world=None):
        if not subject.startswith("final:"):
            return advance(kernel, subject, execution, world)
        with kernel.storage.transaction(write=False) as tx:
            cleanup = tx.one("SELECT * FROM resource_cleanup WHERE subject=?", (subject,))
            run = tx.one("SELECT * FROM runs")
        if cleanup["stage"] != 1 or cleanup["effect_id"] is not None:
            return advance(kernel, subject, execution, world)

        task = TaskKey(Path(run["repository_root"]), run["repository"], run["parent_issue"])
        assert Kernel.open_task(task) is not None
        prior = LocalGit(task.workspace).commit(cleanup["source_ref"])
        base = V6RecordReader(kernel).row(
            "run_condition_base", [run["id"]]
        )["value_sha"]

        with kernel.storage.transaction() as tx:
            if change == "historical_base":
                assert prior.sha != base, (prior, base)
                row = tx.one("SELECT * FROM history WHERE kind='v6_run_import'")
                body = json.loads(tx.get(row["material_id"], row["subject"]))
                body["run_ref_ownership"]["head_evidence"] = [
                    {"table": "run_condition_base", "key": [run["id"]]}
                ]
                body["run_ref_ownership"]["expected_oid"] = base
                purpose, kind = "v6_run_import", "v6_run_import"
            else:
                row = tx.one("SELECT * FROM history WHERE kind='v6_record_import'")
                body = json.loads(tx.get(row["material_id"], row["subject"]))
                table = body["tables"]["work_jobs"]
                index = [column["name"] for column in table["columns"]].index("owner_key")
                changed = False
                for page in table["pages"]:
                    records = json.loads(tx.get(page["material"], row["subject"]))
                    for record in records:
                        if record["key"] == [run["id"], "ticket:2:generation:1"]:
                            assert record["cells"][index]["value"] == "2"
                            record["cells"][index]["value"] = "3"
                            changed = True
                    page["material"] = tx.put(
                        row["subject"], "v6_record_page", wire_json(records)
                    )
                assert changed
                purpose, kind = "v6_record_manifest", "v6_record_import"

            material = tx.put(row["subject"], purpose, wire_json(body))
            tx.execute("UPDATE history SET material_id=? WHERE kind=?", (material, kind))

        # Admission and an already-running consumer authenticate the same
        # historical ownership; neither trusts a re-bound archive declaration.
        with pytest.raises(KernelError, match="legacy_requires_offline_migration"):
            Kernel.open_task(task)
        step = advance(kernel, subject, execution, world)
        assert (step.kind, step.reason, step.effect) == (
            "paused", "cleanup_marker_unproven", None
        ), (change, step)
        assert LocalGit(task.workspace).commit(cleanup["source_ref"]) == prior
        assert kernel.status(subject)["resource_cleanup"]["effect_id"] is None
        raise Checked

    monkeypatch.setattr(Kernel, "advance_cleanup", inspect)
    with pytest.raises(Checked):
        forward(tmp_path, git_repo, monkeypatch)
