"""Source and Delivery facts, independent of business writes and receipts."""
from __future__ import annotations
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
import random
import subprocess
import time
from typing import Any
from agent_run.adapters import github_commands
from agent_run.adapters.auth.runner_transport import runner_options
from agent_run.adapters.github import GhGitHubReader, _SourceObservation
from agent_run.adapters.github_transport import GitHubReadTransport, _number, _sha, _string, _text
from agent_run.adapters.operation_errors import operation_errors
from agent_run.adapters.source import read_remote_heads, read_requirements, read_required_check_facts, normalize_required_checks
from agent_run.contracts.pr_observation import pr_target
from agent_run.contracts.repository import same_repository
from agent_run.core.operation_contract import DeliveryFacts, DeliveryRead, PreparedRequirements
from agent_run.errors import GitHubReadError
from agent_run.model import Commit, Requirements
from agent_run.storage.sqlite import MATERIAL_LIMIT

@dataclass(frozen=True)
class _SourceBasis:
    query: DeliveryRead | None
    observation: _SourceObservation
    owner: RemoteObservation

class RemoteObservation(GitHubReadTransport):
    @operation_errors
    def source(self, parent_number: int) -> Requirements:
        reader = GhGitHubReader(self.repository, working_directory=self.root, runner_identity=self.identity,
                                cancelled=self.cancelled)
        return read_requirements(reader, parent_number)


    @operation_errors
    def source_observation(self, parent_number: int, query: DeliveryRead | None) -> PreparedRequirements:
        from agent_run.core.operation_contract import PreparedRequirements
        if type(parent_number) is not int or parent_number < 1:
            raise ValueError('Parent Issue number must be a positive integer')
        if query is not None:
            self._validate_delivery_read(query)
        reader = GhGitHubReader(self.repository, working_directory=self.root, runner_identity=self.identity,
                                cancelled=self.cancelled)
        projection = None
        if query is not None and self.api is None:
            projection = self._delivery_projection(None, None, query.pr_number if query.read_pr else None,
                (query.source_ref, query.target_ref) if query.read_pr and query.pr_number is None else None)
            declarations, fields, _, arguments = projection
            for key, ref in (('sourceRef', query.source_ref), ('targetRef', query.target_ref)):
                declarations.append('$' + key + ':String!')
                fields.append(key + ':ref(qualifiedName:$' + key + ') { name prefix repository { nameWithOwner } '
                    'target { __typename oid ... on Commit { tree { oid } } } }')
                arguments += ['-f', key + '=' + ref]
        observation = _SourceObservation(reader, delivery_projection=projection)
        return PreparedRequirements(observation.read(parent_number), _SourceBasis(query, observation, self))


    def _projected_refs(self, envelope: Mapping[str, Any], query: DeliveryRead
                        ) -> tuple[tuple[str | None, str | None], dict[str, Any]] | None:
        """Validate ref addressing; short-name fallback requests the exact old reader."""
        if envelope.get('errors'):
            return None
        data = envelope['data']
        repository = data['repository']
        if repository is None:
            raise GitHubReadError('github_read_failed', 'Delivery repository is unavailable')
        if not same_repository(_text(repository['nameWithOwner']), self.repository):
            raise ValueError('foreign Delivery repository')
        refs: list[str | None] = []
        objects: dict[str, Any] = {}
        for key, expected in (('sourceRef', query.source_ref), ('targetRef', query.target_ref)):
            ref = repository[key]
            if ref is None:
                refs.append(None)
                continue
            if not same_repository(_text(ref['repository']['nameWithOwner']), self.repository):
                raise ValueError('foreign Delivery ref repository')
            if _text(ref['prefix']) + _text(ref['name']) != expected:
                return None
            value = ref['target']
            if value is None:
                raise GitHubReadError('github_read_failed', 'Delivery ref target is unavailable')
            if value['__typename'] != 'Commit':
                raise ValueError('Delivery ref does not name a commit')
            sha, tree = _sha(value['oid']), _sha(value['tree']['oid'])
            if sha in objects and objects[sha]['tree']['oid'] != tree:
                raise ValueError('Same commit has contradictory trees')
            objects[sha] = {'__typename': 'Commit', 'oid': sha, 'tree': {'oid': tree}}
            refs.append(sha)
        projected = dict(repository)
        projected.update({'object' + str(index): objects[sha] for index, sha in enumerate(sorted(objects))})
        return (refs[0], refs[1]), {'data': {**data, 'repository': projected}}


    @operation_errors
    def complete_observation(self, query: DeliveryRead, prepared: PreparedRequirements) -> DeliveryFacts:
        stage = prepared.physical
        if not isinstance(stage, _SourceBasis) or stage.owner is not self:
            raise ValueError('Foreign Source observation')
        self._validate_delivery_read(query)
        observation = stage.observation
        if self.api is not None:
            return self.delivery(query)
        observation._remaining()
        projected = None
        if query == stage.query and observation.delivery_packet is not None:
            try:
                projected = self._projected_refs(observation.delivery_packet, query)
            except (ValueError, KeyError, TypeError) as error:
                raise GitHubReadError('github_invalid_response', 'Delivery basis facts are malformed') from error
        # Mismatched demand discards both facts and errors. No failed Source
        # attempt can retain a packet. This starts a new Delivery attempt.
        refs = read_remote_heads(self.root, query.source_ref, query.target_ref,
            runner_identity=self.identity, remote=self.remote, command=observation.command)
        if projected is not None and refs != projected[0]:
            raise GitHubReadError('github_observation_pending', 'GraphQL and Git refs changed during observation')
        basis, raw_pr, actor = self._delivery_basis(*refs, query.pr_number if query.read_pr else None,
            (query.source_ref, query.target_ref) if query.read_pr and query.pr_number is None else None,
            deadline=observation.deadline, attempts=int(observation.combined_attempted),
            initial=None if projected is None else projected[1])
        return self._finish_delivery(query, refs, basis, raw_pr, actor)


    def _delivery_projection(self, source_sha: str | None, target_sha: str | None,
                             pr_number: int | None, discovery: tuple[str, str] | None = None,
                             *, compact: bool = False,
                             ) -> tuple[list[str], list[str], list[str], list[str]]:
        """Build the Delivery fields independently of the request that carries them."""
        owner, name = self.repository.split('/', 1)
        identities = sorted({_sha(sha) for sha in (source_sha, target_sha) if sha is not None})
        if pr_number is not None:
            _number(pr_number)
        declarations = ['$owner:String!', '$name:String!']
        fields = ['id nameWithOwner']
        arguments = ['-f', 'owner=' + owner, '-f', 'name=' + name]
        for index, sha in enumerate(identities):
            key = 'object' + str(index)
            declarations.append('$' + key + ':GitObjectID!')
            fields.append(key + ':object(oid:$' + key + ') { __typename oid ... on Commit { tree { oid } } }')
            arguments += ['-f', key + '=' + sha]
        if pr_number is not None and not compact:
            declarations.append('$pr:Int!')
            fields.append('pullRequest(number:$pr) { number title body state author { login } '
                'repository { nameWithOwner } baseRepository { nameWithOwner } headRepository { nameWithOwner } '
                'headRefName headRefOid baseRefName baseRefOid '
                'mergeCommit { oid parents(first:100) { nodes { oid tree { oid } } } } }')
            arguments += ['-F', 'pr=' + str(pr_number)]
        if discovery is not None and not compact:
            declarations += ['$head:String!', '$base:String!']
            fields.append('discovery:pullRequests(first:100,headRefName:$head,baseRefName:$base,'
                'states:[OPEN,CLOSED,MERGED]) { nodes { number headRefName baseRefName '
                'headRepositoryOwner { login } } pageInfo { hasNextPage } }')
            arguments += ['-f', 'head=' + discovery[0].removeprefix('refs/heads/'),
                          '-f', 'base=' + discovery[1].removeprefix('refs/heads/')]
        root_fields = ['viewer { login }'] if self.identity['source'] == 'host' else []
        return declarations, fields, root_fields, arguments


    def _parse_delivery_basis(self, envelope: Mapping[str, Any],
                              source_sha: str | None, target_sha: str | None,
                              pr_number: int | None, discovery: tuple[str, str] | None = None,
                              *, compact: bool = False, packet: Callable[..., Any],
                              ) -> tuple[dict[str, Commit], dict[str, Any] | None, str | None]:
        """Validate a complete basis and finish its exact PR discovery if needed."""
        owner = self.repository.split('/', 1)[0]
        identities = sorted({_sha(sha) for sha in (source_sha, target_sha) if sha is not None})
        host = self.identity['source'] == 'host'
        errors = envelope.get('errors', [])
        if not isinstance(errors, list):
            raise ValueError('invalid GraphQL errors envelope')
        if errors or 'data' in envelope and envelope['data'] is None:
            raise GitHubReadError('github_read_failed', 'Delivery basis is incomplete')
        data = envelope['data']
        repository = data['repository']
        if repository is None:
            raise GitHubReadError('github_read_failed', 'Delivery repository is unavailable')
        if not _text(repository['id']) or not same_repository(_text(repository['nameWithOwner']), self.repository):
            raise ValueError('foreign Delivery repository')
        commits = {}
        for index, sha in enumerate(identities):
            value = repository['object' + str(index)]
            if value is None:
                raise GitHubReadError('github_read_failed', 'Exact Delivery commit is unavailable')
            if value['__typename'] != 'Commit' or _sha(value['oid']) != sha:
                raise ValueError('foreign Delivery commit')
            commits[sha] = Commit(sha, _sha(value['tree']['oid']))
        actor = None
        if host:
            if data['viewer'] is None:
                raise GitHubReadError('github_read_failed', 'Delivery viewer is unavailable')
            actor = _text(data['viewer']['login'])
        number = pr_number
        if discovery is not None:
            rest = compact
            matches = []
            if not compact:
                connection = repository['discovery']
                nodes, more = connection['nodes'], connection['pageInfo']['hasNextPage']
                if not isinstance(nodes, list) or len(nodes) > 100 or type(more) is not bool:
                    raise ValueError('Malformed PR discovery connection')
                rest = more
                for node in nodes:
                    candidate = _number(node['number'])
                    if (_text(node['headRefName']) != discovery[0].removeprefix('refs/heads/')
                            or _text(node['baseRefName']) != discovery[1].removeprefix('refs/heads/')):
                        raise ValueError('Foreign PR discovery refs')
                    head_owner = node['headRepositoryOwner']
                    if head_owner is None:
                        rest = True  # Legal deleted-head identity, not proof of a foreign owner.
                    elif _text(head_owner['login']).casefold() == owner.casefold():
                        matches.append(candidate)
            if rest:
                rows = packet(['gh', 'api', self._pr_discovery_endpoint(*discovery),
                               '--paginate', '--slurp'], pages=True)
                matches = [_number(row['number']) for row in rows]
            if len(matches) > 1:
                raise ValueError('ambiguous original PR')
            number = matches[0] if matches else None
        raw_pr = None
        if number is not None:
            if compact or discovery is not None:
                raw_pr = packet(['gh', 'api', f'repos/{self.repository}/pulls/{number}'])
                if (_number(raw_pr['number']) != number or type(raw_pr.get('merged')) is not bool
                        or raw_pr['state'] not in {'open', 'closed'}):
                    raise ValueError('foreign or invalid Delivery PR')
                _text(raw_pr['title'])
                _string(raw_pr['body'])
                for side in ('head', 'base'):
                    _sha(raw_pr[side]['sha'])
                    _text(raw_pr[side]['ref'])
                    repo = raw_pr[side].get('repo')
                    if side == 'base' and repo is None:
                        raise GitHubReadError('github_read_failed', 'Delivery PR base repository is unavailable')
                    if repo is not None:
                        _text(repo['full_name'])
                if raw_pr['merged'] and raw_pr.get('merge_commit_sha') is None:
                    raise GitHubReadError('github_read_failed', 'Delivery merge object is unavailable')
            else:
                value = repository['pullRequest']
                if value is None:
                    raise GitHubReadError('github_read_failed', 'Known Delivery PR is unavailable')
                if value['baseRepository'] is None:
                    raise GitHubReadError('github_read_failed', 'Delivery PR base repository is unavailable')
                if not same_repository(_text(value['repository']['nameWithOwner']), self.repository):
                    raise ValueError('foreign Delivery PR repository')
                state = _text(value['state'])
                if _number(value['number']) != number or state not in {'OPEN', 'CLOSED', 'MERGED'}:
                    raise ValueError('foreign or invalid Delivery PR')
                if state == 'MERGED' and value['mergeCommit'] is None:
                    raise GitHubReadError('github_read_failed', 'Delivery merge object is unavailable')
                raw_pr = {'number': number, 'title': _text(value['title']), 'body': _string(value['body']),
                    'state': 'closed' if state == 'MERGED' else state.lower(), 'user': value['author'],
                    'head': {'sha': _sha(value['headRefOid']), 'ref': _text(value['headRefName']),
                             'repo': None if value['headRepository'] is None else
                                     {'full_name': _text(value['headRepository']['nameWithOwner'])}},
                    'base': {'sha': _sha(value['baseRefOid']), 'ref': _text(value['baseRefName']),
                             'repo': {'full_name': _text(value['baseRepository']['nameWithOwner'])}},
                    'merged': state == 'MERGED',
                    'merge_commit_sha': None if state != 'MERGED' else _sha(value['mergeCommit']['oid'])}
                if state == 'MERGED':
                    # This bounded subset supplies trees by exact OID, not parent
                    # order. The original merge REST receipt still selects parent 0.
                    parents = value['mergeCommit']['parents']['nodes']
                    if not isinstance(parents, list) or len(parents) > 100:
                        raise ValueError('Malformed merge parent objects')
                    for parent in parents:
                        if parent is None:
                            raise GitHubReadError('github_read_failed', 'Merge parent object is unavailable')
                        if not isinstance(parent, dict):
                            raise ValueError('Malformed merge parent object')
                        if parent['tree'] is None:
                            raise GitHubReadError('github_read_failed', 'Merge parent tree is unavailable')
                        parent_commit = Commit(_sha(parent['oid']), _sha(parent['tree']['oid']))
                        if parent_commit.sha in commits and commits[parent_commit.sha] != parent_commit:
                            raise ValueError('Same commit has contradictory trees')
                        commits[parent_commit.sha] = parent_commit
        return commits, raw_pr, actor


    def _delivery_basis(self, source_sha: str | None, target_sha: str | None,
                        pr_number: int | None, discovery: tuple[str, str] | None = None,
                        *, deadline: float | None = None, attempts: int = 0,
                        initial: Mapping[str, Any] | None = None,
                        ) -> tuple[dict[str, Commit], dict[str, Any] | None, str | None]:
        """One fresh bounded projection, never retained beyond this Delivery.

        PR discovery supplies a number only. Complete ref-name/owner matches
        share the packet; pagination or an unavailable owner uses the original
        REST collection. An identified PR is then read afresh. Capacity alone
        permits compact projection. All work shares three whole attempts and a
        90-second maximum; incomplete packets supply no reusable facts.
        """
        projection = self._delivery_projection(source_sha, target_sha, pr_number, discovery)
        if deadline is None:
            deadline = time.monotonic() + github_commands.MAX_READ_ATTEMPTS * github_commands.READ_TIMEOUT_SECONDS

        def remaining() -> float:
            if self.cancelled is not None and self.cancelled():
                raise InterruptedError('Delivery read cancelled')
            value = deadline - time.monotonic()
            if value <= 0:
                raise GitHubReadError('github_read_failed', 'Delivery basis observation deadline expired')
            return value

        def before_start() -> None:
            remaining()

        def cancelled() -> bool:
            remaining()
            return False

        def packet(arguments: list[str], *, pages: bool = False) -> Any:
            try:
                response = github_commands._run_bounded_command(arguments, cwd=self.root,
                    timeout=min(github_commands.READ_TIMEOUT_SECONDS, remaining()), complete_stdout=True,
                    before_start=before_start, cancelled=cancelled, **runner_options(self.identity))
            except subprocess.TimeoutExpired as error:
                raise GitHubReadError('github_read_failed', 'Delivery basis command deadline expired') from error
            remaining()
            if response.returncode:
                raise github_commands.read_response_error(response)
            try:
                data = json.loads(response.stdout)
            except (ValueError, RecursionError) as error:
                raise GitHubReadError('github_invalid_response', 'Delivery basis response is not complete JSON') from error
            if pages:
                if not isinstance(data, list) or any(not isinstance(page, list) for page in data):
                    raise ValueError('Expected paginated PR collection')
                rows = [row for page in data for row in page]
                if not all(isinstance(row, dict) for row in rows) or len(json.dumps(rows).encode()) > MATERIAL_LIMIT:
                    raise ValueError('Invalid or oversized PR collection')
                return rows
            if not isinstance(data, dict):
                raise GitHubReadError('github_invalid_response', 'Expected a complete Delivery object')
            return data

        compact = False
        if attempts >= github_commands.MAX_READ_ATTEMPTS and initial is None:
            raise GitHubReadError('github_read_failed', 'Delivery basis observation attempts exhausted')
        for attempt in range(attempts - int(initial is not None), github_commands.MAX_READ_ATTEMPTS):
            remaining()
            try:
                declarations, fields, root_fields, arguments = projection
                query = ('query DeliveryBasis(' + ','.join(declarations) + ') { '
                    + ' '.join(root_fields) + ' '
                    + 'repository(owner:$owner,name:$name) { ' + ' '.join(fields) + ' } }')
                envelope = initial if initial is not None else packet(['gh', 'api', 'graphql', *arguments, '-f', 'query=' + query])
                initial = None
                commits, raw_pr, actor = self._parse_delivery_basis(envelope,
                    source_sha, target_sha, pr_number, discovery, compact=compact, packet=packet)
                remaining()
                return commits, raw_pr, actor
            except github_commands.ReadOutputOverflow as error:
                if compact or pr_number is None and discovery is None:
                    raise
                if attempt == github_commands.MAX_READ_ATTEMPTS - 1:
                    raise GitHubReadError('github_read_failed', 'Delivery basis needs an unread compact projection') from error
                compact = True
                projection = self._delivery_projection(source_sha, target_sha, pr_number, discovery, compact=True)
            except GitHubReadError as error:
                if error.code != 'github_read_failed' or attempt == github_commands.MAX_READ_ATTEMPTS - 1:
                    raise
                time.sleep(min(2 ** attempt + random.uniform(0, .25), remaining()))
            except (ValueError, KeyError, TypeError, UnicodeError, RecursionError) as error:
                raise GitHubReadError('github_invalid_response', 'Delivery basis facts are malformed') from error
        raise AssertionError('Delivery basis exhausted without a result')


    @operation_errors
    def delivery(self, request: DeliveryRead) -> DeliveryFacts:
        """Read exact Core-supplied refs and immutable ancestry anchors."""
        self._validate_delivery_read(request)
        source_sha, target_sha = read_remote_heads(self.root, request.source_ref, request.target_ref,
                                                  runner_identity=self.identity, remote=self.remote)
        basis: dict[str, Commit] = {}
        actor = None
        if self.api is None:
            number = request.pr_number if request.read_pr else None
            if request.read_pr and number is None:
                basis, raw_pr, actor = self._delivery_basis(source_sha, target_sha, None,
                    (request.source_ref, request.target_ref))
            else:
                basis, raw_pr, actor = self._delivery_basis(source_sha, target_sha, number)
        else:
            # PhysicalAPI's existing injected REST interface has no GraphQL port.
            raw_pr = self._find_pr(request.source_ref, request.target_ref, request.pr_number) if request.read_pr else None
        return self._finish_delivery(request, (source_sha, target_sha), basis, raw_pr, actor)


    @staticmethod
    def _validate_delivery_read(request: DeliveryRead) -> None:
        """Reject unbounded or unrelated repair demand before physical reads."""
        from agent_run.core.operation_contract import DeliveryRead, ReadUnavailable, requested_ancestry
        repair = request.repair
        if repair is not None and (not isinstance(repair, DeliveryRead) or repair.repair is not None
                                   or repair.target_ref != request.source_ref):
            raise ReadUnavailable("repair_request_invalid", "Repair must target the Run source and cannot contain another repair")
        for query in (request,) if repair is None else (request, repair):
            if type(query.read_checks) is not bool or not query.read_checks and query.check_head is not None:
                raise ReadUnavailable("checks_request_invalid", "An exact checks head requires a checks observation")
            requested_ancestry(query, None)


    def _finish_delivery(self, request: DeliveryRead, refs: tuple[str | None, str | None],
                         basis: Mapping[str, Commit], raw_pr: Mapping[str, Any] | None,
                         actor: str | None) -> DeliveryFacts:
        """Complete exact objects, ancestry, checks and the closing ref boundary."""
        from agent_run.core.operation_contract import DeliveryFacts, requested_ancestry
        source_sha, target_sha = refs
        commits: dict[str, Commit] = {}
        reused: set[str] = set()
        def read_commit(sha: str) -> Commit:
            sha = _sha(sha)
            if sha in commits:
                reused.add(sha)
                return commits[sha]
            return basis[sha] if sha in (source_sha, target_sha) and sha in basis else self.commit(sha)[0]
        source = None if source_sha is None else read_commit(source_sha)
        if source is not None:
            commits[source.sha] = source
        target = None if target_sha is None else read_commit(target_sha)
        if target is not None:
            commits[target.sha] = target
        pr = None if raw_pr is None else self._pr(raw_pr, read_commit=read_commit, merged_parents=basis)
        if pr is not None and pr.state == 'MERGED' and pr.base.sha in basis:
            # A consumed parent tree must match the actual exact Git object,
            # just like the other API facts reused in this observation.
            commits[pr.base.sha] = pr.base
            reused.add(pr.base.sha)
        checked = requested_ancestry(request, source, target, pr)
        heads = set(request.anchors) | {head for head in (source_sha, target_sha) if head is not None}
        if checked is not None:
            heads.update(head for pair in checked for head in pair)
        if pr is not None:
            heads.add(pr.base.sha)
        # Only exact objects are reused within this observation. Live refs are
        # still bracketed below; no World or write authority is cached.
        prepared = sorted({_sha(head) for head in heads})
        self._prepare_ancestor_objects(prepared)
        if reused:
            # Do not let reuse hide inconsistent API tree evidence. Resolve the
            # exact immutable objects already prepared above, without decoding
            # commit messages or deriving parents from shallow/grafted history.
            identities = sorted(reused)
            objects = self.git.run("log", "--no-patch", "--no-walk=unsorted", "--format=%H %T",
                "--encoding=UTF-8", "--no-use-mailmap", "--no-show-signature", "--no-notes", "--no-color",
                *identities, "--").stdout.splitlines()
            if objects != [sha + " " + commits[sha].tree for sha in identities]:
                raise GitHubReadError("github_observation_pending", "Commit object and API tree do not agree")
        pairs = ((before, after) for before in prepared for after in prepared) if request.ancestry_pairs is None else iter(checked or ())
        ancestry = tuple((before, after) for before, after in pairs
                         if before == after or self.git.is_ancestor(before, after))
        checks = None
        if pr is not None and pr.state == "OPEN":
            if source is None or pr_target(pr, repository=self.repository, pr_number=request.pr_number,
                    source_ref=request.source_ref, target_ref=request.target_ref,
                    source=source, target=target, ancestry=ancestry) is None:
                raise GitHubReadError("github_observation_pending", "PR and remote ref observations have not converged")
            if request.read_checks:
                reader = GhGitHubReader(self.repository, working_directory=self.root, runner_identity=self.identity)
                checks = normalize_required_checks(read_required_check_facts(
                    reader, pr.number, expected_head_sha=request.check_head or source.sha))
        actor = self.actor() if actor is None else actor
        repair = None if request.repair is None else self.delivery(request.repair)
        if repair is not None and (repair.target != source or repair.publisher_actor != actor
                                   or repair.publisher_identity != self.identity):
            raise GitHubReadError("github_observation_pending", "Repair and Run observations have not converged")
        if read_remote_heads(self.root, request.source_ref, request.target_ref,
                runner_identity=self.identity, remote=self.remote) != (source_sha, target_sha):
            raise GitHubReadError("github_observation_pending", "Remote refs changed during delivery observation")
        return DeliveryFacts(target, source, pr, checks, ancestry, actor, dict(self.identity), checked,
                             request.read_checks, repair)
