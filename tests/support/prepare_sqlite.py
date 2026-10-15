"""明确准备阶段构建、核验并临时启用带 WAL-reset 修复的官方 SQLite。"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile


VERSION = "3.51.3"
# https://sqlite.org/releaselog/3_51_3.html
SOURCE_ID = "2026-03-13 10:38:09 737ae4a34738ffa0c3ff7f9bb18df914dd1cad163f28fd6b6e114a344fe6d618"
SOURCE_SHA3 = "32d5424f97e0a7fc5ed2f6335afbb58be4e0298bd7117a34e39d345ff13d859e"
URL = "https://sqlite.org/2026/sqlite-amalgamation-3510300.zip"
MEMBER = "sqlite-amalgamation-3510300/sqlite3.c"

VERIFY = """
from contextlib import closing
from pathlib import Path
import sqlite3, sys
with closing(sqlite3.connect(':memory:')) as connection:
    version, source_id = connection.execute('SELECT sqlite_version(), sqlite_source_id()').fetchone()
if (version, source_id) != (sys.argv[1], sys.argv[2]):
    raise SystemExit(f'Unexpected SQLite runtime: {version}, {source_id}')
library = str(Path(sys.argv[3]).resolve())
if not any(line.endswith(' ' + library) for line in Path('/proc/self/maps').read_text().splitlines()):
    raise SystemExit('Python did not load the prepared SQLite library')
print(f'SQLite {version}, source_id={source_id}')
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path,
                        help="本次准备独占的新目录；作业结束后由调用方删除")
    parser.add_argument("--source", type=Path, help="复用本地 sqlite3.c，仍核验固定 SHA3-256")
    args = parser.parse_args()
    destination = args.output_dir.resolve()
    # 不覆盖或在失败时删除已有目录，避免影响别的准备任务。
    destination.mkdir(parents=True, exist_ok=False)
    try:
        with tempfile.TemporaryDirectory(prefix="build-", dir=destination) as temporary:
            build = Path(temporary)
            source = build / "sqlite3.c"
            if args.source is not None:
                shutil.copyfile(args.source, source)
            else:
                archive = build / "sqlite.zip"
                with urllib.request.urlopen(URL, timeout=60) as response, archive.open("wb") as output:
                    shutil.copyfileobj(response, output, length=64 * 1024)
                with zipfile.ZipFile(archive) as package, package.open(MEMBER) as member, source.open("wb") as output:
                    shutil.copyfileobj(member, output, length=64 * 1024)
            with source.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha3_256").hexdigest()
            if digest != SOURCE_SHA3:
                raise ValueError(f"SQLite sqlite3.c SHA3-256 mismatch: {digest}")
            library = destination / "libsqlite3.so.0"
            subprocess.run([
                "cc", "-shared", "-fPIC", "-O2", "-DSQLITE_ENABLE_COLUMN_METADATA",
                "-DSQLITE_ENABLE_FTS5", "-DSQLITE_ENABLE_RTREE", "-DSQLITE_ENABLE_LOAD_EXTENSION",
                "-Wl,-soname,libsqlite3.so.0", str(source), "-o", str(library),
            ], check=True)
            prior = os.environ.get("LD_LIBRARY_PATH", "")
            library_path = str(destination) + (os.pathsep + prior if prior else "")
            verified = subprocess.run(
                [sys.executable, "-I", "-c", VERIFY, VERSION, SOURCE_ID, str(library)],
                env={**os.environ, "LD_LIBRARY_PATH": library_path},
                check=True, capture_output=True, text=True,
            )
        # 源码和下载包已清理；只有实际加载核验成功的库进入后续作业环境。
        if github_environment := os.environ.get("GITHUB_ENV"):
            with Path(github_environment).open("a", encoding="utf-8") as output:
                output.write(f"LD_LIBRARY_PATH={library_path}\n")
        print(verified.stdout, end="")
        print(f"LD_LIBRARY_PATH={library_path}")
    except BaseException:
        shutil.rmtree(destination)
        raise


if __name__ == "__main__":
    main()
