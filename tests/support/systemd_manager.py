"""Controlled systemd responses and sysfs; not a live manager.

The helpers execute normally, while unit/cgroup facts are synthetic.
"""
import json
import os
from pathlib import Path
import sys

from agent_run.adapters.systemd import SystemdHost
from agent_run.model import HostLaunch


def controlled_manager(tmp_path: Path) -> tuple[SystemdHost, HostLaunch, Path, Path]:
    """Only systemd CLI responses/sysfs are synthetic; helpers really execute."""
    carrier = tmp_path / "environment-carrier"
    carrier.write_text("Private test environment material; never an online credential")
    carrier.chmod(0o600)
    request = HostLaunch("run-1", 3, "executor-3", "nonce-3", "agent-run-nonce-3.service",
                         (sys.executable, "-c", "print('$literal')"), carrier)
    response, calls = tmp_path / "manager-response.json", tmp_path / "helper-calls.jsonl"
    group = tmp_path / "cgroups" / "unit-group"
    group.mkdir(parents=True)
    (group / "cgroup.events").write_text("populated 0\nfrozen 0\n")
    fields = {
        "Id": request.unit_name, "Description": 'agent-run-kernel:["run-1",3,"executor-3","nonce-3"]', "LoadState": "loaded",
        "ActiveState": "active", "SubState": "exited", "InvocationID": "1" * 32,
        "ControlGroup": "/unit-group", "MainPID": "0", "ControlPID": "0", "ExecMainCode": "1",
        "ExecMainStatus": "7", "ExecMainExitTimestamp": "@1700000000.25",
        "ExecMainExitTimestampMonotonic": "1234567",
        "NFileDescriptorStore": "0",
    }
    response.write_text(json.dumps({"fields": fields, "launch_returncode": 0}))
    program = f"#!{sys.executable}\n" + f"""\
import json, sys
from pathlib import Path
response, calls = Path({str(response)!r}), Path({str(calls)!r})
with calls.open('a') as stream:
    stream.write(json.dumps([Path(sys.argv[0]).name, *sys.argv[1:]]) + '\\n')
state = json.loads(response.read_text())
if Path(sys.argv[0]).name == 'systemd-run':
    raise SystemExit(state['launch_returncode'])
if '--property=Version' in sys.argv:
    print('257')
else:
    for key, value in state['fields'].items():
        print(key + '=' + value)
    if 'next_fields' in state:
        state['fields'] = state.pop('next_fields')
        response.write_text(json.dumps(state))
"""
    for name in ("systemctl", "systemd-run"):
        executable = tmp_path / name
        executable.write_text(program)
        executable.chmod(0o700)
    host = SystemdHost(systemctl=str(tmp_path / "systemctl"), systemd_run=str(tmp_path / "systemd-run"),
                       environment={"PATH": os.defpath, "HOME": str(tmp_path)}, cgroup_root=tmp_path / "cgroups")
    return host, request, response, calls
