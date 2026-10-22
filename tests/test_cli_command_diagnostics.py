from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_resume_without_a_managed_workspace_explains_how_to_start(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment['PYTHONPATH'] = str(Path(__file__).parents[1] / 'src')
    result = subprocess.run(
        [sys.executable, '-m', 'agent_run', 'resume', '--parent', '213',
         '--repo', 'example/project', '--json'],
        cwd=tmp_path, env=environment, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 2
    diagnostic = json.loads(result.stdout)['diagnostics'][0]
    assert diagnostic['code'] == 'run_selector_not_found'
    assert 'Runner 工作区' in diagnostic['message']
    assert 'run' in diagnostic['message']
    assert not (tmp_path / '.agent-run').exists()
