"""Byte accounting at the shared reader, independent of business projections."""
from contextlib import closing
import sqlite3

import pytest

from agent_run.errors import KernelError
from agent_run.storage.sqlite import QUERY_LIMIT, Transaction


@pytest.mark.parametrize('encoding', ['UTF-8', 'UTF-16le', 'UTF-16be'])
def test_budget_counts_database_bytes_for_columns_and_expressions(encoding):
    text, blob = '中文\x00🙂', b'\x00\xff\x01'
    with closing(sqlite3.connect(':memory:')) as connection:
        connection.execute(f"PRAGMA encoding='{encoding}'")
        connection.row_factory = sqlite3.Row
        connection.execute('CREATE TABLE facts(t TEXT,b BLOB,i INTEGER,r REAL,n)')
        connection.execute('INSERT INTO facts VALUES (?,?,?,?,?)', (text, blob, 17, 1.25, None))
        tx = Transaction(connection)
        rows = tx.rows('SELECT t AS "quoted""name",b,i,r,n,t||? AS expression FROM facts', ('尾',))
        assert tuple(rows[0]) == (text, blob, 17, 1.25, None, text + '尾')
        expected = len(text.encode(encoding)) + len(blob) + 3 * 16 + len((text + '尾').encode(encoding))
        assert (tx.rows_read, tx.bytes_read) == (1, expected)
        assert tx.rows('SELECT t FROM facts WHERE i=?', (0,)) == []
        assert (tx.rows_read, tx.bytes_read) == (1, expected)


def test_byte_rejection_precedes_python_body_conversion_and_keeps_charge(monkeypatch):
    import agent_run.storage.sqlite as storage

    with closing(sqlite3.connect(':memory:')) as connection:
        connection.execute('CREATE TABLE facts(body TEXT)')
        connection.executemany('INSERT INTO facts VALUES (?)', [('ok',), ('中' * 1024,)])
        conversions = []

        def decode(body):
            conversions.append(len(body))
            assert len(body) < 128, 'Rejected body reached Python'
            return body.decode()

        connection.text_factory = decode
        tx = Transaction(connection)
        assert tx.one('SELECT body FROM facts WHERE rowid=1')[0] == 'ok'
        monkeypatch.setattr(storage, 'QUERY_LIMIT', 3073)
        with pytest.raises(KernelError, match='query_limit'):
            tx.rows('SELECT body FROM facts WHERE rowid=2')
        assert (tx.rows_read, tx.bytes_read) == (1, 2)
        assert conversions == [2]


def test_row_rejection_precedes_python_body_conversion():
    with closing(sqlite3.connect(':memory:')) as connection:
        connection.execute('CREATE TABLE facts(body TEXT)')
        connection.executemany('INSERT INTO facts VALUES (?)', [('one',), ('two',)])

        def unexpected(body):
            raise AssertionError('Rejected body reached Python')

        connection.text_factory = unexpected
        tx = Transaction(connection)
        with pytest.raises(KernelError, match='query_limit'):
            tx.rows('SELECT body FROM facts ORDER BY rowid', limit=1)
        assert (tx.rows_read, tx.bytes_read) == (0, 0)


@pytest.mark.parametrize('counter,value', [('rows_read', 65537), ('bytes_read', QUERY_LIMIT + 1)])
def test_empty_read_does_not_bypass_an_exhausted_transaction_budget(counter, value):
    with closing(sqlite3.connect(':memory:')) as connection:
        tx = Transaction(connection)
        setattr(tx, counter, value)
        with pytest.raises(KernelError, match='query_limit'):
            tx.rows('SELECT 1 WHERE 0')
        assert getattr(tx, counter) == value


@pytest.mark.parametrize('byte_limit', [0, 1])
def test_invalid_text_is_rejected_by_budget_before_decoding_or_keeps_decode_error(monkeypatch, byte_limit):
    import agent_run.storage.sqlite as storage

    with closing(sqlite3.connect(':memory:')) as connection:
        connection.execute('CREATE TABLE facts(body TEXT)')
        connection.execute("INSERT INTO facts VALUES (CAST(X'ff' AS TEXT))")
        monkeypatch.setattr(storage, 'QUERY_LIMIT', byte_limit)
        tx = Transaction(connection)
        expected = KernelError if byte_limit == 0 else sqlite3.OperationalError
        with pytest.raises(expected, match='query_limit' if byte_limit == 0 else 'decode'):
            tx.rows('SELECT body FROM facts')
        assert (tx.rows_read, tx.bytes_read) == (0, 0)
