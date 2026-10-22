"""Seal and verify original checkout/index bytes without changing Git state."""
from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import tarfile

from .migration import MigrationRefused
from .migration_conversion import _git


def _verify_checkout_archive(path: Path, checkout: Path, expected_sha: str) -> None:
    """Bind copied checkout bytes to the same stopped-source observation."""
    metadata = path.lstat()
    if (path.is_symlink() or not path.is_file() or metadata.st_nlink != 1
            or metadata.st_size > 32 * 1024 * 1024):
        raise MigrationRefused("source_checkout_archive")
    body = path.read_bytes()
    if hashlib.sha256(body).hexdigest() != expected_sha:
        raise MigrationRefused("source_checkout_archive")
    expected: dict[Path, bytes] = {}
    modes: dict[Path, int] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:") as archive:
            for member in archive:
                relative = Path(member.name)
                if (not member.isfile() or relative.is_absolute() or not relative.parts
                        or any(part in {"", ".", "..", ".git"} for part in relative.parts)
                        or relative in expected or member.size > 16 * 1024 * 1024):
                    raise MigrationRefused("source_checkout_archive")
                stream = archive.extractfile(member)
                if stream is None:
                    raise MigrationRefused("source_checkout_archive")
                expected[relative] = stream.read()
                modes[relative] = member.mode & 0o777
    except (OSError, tarfile.TarError, ValueError) as error:
        raise MigrationRefused("source_checkout_archive") from error
    paths = [path for path in checkout.rglob("*") if ".git" not in path.relative_to(checkout).parts]
    if any(path.is_symlink() or not (path.is_file() or path.is_dir()) for path in paths):
        raise MigrationRefused("source_checkout_archive")
    actual = {path.relative_to(checkout): path for path in paths if path.is_file()}
    if set(actual) != set(expected):
        raise MigrationRefused("source_checkout_archive")
    for relative, file in actual.items():
        if (file.is_symlink() or file.stat().st_nlink != 1 or file.read_bytes() != expected[relative]
                or file.stat().st_mode & 0o777 != modes[relative]):
            raise MigrationRefused("source_checkout_archive")


def seal_checkout(checkout: Path, repository_root: Path, archive: Path) -> tuple[str, str]:
    paths = sorted(path for path in checkout.rglob('*') if '.git' not in path.relative_to(checkout).parts)
    if any(path.is_symlink() or not (path.is_file() or path.is_dir()) for path in paths):
        raise MigrationRefused('source_checkout_archive')
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:') as bundle:
        for path in paths:
            if path.is_file():
                physical = path.stat()
                if physical.st_nlink != 1 or physical.st_size > 16 * 1024 * 1024:
                    raise MigrationRefused('source_checkout_archive')
                body = path.read_bytes()
                item = tarfile.TarInfo(path.relative_to(checkout).as_posix())
                item.size, item.mode = len(body), physical.st_mode & 0o777
                bundle.addfile(item, io.BytesIO(body))
                if output.tell() > 32 * 1024 * 1024:
                    raise MigrationRefused('source_checkout_archive')
    body = output.getvalue()
    digest = hashlib.sha256(body).hexdigest()
    with archive.open('xb') as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())
    _verify_checkout_archive(archive, checkout, digest)
    index = Path(_git(checkout, 'rev-parse', '--path-format=absolute', '--git-path', 'index'))
    if (index.is_symlink() or index.resolve() != index
            or not index.is_relative_to(repository_root / '.git' / 'worktrees')
            or not index.is_file() or index.stat().st_nlink != 1 or index.stat().st_size > 32 * 1024 * 1024):
        raise MigrationRefused('source_checkout_index')
    body = index.read_bytes()
    with (archive.parent / 'checkout-index').open('xb') as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())
    if index.read_bytes() != body:
        raise MigrationRefused('source_checkout_index')
    return digest, hashlib.sha256(body).hexdigest()
