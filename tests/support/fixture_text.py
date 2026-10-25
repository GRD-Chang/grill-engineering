"""Text I/O for generated gh fixtures; retain Path semantics for other spellings."""
import os


def read_text(filename):
    # normpath only recognizes the fast path. Its result never replaces input.
    if not isinstance(filename, str) or os.path.normpath(filename) != filename:
        from pathlib import Path
        return Path(filename).read_text()
    with open(filename) as stream:
        return stream.read()


def write_text(filename, data):
    if not isinstance(filename, str) or os.path.normpath(filename) != filename:
        from pathlib import Path
        return Path(filename).write_text(data)
    if not isinstance(data, str):
        raise TypeError('data must be str, not %s' % data.__class__.__name__)
    with open(filename, 'w') as stream:
        return stream.write(data)
