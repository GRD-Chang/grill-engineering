"""Run the pre-binding death probe in a fresh interpreter, without pytest threads."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sys
from typing import Any

from agent_run.core.kernel import Kernel
from agent_run.model import AgentCall, Execution, Profile, Purpose, Role
from agent_run.runtime.execution import execute


def main() -> None:
    request = json.loads(Path(sys.argv[1]).read_text())
    write_fd = int(sys.argv[2])
    fields = request["call"]
    fields.update(execution=Execution(**fields["execution"]),
                  profile=Profile(**fields["profile"]),
                  role=Role(fields["role"]), purpose=Purpose(fields["purpose"]))
    call = AgentCall(**fields)

    class HeldBinding(Kernel):
        def agent_started(self, exact_call: AgentCall, identity: dict[str, Any]) -> None:
            os.write(write_fd, str(identity["pid"]).encode("ascii"))
            os.close(write_fd)
            signal.pause()

    execute(HeldBinding(Path(request["database"]), run_id=call.execution.run_id), call,
            arguments=request["arguments"], cwd=Path(request["cwd"]), prompt="",
            environment={"PATH": os.defpath}, timeout=5)


if __name__ == "__main__":
    main()
