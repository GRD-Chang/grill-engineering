# Independent delivery kernel

This is the independent domain implementation for all three delivery kinds.
Development and validation use isolated databases and repositories. The
installed Runner and user databases are not targets of this exploration.

`Kernel` owns business transitions, authorization, immutable attempts/results,
budget purposes, FIFO and the integration seat in short SQLite transactions.
`Storage` provides bounded durable facts; it does not choose successors.
Adapters execute one explicit effect and call the Kernel's read-only guard
before successive actual Git writes. Reviewers receive separate, exact Git
validation checkouts; their temporary files never enter a Candidate.

Run one scenario from the project directory:

```sh
PYTHONPATH=src python -m agent_run_kernel.offline \
  --root /tmp/new-kernel-synthetic-fixture \
  --scenario ticket-review-repair --output /tmp/kernel-observation.json
```

The root must be new. Available scenarios also include `ticket-normal`,
`ticket-fallback` (N=3), `parent-only-normal`, `full-parent-normal`, and
`full-parent-review-repair` (cut before final Publication). The adapter uses
real local Git and SQLite, with scripted Agents and synthetic remote PRs.

`create` freezes configuration, requirements and resource identities.
`refresh` owns the external Requirements read and its observation receipt.
`advance` returns one Agent or mechanical request. Raw output settlement is
separate from domain consumption; `dispatch`, `guard_effect` and `reconcile`
retain the original effect's exact authority across I/O and uncertain replies.
Roles retain the strict Artifact contracts. Public query readers use Storage
read-only transactions without reconstructing a mutable Run.

Implemented representative paths include all three delivery kinds, initial
and integration review/development loops, CI repairs, explicit approval,
Stop/Resume with permanent epoch retirement, raw return reuse, independent
budget renewal and exact unknown-effect observations. Final Parent repair
updates Run Branch without an internal Repair PR.

Requirements graph, persistent FIFO, independent budget windows and the
Completion barrier use one domain authority. Fixed language, Prompt resources,
Profile revisions and identity references belong to the Run; each Thread and
physical output keeps its actual bound configuration, raw bytes and process
facts. Ordinary recovery, capacity recovery and format repair retain their
different authorization, counting and deadline rules.

Managed entry uses `admit_host`, `dispatch_host` and `observe_host`. A Task's
workspace/repository/Parent identity is unique across Run databases. The
locator stores only its Run and database path; replacement decisions stay in
Kernel. A Host is pinned to its original nonce, systemd invocation, boot and
cgroup. Unknown startup cannot authorize another launch or business work.
Shared Runner lease proofs remain distinct from a manager's running/exited
observation. `host_request` and `host_usage` read original request and handoff
evidence for mechanical reconciliation. The direct `claim_executor` and
`confirm_executor_exit` interfaces remain trusted offline fixture seams.

Real process/Codex, public read/settings, source readers and copied-state
migration are separately owned adapters with fixed-snapshot validation.
Complete native entry assembly, continuous guardian/Host/exit-notifier shared
leases, real remote effects, notification delivery and installation remain
in progress. Actual user-systemd validation is blocked by the unavailable user
bus. Supported copied-state migration does not imply inverse continuation
after new external effects. Per-component passes do not establish whole-product
equivalence or full retirement of the old runtime.

Use only newly created test data. The experiment refuses other database
protocols; it must never be opened on a user database. All new code and adapters
count toward replacement cost, even during temporary coexistence.
