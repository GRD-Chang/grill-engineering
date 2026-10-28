"""Generated gh text I/O retains native Path spelling and filesystem semantics."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from support.fixture_text import read_text, write_text


@pytest.mark.parametrize('spelling', ['absolute', 'relative', 'trailing-slash', 'double-separator',
    'dot', 'parent', 'symlink', 'symlink-parent', 'double-root', 'path-object'])
def test_text_io_matches_path_for_existing_files(tmp_path, monkeypatch, spelling):
    monkeypatch.chdir(tmp_path)
    file = tmp_path / 'value.txt'
    original = 'first\r\n保留原文\rfinal\n'.encode()
    file.write_bytes(original)
    (tmp_path / 'folder').mkdir()
    (tmp_path / 'link').symlink_to(file)
    nested = tmp_path / 'elsewhere' / 'nested'
    nested.mkdir(parents=True)
    (tmp_path / 'directory-link').symlink_to(nested, target_is_directory=True)
    (nested.parent / 'value.txt').write_bytes(original)
    names = {'absolute': str(file), 'relative': 'value.txt', 'trailing-slash': str(file) + '/',
        'double-separator': str(tmp_path) + '//value.txt', 'dot': str(tmp_path) + '/./value.txt',
        'parent': str(tmp_path) + '/folder/../value.txt', 'symlink': str(tmp_path / 'link'),
        'symlink-parent': str(tmp_path) + '/directory-link/../value.txt',
        'double-root': '/' + str(file), 'path-object': file}
    name = names[spelling]
    assert read_text(name) == Path(name).read_text()
    replacement = 'new\r\n组合 e\u0301\n'
    expected_count = Path(name).write_text(replacement)
    expected_bytes = Path(name).read_bytes()
    Path(name).write_bytes(original)
    assert write_text(name, replacement) == expected_count
    assert Path(name).read_bytes() == expected_bytes
    if spelling == 'symlink-parent':
        assert file.read_bytes() == original  # Never rewrite the lexical '..' target.
        assert (nested.parent / 'value.txt').read_bytes() == expected_bytes


@pytest.mark.parametrize('case', ['missing', 'directory', 'empty', 'invalid-utf8', 'dangling-symlink'])
def test_read_errors_match_path(tmp_path, monkeypatch, case):
    monkeypatch.chdir(tmp_path)
    name = str(tmp_path / 'missing')
    if case == 'directory': name = str(tmp_path)
    if case == 'empty': name = ''
    if case == 'invalid-utf8':
        name = str(tmp_path / 'invalid')
        Path(name).write_bytes(b'\xff')
    if case == 'dangling-symlink':
        name = str(tmp_path / 'dangling')
        Path(name).symlink_to(tmp_path / 'absent')
    failures = []
    for call in (lambda: Path(name).read_text(), lambda: read_text(name)):
        with pytest.raises(Exception) as caught:
            call()
        error = caught.value
        failures.append((type(error), error.args, getattr(error, 'filename', None)))
    assert failures[0] == failures[1]


@pytest.mark.parametrize('spelling', ['', '/'])
@pytest.mark.parametrize('data', [b'bytes', None])
def test_invalid_write_does_not_truncate_and_matches_path(tmp_path, spelling, data):
    file = tmp_path / 'preserved'
    file.write_bytes(b'original bytes')
    name = str(file) + spelling
    failures = []
    for call in (lambda: Path(name).write_text(data), lambda: write_text(name, data)):
        with pytest.raises(TypeError) as caught:
            call()
        failures.append(caught.value.args)
        assert file.read_bytes() == b'original bytes'
    assert failures[0] == failures[1]
    with pytest.raises(TypeError):
        write_text(name, json.dumps(object()))
    assert file.read_bytes() == b'original bytes'


def test_canonical_child_does_not_import_pathlib_and_alias_uses_original_path(tmp_path):
    file = tmp_path / 'file'
    file.write_bytes(b'initial\r\n')
    code = '''import sys
from support.fixture_text import read_text, write_text
assert 'pathlib' not in sys.modules
assert read_text(sys.argv[1]) == 'initial\\n'
assert write_text(sys.argv[1], 'new\\r\\n') == 5
assert 'pathlib' not in sys.modules
assert read_text(sys.argv[1] + '/') == 'new\\n'
assert 'pathlib' in sys.modules
'''
    environment = {**os.environ, 'PYTHONPATH': str(Path(__file__).parent)}
    result = subprocess.run([sys.executable, '-c', code, str(file)], env=environment,
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert file.read_bytes() == b'new\r\n'


@pytest.mark.parametrize('text,anchor', [('no boundary', 'missing'), ('twice twice', 'twice'), ('anything', '')])
def test_script_transform_rejects_missing_ambiguous_or_empty_boundary(text, anchor):
    from support.fixture_text import replace_once
    with pytest.raises(ValueError, match='exactly once') as error:
        replace_once(text, anchor, 'new')
    assert text not in str(error.value)
    if anchor:
        assert anchor not in str(error.value)


def test_script_transform_changes_one_boundary_without_rescanning_inserted_text():
    from support.fixture_text import replace_once
    assert replace_once('before exact after', 'exact', 'exact exact') == 'before exact exact after'
