"""Reuse two original consumers against an existing installed candidate wheel.

Run with test/support paths only and pytest's source pythonpath disabled. The
Git-backed test overlay supplies unchanged 02 only to its original children;
candidate migration children use isolated mode and the current installed Python.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest


def _installed_origins() -> None:
    prefix = Path(sys.prefix).resolve()
    origins = {name: Path(module.__file__).resolve()
               for name, module in sys.modules.items()
               if (name in {'agent_run', 'agent_run_kernel'}
                   or name.startswith(('agent_run.', 'agent_run_kernel.')))
               and getattr(module, '__file__', None)}
    assert origins and all(path.is_relative_to(prefix) for path in origins.values()), origins


def _frozen_test(name: str, blob: str, monkeypatch: pytest.MonkeyPatch):
    path = Path(__file__).with_name(name + '.py')
    raw = path.read_bytes()
    assert hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest() == blob
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setitem(sys.modules, 'tests.' + name, module)
    spec.loader.exec_module(module)
    return module


def test_installed_migration_cli_apply_resume_inverse_and_original_continuation(
    tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = importlib.import_module('test_kernel_migration_original_returned')
    _installed_origins()
    run = subprocess.run
    commands: list[list[str]] = []

    def installed_cli(args, *positional, **keywords):
        if (isinstance(args, (list, tuple))
                and list(args[:3]) == [sys.executable, '-m', 'agent_run_kernel.migration_cutover']):
            commands.append(list(args))
            environment = dict(keywords['env'])
            for variable in ('PYTHONPATH', 'PYTHONHOME'):
                environment.pop(variable, None)
            environment['PYTHONNOUSERSITE'] = '1'
            keywords['env'] = environment
            args = [sys.executable, '-I', *args[1:]]
        return run(args, *positional, **keywords)

    with monkeypatch.context() as candidate_cli:
        candidate_cli.setattr(subprocess, 'run', installed_cli)
        original.test_original_development_raw_returns_exact_unadvanced_layout_and_old_continuation(
            tmp_path, git_repo, monkeypatch)
    assert len(commands) == 3
    assert not any(flag in commands[0] for flag in ('--resume', '--rollback'))
    assert commands[1][-1] == '--resume' and commands[2][-1] == '--rollback'
    _installed_origins()


def test_installed_public_run_refuses_original_authority_before_preparation(
    tmp_path: Path, git_repo: Path, tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    # These aliases preserve the frozen consumer's original imports. They refer
    # only to test modules; no candidate package or product result is replaced.
    host = importlib.import_module('test_kernel_host_adapter')
    monkeypatch.setitem(sys.modules, 'tests.test_kernel_host_adapter', host)
    legacy = _frozen_test('test_kernel_legacy_admission',
                          '2e2356a8397cd9eaf7dc8ece47746559d0237e23', monkeypatch)
    original = _frozen_test('test_kernel_public_legacy_admission',
                            '6a3df3d753841cb7fc2a87d4b257547d499dacd3', monkeypatch)
    _installed_origins()
    source = legacy.unchanged_v6_source.__wrapped__(tmp_path_factory)
    original.test_public_run_refuses_the_original_unknown_v6_task_before_creation_or_launch(
        git_repo, tmp_path, monkeypatch, capsys, source)
    _installed_origins()
