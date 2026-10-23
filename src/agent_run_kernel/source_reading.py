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

from agent_run.github import GhGitHubReader
from agent_run.git_errors import GitHubReadError
from agent_run.github_retry import run_read_command

from .storage import MATERIAL_LIMIT
from .effects import LocalGit
from agent_run.github_identity import validate_identity_snapshot

from .model import Checks, Requirements


def read_requirements(reader: GhGitHubReader, parent_number: int) -> Requirements:
    """Preserve original native Issue order, text, labels and blocker states.

    GitHubReadError propagates as an observation failure, never an empty graph.
    External blockers remain source facts and do not expand the Ticket Set.
    """
    if type(parent_number) is not int or parent_number < 1:
        raise ValueError('Parent Issue number must be a positive integer')
    validate_identity_snapshot(reader.runner_identity)
    graph = reader.delivery_graph(parent_number)
    return Requirements(graph.parent, tuple(graph.issues.values()))


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
                                cwd=root, runner_identity=identity)
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


def _read(reader: GhGitHubReader, arguments: list[str], *, allowed: frozenset[int] = frozenset({0})) -> subprocess.CompletedProcess[str]:
    identity = validate_identity_snapshot(reader.runner_identity)
    response = run_read_command(['gh', *arguments], cwd=reader.working_directory, runner_identity=identity)
    if response.returncode not in allowed:
        raise GitHubReadError('github_read_failed', response.stderr.strip() or 'GitHub observation failed')
    return response


def _json_response(response: subprocess.CompletedProcess[str]) -> Any:
    import json
    try:
        return json.loads(response.stdout)
    except json.JSONDecodeError as error:
        raise GitHubReadError('github_invalid_response', 'GitHub returned invalid JSON') from error


def read_pull_request_facts(reader: GhGitHubReader, pr_number: int) -> dict[str, Any]:
    """Keep original PR ownership, actor, narrative and head/base facts intact.

    A fork or deleted head repository remains distinguishable from the selected
    base repository. No authorization or default publisher identity is inferred.
    """
    if type(pr_number) is not int or pr_number < 1:
        raise ValueError('PR number must be a positive integer')
    repository = reader.repository().name_with_owner
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
                           required_contexts: set[str]) -> list[dict[str, Any]]:
    """Supplement real failures with exact completed jobs and available logs.

    No failure is preclassified as code-repairable. Missing supplementary logs
    remain explicit diagnostics; mismatched primary job evidence is not adopted.
    """
    import json
    evidence = []
    total_bytes = 0
    for raw in checks:
        check = dict(_object(raw))
        if (required_contexts and check['name'] not in required_contexts
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
                logs = _read(reader, ['api', endpoint + '/logs'], allowed=frozenset({0, 1, 403, 404}))
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

def read_required_check_facts(reader: GhGitHubReader, pr_number: int, *, expected_head_sha: str) -> dict[str, Any]:
    """Read complete required-check configuration and original check observations.

    This preserves classic protection, paginated rulesets and unreported contexts.
    Only an explicit classic-protection HTTP404 is absence; other failures raise.
    The exact PR head brackets these independent observations.
    """
    from urllib.parse import quote
    before = read_pull_request_facts(reader, pr_number)
    if before['head']['sha'] != expected_head_sha:
        raise GitHubReadError('change_pr_head_drift', 'Required checks refer to another PR head')
    metadata = reader.repository()
    repository = metadata.name_with_owner
    response = _read(reader, ['pr', 'checks', str(pr_number), '--repo', repository, '--required', '--json',
                             'name,bucket,state,link,workflow,description'], allowed=frozenset({0, 1, 8}))
    if response.returncode == 1 and 'no required checks reported' in response.stderr.lower():
        checks: list[Any] = []
    else:
        checks = _array(_json_response(response))
        if response.returncode == 1 and not checks:
            raise GitHubReadError('github_read_failed', response.stderr.strip() or 'Required checks could not be read')
    for check in checks:
        _text(_object(check).get('name'))
    pages = _array(_json_response(_read(reader, ['api', f'repos/{repository}/rulesets', '--paginate', '--slurp'])))
    import json
    total_bytes = len(json.dumps(before, ensure_ascii=False).encode()) + len(response.stdout.encode())
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
    protection = _read(reader, ['api', f'repos/{repository}/branches/{quote(branch, safe="")}/protection/required_status_checks/contexts'],
                       allowed=frozenset({0, 1, 404}))
    absent = protection.returncode == 404 or (protection.returncode != 0 and 'http 404' in protection.stderr.lower())
    if protection.returncode and not absent:
        raise GitHubReadError('github_read_failed', protection.stderr.strip() or 'Branch protection could not be read')
    classic = None if absent else _array(_json_response(protection))
    if classic is not None:
        for context in classic:
            _text(context)
    contexts = _configured_contexts({'classic_protection_absent': absent, 'classic_contexts': classic,
                                    'rulesets': rulesets, 'repository_default_branch': metadata.default_branch}, branch)
    checks = _failed_check_evidence(reader, repository, checks, expected_head_sha, set(contexts))
    after = read_pull_request_facts(reader, pr_number)
    if after['head']['sha'] != expected_head_sha:
        raise GitHubReadError('change_pr_head_drift', 'PR head changed during required-check observation')
    return {'pr_before': before, 'pr_after': after, 'head_sha': expected_head_sha,
            'required_checks': checks, 'checks_exit_code': response.returncode, 'checks_stderr': response.stderr,
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


def _configured_contexts(facts: Mapping[str, Any], branch: str) -> list[str]:
    contexts = [] if facts['classic_protection_absent'] else [_text(item) for item in _array(facts['classic_contexts'])]
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
                if check.get('integration_id') is not None:
                    raise GitHubReadError('github_unsupported_ruleset', 'Integration-bound check identity is not observable')
                contexts.append(_text(check.get('context')))
    if any(not context.strip() for context in contexts):
        raise GitHubReadError('github_invalid_response', 'Required check context is empty')
    return list(dict.fromkeys(contexts))


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
    observed = set()
    states = {'pass': 'pass', 'skipping': 'pass', 'neutral': 'pass', 'fail': 'fail', 'cancel': 'fail', 'pending': 'pending'}
    for check in checks:
        name = _text(check.get('name'))
        if contexts and name not in contexts:
            continue
        observed.add(name)
        details.append((name, states.get(_text(check.get('bucket')).lower(), 'unknown')))
    details.extend((name, 'pending') for name in contexts if name not in observed)
    status = {state for _, state in details}
    result = ('fail' if 'fail' in status else 'pending' if 'pending' in status else 'unknown' if 'unknown' in status
              else 'pass' if details else 'unknown' if facts['checks_exit_code'] == 8 else 'none')
    evidence = json.dumps({'source': 'original GitHub check/configuration observation',
                           'job_logs': 'per_check' if any('job' in check for check in checks) else 'not_collected',
                           'observation': dict(facts)}, ensure_ascii=False, sort_keys=True)
    if len(evidence.encode()) > MATERIAL_LIMIT:
        raise GitHubReadError('github_read_limit', 'Required-check evidence exceeds the material limit')
    return Checks(after['number'], facts['head_sha'], result, tuple(details), evidence)
