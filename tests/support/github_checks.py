"""Current repository and check responses for isolated gh executables."""
from collections.abc import Mapping, Sequence
import json
import subprocess
from urllib.parse import quote


def repository_response(arguments: Sequence[str], *, repository: str,
                        default_branch: str, refs: Mapping[str, str],
                        errors: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Route only repository metadata and exact refs; absence is never a tip."""
    command = list(arguments)
    if command[:2] == ["repo", "view"]:
        if len(command) > 2 and command[2] not in {repository, "--json"}:
            return subprocess.CompletedProcess(command, 1, "", "HTTP 404 repository absent")
        data = {"nameWithOwner": repository, "defaultBranchRef": {"name": default_branch}}
    elif len(command) == 2 and command[0] == "api":
        endpoint = command[1]
        if errors and endpoint in errors:
            return subprocess.CompletedProcess(command, 1, "", errors[endpoint])
        matches = [ref for ref in refs if ref.startswith("refs/heads/")
                   and endpoint == f"repos/{repository}/git/ref/heads/{quote(ref.removeprefix('refs/heads/'), safe='')}"]
        if not matches:
            return subprocess.CompletedProcess(command, 1, "", "HTTP 404 ref absent")
        ref = matches[0]
        data = {"ref": ref, "object": {"type": "commit", "sha": refs[ref]}}
    else:
        raise AssertionError(f"Unexpected repository fixture command: {command!r}")
    return subprocess.CompletedProcess(command, 0, json.dumps(data), "")


def check_response(args, data, log):
    """Serve one CLI response from external PR, rule, job and check facts."""
    from io import StringIO
    from pathlib import Path
    from support.check_rollup import current_checks, print_latest_response

    stdout, stderr = StringIO(), StringIO()
    code = 0
    endpoint = args[1]
    if args[:2] == ['repo', 'view'] or (args[0] == 'api' and '/git/ref/heads/' in endpoint):
        return repository_response(args, repository=data['repository'], default_branch=data['default_branch'],
                                   refs=data['refs'], errors=data.get('ref_errors'))
    try:
        if args[:2] == ['api', 'graphql']:
            fields = dict(value.split('=', 1) for index, value in enumerate(args) if index and args[index-1] in ('-F', '-f'))
            assert 'CurrentRequiredChecks' in fields['query']
            print(json.dumps(current_checks(data, fields, stderr=stderr)), file=stdout)
        elif args[0] == 'api' and '/commits/' in endpoint:
            assert endpoint.split('?')[0].endswith(('/check-runs', '/check-suites', '/status'))
            print_latest_response(data, args, stdout=stdout, stderr=stderr)
        elif args[:2] == ['api', 'repos/example/project/pulls/17']:
            pr = data['pull_request']
            if 'pr_head_after' in data:
                reads = sum(json.loads(line)[:2] == args[:2] for line in Path(log).read_text().splitlines())
                if reads > 1:
                    pr['head']['sha'] = data['pr_head_after']
            print(json.dumps(pr), file=stdout)
        elif args[0] == 'api' and endpoint.startswith('repos/example/project/actions/jobs/'):
            if endpoint.endswith('/logs'):
                if data.get('logs_http'):
                    return subprocess.CompletedProcess(args, 1, '', 'HTTP ' + str(data['logs_http']))
                stdout.write(data['job_logs'])
            else:
                print(json.dumps(data['job']), file=stdout)
        elif args[:2] == ['pr', 'checks']:
            assert '--required' in args
            print(json.dumps(data['checks']), file=stdout)
        elif args[:2] == ['api', 'repos/example/project/rulesets']:
            assert '--paginate' in args and '--slurp' in args
            print(json.dumps([data['rulesets']]), file=stdout)
        elif args[0] == 'api' and endpoint.startswith('repos/example/project/rulesets/'):
            print(json.dumps(data['ruleset_detail']), file=stdout)
        else:
            classic = f"repos/example/project/branches/{quote(data['default_branch'], safe='')}/protection/required_status_checks"
            assert args[:2] in (['api', classic], ['api', classic + '/contexts']), args
            if data.get('classic_http'):
                return subprocess.CompletedProcess(args, 1, '', 'HTTP ' + str(data['classic_http']))
            checks = data.get('classic_checks', [{'context': context, 'app_id': None} for context in data['classic']])
            result = data['classic'] if endpoint.endswith('/contexts') else {'contexts': data['classic'], 'checks': checks}
            print(json.dumps(result), file=stdout)
    except SystemExit as error:
        code = error.code
    return subprocess.CompletedProcess(args, code, stdout.getvalue(), stderr.getvalue())
