"""Fast public shell tests: fake host commands, no package/network/account writes."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _script(path: Path, body: str) -> None:
    path.write_text('#!/bin/sh\n' + body)
    path.chmod(0o755)


@pytest.fixture
def setup_host(tmp_path: Path, request: pytest.FixtureRequest):
    source = tmp_path / 'source'
    source.mkdir()
    shutil.copy2(ROOT / 'setup.sh', source / 'setup.sh')
    (source / 'src').symlink_to(ROOT / 'src', target_is_directory=True)
    binaries = tmp_path / 'bin'
    binaries.mkdir()
    home = tmp_path / 'home'
    home.mkdir()
    log = tmp_path / 'calls'
    for name in ('dirname', 'tr', 'awk', 'sh', 'mkdir', 'chmod', 'cat', 'timeout', 'flock'):
        (binaries / name).symlink_to(shutil.which(name))
    _script(binaries / 'uname', 'case "$1" in -s) echo "${TEST_KERNEL:-Linux}";; *) echo x86_64;; esac\n')
    _script(binaries / 'sed', 'case "$1" in *VERSION_ID*) echo 24.04;; *) echo "${TEST_DISTRO:-ubuntu}";; esac\n')
    _script(binaries / 'id', 'echo "${TEST_UID:-1000}"\n')
    _script(binaries / 'sudo', 'exit 1\n')
    _script(binaries / 'apt-cache', 'echo "Candidate: test-version; configured test source"\n')
    _script(binaries / 'apt-get', 'echo "apt $*" >> "$TEST_LOG"\nexit "${TEST_APT_EXIT:-0}"\n')
    # Policy cases execute the controlled, immediately returning fake tools
    # directly. Representative probe/cleanup cases keep the real boundary;
    # no-argument finalization always uses it, including doctor and unit cleanup.
    probe_dispatch = '' if getattr(request, 'param', False) else '''
if [ "${1##*/}" = runner_setup.py ] && [ "$#" -gt 1 ]; then
    shift
    exec "$@" 2>&1
fi
'''
    _script(binaries / 'python3', probe_dispatch + f'if [ "$1" = - ] || [ "${{1##*/}}" = runner_setup.py ]; then exec "{sys.executable}" "$@"; fi\nexit "${{TEST_PYTHON_EXIT:-0}}"\n')
    _script(binaries / 'git', 'echo "git version ${TEST_GIT_VERSION:-2.43.0}"\n')
    for name in ('gh', 'bwrap', 'openssl', 'codex', 'systemctl', 'systemd-run'):
        _script(binaries / name, 'echo "compatible --paginate --slurp --method --header"\n')
    _script(source / 'install.sh', '''echo install >> "$TEST_LOG"
if [ "${TEST_INSTALL_EXIT:-0}" != 0 ]; then exit "$TEST_INSTALL_EXIT"; fi
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/agent-run" <<'SCRIPT'
#!/bin/sh
echo '{"status":"ready","execution_readiness":{"status":"ready"}}'
SCRIPT
chmod +x "$HOME/.local/bin/agent-run"
''')
    env = {**os.environ, 'PATH': str(binaries), 'HOME': str(home), 'TEST_LOG': str(log)}
    for variable in ('XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME', 'XDG_RUNTIME_DIR'):
        env[variable] = str(tmp_path / variable)

    def run(*args: str, answer: str = '', **settings: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(['/bin/sh', str(source / 'setup.sh'), *args], env={**env, **settings}, input=answer, capture_output=True, text=True, timeout=10)

    return run, binaries, source, log, home


@pytest.mark.parametrize('setup_host', [True], indirect=True, ids=['real-probes'])
def test_compatible_tools_reused_and_rerun(setup_host):
    run, _, _, log, _ = setup_host
    for _ in range(2):
        result = run('--yes')
        assert result.returncode == 0, result.stdout + result.stderr
        assert '安装及执行条件均满足' in result.stdout
    assert log.read_text().splitlines() == ['install', 'install']


def test_one_confirmation_installs_only_missing_package(setup_host):
    run, binaries, _, log, _ = setup_host
    (binaries / 'gh').unlink()
    result = run(answer='y\n', TEST_UID='0')
    assert result.returncode == 0, result.stderr
    assert result.stdout.count('执行以上计划？') == 1
    assert log.read_text().splitlines() == ['apt update', 'apt install -y --no-install-recommends gh', 'install']
    assert 'Candidate: test-version' in result.stdout


def test_rejection_performs_no_changes(setup_host):
    run, binaries, _, log, _ = setup_host
    (binaries / 'gh').unlink()
    result = run(answer='n\n', TEST_UID='0')
    assert result.returncode == 1
    assert not log.exists()


@pytest.mark.parametrize('settings,reason', [({}, '权限不足'), ({'TEST_DISTRO': 'alpine'}, '无 apt 适配'), ({'TEST_UID': '0', 'TEST_APT_EXIT': '1'}, '依赖准备失败')])
def test_unavailable_preparation_continues_install(setup_host, settings, reason):
    run, binaries, _, log, _ = setup_host
    (binaries / 'gh').unlink()
    result = run('--yes', **settings)
    assert reason in result.stdout
    assert log.read_text().splitlines()[-1] == 'install'
    if reason != '依赖准备失败':
        assert log.read_text() == 'install\n'


def test_missing_python_stops_before_installer_then_manual_rerun(setup_host):
    run, _, _, log, _ = setup_host
    result = run('--yes', TEST_PYTHON_EXIT='1')
    assert result.returncode == 1
    assert '无法构建' in result.stdout
    assert not log.exists()
    assert run('--yes').returncode == 0


def test_installer_failure_preserves_old_entry(setup_host):
    run, _, _, _, home = setup_host
    entry = home / '.local/bin/agent-run'
    entry.parent.mkdir(parents=True)
    entry.write_text('old unmanaged sentinel')
    result = run('--yes', TEST_INSTALL_EXIT='1')
    assert result.returncode == 1
    assert '新版本未激活' in result.stdout
    assert entry.read_text() == 'old unmanaged sentinel'


def test_diagnostic_exit_zero_does_not_imply_readiness(setup_host):
    run, _, source, _, _ = setup_host
    installer = source / 'install.sh'
    installer.write_text(installer.read_text().replace('"status":"ready"', '"status":"issues"'))
    result = run('--yes')
    assert result.returncode == 2
    assert '已安装但执行环境未就绪' in result.stdout


def test_non_linux_rejected_before_modification(setup_host):
    run, _, _, log, _ = setup_host
    result = run('--yes', TEST_KERNEL='Darwin')
    assert result.returncode == 1
    assert '仅面向 Linux' in result.stdout
    assert not log.exists()


def test_transient_unit_failure_is_not_ready_and_is_cleaned(setup_host):
    run, binaries, _, log, _ = setup_host
    _script(binaries / 'systemd-run', 'echo user-bus-denied >&2\nexit 1\n')
    _script(binaries / 'systemctl', 'echo "systemctl $*" >> "$TEST_LOG"\n')
    result = run('--yes')
    assert result.returncode == 2
    assert 'user-bus-denied' in result.stderr
    assert 'systemctl --user stop agent-run-setup-' in log.read_text()
    assert 'systemctl --user reset-failed agent-run-setup-' in log.read_text()


def test_incompatible_git_is_in_preparation_plan(setup_host):
    run, _, _, log, _ = setup_host
    result = run('--yes', TEST_GIT_VERSION='2.39.2', TEST_UID='0')
    assert result.returncode == 0
    assert '缺失或不兼容：git' in result.stdout
    assert log.read_text().splitlines()[1] == 'apt install -y --no-install-recommends git'


def test_outer_entry_runs_with_no_python_or_git_on_path(setup_host):
    run, binaries, _, log, _ = setup_host
    (binaries / 'python3').unlink()
    (binaries / 'git').unlink()
    result = run('--yes', TEST_DISTRO='alpine')
    assert result.returncode == 1
    assert '缺失或不兼容：python3' in result.stdout
    assert '缺失或不兼容：git' in result.stdout
    assert '无法构建' in result.stdout
    assert not log.exists()


@pytest.mark.parametrize('body', ['exit 1\n', 'exit 0\n', 'echo invalid\n'])
def test_git_empty_failed_or_invalid_output_is_not_reused(setup_host, body):
    run, binaries, _, log, _ = setup_host
    _script(binaries / 'git', body)
    result = run('--yes', TEST_UID='0')
    assert result.returncode == 0, result.stdout + result.stderr
    assert '复用：Git' not in result.stdout
    assert 'apt install -y --no-install-recommends git' in log.read_text()


def test_old_github_cli_is_included_in_preparation(setup_host):
    run, binaries, _, log, _ = setup_host
    _script(binaries / 'gh', 'echo "--paginate --method --header"\n')
    result = run('--yes', TEST_UID='0')
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'apt install -y --no-install-recommends gh' in log.read_text()


@pytest.mark.parametrize('setup_host', [True], indirect=True, ids=['real-probes'])
def test_setup_probe_reaps_escaped_child_after_normal_exit(setup_host):
    run, binaries, source, _, _ = setup_host
    pid_path = source / 'probe-child.pid'
    executable = binaries / 'codex'
    executable.write_text(
        f'#!{sys.executable}\n'
        'import subprocess\nfrom pathlib import Path\n'
        f'child = subprocess.Popen([{sys.executable!r}, "-c", "import time; time.sleep(30)"], '
        'start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n'
        f'Path({str(pid_path)!r}).write_text(str(child.pid))\n'
    )
    result = run('--yes')
    assert result.returncode == 0, result.stdout + result.stderr
    assert not Path('/proc', pid_path.read_text()).exists()


def test_host_package_preparation_respects_runner_usage_lease(setup_host):
    from agent_run.runner_lease import runner_usage_lease

    run, binaries, _, log, home = setup_host
    (binaries / 'gh').unlink()
    lock = home.parent / 'XDG_DATA_HOME' / 'agent-run' / 'install.lock'
    with runner_usage_lease(lock):
        result = run('--yes', TEST_UID='0')
    assert 'Runner 管理租约忙' in result.stdout
    assert log.read_text() == 'install\n'  # No apt invocation; installer has its own lease.


def test_setup_checks_do_not_write_github_or_source_tree(setup_host):
    run, binaries, source, log, _ = setup_host
    _script(binaries / 'gh', 'echo "gh $*" >> "$TEST_LOG"\necho "--paginate --slurp --method --header"\n')
    before = {p.name: p.read_bytes() for p in source.iterdir() if p.is_file()}
    result = run('--yes')
    assert result.returncode == 0, result.stdout + result.stderr
    assert {p.name: p.read_bytes() for p in source.iterdir() if p.is_file()} == before
    assert [line for line in log.read_text().splitlines() if line.startswith('gh ')] == ['gh api --help', 'gh auth status']


@pytest.mark.parametrize('answer,install_exit,python_exit', [('y\n', '1', '0'), ('n\n', '0', '0'), ('y\n', '1', '1')])
def test_readonly_host_failures_are_reported_before_confirmation(
    setup_host, answer, install_exit, python_exit,
):
    run, binaries, _, log, _ = setup_host
    scripts = {
        'gh': 'case "$1" in auth) exit 1;; *) echo "--paginate --slurp --method --header";; esac\n',
        'codex': '[ "$1" = login ] && exit 1\nexit 0\n',
        'bwrap': '[ "$1" = --version ] && exit 0\nexit 1\n',
        'systemctl': 'exit 1\n',
        'systemd-run': 'exit 99\n',
    }
    for name, body in scripts.items():
        _script(binaries / name, f'echo "{name} $*" >> "$TEST_LOG"\n' + 'echo private-host-output >&2\n' + body)
    result = run(answer=answer, TEST_INSTALL_EXIT=install_exit, TEST_PYTHON_EXIT=python_exit)
    assert result.returncode == 1
    before_confirmation = result.stdout.split('执行以上计划？')[0]
    for reason in ('gh 认证未就绪', 'Codex 认证未就绪', 'bubblewrap namespace 探针失败', 'user systemd 会话不可用'):
        assert reason in before_confirmation
    calls = log.read_text().splitlines()
    assert 'gh auth status' in calls
    assert 'codex login status' in calls
    assert 'systemctl --user show-environment' in calls
    assert any(line.startswith('bwrap --die-with-parent') for line in calls)
    assert not any(line.startswith(('apt ', 'systemd-run ')) for line in calls)
    assert ('install' in calls) == (answer == 'y\n' and python_exit == '0')
    if 'install' in calls:
        assert calls.index('gh auth status') < calls.index('install')
        assert calls[-1] == 'systemctl --user show-environment'
        assert '新版本未激活' in result.stdout
    assert '安装及执行条件均满足' not in result.stdout
    assert 'private-host-output' not in result.stdout + result.stderr
