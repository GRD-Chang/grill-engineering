"""Recorded approval inputs for merge settlement, never proof of Final admission.

The real Requirements journey owns Ticket completion, review and approval.
This fixture supplies their relational outputs; Core alone reconciles the
new external observation, validates its receipt and decides the next phase.
"""
from contextlib import closing, contextmanager

from agent_run.core.operation_contract import Work
from agent_run.model import Commit, EffectResult, Execution, PullRequest, World, wire_json
from agent_run.storage.run_scope import RunTransaction
from agent_run.storage.sqlite import Transaction
from support.repository_scope import connection, insert, seed
from support.run_scoped_kernel import ScopedKernel


@contextmanager
def approved_final_merge():
    subject, run = "final:1", "recorded-final"
    base, head = Commit("a" * 40, "b" * 40), Commit("c" * 40, "d" * 40)
    merged = Commit("e" * 40, head.tree)
    with closing(connection()) as db:
        db.execute("BEGIN")
        seed(db, run, subject=subject)
        tx = RunTransaction(Transaction(db), run)
        context = tx.put(subject, "agent_context", wire_json({}))
        checks = tx.put(subject, "checks", wire_json({"result": "none"}))
        barrier = tx.put(subject, "completion_barrier", wire_json([{"subject": "ticket:3", "generation": 1}]))
        insert(db, "attempts", dict(run_id=run, id="review", subject=subject, generation=1,
            ordinal=1, role="review", purpose="integration", window=1, parent_revision="r",
            graph_revision="g", revision="r", base_sha=base.sha, candidate_sha=head.sha,
            candidate_tree=head.tree, completion_revision="completed", context_id=context,
            status="consumed", outcome="pass", accounted=1))
        insert(db, "checks", dict(run_id=run, id="checks", subject=subject, pr_number=11,
            head_sha=head.sha, result="none", material_id=checks))
        insert(db, "actions", dict(run_id=run, id="approval", subject=subject,
            kind="approve", status="pending"))
        insert(db, "grants", dict(run_id=run, id="grant", subject=subject, action="approval",
            pr_number=11, head_sha=head.sha, base_sha=base.sha, review_attempt="review",
            checks_id="checks", revision="r", parent_revision="r", graph_revision="g",
            completion_revision="completed"))
        insert(db, "budgets", dict(run_id=run, subject=subject, purpose="integration", window=1,
            development_limit=3, review_limit=3, review_required=1, reviews_used=1))
        insert(db, "final_barriers", dict(run_id=run, subject=subject, generation=1,
            completion_revision="completed", run_sha=head.sha, default_sha=base.sha, evidence=barrier))
        insert(db, "source_observations", dict(run_id=run, sequence=1,
            parent_revision="r", graph_revision="g", body=wire_json({})))
        db.execute("UPDATE runs SET executor='owner',epoch=1,source_sequence=1,"
                   "observed_parent_revision='r',observed_graph_revision='g',final_action='approval' WHERE id=?", (run,))
        db.execute("UPDATE subjects SET kind='final_run',phase='merge',purpose='integration',"
                   "completion_revision='completed',source_sha=?,publication_sha=?,publication_tree=?,"
                   "pr_number=11,review_attempt='review',checks_id='checks',grant_id='grant',owned_root='/fixture'",
                   (head.sha, head.sha, head.tree))
        db.execute("UPDATE subjects SET workspace_sha=publication_sha,candidate_sha=publication_sha,"
                   "candidate_tree=publication_tree,checkout='/fixture/final-1',common_git_dir='/fixture/.git'")
        payload = tx.put(subject, "effect_intent", wire_json({"method": "merge", "parents": [base.sha, head.sha]}))
        insert(db, "effects", dict(run_id=run, id="merge", subject=subject, operation="merge",
            status="sent", executor="owner", epoch=1, parent_revision="r", graph_revision="g",
            revision="r", generation=1, source_ref="refs/heads/work", target_ref="refs/heads/main",
            before_sha=base.sha, after_sha=head.sha, base_sha=base.sha, base_tree=base.tree,
            pr_number=11, actor="publisher", payload_id=payload, attempts=1))
        db.commit()
        kernel = ScopedKernel(db, run)
        with kernel.storage.transaction(write=False) as tx:
            row = tx.one("SELECT * FROM effects")
            request = kernel._effect_work(tx, Work(Execution(run, "owner", 1), "pending",
                subject, False, True, None, None), row)
        world = World("r", "g", "r", base, head, completion_revision="completed")
        pr = PullRequest(11, "example/project", "refs/heads/work", "refs/heads/main", head, base,
            "Original PR", "Original body", "MERGED", "publisher", merged, "example/project")
        receipt = EffectResult("target", commit=merged, pr=pr, raw={"parents": [base.sha, head.sha]})
        yield db, kernel, request, world, receipt
