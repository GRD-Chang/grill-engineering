"""Compatibility failures expose fixed categories, never Codex stderr."""
from pathlib import Path
import shutil
import sys

import pytest

from agent_run.install.probe import RunnerProbeBackend, RunnerProbeError
from agent_run.install.installer import _safe_candidate_probe_error


@pytest.mark.parametrize("known,large", [(True, False), (True, True), (False, False), (False, True)])
def test_probe_stderr_is_classified_without_disclosing_output(tmp_path, known, large):
    candidate = _candidate(tmp_path)
    fake = tmp_path / "codex"
    marker = "app-server socket directory must be a user-owned directory with mode 0700"
    body = (marker if known else "unexpected initialization failure")
    fake.write_text(
        f"#!{sys.executable}\nimport os, sys\nsys.stdin.read()\n"
        + ("os.write(2, b'x' * 200000)\n" if large else "")
        + f"os.write(2, {('token=SECRET /private/user/path ' + body).encode()!r})\n"
        + ("os.write(2, b'y' * 200000)\n" if large else "")
        + "raise SystemExit(9)\n"
    )
    fake.chmod(0o700)
    with pytest.raises(RunnerProbeError) as caught:
        RunnerProbeBackend(str(fake), timeout_seconds=3).check(candidate)
    message = str(caught.value)
    if known:
        assert "Codex沙箱/执行环境初始化失败" in message
        assert "codex_sandbox_initialization_failed" in message
        assert "环境管理员" in message
    else:
        assert message == "Codex Compatibility Check returned a non-zero exit"
    assert "SECRET" not in message and "/private/user/path" not in message
    assert len(message) < 300
    assert _safe_candidate_probe_error(f"runner probe: {message}\n".encode()) == message
    assert _safe_candidate_probe_error(f"runner probe: {message} SECRET".encode()) == "Runner Compatibility Check returned a non-zero exit"


def _candidate(tmp_path: Path) -> Path:
    root = tmp_path / "candidate"
    package = root / "lib/python3.12/site-packages/agent_run"
    (package / "contracts").mkdir(parents=True)
    (package / "__init__.py").write_text("")
    for source in (Path(__file__).parents[1] / "src/agent_run/contracts").glob("text_*.py"):
        shutil.copyfile(source, package / "contracts" / source.name)
    return root


@pytest.mark.parametrize("status", ["ok", "wrong"])
def test_probe_stderr_does_not_replace_the_final_schema(tmp_path, status):
    candidate = _candidate(tmp_path)
    fake = tmp_path / "codex"
    fake.write_text(
        f"#!{sys.executable}\nimport os, sys, pathlib, json\nsys.stdin.read()\n"
        "os.write(2, b'app-server socket directory must be a user-owned directory with mode 0700')\n"
        "os.write(2, b'x' * 200000)\n"
        "output = pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1])\n"
        f"output.write_text(json.dumps({{'status': {status!r}}}))\n"
    )
    fake.chmod(0o700)
    backend = RunnerProbeBackend(str(fake), timeout_seconds=3)
    if status == "ok":
        assert backend.check(candidate) == {"result": "passed"}
    else:
        with pytest.raises(RunnerProbeError, match="rejected the required schema"):
            backend.check(candidate)


def test_unending_stderr_still_times_out_and_closes_pipes(tmp_path, monkeypatch):
    import subprocess
    import agent_run.install.probe as probe

    candidate = _candidate(tmp_path)
    fake = tmp_path / "codex"
    fake.write_text(f"#!{sys.executable}\nimport os\nwhile True: os.write(2, b'x' * 16384)\n")
    fake.chmod(0o700)
    started = []
    original = subprocess.Popen

    def record(*args, **kwargs):
        process = original(*args, **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(probe.subprocess, "Popen", record)
    with pytest.raises(RunnerProbeError, match="timed out"):
        RunnerProbeBackend(str(fake), timeout_seconds=0.15).check(candidate)
    assert len(started) == 1
    assert started[0].poll() is not None
    assert started[0].stdin.closed and started[0].stderr.closed


def test_stderr_marker_across_chunks_keeps_only_bounded_overlap():
    from agent_run.install.probe import _ProbeStderr

    capture = _ProbeStderr()
    for part in [b'x' * 200000, b'app-server socket directory must be a user-owned ',
                 b'directory with mode 0700', b'y' * 200000]:
        capture.feed(part)
        assert len(capture.data) <= 74
    assert capture.sandbox_initialization_failed


def test_installer_preserves_safe_probe_category_and_refuses_activation(tmp_path):
    from test_runner_installer import _source_tree, _fake_codex, _run, _data_root

    source = _source_tree(tmp_path)
    fake_bin, count, _ = _fake_codex(tmp_path, behavior="nonzero")
    codex = fake_bin / "codex"
    codex.write_text(codex.read_text().replace(
        "    sys.exit(7)",
        "    sys.stderr.write('token=SECRET app-server socket directory must be a user-owned directory with mode 0700')\n    sys.exit(7)",
    ))
    home = tmp_path / "home"
    home.mkdir()
    result = _run(source, home, fake_bin)
    assert result.returncode == 1
    assert "codex_sandbox_initialization_failed" in result.stderr
    assert "Codex沙箱/执行环境初始化失败" in result.stderr
    assert "SECRET" not in result.stderr and "Traceback" not in result.stderr
    assert int(count.read_text()) == 1
    assert not (_data_root(home) / "active").exists()
    assert not list((_data_root(home) / "staging").glob("*"))
