"""One isolated copied-v6 cutover and effect-free return, using an actual local child."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest

from agent_run.task_identity import TaskKey

from agent_run_kernel.migration import MigrationRefused, capture_v6
from agent_run_kernel.migration_conversion import convert_initial_v6
from agent_run_kernel.migration_cutover import (cutover_idle_copied_v6,
                                                 restore_idle_copied_v6_without_new_effects)
from agent_run_kernel.model import HostObservation, Profile
from agent_run_kernel.offline import OfflinePort
from agent_run_kernel.task_location import locked_task, read_task


def test_fixed_4aba_private_copy_cutover_and_no_effect_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    oracle = Path(__file__).resolve().parents[3] / "results" / "kernel-initial-observation"
    source = oracle / "kernel-initial-v6.sqlite3"
    bundle = oracle / "kernel-initial-v6.bundle"
    facts = json.loads((oracle / "kernel-initial-v6.json").read_text(encoding="utf-8"))
    assert hashlib.sha256(source.read_bytes()).hexdigest() == "64a39d083111ad3625b7da038c2904040da32eb0790415a06a029ff334af1203"
    assert hashlib.sha256(bundle.read_bytes()).hexdigest() == "cbc45212d4743008729c1662abdc253928352eb4e07f5045827a90ad646d4e71"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "private-data"))
    snapshot = capture_v6(source, tmp_path / "sealed.sqlite3")
    private = tmp_path / "private"
    port = OfflinePort.create(private / "new-runtime")
    run_branch = facts["root"]["value_run_branch"]
    base_sha = facts["refs"][run_branch][0]
    port.git.run("fetch", str(bundle), "refs/heads/main:refs/remotes/v6/main",
                 f"refs/heads/{run_branch}:refs/remotes/v6/run")
    port.git.run("reset", "--hard", base_sha)
    port.git.run("update-ref", f"refs/heads/{run_branch}", base_sha)
    with sqlite3.connect(port.database) as db:
        db.execute("UPDATE refs SET sha=? WHERE ref='refs/heads/main'", (base_sha,))
        db.execute("INSERT INTO refs VALUES (?,?)", (f"refs/heads/{run_branch}", base_sha))
    checkout = private / "new-runtime" / "work" / "ticket-2"
    checkout.parent.mkdir()
    port.git.run("worktree", "add", "--detach", str(checkout), base_sha)
    kernel = convert_initial_v6(
        snapshot, private / "new-runtime" / "kernel.sqlite3", repository_root=port.git.root,
        source_ref=f"refs/heads/agent-run/{facts['run']['run_id']}/converted-ticket-2",
        checkout=checkout, final_checkout=private / "new-runtime" / "work" / "final",
        development_profile=Profile("fixture-development", "high"),
        review_profile=Profile("fixture-review", "high"),
        publication_profile=Profile("fixture-publication", "high"))
    old_copy = private / "old-state" / "state.sqlite3"
    carrier = private / "local-host-carrier"
    carrier.write_text("private synthetic host only", encoding="utf-8")
    nonce, launch = cutover_idle_copied_v6(
        snapshot, old_copy, kernel, private_root=private, action_id="copy-cutover-run",
        command=(sys.executable, "-c", "import sys; sys.stdin.buffer.read()"),
        environment_carrier=carrier, preflight=lambda: None)
    with closing(sqlite3.connect(old_copy)) as db:
        protocol = db.execute("SELECT value FROM schema_meta WHERE key='protocol'").fetchone()[0]
    assert protocol.startswith("retired-to-native-copy/1:")
    task = TaskKey(port.git.root, facts["run"]["repository"], facts["run"]["parent_number"])
    with locked_task(task) as path:
        assert read_task(path) == (facts["run"]["run_id"], kernel.storage.path)

    # A local, reaped process supplies actual PID, start token, cgroup and boot
    # identity. It is a private test Host, not a systemd or userbus assertion.
    assert kernel.dispatch_host(launch)
    child = subprocess.Popen(launch.command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        stat = Path(f"/proc/{child.pid}/stat").read_text(encoding="ascii")
        start_token = stat[stat.rfind(")") + 2:].split()[19]
        cgroup = Path(f"/proc/{child.pid}/cgroup").read_text(encoding="ascii").splitlines()[0].split(":", 2)[2]
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        invocation = f"local-{child.pid}-{start_token}"
        assert kernel.observe_host(launch, HostObservation(
            "running", launch.unit_name, invocation, cgroup, boot_id, child.pid,
            start_token, evidence="actual local child process", usage_ready=True)) == "running"
    finally:
        assert child.stdin is not None
        child.stdin.close()
        assert child.wait(timeout=10) == 0
    assert kernel.observe_host(launch, HostObservation(
        "exited", launch.unit_name, invocation, cgroup, boot_id, child.pid, start_token,
        0, time.time(), "actual child reaped; local usage released", usage_released=True)) == "exited"
    kernel.control("stop", "copy-cutover-stop")
    owner = sqlite3.connect(old_copy, isolation_level=None)
    try:
        assert owner.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        owner.execute("BEGIN IMMEDIATE")
        with pytest.raises(MigrationRefused, match="source_not_offline"):
            restore_idle_copied_v6_without_new_effects(snapshot, old_copy, kernel, nonce=nonce)
        # A second SQLite descriptor opened and closed by the refusal path
        # would cancel this process's POSIX main-file lock.
        probe = subprocess.run([sys.executable, "-c", """
import sqlite3, sys
db = sqlite3.connect(sys.argv[1], timeout=0)
try:
    db.execute('BEGIN IMMEDIATE')
except sqlite3.OperationalError as error:
    assert 'locked' in str(error), error
else:
    raise AssertionError('rollback refusal released the existing SQLite writer lock')
finally:
    db.close()
""", str(old_copy)], capture_output=True, text=True, timeout=10)
        assert probe.returncode == 0, probe.stdout + probe.stderr
    finally:
        owner.rollback()
        owner.close()
    foreign = old_copy.parent / (".restored-" + nonce + ".sqlite3")
    foreign.write_bytes(b"another attempt owns this name")
    assert restore_idle_copied_v6_without_new_effects(snapshot, old_copy, kernel, nonce=nonce) == old_copy
    assert foreign.read_bytes() == b"another attempt owns this name"
    assert hashlib.sha256(old_copy.read_bytes()).hexdigest() == snapshot.sha256
    assert hashlib.sha256(source.read_bytes()).hexdigest() == snapshot.sha256
    with closing(sqlite3.connect(old_copy)) as db:
        assert db.execute("SELECT value FROM schema_meta WHERE key='protocol'").fetchone()[0] == "sqlite-work-records-v6"
    with locked_task(task) as path:
        assert read_task(path) == (facts["run"]["run_id"], old_copy)
