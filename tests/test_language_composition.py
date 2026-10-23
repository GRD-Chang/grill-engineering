"""Personal selection belongs to CLI composition, not text/value leaves."""
import json
from pathlib import Path
import shutil
import sys

import pytest

import agent_run.config.resources as prompt_resources
from agent_run.errors import error_message
from agent_run.contracts.copy import selected_language
from agent_run.install.probe import RunnerProbeBackend, RunnerProbeError
from agent_run.config.defaults import UserDefaultsStore
from agent_run.queries.run import read
from agent_run.cli.main import main
from test_kernel_query_cli import _create


def test_builtin_language_and_error_audit_do_not_read_personal_defaults(monkeypatch) -> None:
    UserDefaultsStore().configure(language='en')
    def forbidden(*args, **kwargs):
        raise AssertionError('A text/value leaf read personal language')
    monkeypatch.setattr(UserDefaultsStore, 'language', forbidden)
    assert prompt_resources.selected_language() == 'zh'
    assert prompt_resources.read_builtin_resource('methods/development').startswith('你是负责')
    assert str(error_message('cli.positive_integer')) == '必须是正整数'
    assert error_message('cli.positive_integer').render('en') == 'Must be a positive integer'


def test_cli_resolves_personal_and_explicit_preview_without_hiding_bad_config(tmp_path: Path, capsys) -> None:
    store = UserDefaultsStore()
    store.configure(language='en')
    assert main(['--help']) == 0
    assert 'usage: ' in capsys.readouterr().out
    request = tmp_path / 'request.json'
    request.write_text(json.dumps({'task_issue_url': 'https://example.invalid/2'}))
    command = ['prompts', 'preview', '--request', str(request), '--role', 'development', '--json']
    assert main(command) == 0
    preview = json.loads(capsys.readouterr().out)
    assert 'You are the development engineer' in preview['prompt']
    assert all(source.startswith('builtin/en/') for source in preview['sources'].values())
    assert main(['run', '0']) == 2
    assert 'Must be a positive integer' in capsys.readouterr().err
    store.path.write_text('{"language":"invalid"}')
    before = (store.path.read_bytes(), store.path.stat().st_mtime_ns)
    assert main(['--help']) == 0
    assert '用法: ' in capsys.readouterr().out
    assert main(['runs', '--data-root', str(tmp_path / 'empty'), '--json']) == 0
    assert json.loads(capsys.readouterr().out) == {'result': 'runs', 'runs': []}
    assert main(command) == 2  # Actual implicit resource selection stays strict.
    capsys.readouterr()
    request.write_text(json.dumps({'language': 'en', 'task_issue_url': 'https://example.invalid/2'}))
    assert main(command) == 0
    assert 'You are the development engineer' in json.loads(capsys.readouterr().out)['prompt']
    assert (store.path.read_bytes(), store.path.stat().st_mtime_ns) == before


def test_run_language_remains_frozen_and_missing_values_never_use_personal_defaults(tmp_path: Path, monkeypatch) -> None:
    kernel = _create(tmp_path, language='en')
    UserDefaultsStore().configure(language='zh')
    def forbidden(*args, **kwargs):
        raise AssertionError('A frozen Run consulted current personal language')
    monkeypatch.setattr(UserDefaultsStore, 'language', forbidden)
    document = read(kernel.storage.path, 'status')
    assert document.language == selected_language({'language': document.language}) == 'en'
    for state in ({}, {'language': None}, {'language': 'invalid'}):
        with pytest.raises(ValueError, match='must be zh or en'):
            selected_language(state)


def test_install_probe_selects_personal_language_from_the_candidate_package(tmp_path: Path) -> None:
    candidate = tmp_path / 'candidate'
    package = candidate / 'lib/python3.11/site-packages/agent_run'
    package.mkdir(parents=True)
    for module in (Path(prompt_resources.__file__).resolve().parents[1] / 'contracts').glob('text_*.py'):
        (package / 'contracts').mkdir(exist_ok=True)
        shutil.copyfile(module, package / 'contracts' / module.name)
    context = package / 'contracts/text_context.py'
    with context.open('a', encoding='utf-8') as stream:
        stream.write('\nTEXTS["context/probe"]["en"] += "\\ncandidate-probe-en"\n'
                     'TEXTS["context/probe"]["zh"] += "\\ncandidate-probe-zh"\n')
    captured = tmp_path / 'captured-prompt'
    executable = tmp_path / 'fake-codex'
    executable.write_text(
        f'#!{sys.executable}\n'
        'import pathlib, sys\n'
        f'pathlib.Path({str(captured)!r}).write_text(sys.stdin.read(), encoding="utf-8")\n'
        'pathlib.Path(sys.argv[sys.argv.index("--output-last-message") + 1]).write_text(\'{"status":"ok"}\')\n',
        encoding='utf-8',
    )
    executable.chmod(0o700)
    backend = RunnerProbeBackend(executable=str(executable))
    store = UserDefaultsStore()
    for language, other in (('en', 'zh'), ('zh', 'en')):
        store.configure(language=language)
        assert backend.check(candidate) == {'result': 'passed'}
        prompt = captured.read_text(encoding='utf-8')
        assert f'candidate-probe-{language}' in prompt
        assert f'candidate-probe-{other}' not in prompt
    store.path.write_text('{"language":"invalid"}', encoding='utf-8')
    before = (captured.read_bytes(), captured.stat().st_mtime_ns)
    with pytest.raises(RunnerProbeError):
        backend.check(candidate)
    assert (captured.read_bytes(), captured.stat().st_mtime_ns) == before
