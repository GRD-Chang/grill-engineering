"""Read native requirement facts without selecting work or interpreting revisions.

The supplied reader owns repository selection and frozen Runner transport. Its
GitHub pagination is reused intact; Kernel alone applies scope, graph/currentness,
readiness, Completion and authorization rules. No Run state is read or written.
"""
from __future__ import annotations

from pathlib import Path
import re
import subprocess
from typing import Any, Mapping

from agent_run.adapters.github import GhGitHubReader
from agent_run.errors import GitHubReadError
from agent_run.adapters.github_commands import read_response_error, run_read_command

from agent_run.storage.sqlite import MATERIAL_LIMIT
from agent_run.adapters.git_objects import LocalGit
from agent_run.contracts.identity import validate_identity_snapshot

from agent_run.model import Checks, Requirements


def read_requirements(reader: GhGitHubReader, parent_number: int) -> Requirements:
    """Preserve original native Issue order, text, labels and blocker states.

    GitHubReadError propagates as an observation failure, never an empty graph.
    External blockers remain source facts and do not expand the Ticket Set.
    """
    if type(parent_number) is not int or parent_number < 1:
        raise ValueError('Parent Issue number must be a positive integer')
    validate_identity_snapshot(reader.runner_identity)
    return reader.requirements(parent_number)


def read_remote_heads(root: Path, source_ref: str, target_ref: str, *,
                      runner_identity: Mapping[str, Any], remote: str = "origin") -> tuple[str | None, str | None]:
    """Read exact remote refs in one response; never use local tracking refs.

    Runner identity must be the explicit frozen reference. A successful empty
    ref read is None; Kernel decides whether that absence permits any operation.
    Unknown observations fail rather than being converted into an absent ref.
    This neither fetches objects nor modifies local refs or Git configuration.
    """
    if source_ref == target_ref or not remote or "\0" in remote:
        raise ValueError("Source, target and remote identities must be distinct and valid")
    identity = validate_identity_snapshot(runner_identity)
    git = LocalGit(root)
    for ref in (source_ref, target_ref):
        if not ref.startswith("refs/heads/") or git.run("check-ref-format", ref, check=False).returncode:
            raise ValueError("Expected an exact refs/heads branch identity")
    response = run_read_command(["git", "ls-remote", "--refs", "--", remote, source_ref, target_ref],
                                cwd=root, runner_identity=identity, complete_stdout=True)
    if response.returncode:
        raise GitHubReadError("github_read_failed", response.stderr.strip() or "Remote refs could not be read")
    heads: dict[str, str | None] = {source_ref: None, target_ref: None}
    for line in response.stdout.splitlines():
        sha, separator, ref = line.partition("\t")
        if not separator or ref not in heads or heads[ref] is not None or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", sha):
            raise GitHubReadError("github_invalid_response", "Remote returned an invalid or unexpected ref")
        heads[ref] = sha
    return heads[source_ref], heads[target_ref]


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GitHubReadError('github_invalid_response', 'Expected a GitHub object')
    return value


def _array(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise GitHubReadError('github_invalid_response', 'Expected a GitHub array')
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise GitHubReadError('github_invalid_response', 'Expected GitHub text')
    return value


def _read(reader: GhGitHubReader, arguments: list[str], *, allowed: frozenset[int] = frozenset({0}),
          complete_stdout: bool = True) -> subprocess.CompletedProcess[str]:
    identity = validate_identity_snapshot(reader.runner_identity)
    response = run_read_command(['gh', *arguments], cwd=reader.working_directory, runner_identity=identity,
                                complete_stdout=complete_stdout)
    if response.returncode not in allowed:
        raise read_response_error(response)
    return response


def _json_response(response: subprocess.CompletedProcess[str]) -> Any:
    import json
    try:
        return json.loads(response.stdout)
    except json.JSONDecodeError as error:
        raise read_response_error(response, invalid_json=True) from error


def read_pull_request_facts(reader: GhGitHubReader, pr_number: int) -> dict[str, Any]:
    """Keep original PR ownership, actor, narrative and head/base facts intact.

    A fork or deleted head repository remains distinguishable from the selected
    base repository. No authorization or default publisher identity is inferred.
    """
    if type(pr_number) is not int or pr_number < 1:
        raise ValueError('PR number must be a positive integer')
    return _pull_request_facts(reader, reader.repository().name_with_owner, pr_number)


def _pull_request_facts(reader: GhGitHubReader, repository: str, pr_number: int) -> dict[str, Any]:
    data = _object(_json_response(_read(reader, ['api', f'repos/{repository}/pulls/{pr_number}'])))
    if type(data.get('number')) is not int or data['number'] != pr_number:
        raise GitHubReadError('github_invalid_response', 'PR observation has another identity')
    for key in ('title', 'state'):
        _text(data.get(key))
    if data.get('body') is not None:
        _text(data['body'])
    for side in ('head', 'base'):
        value = _object(data.get(side))
        _text(value.get('ref'))
        _text(value.get('sha'))
        if value.get('repo') is not None:
            _text(_object(value['repo']).get('full_name'))
    if data.get('user') is not None:
        _text(_object(data['user']).get('login'))
    return data



def _failed_check_evidence(reader: GhGitHubReader, repository: str, checks: list[Any], head_sha: str,
                           required_contexts: list[tuple[str, int | None]]) -> list[dict[str, Any]]:
    """Supplement real failures with exact completed jobs and available logs.

    No failure is preclassified as code-repairable. Missing supplementary logs
    remain explicit diagnostics; mismatched primary job evidence is not adopted.
    """
    import json
    evidence = []
    total_bytes = 0
    for raw in checks:
        check = dict(_object(raw))
        if (required_contexts and not _matching_requirements(check, required_contexts)
                or str(check.get('bucket', '')).lower() not in {'fail', 'cancel'}):
            evidence.append(check)
            continue
        link = check.get('link')
        match = re.fullmatch(rf'https://github\.com/{re.escape(repository)}/(?:actions/runs/\d+/job|runs/\d+/jobs)/(\d+)(?:[/?#].*)?', link) if isinstance(link, str) else None
        if match is not None:
            identity = int(match.group(1))
            endpoint = f'repos/{repository}/actions/jobs/{identity}'
            job = _object(_json_response(_read(reader, ['api', endpoint])))
            if type(job.get('id')) is not int or job['id'] != identity:
                raise GitHubReadError('github_invalid_response', 'Actions job response has another identity')
            if job.get('head_sha') != head_sha or job.get('status') != 'completed':
                raise GitHubReadError('github_check_evidence_observation_pending', 'Actions job is not completed at the expected failed head')
            check['job'] = job
            try:
                logs = _read(reader, ['api', endpoint + '/logs'], allowed=frozenset({0, 1, 403, 404}), complete_stdout=False)
                if logs.returncode == 0:
                    check['logs'] = logs.stdout[:65536]
                    check['logs_truncated'] = len(logs.stdout) > 65536
                else:
                    check['logs_unavailable'] = logs.stderr[:4096] or f'gh exited {logs.returncode}'
            except (GitHubReadError, OSError, TimeoutError) as error:
                check['logs_unavailable'] = str(error)[:4096]
        total_bytes += len(json.dumps(check, ensure_ascii=False).encode())
        if total_bytes > MATERIAL_LIMIT:
            raise GitHubReadError('github_read_limit', 'Failed check evidence exceeds the material limit')
        evidence.append(check)
    return evidence

def _check_collection(reader: GhGitHubReader, endpoint: str, field: str, head: str, *,
                      app: int | None = None, suites: Mapping[int, int] | None = None,
                      member_ids: set[int] | None = None) -> list[dict[str, Any]]:
    """Validate the entire bounded collection before any required-name filter."""
    rows: list[dict[str, Any]] = []
    identities = set() if member_ids is None else member_ids
    nodes: set[str] = set()
    contexts_seen: set[str] = set()
    count: int | None = None
    page_number, size = 1, 0
    while True:
        separator = '&' if '?' in endpoint else '?'
        response = _read(reader, ['api', endpoint + separator + 'page=' + str(page_number), '--include'])
        headers, separator_line, body = response.stdout.replace('\r\n', '\n').partition('\n\n')
        lines = headers.splitlines()
        if not separator_line or not lines or not re.fullmatch(r'HTTP/[0-9.]+ 200(?: .*)?', lines[0]):
            raise GitHubReadError('github_check_currentness_unproven', 'Latest-check pagination headers are unavailable')
        links = []
        for line in lines[1:]:
            key, colon, value = line.partition(':')
            if not colon:
                raise GitHubReadError('github_check_currentness_unproven', 'Latest-check pagination headers are malformed')
            if key.lower() == 'link':
                links.extend(value.strip().split(','))
        relations = []
        for link in links:
            relation = re.fullmatch(r'\s*<[^>]+>;\s*rel="([a-z]+)"\s*', link)
            if relation is None:
                raise GitHubReadError('github_check_currentness_unproven', 'Latest-check pagination link is malformed')
            relations.append(relation[1])
        if len(relations) != len(set(relations)):
            raise GitHubReadError('github_check_currentness_unproven', 'Latest-check pagination links are ambiguous')
        has_next = 'next' in relations
        page = _object(_json_response(subprocess.CompletedProcess(response.args, 0, body, response.stderr)))
        size += len(response.stdout.encode())
        if size > MATERIAL_LIMIT:
            raise GitHubReadError('github_read_limit', 'Latest-check collection exceeds the material limit')
        observed = page.get('total_count')
        if type(observed) is not int or observed < 0 or count is not None and observed != count:
            raise GitHubReadError('github_check_currentness_unproven', 'Latest-check count is missing or changed')
        count = observed
        if field == 'check_suites' and not 0 < count <= 1000:
            raise GitHubReadError('github_check_currentness_unproven', 'Latest-check coverage cannot be proven within 1000 suites')
        if field == 'statuses' and page.get('sha') != head:
            raise GitHubReadError('github_check_currentness_unproven', 'Latest status collection has another head')
        chunk = _array(page.get(field))
        for value in chunk:
            row = _object(value)
            identity = row.get('id')
            if type(identity) is not int or identity <= 0 or identity in identities:
                raise GitHubReadError('github_check_currentness_unproven', 'Latest-check collection has missing or duplicate member IDs')
            identities.add(identity)
            if field == 'statuses':
                node = row.get('node_id')
                if not isinstance(node, str) or not node.strip() or node in nodes:
                    raise GitHubReadError('github_check_currentness_unproven', 'Latest status collection has missing or duplicate node IDs')
                nodes.add(node)
                name = row.get('context')
            else:
                provider = _object(row.get('app')).get('id')
                if (row.get('head_sha') != head or type(provider) is not int or provider <= 0
                        or app is not None and provider != app):
                    raise GitHubReadError('github_check_currentness_unproven', 'Latest-check member has another head or App')
                name = row.get('name') if field == 'check_runs' else None
                if field == 'check_runs':
                    suite = _object(row.get('check_suite')).get('id')
                    if type(suite) is not int or suites is None or suites.get(suite) != provider:
                        raise GitHubReadError('github_check_currentness_unproven', 'Latest check is outside the complete suite scope')
            if field != 'check_suites' and (not isinstance(name, str) or not name.strip()):
                raise GitHubReadError('github_check_currentness_unproven', 'Latest-check context is missing')
            if field == 'statuses':
                if name in contexts_seen:
                    raise GitHubReadError('github_check_currentness_unproven', 'Latest status collection has duplicate current contexts')
                contexts_seen.add(_text(name))
            rows.append(row)
        if (len(rows) > count or has_next and (not chunk or len(rows) >= count)
                or not has_next and len(rows) != count):
            raise GitHubReadError('github_check_currentness_unproven', 'Latest-check pagination is incomplete or its count changed')
        if not has_next:
            return rows
        page_number += 1


def _latest_checks(reader: GhGitHubReader, repository: str, head: str,
                   checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter rollup members using server latest views, never client ID/time order.

    The ref check-runs API only covers 1000 suites. Verify the complete bounded
    suite identity set before and after; refuse larger/unproven collections.
    """
    from urllib.parse import urlencode
    import json
    prefix = f'repos/{repository}/commits/{head}'
    runs = [row for row in checks if row['kind'] == 'CheckRun']
    result: list[dict[str, Any]] = []

    def suite_scope() -> dict[int, int]:
        rows = _check_collection(reader, prefix + '/check-suites?per_page=100', 'check_suites', head)
        return {row['id']: row['app']['id'] for row in rows}

    before = suite_scope() if runs else {}
    latest_run_ids: set[int] = set()
    for app in sorted({row['app_id'] for row in runs}):
        group = [row for row in runs if row['app_id'] == app]
        by_id = {}
        for row in group:
            original = row['original']
            run_id, suite_id = original.get('databaseId'), original['checkSuite'].get('databaseId')
            if type(run_id) is not int or run_id <= 0 or type(suite_id) is not int or suite_id <= 0:
                raise GitHubReadError('github_invalid_response', 'Required check has no exact REST run/suite identity')
            if run_id in by_id:
                raise GitHubReadError('github_check_currentness_unproven', 'Rollup has conflicting run identities')
            by_id[run_id] = row
        endpoint = prefix + '/check-runs?' + urlencode({'filter': 'latest', 'app_id': app, 'per_page': 100})
        latest = _check_collection(reader, endpoint, 'check_runs', head, app=app, suites=before, member_ids=latest_run_ids)
        names: set[str] = set()
        matched: set[str] = set()
        for run in latest:
            if run.get('head_sha') != head or _object(run.get('app')).get('id') != app:
                raise GitHubReadError('github_invalid_response', 'Latest check has another App or head')
            name = _text(run.get('name'))
            if name not in {row['name'] for row in group}:
                continue
            if name in names:
                raise GitHubReadError('github_check_currentness_unproven', 'Server latest view has ambiguous same-App contexts')
            names.add(name)
            run_id = run.get('id')
            original = by_id.get(run_id) if type(run_id) is int else None
            if (original is None or original['name'] != name
                    or _object(run.get('check_suite')).get('id') != original['original']['checkSuite']['databaseId']):
                raise GitHubReadError('github_check_currentness_unproven', 'Latest check is not an exactly observed required member')
            state, bucket = _check_state(_text(run.get('status')).upper(), run.get('conclusion'))
            result.append({**original, 'state': state, 'bucket': bucket, 'latest': run})
            matched.add(name)
        if len(json.dumps(result).encode()) > MATERIAL_LIMIT:
            raise GitHubReadError('github_read_limit', 'Latest-check evidence exceeds the material limit')
        if matched != {row['name'] for row in group}:
            raise GitHubReadError('github_check_currentness_unproven', 'Latest view is missing an observed required context')
    if runs and suite_scope() != before:
        raise GitHubReadError('github_check_currentness_unproven', 'Check suites changed during observation')
    statuses = [row for row in checks if row['kind'] == 'StatusContext']
    if statuses:
        by_node = {row['original']['id']: row for row in statuses}
        latest = _check_collection(reader, prefix + '/status?per_page=100', 'statuses', head)
        seen: set[str] = set()
        for status in latest:
            name = _text(status.get('context'))
            if name not in {row['name'] for row in statuses}:
                continue
            if name in seen:
                raise GitHubReadError('github_check_currentness_unproven', 'Combined status has duplicate current contexts')
            original = by_node.get(status.get('node_id'))
            if original is None or original['name'] != name:
                raise GitHubReadError('github_check_currentness_unproven', 'Latest status is not an observed required member')
            state = _text(status.get('state')).upper()
            bucket = {'SUCCESS': 'pass', 'FAILURE': 'fail', 'ERROR': 'fail', 'PENDING': 'pending'}.get(state, 'unknown')
            result.append({**original, 'state': state, 'bucket': bucket, 'latest': status})
            seen.add(name)
        if seen != {row['name'] for row in statuses}:
            raise GitHubReadError('github_check_currentness_unproven', 'Latest status collection is incomplete')
    if len(json.dumps(result).encode()) > MATERIAL_LIMIT:
        raise GitHubReadError('github_read_limit', 'Latest-check evidence exceeds the material limit')
    # Preserve rollup presentation order only after server-side selection.
    selected = {row['original']['id']: row for row in result}
    return [selected[row['original']['id']] for row in checks if row['original']['id'] in selected]


def _check_state(status: str, conclusion: Any) -> tuple[str, str]:
    state = str(conclusion).upper() if status == 'COMPLETED' else status
    bucket = {'SUCCESS': 'pass', 'NEUTRAL': 'pass', 'SKIPPED': 'pass',
              'FAILURE': 'fail', 'TIMED_OUT': 'fail', 'CANCELLED': 'fail',
              'ACTION_REQUIRED': 'fail', 'STARTUP_FAILURE': 'fail',
              'QUEUED': 'pending', 'IN_PROGRESS': 'pending', 'WAITING': 'pending',
              'REQUESTED': 'pending', 'PENDING': 'pending'}.get(state, 'unknown')
    return state, bucket


def _rollup_checks(reader: GhGitHubReader, repository: str, number: int, head: str) -> list[dict[str, Any]]:
    # Rollup supplies required applicability and identity, not rerun ordering.
    # The server latest endpoints below determine current members.
    query = '''query CurrentRequiredChecks($owner:String!,$repo:String!,$number:Int!,$cursor:String) {
      repository(owner:$owner,name:$repo) { pullRequest(number:$number) {
        number headRefOid commits(last:1) { nodes { commit { oid statusCheckRollup {
          contexts(first:100,after:$cursor) { nodes { __typename
            ... on CheckRun { id databaseId name status conclusion detailsUrl title
              isRequired(pullRequestNumber:$number)
              checkSuite { id databaseId commit { oid } app { databaseId }
                workflowRun { workflow { name } } } }
            ... on StatusContext { id context state targetUrl description
              isRequired(pullRequestNumber:$number) }
          } pageInfo { hasNextPage endCursor } }
        } } } }
      } }
    }'''
    owner, repo = repository.split('/')
    cursor: str | None = None
    cursors: set[str] = set()
    identities: set[str] = set()
    checks: list[dict[str, Any]] = []
    total = 0
    import json
    while True:
        args = ['api', 'graphql', '-f', 'query=' + query, '-f', 'owner=' + owner,
                '-f', 'repo=' + repo, '-F', 'number=' + str(number)]
        if cursor is not None:
            args += ['-f', 'cursor=' + cursor]
        loaded = _object(_json_response(_read(reader, args)))
        total += len(json.dumps(loaded).encode())
        if total > MATERIAL_LIMIT:
            raise GitHubReadError('github_read_limit', 'Required-check rollup exceeds the material limit')
        if loaded.get('errors'):
            raise GitHubReadError('github_read_failed', 'Required-check rollup is incomplete: '
                                      + json.dumps(loaded['errors'], ensure_ascii=False)[:4096])
        pr = _object(_object(_object(loaded.get('data')).get('repository')).get('pullRequest'))
        if pr.get('number') != number or pr.get('headRefOid') != head:
            raise GitHubReadError('change_pr_head_drift', 'Required-check rollup refers to another PR head')
        commits = _array(_object(pr.get('commits')).get('nodes'))
        if len(commits) != 1:
            raise GitHubReadError('github_invalid_response', 'Required-check rollup has no exact commit')
        commit = _object(_object(commits[0]).get('commit'))
        if commit.get('oid') != head:
            raise GitHubReadError('change_pr_head_drift', 'Required-check rollup commit changed')
        if 'statusCheckRollup' not in commit:
            raise GitHubReadError('github_invalid_response', 'Required-check rollup field is missing')
        rollup = commit['statusCheckRollup']
        if rollup is None:
            if cursor is not None:
                raise GitHubReadError('github_read_failed', 'Required-check rollup disappeared during pagination')
            return []
        connection = _object(_object(rollup).get('contexts'))
        for value in _array(connection.get('nodes')):
            node = _object(value)
            if type(node.get('isRequired')) is not bool:
                raise GitHubReadError('github_invalid_response', 'Required-check applicability is missing')
            if not node['isRequired']:
                continue
            node_id = _text(node.get('id'))
            if not node_id or node_id in identities:
                raise GitHubReadError('github_read_failed', 'Required-check rollup has ambiguous members')
            identities.add(node_id)
            kind = node.get('__typename')
            if kind == 'CheckRun':
                suite = _object(node.get('checkSuite'))
                app = _object(suite.get('app')).get('databaseId')
                if type(app) is not int or app <= 0 or not isinstance(suite.get('id'), str) or not suite['id']:
                    raise GitHubReadError('github_invalid_response', 'Required check has no exact App/suite identity')
                if _object(suite.get('commit')).get('oid') != head:
                    raise GitHubReadError('change_pr_head_drift', 'Required check suite refers to another head')
                status = _text(node.get('status'))
                state, bucket = _check_state(status, node.get('conclusion'))
                workflow_run = suite.get('workflowRun')
                workflow = '' if workflow_run is None else _text(_object(_object(workflow_run).get('workflow')).get('name'))
                name, link = node.get('name'), node.get('detailsUrl')
            elif kind == 'StatusContext':
                app, workflow = None, ''
                name, state, link = node.get('context'), _text(node.get('state')), node.get('targetUrl')
                bucket = {'SUCCESS': 'pass', 'FAILURE': 'fail', 'ERROR': 'fail', 'PENDING': 'pending', 'EXPECTED': 'pending'}.get(str(state), 'unknown')
            else:
                raise GitHubReadError('github_invalid_response', 'Unknown required-check member type')
            name = _text(name)
            if not name.strip():
                raise GitHubReadError('github_invalid_response', 'Required check has an empty context')
            checks.append({'name': name, 'bucket': bucket, 'state': state, 'link': link,
                           'workflow': workflow, 'description': node.get('description', node.get('title', '')),
                           'kind': kind, 'app_id': app, 'head_sha': head, 'original': node})
        page = _object(connection.get('pageInfo'))
        if type(page.get('hasNextPage')) is not bool:
            raise GitHubReadError('github_invalid_response', 'Required-check pagination is missing')
        if not page['hasNextPage']:
            return checks
        next_cursor = page.get('endCursor')
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor in cursors:
            raise GitHubReadError('github_read_failed', 'Required-check pagination did not advance')
        cursors.add(next_cursor)
        cursor = next_cursor


def read_required_check_facts(reader: GhGitHubReader, pr_number: int, *, expected_head_sha: str) -> dict[str, Any]:
    """Read complete required-check configuration and original check observations.

    This preserves classic protection, paginated rulesets and unreported contexts.
    Only an explicit classic-protection HTTP404 is absence; other failures raise.
    The exact PR head brackets these independent observations.
    """
    from urllib.parse import quote
    if type(pr_number) is not int or pr_number < 1:
        raise ValueError('PR number must be a positive integer')
    # Resolve once for this observation, never across calls. PR head reads
    # still bracket the complete checks/configuration observation below.
    metadata = reader.repository()
    repository = metadata.name_with_owner
    before = _pull_request_facts(reader, repository, pr_number)
    if before['head']['sha'] != expected_head_sha:
        raise GitHubReadError('change_pr_head_drift', 'Required checks refer to another PR head')
    checks = _latest_checks(reader, repository, expected_head_sha,
                            _rollup_checks(reader, repository, pr_number, expected_head_sha))
    pages = _array(_json_response(_read(reader, ['api', f'repos/{repository}/rulesets', '--paginate', '--slurp'])))
    import json
    total_bytes = len(json.dumps(before, ensure_ascii=False).encode()) + len(json.dumps(checks).encode())
    rulesets = []
    for page in pages:
        for item in _array(page):
            summary = _object(item)
            identity = summary.get('id')
            if type(identity) is not int or identity < 1:
                raise GitHubReadError('github_invalid_response', 'Ruleset identity is invalid')
            detail = _object(_json_response(_read(reader, ['api', f'repos/{repository}/rulesets/{identity}'])))
            if type(detail.get('id')) is not int or detail['id'] != identity:
                raise GitHubReadError('github_invalid_response', 'Ruleset observation has another identity')
            total_bytes += len(json.dumps(detail, ensure_ascii=False).encode())
            if total_bytes > MATERIAL_LIMIT:
                raise GitHubReadError('github_read_limit', 'Required-check source evidence exceeds the material limit')
            rulesets.append({'summary': summary, 'detail': detail})
    branch = _text(before['base']['ref'])
    protection = _read(reader, ['api', f'repos/{repository}/branches/{quote(branch, safe="")}/protection/required_status_checks'],
                       allowed=frozenset({0, 1, 404}))
    absent = protection.returncode == 404 or (protection.returncode != 0 and 'http 404' in protection.stderr.lower())
    if protection.returncode and not absent:
        raise read_response_error(protection)
    classic_config = None if absent else _object(_json_response(protection))
    classic = None if classic_config is None else _array(classic_config.get('contexts'))
    classic_checks = [] if classic_config is None else _array(classic_config.get('checks'))
    if classic is not None:
        for context in classic:
            _text(context)
    contexts = _configured_contexts({'classic_protection_absent': absent, 'classic_contexts': classic, 'classic_checks': classic_checks,
                                    'rulesets': rulesets, 'repository_default_branch': metadata.default_branch}, branch)
    checks = _failed_check_evidence(reader, repository, checks, expected_head_sha, contexts)
    after = _pull_request_facts(reader, repository, pr_number)
    if after['head']['sha'] != expected_head_sha:
        raise GitHubReadError('change_pr_head_drift', 'PR head changed during required-check observation')
    return {'pr_before': before, 'pr_after': after, 'head_sha': expected_head_sha,
            'required_checks': checks, 'classic_checks': classic_checks,
            'repository': repository, 'repository_default_branch': metadata.default_branch, 'rulesets': rulesets, 'classic_contexts': classic, 'classic_protection_absent': absent}


def _matches_ref(reference: str, pattern: Any, default_branch: str) -> bool:
    # Same GitHub glob interpretation as the original mechanical reader:
    # one star cannot cross '/', two stars can; unsupported syntax is explicit.
    pattern = _text(pattern)
    if pattern == "~DEFAULT_BRANCH":
        if not default_branch:
            raise GitHubReadError("github_invalid_response", "Default branch is not recorded")
        return reference == "refs/heads/" + default_branch
    if pattern == "~ALL":
        return True
    if pattern.startswith("~"):
        raise GitHubReadError("github_unsupported_ruleset", "Unsupported ruleset ref selector")
    expression = ""
    position = 0
    while position < len(pattern):
        character = pattern[position]
        if character == "*":
            if position + 1 < len(pattern) and pattern[position + 1] == "*":
                expression += ".*"
                position += 2
                continue
            expression += "[^/]*"
        elif character == "?":
            expression += "[^/]"
        elif character == "[":
            raise GitHubReadError("github_unsupported_ruleset", "Unsupported ruleset ref character class")
        else:
            expression += re.escape(character)
        position += 1
    return re.fullmatch(expression, reference) is not None


def _check_app(value: Any, *, any_app: int | None = None) -> int | None:
    if value is None or type(value) is int and value == any_app:
        return None
    if type(value) is not int or value <= 0:
        raise GitHubReadError('github_invalid_response', 'Invalid required-check App identity')
    return value


def _configured_contexts(facts: Mapping[str, Any], branch: str) -> list[tuple[str, int | None]]:
    contexts: list[tuple[str, int | None]] = []
    if not facts['classic_protection_absent']:
        for item in _array(facts['classic_checks']):
            entry = _object(item)
            if 'app_id' not in entry:
                raise GitHubReadError('github_invalid_response', 'Classic required check is missing app_id')
            contexts.append((_text(entry.get('context')), _check_app(entry.get('app_id'), any_app=-1)))
        represented = {name for name, _app in contexts}
        if any(_text(name) not in represented for name in _array(facts['classic_contexts'])):
            raise GitHubReadError('github_invalid_response', 'Classic required context has no explicit App binding')
    for item in _array(facts['rulesets']):
        entry = _object(item)
        summary, detail = _object(entry['summary']), _object(entry['detail'])
        if summary.get('target') != 'branch' or summary.get('enforcement') != 'active':
            continue
        conditions = detail.get('conditions')
        if conditions is not None:
            selector = _object(conditions).get('ref_name')
            if selector is not None:
                ref_names = _object(selector)
                include, exclude = _array(ref_names.get('include', [])), _array(ref_names.get('exclude', []))
                reference = 'refs/heads/' + branch
                if (include and not any(_matches_ref(reference, pattern, _text(facts["repository_default_branch"])) for pattern in include)
                        or any(_matches_ref(reference, pattern, _text(facts["repository_default_branch"])) for pattern in exclude)):
                    continue
        for rule in _array(detail.get('rules')):
            rule = _object(rule)
            if rule.get('type') != 'required_status_checks':
                continue
            parameters = _object(rule.get('parameters'))
            for check in _array(parameters.get('required_status_checks')):
                check = _object(check)
                contexts.append((_text(check.get('context')), _check_app(check.get('integration_id'))))
    if any(not context.strip() for context, _app in contexts):
        raise GitHubReadError('github_invalid_response', 'Required check context is empty')
    return list(dict.fromkeys(contexts))


def _matching_requirements(check: Mapping[str, Any], contexts: list[tuple[str, int | None]]) -> list[tuple[str, int | None]]:
    return [(name, app) for name, app in contexts if name == check.get('name')
            and (app is None or check.get('kind') == 'CheckRun' and check.get('app_id') == app)]


def normalize_required_checks(facts: Mapping[str, Any]) -> Checks:
    """Interpret complete GitHub check facts, without selecting any successor.

    Unreported configured checks are pending; unrecognized server states stay
    unknown. A 404 classic-protection response
    removes only classic protection, never rulesets. Errors stay errors. The
    Original API facts and available exact job logs are retained as diagnostic
    evidence. Supplementary log failures stay explicit, never fabricated empty.
    """
    import json
    before, after = _object(facts['pr_before']), _object(facts['pr_after'])
    def identity(pr: dict[str, Any]) -> tuple[Any, ...]:
        return (pr['number'], pr['head']['sha'], _object(pr['head']['repo'])['full_name'], pr['head']['ref'],
                _object(pr['base']['repo'])['full_name'], pr['base']['ref'])
    if (identity(before) != identity(after) or after['head']['sha'] != facts['head_sha']
            or after['base']['repo']['full_name'].lower() != _text(facts['repository']).lower()):
        raise GitHubReadError('change_pr_identity_drift', 'Required-check observation changed PR identity')
    contexts = _configured_contexts(facts, _text(after['base']['ref']))
    checks = [_object(value) for value in _array(facts['required_checks'])]
    details = []
    observed: set[tuple[str, int | None]] = set()
    states = {'pass': 'pass', 'skipping': 'pass', 'neutral': 'pass', 'fail': 'fail', 'cancel': 'fail', 'pending': 'pending'}
    for check in checks:
        name = _text(check.get('name'))
        matches = _matching_requirements(check, contexts)
        if contexts and not matches:
            continue
        observed.update(matches)
        state = states.get(_text(check.get('bucket')).lower(), 'unknown')
        details.append((name, state))
    details.extend((name, 'pending') for name, app in contexts if (name, app) not in observed)
    status = {state for _, state in details}
    result = ('fail' if 'fail' in status else 'pending' if 'pending' in status else 'unknown' if 'unknown' in status
              else 'pass' if details else 'none')
    evidence = json.dumps({'source': 'original GitHub check/configuration observation',
                           'job_logs': 'per_check' if any('job' in check for check in checks) else 'not_collected',
                           'observation': dict(facts)}, ensure_ascii=False, sort_keys=True)
    if len(evidence.encode()) > MATERIAL_LIMIT:
        raise GitHubReadError('github_read_limit', 'Required-check evidence exceeds the material limit')
    return Checks(after['number'], facts['head_sha'], result, tuple(details), evidence)
