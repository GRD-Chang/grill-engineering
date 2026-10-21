"""Isolated remote service whose writes never move original local branch refs."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import sqlite3

from agent_run_kernel import Effect, EffectResult
from agent_run_kernel.effects import LocalGit
from agent_run_kernel.model import KernelError, wire_json
from test_kernel_migration_original_cutover import _ManagedSourcePort


class ReturnedRemote(_ManagedSourcePort):
    """Use the existing remote database and real Git objects, keeping refs separate."""

    def reconcile(self, effect: Effect) -> EffectResult:
        if effect.operation != 'run_branch':
            return super().reconcile(effect)
        remote = self.remote_refs().get(effect.source_ref)
        return EffectResult('target' if remote == effect.after_sha else
                            'prior' if remote == effect.before_sha else 'third',
                            commit=None if remote is None else self.git.commit(remote))

    def execute(self, effect: Effect, authorize: Callable[[], None] | None = None) -> EffectResult | None:
        if effect.operation == 'run_branch':
            if self.reconcile(effect).disposition == 'prior':
                if authorize is not None:
                    authorize()
                assert effect.after_sha is not None
                self._remote_cas(effect.source_ref, effect.before_sha, effect.after_sha)
            return self.reconcile(effect)
        if effect.operation != 'merge':
            return super().execute(effect, authorize)
        pr = self.pull_request(effect.subject, effect.source_ref)
        if pr is None or pr.head.sha != effect.after_sha or pr.base.sha != effect.before_sha or pr.state != 'OPEN':
            raise KernelError('merge_compare_and_swap')
        git = LocalGit(self.git.root, authorize)
        git.env = self.git.env.copy()
        commit = git.make_commit(pr.head.tree, effect.payload['parents'], effect.payload['commit_message'])
        if authorize is not None:
            authorize()
        with sqlite3.connect(self.database) as db:
            db.execute('BEGIN IMMEDIATE')
            source = db.execute('SELECT sha FROM refs WHERE ref=?', (effect.source_ref,)).fetchone()
            target = db.execute('SELECT sha FROM refs WHERE ref=?', (effect.target_ref,)).fetchone()
            if source != (effect.after_sha,) or target != (effect.before_sha,):
                raise KernelError('merge_compare_and_swap')
            self._remote_cas_in(db, effect.target_ref, effect.before_sha, commit.sha)
            self._write_pr_in(db, effect.subject, replace(pr, state='MERGED', actor=effect.actor, merged=commit))
            db.execute('INSERT INTO writes(operation,subject,intent,facts) VALUES (?,?,?,?)',
                       (effect.operation, effect.subject, effect.effect_id,
                        wire_json({'before': effect.before_sha, 'after': effect.after_sha})))
        return self.reconcile(effect)
