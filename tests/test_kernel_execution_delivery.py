"""Kernel-selected roles do real child work before real local Git/PR effects."""

import json
import os
from pathlib import Path
import sys

import pytest

from agent_run_kernel import DeliveryKind
from agent_run_kernel.execution import execute
from agent_run_kernel.offline import Scenario


_CONTENT = "Implemented by the allocated development process.\n"
_BODY = "The controlled child implemented the requested change and an isolated child inspected it."
_ROLE_PROGRAM = """\
import json, os, subprocess, sys
from pathlib import Path
role, log_path = sys.argv[1:]
context = json.load(sys.stdin)
assert Path.cwd() == Path(context['checkout'])
with Path(log_path).open('a') as log:
    log.write(role + ':' + str(os.getpid()) + '\\n')
content = 'Implemented by the allocated development process.\\n'
if role == 'development':
    Path('child-delivery.txt').write_text(content)
    result = {'result_kind': 'development', 'summary': 'Implemented the requested file in this process.', 'human_blockers': None}
elif role == 'review':
    observed = subprocess.run(['git', 'rev-parse', 'HEAD'], check=True, text=True, capture_output=True).stdout.strip()
    assert observed == context['reviewed_commit']
    found = Path('child-delivery.txt').is_file() and Path('child-delivery.txt').read_text() == content
    Path('validation.tmp').write_text('Temporary inspection artifact from the review process.\\n')
    result = {'checks': {name: {'status': 'fail' if name == 'spec' and not found else 'pass',
        'evidence': 'This isolated process inspected the exact Git checkout and requested file.',
        'findings': ['Implement child-delivery.txt with the requested content and inspect it again.']
                    if name == 'spec' and not found else []}
        for name in ('e2e', 'standards', 'spec')}}
elif role == 'publication':
    result = {'result_kind': 'publication', 'commit_message': 'feat: deliver the child implementation',
        'pr_title': 'feat: deliver the child implementation',
        'pr_body_markdown': 'The controlled child implemented the requested change and an isolated child inspected it.',
        'human_blockers': None}
else:
    raise AssertionError('The Kernel selected an unsupported role')
sys.stdout.write(json.dumps(result))
"""


@pytest.mark.parametrize(("kind", "header"), [
    (DeliveryKind.TICKET, "Parent Issue: #1\nPrimary Ticket: #2\nDelivery Type: Ticket\n\n"),
    (DeliveryKind.PARENT_ONLY, "Parent Issue: #1\nDelivery Type: Parent-only\n\n"),
    (DeliveryKind.FINAL_RUN, "Parent Issue: #1\nDelivery Type: Final Run\n\n"),
])
def test_real_role_processes_feed_all_three_deliveries(tmp_path: Path, kind: DeliveryKind, header: str) -> None:
    scenario = Scenario(tmp_path / "scenario", kind)
    processes = tmp_path / "role-processes"
    for _ in range(100):
        step = scenario.kernel.advance(scenario.delivery.key, scenario.execution, scenario.world())
        if step.kind == "agent":
            assert step.call is not None
            call = step.call
            checkout = Path(call.context["checkout"])
            if call.role.value == "review":
                assert checkout != Path(scenario.delivery.checkout)
            result = execute(
                scenario.kernel, call,
                arguments=[sys.executable, "-I", "-c", _ROLE_PROGRAM, call.role.value, str(processes)],
                cwd=checkout, prompt=json.dumps(dict(call.context)),
                environment=os.environ.copy(), timeout=5,
            )
            assert result is not None and result.returncode == 0, result
        elif step.kind == "approval":
            scenario.kernel.control("approve", "approve-child-delivery", subject=scenario.delivery.key,
                                    world=scenario.world())
        else:
            assert step.kind not in {"paused", "waiting"}, step
            scenario.handle(step)
            if step.kind == "complete":
                break
    else:
        pytest.fail("Real child execution did not reach the delivery boundary")

    observed = [line.split(":") for line in processes.read_text().splitlines()]
    assert [role for role, _ in observed] == (
        ["review", "development", "review", "publication"] if kind == DeliveryKind.FINAL_RUN
        else ["development", "review", "publication"]
    )
    assert all(int(pid) != os.getpid() for _, pid in observed)
    target = scenario.port.git.commit(scenario.delivery.target_ref)
    assert scenario.port.git.run("show", f"{target.sha}:child-delivery.txt").stdout == _CONTENT
    assert "validation.tmp" not in scenario.port.git.run("ls-tree", "-r", "--name-only", target.sha).stdout.splitlines()
    pr = scenario.port.pull_request(scenario.delivery.key)
    assert pr is not None and pr.state == "MERGED"
    assert pr.title == "feat: deliver the child implementation" and pr.body == header + _BODY
    assert scenario.kernel.status(scenario.delivery.key)["phase"] == "complete"
