"""Synthetic GitHub transport over raw external records and private Git."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import json
import re
from pathlib import Path
import sqlite3
import subprocess
from typing import Any
from urllib.parse import parse_qs, urlsplit

from agent_run.errors import GitHubReadError
from agent_run.adapters.git_objects import LocalGit
from .offline_git import OfflineGit


class ExternalObjects:
    def __init__(self, database: Path) -> None:
        self.database = database
        with self._connect() as db:
            present = db.execute("SELECT name FROM sqlite_master WHERE name='external_format'").fetchone()
            versions = db.execute('SELECT version FROM external_format').fetchall() if present else []
        if versions != [(2,)]:
            raise GitHubReadError('offline_format_unsupported', 'Only the current raw API world format is supported')

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database.as_uri() + '?mode=ro', uri=True)

    @staticmethod
    def create(db: sqlite3.Connection) -> None:
        db.executescript('''
            CREATE TABLE external_format(version INTEGER NOT NULL,repository TEXT);
            INSERT INTO external_format VALUES (2,NULL);
            CREATE TABLE prs(number INTEGER PRIMARY KEY,body TEXT NOT NULL);
            CREATE TABLE issues(number INTEGER PRIMARY KEY,state TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
            CREATE TABLE comments(id INTEGER PRIMARY KEY AUTOINCREMENT,issue INTEGER NOT NULL,
                actor TEXT NOT NULL,body TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
            CREATE TABLE issue_events(id INTEGER PRIMARY KEY AUTOINCREMENT,issue INTEGER NOT NULL,
                event TEXT NOT NULL,actor TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
            CREATE TABLE api_requests(position INTEGER PRIMARY KEY,method TEXT,endpoint TEXT,body TEXT,response TEXT);
        ''')

    @property
    def repository(self) -> str:
        with self._connect() as db:
            row = db.execute('SELECT repository FROM external_format').fetchone()
        value = row[0] if row else None
        if not isinstance(value, str):
            raise GitHubReadError('offline_repository_unbound', 'The saved world has no repository identity')
        return value

    def bind(self, repository: str, issues: Mapping[int, str]) -> None:
        with sqlite3.connect(self.database) as db:
            current = db.execute('SELECT repository FROM external_format').fetchone()[0]
            if current is not None and current != repository:
                raise GitHubReadError('offline_repository_changed', 'External world belongs to a different repository')
            db.execute('UPDATE external_format SET repository=?', (repository,))
            db.executemany('INSERT OR IGNORE INTO issues(number,state) VALUES (?,?)', issues.items())

    def pr(self, number: int) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute('SELECT body FROM prs WHERE number=?', (number,)).fetchone()
            if row is None:
                return None
            value = json.loads(row[0])
            if not isinstance(value, dict):
                raise GitHubReadError('offline_object_invalid', 'Expected an original PR object')
            return value

    def pulls(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            values = [json.loads(row[0]) for row in db.execute('SELECT body FROM prs ORDER BY number')]
        if not all(isinstance(value, dict) for value in values):
            raise GitHubReadError('offline_object_invalid', 'Expected an original PR object')
        return values

    def put_pr(self, value: Mapping[str, Any]) -> None:
        with sqlite3.connect(self.database) as db:
            db.execute('INSERT INTO prs VALUES (?,?) ON CONFLICT(number) DO UPDATE SET body=excluded.body',
                       (value['number'], json.dumps(dict(value))))

    def next_number(self) -> int:
        with self._connect() as db:
            return int(db.execute('SELECT coalesce(max(number),0)+1 FROM (SELECT number FROM prs UNION ALL SELECT number FROM issues)').fetchone()[0])

    def issue_state(self, number: int) -> str:
        with self._connect() as db:
            row = db.execute('SELECT state FROM issues WHERE number=?', (number,)).fetchone()
        return 'open' if row is None else row[0]

    def issue(self, number: int) -> dict[str, Any]:
        pr = self.pr(number)
        if pr is not None:
            state, updated_at = pr['state'], pr['updated_at']
        else:
            with self._connect() as db:
                row = db.execute('SELECT state,updated_at FROM issues WHERE number=?', (number,)).fetchone()
            if row is None:
                raise GitHubReadError('remote_read_failed', 'HTTP 404 Issue absent')
            state, updated_at = row
        return {'number': number, 'node_id': 'synthetic-issue-' + str(number),
                'state': state, 'updated_at': updated_at}

    def closed_issues(self) -> set[int]:
        with self._connect() as db:
            return {row[0] for row in db.execute("SELECT number FROM issues WHERE state='closed'")}

    def set_state(self, number: int, state: str, actor: str) -> None:
        prior = self.pr(number)
        before = prior['state'] if prior is not None else self.issue_state(number)
        if before == state:
            return
        updated_at = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.database) as db:
            if prior is not None:
                db.execute('UPDATE prs SET body=? WHERE number=?',
                           (json.dumps({**prior, 'state': state, 'updated_at': updated_at}), number))
            else:
                db.execute('INSERT INTO issues VALUES (?,?,?) ON CONFLICT(number) DO UPDATE SET '
                           'state=excluded.state,updated_at=excluded.updated_at', (number, state, updated_at))
            db.execute('INSERT INTO issue_events(issue,event,actor,created_at) VALUES (?,?,?,?)',
                       (number, 'closed' if state == 'closed' else 'reopened', actor, updated_at))

    def comments(self, number: int) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute('SELECT rowid,body,actor,created_at FROM comments WHERE issue=? ORDER BY rowid', (number,)).fetchall()
        return [{'id': n, 'body': body,
                 'user': {'login': actor}, 'created_at': at} for n, body, actor, at in rows]

    def post_comment(self, number: int, body: str, actor: str) -> int:
        with sqlite3.connect(self.database) as db:
            cursor = db.execute('INSERT INTO comments(issue,actor,body) VALUES (?,?,?)', (number, actor, body))
            assert cursor.lastrowid is not None
            return cursor.lastrowid

    def events(self, number: int) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute('SELECT id,event,actor,created_at FROM issue_events WHERE issue=? ORDER BY id', (number,)).fetchall()
        return [{'id': n, 'event': event, 'actor': {'login': actor}, 'created_at': at} for n, event, actor, at in rows]

    def snapshot(self) -> dict[str, Any]:
        """Lossless projection of actual records; never a write authority."""
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            return {'format_version': 2, **{table: [dict(row) for row in
                db.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]
                for table in ('prs', 'issues', 'comments', 'issue_events', 'api_requests')}}

    def completion_trace(self) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
        """Display recorded marker comments, never a receipt or write authority."""
        with self._connect() as db:
            issues = [row[0] for row in db.execute('SELECT DISTINCT issue FROM comments ORDER BY issue')]
        comments, closed = [], []
        closed_issues = self.closed_issues()
        for issue in sorted(set(issues) | closed_issues):
            summaries = []
            for comment in self.comments(issue):
                marker = re.search(r'<!-- agent-run-close-intent:(.*?) -->', comment['body'])
                if marker is None:
                    continue
                try:
                    payload = json.loads(marker[1])
                except (ValueError, TypeError):
                    continue
                if not isinstance(payload, dict) or 'merge_sha' not in payload:
                    continue
                intent = payload.get('effect_id', '')
                entry = (intent, issue, comment['user']['login'], json.dumps(payload).encode())
                comments.append(entry)
                summaries.append(entry)
            if issue in closed_issues:
                identities = {(entry[0], json.loads(entry[3])['merge_sha']) for entry in summaries}
                intent, merge = next(iter(identities)) if len(identities) == 1 else ('', None)
                closed.append((issue, intent, None, merge))
        return comments, closed


class SyntheticGitHub:
    def __init__(self, git: LocalGit, database: Path, repository: str, *,
                 physical: OfflineGit, written: Callable[[int, Mapping[str, Any], subprocess.CompletedProcess[str]], None], read_faults: set[str],
                 actor: str = 'offline-publisher') -> None:
        self.git, self.database, self.repository = git, database, repository
        self.objects = ExternalObjects(database)
        self.physical, self.remote = physical, physical.remote
        self.written, self.read_faults = written, read_faults
        self.actor = actor

    def _record(self, method: str, endpoint: str, body: Mapping[str, Any], response: Any) -> int:
        if method == 'GET':
            return 0  # Observation is read only, including inside write authorization.
        with sqlite3.connect(self.database) as db:
            cursor = db.execute('INSERT INTO api_requests(method,endpoint,body,response) VALUES (?,?,?,?)',
                       (method, endpoint, json.dumps(dict(body)), json.dumps(response)))
            assert cursor.lastrowid is not None
            return cursor.lastrowid

    def _read(self, endpoint: str) -> None:
        if '*' in self.read_faults or endpoint in self.read_faults:
            self._record('GET', endpoint, {}, {'error': 'read unavailable'})
            raise GitHubReadError('remote_read_failed', 'Synthetic physical API read unavailable')

    def _pr(self, number: int) -> dict[str, Any]:
        value = self.objects.pr(number)
        if value is None:
            raise GitHubReadError('remote_read_failed', 'HTTP 404 PR absent')
        return self._with_refs(value, self.physical.refs())

    @staticmethod
    def _with_refs(value: dict[str, Any], refs: Mapping[str, str]) -> dict[str, Any]:
        for field in ('head', 'base'):
            branch = value[field]
            branch['sha'] = refs.get('refs/heads/' + branch['ref'], branch['sha'])
        return value

    def object(self, endpoint: str) -> dict[str, Any]:
        self._read(endpoint)
        tail = endpoint.removeprefix('repos/' + self.repository + '/')
        if endpoint == 'user':
            result: dict[str, Any] = {'login': self.actor}
        elif tail.startswith('git/commits/'):
            result = self.physical.commit(tail.rsplit('/', 1)[1])
        elif tail.startswith('compare/'):
            a, b = tail.removeprefix('compare/').split('...')
            result = self.physical.compare(a, b)
        elif tail.startswith('pulls/'):
            result = self._pr(int(tail.split('/')[1]))
        elif tail.startswith('issues/'):
            result = self.objects.issue(int(tail.split('/')[1]))
        else:
            raise ValueError('Unsupported physical object: ' + endpoint)
        self._record('GET', endpoint, {}, result)
        return result

    def list(self, endpoint: str) -> list[dict[str, Any]]:
        self._read(endpoint); path = urlsplit(endpoint); tail = path.path.removeprefix('repos/' + self.repository + '/')
        if tail == 'pulls':
            query = parse_qs(path.query)
            result = [p for p in self.objects.pulls() if p['head']['ref'] == query['head'][0].split(':', 1)[-1]
                      and p['base']['ref'] == query['base'][0]
                      and p['base']['repo']['full_name'].casefold() == self.repository.casefold()]
            if result:
                refs = self.physical.refs()
                result = [self._with_refs(p, refs) for p in result]
        elif tail.endswith('/comments'):
            result = self.objects.comments(int(tail.split('/')[1]))
        elif tail.endswith('/events'):
            result = self.objects.events(int(tail.split('/')[1]))
        else:
            raise ValueError('Unsupported physical list: ' + endpoint)
        self._record('GET', endpoint, {}, result)
        return result

    def write(self, endpoint: str, method: str, body: Mapping[str, Any]) -> subprocess.CompletedProcess[str]:
        tail = endpoint.removeprefix('repos/' + self.repository + '/'); refs = self.physical.refs()
        actor = self.actor; response: dict[str, Any]
        if tail == 'pulls' and method == 'POST':
            number = self.objects.next_number()
            source, target = 'refs/heads/' + body['head'], 'refs/heads/' + body['base']
            self.objects.put_pr({'number': number, 'state': 'open', 'title': body['title'], 'body': body['body'],
                'updated_at': datetime.now(timezone.utc).isoformat(),
                'user': {'login': actor}, 'head': {'ref': body['head'], 'sha': refs[source], 'repo': {'full_name': self.repository}},
                'base': {'ref': body['base'], 'sha': refs[target], 'repo': {'full_name': self.repository}},
                'merged': False, 'merge_commit_sha': None, 'merged_by': None})
            response = {'number': number}
        elif tail.startswith('pulls/'):
            number = int(tail.split('/')[1]); pr = self._pr(number)
            if tail.endswith('/merge'):
                if pr['merged'] or pr['state'] != 'open' or body['sha'] != pr['head']['sha']:
                    return self._complete(endpoint, method, body, {'message': 'head changed'},
                                          returncode=1, diagnostic='HTTP 409 head changed')
                merged = self.physical.merge('refs/heads/' + pr['base']['ref'], 'refs/heads/' + pr['head']['ref'],
                    sha=body['sha'], merge_method=body['merge_method'],
                    commit_title=body['commit_title'], commit_message=body['commit_message'])
                self.physical.fetch(merged)
                pr.update(merged=True, merge_commit_sha=merged, merged_by={'login': actor})
                response = {'merged': True, 'sha': merged}
            else:
                pr.update({key: body[key] for key in ('title', 'body') if key in body})
                if any(key in body for key in ('title', 'body')):
                    pr['updated_at'] = datetime.now(timezone.utc).isoformat()
                response = {'number': number}
            self.objects.put_pr(pr)
            if tail.endswith('/merge') or 'state' in body:
                self.objects.set_state(number, 'closed' if tail.endswith('/merge') else body['state'], actor)
        elif tail.endswith('/comments'):
            response = {'id': self.objects.post_comment(int(tail.split('/')[1]), body['body'], actor)}
        elif tail.startswith('issues/') and method == 'PATCH':
            self.objects.set_state(int(tail.split('/')[1]), body['state'], actor)
            response = {'state': body['state']}
        else:
            raise ValueError('Unsupported physical write: ' + method + ' ' + endpoint)
        return self._complete(endpoint, method, body, response)

    def record_git_write(self, operation: str, body: Mapping[str, Any], result: subprocess.CompletedProcess[str]) -> None:
        position = self._record('GIT', operation, body,
                                {'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr})
        self.written(position, {}, result)

    def _complete(self, endpoint: str, method: str, body: Mapping[str, Any], response: Mapping[str, Any], *,
                  returncode: int = 0, diagnostic: str = '') -> subprocess.CompletedProcess[str]:
        position = self._record(method, endpoint, body, response)
        result = subprocess.CompletedProcess(['synthetic-api', method, endpoint], returncode, json.dumps(dict(response)), diagnostic)
        self.written(position, response, result)
        return result
