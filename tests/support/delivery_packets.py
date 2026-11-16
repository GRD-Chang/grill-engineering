"""Finite GitHub response and ref facts for rejected delivery projections."""
import json
import subprocess
import time
from types import SimpleNamespace

from agent_run.adapters import github_commands, remote
from agent_run.core.operation_contract import DeliveryRead
from support.github_delivery import delivery_basis


def rejected_basis(tmp_path, monkeypatch, mutation, *, discover=False):
    repository, actor = 'example/project', 'publisher'
    port = remote.RemoteEffects(tmp_path, repository, runner_identity={'source': 'host'})
    source, target = 'a' * 40, 'b' * 40
    query = DeliveryRead('refs/heads/delivery', 'refs/heads/main', None if discover else 17, (), read_checks=False)
    pr = {'number': 17, 'title': 'Title', 'body': 'Body', 'user': {'login': actor},
          'state': 'open', 'merged': False,
          'head': {'ref': 'delivery', 'sha': source, 'repo': {'full_name': repository}},
          'base': {'ref': 'main', 'sha': target, 'repo': {'full_name': repository}}}
    calls = []

    def git(command, kind, sha):
        assert (command, kind) == ('cat-file', 'commit') and sha in (source, target)
        return 'tree ' + ('c' if sha == source else 'd') * 40

    def packet(arguments, **options):
        assert arguments[:3] == ['gh', 'api', 'graphql'] and '--method' not in arguments
        assert options['complete_stdout']
        fields = dict(value.split('=', 1) for i, value in enumerate(arguments)
                      if i and arguments[i - 1] in ('-F', '-f'))
        calls.append(arguments)
        body = delivery_basis({'prs': [pr]}, fields, git, repository=repository, actor=actor)
        mutation(body)
        return subprocess.CompletedProcess(arguments, 0, json.dumps(body), '')

    monkeypatch.setattr(remote, 'read_remote_heads', lambda *args, **kwargs: (source, target))
    monkeypatch.setattr(remote, 'time', SimpleNamespace(monotonic=time.monotonic, sleep=lambda _: None))
    monkeypatch.setattr(github_commands, '_run_bounded_command', packet)
    return port, query, calls
